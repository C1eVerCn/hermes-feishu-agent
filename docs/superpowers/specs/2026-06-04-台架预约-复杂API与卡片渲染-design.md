# 台架预约：复杂 API 重建 + 飞书卡片渲染 — 设计文档

**日期：** 2026-06-04
**项目：** hermes-feishu-agent
**作者：** Claude + chris

---

## 1. 背景与目标

当前 `mock_api`（用户/订单/报表）过于简单，弱模型（MiniMax）几乎不会出错，OCL
输出控制层缺乏可演示的失败场景；同时 agent 回复以飞书**纯文本**发送，markdown
（`**粗体**`、`- 列表`）原样渲染成字面星号/横线，观感差。

本设计做两件事：

1. **复杂化 API**：用真实的「台架预约」8 接口（见接口文档 v1.0）替换现有
   mock_api，本地 mock，保留文档中的严格业务校验，让弱模型有真实出错空间，
   OCL 全程兜底。
2. **卡片渲染层**：利用 hermes 的 `post_tool_call` 钩子捕获工具返回的**原始
   JSON**，确定性地渲染成飞书互动卡片（无标题栏、摘要块 + 确定性数据块 +
   可交互按钮），数据不经弱模型转述。

### 关键决策（已与用户确认）

| 决策点 | 结论 |
|---|---|
| 后端来源 | 本地 mock 这 8 个接口（自包含、可单测） |
| 复杂化手段 | 多步骤依赖链 + 严格校验/易混参数 + 状态机/业务规则 + 分页/关联查询 全要 |
| 台架数据规模 | 30 个台架 |
| 权限模型 | 按文档 role=1/2/3（普通/调度员/管理员），重构 permission.py 为角色模型 |
| 身份打通 | 系统注入 emailAddress（open_id→email 映射），emailAddress 不暴露给 LLM |
| 自助权限申请 | 删除「申请权限/管理员批准」流程，改为管理员「设置角色」 |
| 渲染方式 | 卡片 + 结构化渲染；结构化数据来自 hermes 工具结果（确定性） |
| 卡片结构 | **无标题栏**；摘要块 + 确定性数据块 + 可交互按钮 |
| 按钮行为 | 真回调动作（卡片回调 → 直接调 API，不经 LLM），OCL 角色 + API 业务两道闸 |

---

## 2. 第一部分 — 台架预约 API 层

### 2.1 重建 `mock_api/`

按文档实现 8 个接口，路径前缀 `/fmp/testBenchReservationForAgent`，端口 9013，
统一响应 `{code, message, data}`。

| # | 路由 | 方法 | 必填参数 | 关键约束 |
|---|---|---|---|---|
| 1 | `/architectures` | GET | 无 | 返回架构列表 |
| 2 | `/availableTestBenches` | POST | emailAddress | 平台用户；architecture、needParkingTest(0/1) 可选过滤 |
| 3 | `/reserveTestBench` | POST | emailAddress, benchNo, startTime, endTime, taskName, testPurpose | start<end、start>now、台架 status=1、非管理员仅同组、台架须有分组且有调度员；remark 可选 |
| 4 | `/cancel` | POST | emailAddress, benchNo | 仅能取消 status=0；startTime/endTime 用于精确定位 |
| 5 | `/approve` | POST | emailAddress, benchNo, approvalResult(1批/2拒) | 仅 role≥2、调度员仅审本组、仅 status=0；approvalRemark/startTime/endTime 可选 |
| 6 | `/myReservations` | POST | emailAddress | benchNo/startTime/endTime/taskName/status(0-4) 可选过滤 |
| 7 | `/myApprovals` | POST | emailAddress | 仅 role≥2；status(0-4) 可选 |
| 8 | `/returnTestBench` | POST | emailAddress, benchNo, returnLocation | 仅能归还 status=1，完成后→4；多条已批准取第一条 |

**状态机**（`mock_api/state_machine.py`）：

```
0 待审批  ──approve(1)──▶ 1 已批准  ──return──▶ 4 已完成
   │       ──approve(2)──▶ 2 已拒绝
   └──cancel──▶ 3 已取消
```

VALID_TRANSITIONS：`0→{1,2,3}`，`1→{4}`，`2/3/4→{}`。

**业务校验严格实现**（失败返回文档原文错误提示，`code≠200`）：

- 预约：邮箱/编号/时间/任务/目的非空；start<end；start>now；台架存在且 status=1；
  非管理员仅同组；台架须分组且有调度员。
- 取消：仅 status=0；找不到待审批记录则报错。
- 审批：仅 role∈{2,3}；调度员仅审本组；仅 status=0；approvalResult∈{1,2}；
  支持批量审批同时间段、同台架的多条。
- 归还：仅 status=1；多条取第一条；完成后→4。
- 时间格式严格 `yyyy-MM-dd HH:mm:ss`；needParkingTest∈{0,1}。

**种子数据**（`mock_api/fake_db.py`）：

- 架构 5 种：`1.0架构 / 1.5架构 / 3.0架构 / L3架构 / L4架构`（对齐文档实测）。
- 台架 30 个：`TJ001…TJ030`，各带 architecture、status、groupId、needParkingTest。
  少量置为不可用 / 未分组 / 分组无调度员，用于触发对应错误分支。
- 分组若干（如 G1…G5），每组 1–2 名调度员（姓名 + 邮箱）。
- 用户：每种角色至少 1–2 名（普通/调度员/管理员），各带 email + groupId。
- 预约记录：预置若干 status=0/1 的记录，方便演示取消/审批/归还与查询。

> 注：mock 内部按 emailAddress 反查用户的 role 与 groupId 来强制业务规则。

### 2.2 工具注册 `mock_tools/`

8 个工具，schema 贴近真实文档以**保留易混参数**（benchNo vs benchId vs id、
needParkingTest 0/1、approvalResult 1/2、时间格式 `yyyy-MM-dd HH:mm:ss`），让弱
模型有机会传错 → API 返回结构化错误 → agent 自我纠正或 OCL 兜底。

**emailAddress 不进 schema**：工具参数中不含 emailAddress；`handlers.py` 在发起
HTTP 请求时，从「当前用户上下文」（open_id→email）注入。弱模型既无法编造邮箱，
也无法给别人预约。

工具清单：`list_architectures`、`list_available_benches`、`reserve_bench`、
`cancel_reservation`、`approve_reservation`、`list_my_reservations`、
`list_my_approvals`、`return_bench`。

### 2.3 修复真 bug（task_id 透传）

`tools.registry.dispatch` 会向 handler 注入 `task_id`/`user_task` 等 kwargs，而现
有 handler 签名仅 `def f(args)`，导致截图中 `list_orders` 报「意外参数 task_id」。
新 handler 统一签名 `def f(args: dict, **_) -> str`，容忍多余 kwargs，消除该真
bug，只保留**故意埋的业务坑**。

### 2.4 身份与角色

#### 2.4.1 身份映射 `data/identity_map.json`

一份表打通 open_id → email、role、name：

```json
{
  "ou_zhang": {"email": "zhangsan@example.com", "name": "张三", "role": 1},
  "ou_diao":  {"email": "scheduler@example.com", "name": "李四", "role": 2},
  "ou_admin": {"email": "admin@example.com",     "name": "王五", "role": 3}
}
```

- role 由身份决定，不自助申请。
- 消息进来 → open_id 查表 → email（注入工具）+ role（OCL 门控）+ name。
- 查不到 = 非平台用户 → 友好提示联系管理员开通（对齐文档错误提示）。

#### 2.4.2 OCL 工具门控按角色（`ocl/permission.py` 重构）

group 模型 → role 模型。每个工具标注最低角色：

| 工具 | 最低角色 |
|---|---|
| list_architectures, list_available_benches, reserve_bench, cancel_reservation, return_bench, list_my_reservations | 1 普通用户 |
| approve_reservation, list_my_approvals | 2 调度员 |

- OCL 只做**粗粒度**门控（能否调 `approve`）。
- **细粒度规则**（同组、status 限制）由 mock API 强制 → 两道独立的闸，演示分层防御。
- 双层防御不变：Layer 1（feishu_acl 插件 pre_tool_call）+ Layer 2（guarded），
  内部 `is_tool_permitted(open_id, tool)` 改为查角色。

#### 2.4.3 飞书侧流程调整（`bot/handler.py`）

- **删除**「申请写入/报表权限」自助申请 + 管理员批准的 regex 流程。
- **新增**管理员指令：`查看用户`、`设置角色 <open_id> <1|2|3>`（写 identity_map）。
- 简单意图回复（你好/帮助等）保留，文案改为台架预约场景。

---

## 3. 第二部分 — 飞书卡片渲染

### 3.1 捕获工具结果（hermes `post_tool_call` 钩子）

在现有 `hermes_plugins/feishu_acl` 插件中**追加注册** `post_tool_call` 钩子（已能
拿到 session_id）。每次工具返回后，把 `(tool_name, args, result_json)` 按 session
存入新模块 `ocl/tool_capture.py`（session 键、带锁，仿 session_map）。

```
agent.chat() 内部多次调用工具
  → post_tool_call(tool_name, args, result, session_id)
  → tool_capture.record(session_id, tool_name, result)   # 累积本轮
handler 在 chat 前 clear(session)、chat 后 read(session) → 本轮所有工具原始 JSON
```

数据 100% 来自 API 原始返回，不经弱模型转述 → 确定性渲染。

### 3.2 卡片构建（纯函数模块，可单测）

- **`ocl/markdown_to_lark.py`**：把 agent 最终文本的 markdown（标题/粗体/列表/
  代码块）转成飞书 `lark_md` 元素。解决字面星号/横线问题。
- **`ocl/card_builder.py`**：组装互动卡片（**无 header 标题栏**）：
  - **摘要块**：agent 自然语言回复（markdown→lark_md）。
  - **确定性数据块**：若本轮捕获到结构化查询（list_my_reservations /
    list_my_approvals / list_available_benches / list_architectures），用**原始
    JSON** 渲染：
    - 预约/审批列表 → 每条一个字段块（台架编号、时间、状态徽标、任务、审批人…）。
    - 可用台架/架构 → 标签/列表。
  - **交互按钮**：见 3.4。
  - 状态以文字/徽标体现（不靠 header 着色）。
- **`ocl/tool_capture.py`**：上面的捕获存储（record / read / clear，带锁）。

### 3.3 发送层（`feishu/sender.py`）

新增 `send_card(chat_id, card: dict)`，`msg_type="interactive"`。现有 text 路径、
限流、重试、分块全部保留（异常/纯问答 → 纯文本兜底）。

### 3.4 卡片按钮回调（真回调动作）

飞书卡片按钮点击是**同步回调**（服务端需快速返回 toast + 更新后的卡片）。本地
mock API 很快，可在回调内同步完成。

- **`feishu/ws_client.py`**：注册卡片 action 处理器
  （`register_p2_card_action_trigger_v1`），与消息事件并存。
- **`bot/card_action_handler.py`（新）**：
  1. 从 action 的 `value` 取 `{action, benchNo, startTime, endTime, approvalResult,
     returnLocation…}`——**不含 email**。
  2. 用点击者 open_id 注入 email + 查角色 → **OCL 角色门控**。
  3. 直接调对应 mock_api 工具（不经 LLM，确定性）。
  4. 构建更新后的卡片（按钮置灰/状态变更）+ toast，同步返回。
  - API 自身业务校验仍在 → OCL 角色 + API 业务两道闸，伪造 value 也越不了权。

**按钮边界**（只对参数自足的动作放真回调按钮）：

| 卡片来源 | 按钮 | value |
|---|---|---|
| list_my_reservations 中 status=0 | 取消预约 | `{action:"cancel", benchNo, startTime, endTime}` |
| reserve 成功卡片 | 取消预约 | 同上 |
| list_my_approvals 中 status=0 | 批准 / 拒绝 | `{action:"approve", benchNo, approvalResult:1/2, startTime, endTime}` |
| 已批准(status=1)的预约 | 归还台架 | `{action:"return", benchNo}`（地点走后续消息补充，见下） |

- `list_available_benches` 的「预约」按钮**不放真回调**——预约缺时间/任务/目的，
  参数不自足；预约仍走对话。
- `returnTestBench` 需 `returnLocation`，卡片按钮无法直接输入 → 该按钮点击后**自动
  发后续消息**（「归还 TJxxx，地点…」）让用户补地点。

### 3.5 接入 OCL 管线（`ocl/pipeline.py` + `bot/handler.py`）

`pipeline.apply()` 仍先跑 format→content→length（作用于**文本**），再多一步：用
过滤后的文本 + 捕获的工具结果交给 `card_builder` 生成卡片。`OclResult` 增加
`card: dict | None` 字段。

`handler.py`：`ocl_result.card` 存在 → `sender.send_card`；否则 → `sender.send(text)`。
content 被拦截 / 异常 → 仍发文本提示。

### 3.6 数据流全景

```
飞书消息 → handler → tool_capture.clear(session)
  → agent.chat()  ──工具调用──▶ post_tool_call 钩子 → tool_capture.record()
  → 拿到 final text + 捕获的原始 JSON
  → ocl.pipeline.apply(text, user_id)
        ├ format / content / length   (作用于文本)
        └ card_builder(text, captured) → 互动卡片
  → sender.send_card(chat_id, card)    (异常/纯问答 → send text)

飞书卡片按钮点击 → ws_client(card action) → card_action_handler
  → open_id 注入 email + OCL 角色门控 → 调 mock_api 工具
  → 更新后的卡片 + toast（同步返回）
```

---

## 4. 模块边界一览

| 模块 | 职责 | 依赖 |
|---|---|---|
| `mock_api/` | 8 接口 + 状态机 + 业务校验 + 种子数据 | 无（独立 FastAPI） |
| `mock_tools/` | 8 工具注册；email 注入；handler 容忍多余 kwargs | mock_api（HTTP）、tool_guard |
| `ocl/identity.py`(新) | open_id ↔ email/role/name 映射读写 | data/identity_map.json |
| `ocl/permission.py`(重构) | 角色模型 + 工具最低角色门控 | identity |
| `ocl/tool_capture.py`(新) | 按 session 捕获工具结果 | 无 |
| `ocl/markdown_to_lark.py`(新) | markdown → lark_md 元素 | 无 |
| `ocl/card_builder.py`(新) | 组装互动卡片（摘要+数据+按钮） | markdown_to_lark |
| `ocl/pipeline.py`(改) | format/content/length + 生成卡片 | card_builder, tool_capture |
| `feishu/sender.py`(改) | 新增 send_card | 无 |
| `feishu/ws_client.py`(改) | 注册卡片 action 事件 | 无 |
| `bot/card_action_handler.py`(新) | 卡片回调 → 注入身份 → OCL → 调工具 → 更新卡片 | permission, identity, mock_tools |
| `bot/handler.py`(改) | 删自助申请、加设角色、接卡片发送 | pipeline, sender, identity |
| `hermes_plugins/feishu_acl/`(改) | 追加 post_tool_call 钩子 | tool_capture |

---

## 5. 测试范围

延续现有 pytest「全绿」目标，新增/更新单测：

- `mock_api`：8 接口的成功路径 + 各业务校验失败分支（状态机、角色、同组、时间、
  needParkingTest、approvalResult）。
- `mock_tools`：8 工具注册、email 注入、handler 容忍 `**kwargs`。
- `ocl/identity`：映射读写、缺失 fallback。
- `ocl/permission`：角色 → 工具门控矩阵。
- `ocl/tool_capture`：record/read/clear、按 session 隔离、并发安全。
- `ocl/markdown_to_lark`：粗体/列表/标题/代码块转换。
- `ocl/card_builder`：摘要块、各结构化数据块、按钮生成与边界。
- `ocl/pipeline`：text 路径 + card 路径、拦截/异常 fallback。
- `bot/card_action_handler`：value 解析、email 注入、OCL 拒绝、API 业务拒绝、
  卡片更新。
- `feishu/sender`：send_card payload。

集成测试（需 live env）：mock_api 起服后端到端预约/审批/归还/查询。

---

## 6. 不做（YAGNI）

- 不接真实台架后端（本地 mock）。
- 不实现 `available_benches` 的「预约」真回调按钮（参数不自足）。
- 不做卡片内复杂表单输入（归还地点走后续消息）。
- 不引入 DB/Redis（identity_map / 预约数据用 JSON / 内存，沿用现有约定）。
- 不保留旧的 用户/订单/报表 域与自助权限申请流程。

---

## 7. 风险与注意

- **卡片回调时延**：飞书要求回调快速返回；本地 mock 足够快，但需确保
  card_action_handler 无阻塞 I/O（限流锁不要卡在回调路径）。
- **session→email 一致性**：identity_map 缺失时必须 fail-safe（提示非平台用户，
  不得编造 email）。
- **OCL 不变量**：`pipeline.apply()` 仍需 <100ms、不抛异常、不记录正文内容；
  card_builder 为纯 CPU，满足。
- **删除旧域影响范围**：会删除/重写 `test_mock_api.py`、`test_mock_tools_register.py`、
  `test_ocl_permission.py` 等，需同步更新文档（架构/部署/CLAUDE.md）。
