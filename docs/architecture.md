# hermes-feishu-agent 系统架构

## 整体架构

```
飞书用户/管理员
    │  WebSocket
    ▼
feishu/ws_client.py          ── 接收消息，监管重连
    │  queue.Queue
    ▼
bot/handler.py               ── 消息消费、意图检测、调用 Agent
    │
    ├─ 意图检测（关键词匹配）
    │   ├─ 权限申请/审批 → bypass Agent，直接处理
    │   └─ 普通消息 → 进入 Agent
    │
    └─ agent.chat(text)
           │
           ▼
       hermes AIAgent（per user，LRU pool max=100）
           │
           ├─ LLM 推理（Minimax API）
           ├─ 工具调度
           │   ├─ Layer 1: pre_tool_call plugin（feishu_acl）→ 硬阻断
           │   └─ Layer 2: guarded() handler 包装器 → 兜底
           │
           └─ 返回 response 字符串
    │
    ▼
ocl/pipeline.apply()         ── format → content → length
    │
    ▼
feishu/sender.py             ── 分片发送 + 限流
    │
    ▼
飞书 API → 用户
```

## 模块职责

| 模块 | 职责 | 关键文件 |
|------|------|---------|
| `feishu/` | 飞书传输层（WS 接收、消息发送、限流）| ws_client.py, sender.py |
| `bot/` | Agent 桥接（消息消费、Agent 池管理）| handler.py, agent_pool.py |
| `ocl/` | 输出控制层（格式、内容、长度、权限）| pipeline.py, permission.py, tool_guard.py |
| `mock_tools/` | Mock API 工具注册和 handler | register.py, handlers.py |
| `mock_api/` | Mock 企业 REST API 服务 | main.py, routes/ |
| `infra/` | 基础设施（去重、指标、健康检查）| dedup.py, metrics.py, health.py |
| `config/` | 统一配置入口 | settings.py |
| `hermes_plugins/` | hermes 插件（pre_tool_call ACL）| feishu_acl/ |

## 双层防御架构

```
hermes 工具执行前
    │
    ├─ Layer 1: pre_tool_call plugin
    │   路径: ~/.hermes/plugins/feishu_acl/
    │   机制: session_id → session_map.lookup() → user_id → permission.is_tool_permitted()
    │   返回: {"action":"block","message":"..."} 阻断工具
    │         None 放行
    │   位置: hermes 内部，工具执行前的最后一个检查点
    │
    └─ Layer 2: guarded() handler 包装器
        路径: mock_tools/register.py
        机制: threading.local → get_current_user() → permission.is_tool_permitted()
        返回: {"error":"..."} JSON 字符串作为工具结果
        位置: 工具 handler 入口
```

**为什么需要两层：**
- Layer 1（plugin）在 hermes 内部，通过 `session_id` 传递 user identity，不依赖 thread-local。但如果 plugin 未加载、session_map 缺失或权限检查异常，它 fail-open（放行）。
- Layer 2（guarded）在 handler 入口，通过 thread-local 传递 user identity。它覆盖 Layer 1 失效的所有场景。
- 正常运行时，Layer 1 先阻断，Layer 2 不会执行（hermes 返回 error 而不是调用 handler）。

## 权限模型

三个权限组，JSON 文件持久化（`data/permissions.json`）：

| 组 | 工具 | 获取方式 |
|----|------|---------|
| read | list_users, get_user, list_orders, get_order | 默认 |
| write | create_user, create_order, pay_order, ship_order | 飞书内申请，管理员审批 |
| report | create_report_job, get_report_status, get_report_data | 飞书内申请，管理员审批 |

审批流程全程在飞书 IM 内完成，不走 Agent——handler.py 用正则匹配关键词直接处理。

## 关键不变量

- `session_id` 格式：`feishu_{user_id}`，稳定不变，直到 Agent 被 LRU 淘汰
- Agent 池淘汰 → 同步淘汰 session_map 映射
- Plugin fail-open：任何异常都放行
- guarded() 不处理空 user_id（系统内部调用）
- 不记录消息内容，只记录 user_id、block_reason、长度
- OCL pipeline < 100ms（无网络调用）

## 线程模型

```
主线程: uvicorn (health HTTP)
WS 监管线程: lark-oapi WSClient + 重连
消费线程: handler.py start_consumer()
Agent 线程池: ThreadPoolExecutor(max_workers=5)
    ├─ worker-0: AIAgent.chat() → Minimax API
    ├─ worker-1: ...
    └─ worker-4: ...
hermes 工具线程池: _MAX_TOOL_WORKERS=8
    ├─ tool-worker-0: handler(args) 执行
    └─ tool-worker-7: ...
```

session_map 桥接消费线程（注册映射）和工具线程（查询映射）。
