# 约车助手

飞书机器人，通过 WebSocket接收消息并用 hermes-agent + Minimax API 回复。
整合**单一业务域**：

- **车辆预约**（车辆是测试车的升级版；含芯片平台枚举 Xavier/ADCU/Orin/Thor），
  通过外部 MCP server（用户在 `~/.hermes/config.yaml::mcp_servers` 配置）调用

设计参考自原 `reservation_agent-test-agent_multigraph`（LangGraph 实现），
本项目保留其**业务流程与设计哲学**，但内核沿用 hermes-feishu-agent 的
架构（OCL 流水线、双层防御、AIAgent 池、跨会话记忆、Curator 等）。

> 设计目标：在不重写内核的前提下，把车辆预约业务接进现有的
> hermes-feishu-agent 飞书通道。完成替换后代码保留 ~90% 现有结构，新增/改造
> 集中在业务层。

## 快速开始

```bash
cp .env.example .env # 填入 FEISHU_APP_ID, FEISHU_APP_SECRET, MINIMAX_API_KEY
cp data/identity_map.json.example data/identity_map.json # 配置 open_id → email/role
# 配置 MCP server（用户提供的接口文档）：
# ~/.hermes/config.yaml::mcp_servers::car_booking（command/args 或 url）
pip install -e ".[dev]"
python main.py # 启动机器人（WebSocket + 健康检查 HTTP）
```

## 常用命令

```bash
python main.py # 启动机器人
pytest tests/unit/ # 单元测试（无网络）
python scripts/selfcheck.py # 一键自动化自检（测试+编译+导入+配置漂移+文档/接线）
scripts/autofix.sh # 自检 + 调 Claude Code headless 回环修复至全绿
curl localhost:8080/health # 检查 ws_connected、metrics
```

## 架构——一句话一层

- `feishu/` — 只做 WebSocket收发；零业务逻辑
- `bot/` — 包装 hermes-agent 的 `AIAgent`；每用户一个实例（池化）；意图识别；从 identity 解析 email
- `ocl/` — Output Control Layer：格式、内容、长度、role-based工具 ACL、identity map、双层防御
- `car_tools/` — 车辆预约工具（handlers 用 `guarded()` 包裹做 Layer2）；emailAddress 服务端注入，LLM永远看不到
- `hermes_plugins/` — hermes插件：`feishu_acl` pre_tool_call钩子（Layer1硬拦截）
- `infra/` — dedup、metrics、health；无领域知识
- `config/` — 读 `.env`；所有配置的唯一来源
- `data/` — `identity_map.json`（open_id → email/role/name；gitignored，见 `.example`）
- `tests/` — 单元测试 mock一切

## 业务域

车辆预约（car_tools/）：外部 MCP server（用户提供接口文档，hermes 通过
`~/.hermes/config.yaml::mcp_servers` 连接）。

| 域 | 后端 | LLM-facing 层 | 鉴权方式 |
|------|------|----------|------|
| 车辆预约 | 外部 MCP server | `car_tools/` | openid + email 服务端注入 |

## 权限 /角色模型

角色来源：`data/identity_map.json`（open_id → role）。三档：

- **role=1 普通用户** — 查询可用车辆、预约/取消/归还自己的车辆、查询自己的预约
- **role=2调度员** — +审批本组车辆预约、查询待我审批列表
- **role=3管理员** — +跨组审批、系统级操作（未来）

OCL 按 min_role 粗粒度门控工具；后端按 openid/email 细粒度校验（角色 + 分组 + 状态）。
两道闸相互独立。

| Min role | 车辆预约工具 |
|----------|----------|
| 1 | fetch_available_vehicles, _dry_run_vehicle_reservation, _commit_vehicle_reservation, cancel_vehicle_reservation, return_vehicle, fetch_user_reservation, get_user_context, get_common_dictionary |
| 2 | approval_vehicle_reservation, fetch_user_approval |

管理员在飞书里指派角色：`设置角色 <open_id> <1|2|3>`。无自助申请流程。

## 不允许破坏的不变量

- WS回调必须立即返回（不阻塞；改用队列）
- 每个 `user_id` 严格对应一个 `AIAgent` 实例（池强制）
- `MINIMAX_API_KEY` 不出现在日志或错误消息中
- `max_iterations=30` 和 `timeout=120s` 是硬上限——非经讨论不要调
- Agent池 `get_or_create()` **必须**在 `ocl.session_map` 注册 `session_id`，按 LRU 驱逐
- `ocl/session_map.lookup()` miss 时返回 `""`——调用方必须 fail-open（plugin 返回 None、guarded 通过）
- 权限强制是双层防御：L1 pre_tool_call 插件（硬拦截） + L2 guarded 包裹（兜底）
- **车辆预约域**：`emailAddress` / `openId` / `mobile` 从 CallerIdentity 注入；绝不作为 LLM 工具参数
- **不变量**：`car_tools/register.py` 注册的工具 schema **不**含 emailAddress / openId / mobile 字段（结构性防御）
- 非平台用户（identity miss）无法进入 agent——handler 回友好提示

## 不允许做的事

- 不要添加任务没明确要求的功能
- 不要静默 catch 异常——带上下文 log 后 re-raise 或返回给用户
- 不要 `feishu` import `agent` 或反之——只能通过 `handler.py` 通信
- 不要把用户消息内容存到 metrics 或 logs——只存 ID 和延迟
- 不要在运行时修改 `~/.hermes/config.yaml`——启动后只读
- 不要在 LLM-facing tool schema 里加 emailAddress / openId / mobile 字段（由服务端从 contextvars 注入）
- 不要在车辆预约业务下加 `/fmp/` 路径或 mock_api/mock_tools 相关 token
- 不要新增工具前不在 `ocl/permission.py::TOOL_MIN_ROLE` 加 role 门控

## 跨会话记忆（DMZ 自进化 Phase 1）

约车助手已实现 hermes `MemoryProvider` 协议，把用户偏好、近期操作、错误模式跨会话持久化。

**已默认启用（无需配置）。** 接线方式见 `bot/agent_pool.py:_wire_dmz_memory`：

- hermes 只从 `plugins/memory/` 或 `$HERMES_HOME/plugins/` 发现 provider（靠 `memory.provider` 配置键激活）。我们的 provider 在 repo 内 `bot/dmz_memory.py`，**不走** hermes 的插件发现，而是在 `agent_pool.get_or_create()` 构造完 `AIAgent` 后**直接挂载**：建 `MemoryManager` → `add_provider(DMZMemoryProvider())` → `initialize(session_id, user_id, hermes_home)`。
- **关键前提**：`AIAgent` 构造时必须传 `user_id=user_id`（否则 hermes 不把 user 身份透传给 provider，`sync_turn` 会因 `user_id` 为空而空转，记忆永不落盘）。
- hermes 运行时自动调用：`conversation_loop` 每轮调 `prefetch_all`（召回），`run_agent` 每轮结束调 `sync_all`→`sync_turn`（写回）。
- 记住：常查的车辆 / 芯片平台（Xavier/ADCU/Orin/Thor）/ 车辆类型（DM2/CT1/大F车/CM0/BM2）、最近 20 步工具调用模式（仅工具名+参数 key）、累计 4xx/5xx 错误模式（prefetch 时提示「曾频繁遇到…注意规避」）。

**存储位置：** `$DMZ_MEMORY_HOME/dmz_memory/<user_hash16>/memory.json`，默认 `data/dmz_memory/…`（落在挂载进容器的项目卷里，跨重启/重建持久化；已 gitignore）。可用环境变量 `DMZ_MEMORY_HOME` 覆盖。

**验证已生效：** 启动日志出现 `dmz_memory wired user=…` + `dmz_memory_init`；发几条消息后 `data/dmz_memory/<hash>/memory.json` 会出现并累积。

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
- 不要静默 catch 异常——带上下文 log 后 re-raise 或返回给用户
- 不要 `feishu` import `agent` 或反之——只能通过 `handler.py` 通信
- 不要把用户消息内容存到 metrics 或 logs——只存 ID 和延迟
- 不要在运行时修改 `~/.hermes/config.yaml`——启动后只读
- 记忆层**不要**存敏感字段（即使通过"用户体验改进"为由）
