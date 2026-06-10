# DMZ智能体

飞书机器人，通过 WebSocket接收消息并用 hermes-agent + Minimax API 回复。整合两个业务域：
- **台架预约**（台架预约 REST API @9013）
- **VLM精标数据**（dmz-ess-vlm REST API @9014）

用途：在真实通道上探索受控 LLM 输出（格式、内容边界、权限、工具调用）。

##快速开始

```bash
cp .env.example .env #填入 FEISHU_APP_ID, FEISHU_APP_SECRET, MINIMAX_API_KEY
cp data/identity_map.json.example data/identity_map.json # 配置 open_id → email/role
pip install -e ".[dev]"
python main.py #启动机器人（WebSocket + 健康检查 HTTP）
```

##常用命令

```bash
python main.py #启动机器人
pytest tests/unit/ #单元测试（无网络）
curl localhost:8080/health # 检查 ws_connected、metrics
```

##架构——一句话一层

- `feishu/` — 只做 WebSocket收发；零业务逻辑
- `bot/` —包装 hermes-agent 的 `AIAgent`；每用户一个实例（池化）；意图识别；从 identity解析 email
- `ocl/` — Output Control Layer：格式、内容、长度、role-based工具 ACL、identity map、双层防御
- `bench_tools/` — 台架预约工具（handlers 用 `guarded()`包裹做 Layer2）；emailAddress 服务端注入，LLM永远看不到
- `vlm_tools/` — VLM精标工具（直接 httpx调真实 dmz-ess-vlm API；无身份注入）
- `hermes_plugins/` — hermes插件：`feishu_acl` pre_tool_call钩子（Layer1硬拦截）
- `infra/` — dedup、metrics、health；无领域知识
- `config/` —读 `.env`；所有配置的唯一来源
- `data/` — `identity_map.json`（open_id → email/role/name；gitignored，见 `.example`）
- `tests/` —单元测试 mock一切

## 双业务域

|域 | 后端 |端口 | LLM-facing 层 |鉴权方式 |
|------|------|------|----------|------|
| 台架预约 | （Docker容器） |9013 | `bench_tools/` | emailAddress 服务端注入 |
| VLM精标 | dmz-ess-vlm（Docker） |9014 | `vlm_tools/` | 无（VLM API 不鉴权） |

##权限 /角色模型

角色来源：`data/identity_map.json`（open_id → role）。三档：

- **role=1 普通用户** — 查询/预约自己权限范围内的台架、查询 VLM公开数据
- **role=2调度员** — +审批本组台架预约、下载 VLM 数据导出
- **role=3管理员** — +跨组审批、触发 VLM同步等系统级操作

OCL 按 min_role粗粒度门控工具；后端按 emailAddress细粒度校验（角色 + 分组 +状态）。两道闸相互独立。

| Min role | 台架预约工具 | VLM精标工具 |
|----------|----------|----------|
|1 | list_architectures, list_available_benches, reserve_bench, cancel_reservation, return_bench, list_my_reservations | list_event_names, list_camera_types, list_bags, get_bag, list_frames, get_frame, playback_bag |
|2 | approve_reservation, list_my_approvals | download_bag_metadata, frame_image_url |
|3 | — | sync_execute, trigger_sync_async, sync_status |

管理员在飞书里指派角色：`设置角色 <open_id> <1|2|3>`。无自助申请流程。

## 不允许破坏的不变量

- WS回调必须立即返回（不阻塞；改用队列）
- 每个 `user_id`严格对应一个 `AIAgent` 实例（池强制）
- `MINIMAX_API_KEY` 不出现在日志或错误消息中
- `max_iterations=30` 和 `timeout=120s` 是硬上限——非经讨论不要调
- Agent池 `get_or_create()` **必须**在 `ocl.session_map` 注册 `session_id`，按 LRU驱逐
- `ocl/session_map.lookup()` miss 时返回 `""`——调用方必须 fail-open（plugin 返回 None、guarded 通过）
-权限强制是双层防御：L1 pre_tool_call插件（硬拦截） + L2 guarded包裹（兜底）
- **台架预约域**：`emailAddress` 从 open_id注入；绝不作为 LLM工具参数
- **VLM精标域**：无身份字段；工具 schema 不能含 `emailAddress`
- 非平台用户（identity miss）无法进入 agent——handler 回友好提示

## 不允许做的事

- 不要添加任务没明确要求的功能
- 不要静默 catch异常——带上下文 log 后 re-raise 或返回给用户
- 不要 `feishu` import `agent` 或反之——只能通过 `handler.py`通信
- 不要把用户消息内容存到 metrics 或 logs——只存 ID 和延迟
- 不要在运行时修改 `~/.hermes/config.yaml`——启动后只读
- VLM工具**不要**加 `emailAddress`字段（与台架预约相反）


## 跨会话记忆（DMZ 自进化 Phase 1）

DMZ智能体已实现 hermes `MemoryProvider` 协议，把用户偏好、近期操作、错误模式跨会话持久化。

**启用方式：**

1. 在项目根目录运行：
 ```bash
 export DMZ_PROJECT_ROOT=/Users/chris/IM/hermes-feishu-agent
 ```

2. 把 hermes 配置改成 DMZ 记忆：
 ```yaml
 # ~/.hermes/config.yaml
 memory:
 provider: dmz
 ```

3. 启动后，agent 会自动从 `bot/dmz_memory.py` 加载 `DMZMemoryProvider`：
 - 跨会话记住用户常查的台架架构（如 "1.0架构"）、VLM场景名（如 "hotupdate_filter_..."）
 - 记住最近 20 步工具调用模式（仅工具名+参数 key，不存参数值）
 - 累计 4xx/5xx 错误模式，prefetch 时给 LLM 提示「用户曾频繁遇到...注意规避」

**存储位置：** `~/.hermes/dmz_memory/<user_hash16>/memory.json`

**安全铁律（不可变）：**

- ❌ 永不存 `emailAddress` / API key / 密码 / cookie
- ❌ 永不存完整用户消息原文（仅元数据：工具名 + 参数 key 名 + 成功/失败）
- ❌ 永不存 OCL 安全规则或权限配置
- ✅ 30天 TTL 自动过期
- ✅ 匿名用户（无 open_id）不落盘
- ✅ 落盘前 `_strip_sensitive` 双重保险剥敏感字段

**测试覆盖：** `tests/unit/test_dmz_memory.py` — 32 个用例。

**Phase 2 计划（feedback loop）：** 卡片按钮回调记录"用户操作模式"；`data/feedback/` 存人工反馈；周报分析失败模式 → 调 prompt / 工具。

**Phase 3 计划（Curator 集成）：** 启用 `agent/curator.py`，让它巡检工具 schema 变化，**仅**生成 Skill / 工具描述建议，不写业务规则。

## 不允许做的事

- 不要添加任务没明确要求的功能
- 不要静默 catch异常——带上下文 log 后 re-raise 或返回给用户
- 不要 `feishu` import `agent` 或反之——只能通过 `handler.py`通信
- 不要把用户消息内容存到 metrics 或 logs——只存 ID 和延迟
- 不要在运行时修改 `~/.hermes/config.yaml`——启动后只读
- VLM工具**不要**加 `emailAddress`字段（与台架预约相反）
- 记忆层**不要**存敏感字段（即使通过"用户体验改进"为由）
