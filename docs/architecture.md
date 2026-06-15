# hermes-feishu-agent 系统架构

> 完整图文版见 [`docs/项目说明.html`](项目说明.html)。本文为速查版。

## 整体架构

```
飞书用户/管理员
    │  WebSocket（飞书主动推送，无需公网 IP）
    ▼
feishu/ws_client.py          接收事件 → 去重 → 入队（回调零阻塞）
    │  queue.Queue
    ▼
bot/handler.py               消费线程：意图分层 → Agent → OCL → 发送
    │
    ├─ Layer 0    简单意图（你好/帮助）            → 即时回复
    ├─ Layer 0.5  查询快速路径（查可用台架…）       → 直接调工具 <1s
    ├─ Layer 0.6  预约快速路径（正则抽参 → dry_run） → 确认卡片
    ├─ 身份/管理命令（我的权限 / 设置角色 / 查看用户）→ bypass Agent
    └─ Agent.chat（注入「已核验身份」前缀）
           │
           ▼
       hermes AIAgent（per user，LRU pool max=100）
           │  Minimax 推理 + 工具调度
           │  ├─ Layer 1: pre_tool_call 插件（feishu_acl）→ 硬阻断
           │  └─ Layer 2: guarded() handler 包装器 → 兜底
           │  工具：bench_tools@9013 / vlm_tools@9014
           ▼
       ocl/pipeline.apply()  format → content → length → card_builder
           ▼
       feishu/sender.py      互动卡片（或纯文本兜底）→ 飞书
    ▲
    └── 卡片按钮点击 → ws_client(card action) → bot/card_action_handler（确定性，不经 LLM）
```

## 模块职责

| 模块 | 职责 | 关键文件 |
|------|------|---------|
| `feishu/` | 飞书传输层（WS、发送、限流、通知） | ws_client, sender, typing_indicator, notify |
| `bot/` | Agent 桥接、意图分层、卡片回调、身份管理、自进化 | handler, agent_pool, card_action_handler, identity_admin, dmz_memory, feedback, curator_runner |
| `ocl/` | 输出控制层（格式/内容/长度、角色 ACL、卡片构建） | pipeline, format_control, content_filter, length_limiter, permission, identity, session_map, tool_guard, tool_capture, card_builder, markdown_to_lark |
| `bench_tools/` | 台架预约工具；emailAddress 服务端注入 + JWT | register, handlers, jwt_auth |
| `vlm_tools/` | VLM 精标工具；无身份注入 | register, handlers |
| `hermes_plugins/` | hermes 插件（pre/post_tool_call） | feishu_acl/ |
| `infra/` | 去重、指标、健康检查、原子 JSON 存储 | dedup, metrics, health, json_store |
| `config/` | 统一配置入口 | settings.py |

## 双业务域

| 域 | 后端端口 | LLM-facing 层 | 鉴权方式 |
|----|---------|--------------|---------|
| 台架预约 | 9013 | `bench_tools/` | emailAddress 服务端按 open_id 注入 + 服务账号 JWT |
| VLM 精标 | 9014 | `vlm_tools/` | 无（VLM API 不鉴权） |

**不变量：** 台架域 `emailAddress` 永不作为 LLM 工具参数；VLM 域工具 schema 不得含 `emailAddress`。

## 双层防御

```
hermes 工具执行前
    ├─ Layer 1: pre_tool_call 插件（~/.hermes/plugins/feishu_acl）
    │   session_id → session_map.lookup() → user_id → permission.is_tool_permitted()
    │   返回 {"action":"block"} 阻断；任何异常 fail-open（放行），交 Layer 2 兜底
    └─ Layer 2: guarded() handler 包装器（bench_tools/register.py 等）
        threading.local → get_current_user() → permission.is_tool_permitted()
        返回 {"error": ...} 作为工具结果
```

OCL 只做**粗粒度**门控（按 `TOOL_MIN_ROLE` 表）；**细粒度**规则（同组、状态机）由后端按 `emailAddress` 强制——两道独立的闸。

## 关键不变量

- WS 回调立即返回（不阻塞；改用队列）
- 每个 `user_id` 严格对应一个 `AIAgent` 实例（池强制）；LRU 淘汰同步清理 session_map
- `MINIMAX_API_KEY` 不出现在日志或错误消息中
- `max_iterations=30`、`timeout=120s` 为硬上限
- 提交 `agent.chat` 前用 `contextvars.copy_context()` 复制上下文，否则工具线程读不到注入的 email
- OCL pipeline < 100ms、不抛异常、不记录正文内容
- 记忆/反馈层不存敏感字段（email/key/原文）

## 线程模型

```
主线程          uvicorn（/health HTTP，端口 8088）
ws-supervisor   lark WSClient + 指数退避重连
consumer        从队列取事件 → handler._handle（串行）
agent-worker     ThreadPoolExecutor(max_workers=5)
agent-warmup    首次建 Agent 时后台预热（lazy init + Curator 巡检）
hermes tool      _MAX_TOOL_WORKERS=8
```
