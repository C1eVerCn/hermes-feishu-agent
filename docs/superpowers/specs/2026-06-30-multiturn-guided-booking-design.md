# 多轮引导式约车 — 发挥 Hermes agent 最大优势

**日期**：2026-06-30
**范围**：核心 + 引导配套（接多轮对话历史 + dry_run 守卫跨轮存活 + 引导式澄清 + 时间锚点刷新）
**设计哲学**：最大化 LLM 自由推理，结构性护栏兜底，**不**把对话流程写死成状态机（否则不如用 LangGraph）。收束在约车域内。

---

## 1. 背景与问题

项目正从旧的 FSM（确定性多步状态机）迁移到 LLM 驱动的自由对话。FSM 已删除
（`car_booking_fsm.py` / `return_fsm.py` / `car_state.py` / `card_action_handler.py`
git 标记 deleted），但**迁移没走完**——最关键的一步漏了。

### 致命诊断（代码确认）

> **当前每一条飞书消息，对 agent 都是一次全新的单轮对话。它不记得上一句。**

- `bot/handler.py:225` → `agent.chat(agent_input, _on_delta)`
- `run_agent.py:4373` → `chat()` 调 `run_conversation(message, stream_callback=...)`，**从不传 `conversation_history`**
- `agent/conversation_loop.py:506` → `messages = list(conversation_history) if conversation_history else []` → 永远 `[]`
- `handler.py:201-202` 的注释「agent.history 永久保留 / agent 已记住」**是错的**；池化实例的 `_session_messages` 是只写、下一轮从不回读。

这正是"一步步引导发散用户"的死穴：用户说"约车"→ bot 问"哪个平台"→ 用户答"Orin"→
"Orin" 到达时是全新对话，agent 根本不知道刚才在约车。发散思维（"算了换明天"、
"不对是 Orin 不是 Thor"）的修正全部丢失上下文。

现在唯一能跑通的是：fast_path 纯查询（不需记忆）+ 一句话说全的约车（dry_run 和
commit 挤在同一轮）。**真正的多轮引导，地基没接上。**

---

## 2. 目标 / 非目标

### 目标
1. agent 在一次约车对话内记得上文，能跨轮收集槽位、应对用户改主意。
2. 多轮开启后，dry_run→commit 安全不变量**仍然成立**（这是开多轮的硬前置）。
3. 对真正发散/欠定的请求，agent 能**问一个**聚焦的澄清问题（而非瞎猜或无限追问）。
4. 相对时间（明天/后天）始终基于**当前**时间，不漂移。

### 非目标（明确不做）
- 不重写臃肿/自相矛盾的 system prompt（option 3 范围）。
- 不收缩/移除 fast_path 查询旁路（option 3 范围）。
- 不修工具 schema 与 prompt 的 required 矛盾（option 3 范围）。
- 不接 hermes native `clarify_callback`（见 §3.2，它是结构性误配）。
- 不动任何结构性护栏（L1/L2 权限、email 注入、schema 不含身份字段、ROLE_TOOLS、单一 car toolset）。

---

## 3. 关键发现（决定方案）

### 3.1 多轮历史：hermes 有加载 API，但 bot 没用，且持久化关着
- 加载器：`SessionDB.get_messages_as_conversation(session_id)`（`hermes_state.py:1951`）。
- 喂回：`AIAgent.run_conversation(user_message, system_message=None, conversation_history=None, task_id=None, stream_callback=None, persist_user_message=None)`（`run_agent.py:4360`）。**必须用 `run_conversation` 而非 `chat()`** 才能带历史。
- 默认 `session_db=None`（`run_agent.py:407`），所以 hermes **不**往 `state.db` 落盘；`CLAUDE.md`「自动落盘」的说法对当前接线**不成立**。
- **决策（已与用户确认）：方案 B —— 我们自己在 pool 里维护 per-user 内存 deque**，隐私干净（不落盘原始对话）、无热路径 SQLite 读、完全可控窗口/TTL。

### 3.2 ⚠️ 地雷：开多轮会让 commit 被守卫拒
- `ocl/tool_capture.py` 的 `_store` 在 `handler.py:216`（轮初）和 `handler.py:243`（finally）**每轮清空**。
- 今天能约车：dry_run 和 commit 在**同一个 `chat()` 调用**内（同一轮），buffer 只需活在一轮内。
- 多轮后：dry_run 在第 N 轮，用户"确认"在第 N+1 轮 → commit 时 buffer 已被清 →
  `_check_dry_run_guard`（`car_tools/handlers.py:41-76`）`tool_capture.read()` 返回空 →
  落到 `handlers.py:76` 返回「未找到有效的 _dry_run」→ **合法预约被拒**。
- session_id 跨轮稳定（`feishu_{user_id}`），**不是** session 不匹配；是 buffer 被清。
- **所以 Part 2（dry_run 状态跨轮存活）是开多轮的强制前置，不是可选项。**

### 3.3 clarify_callback 是飞书的结构性误配
- 它**同步阻塞**：`tools/clarify_tool.py:64` 直接 `callback(question, choices)` 等字符串返回，gateway 实现用 `threading.Event` 阻塞 worker 线程最多 600s 等用户下一条消息。
- 与本项目冲突：`handler.py:226` 的 `future.result(timeout=120)` 会在 120s 杀掉 run，用户回复永远到不了被卡住的线程；且违反「WS 回调必须立即返回」不变量。而且**根本没接线**。
- **结论：不接。** 想要的"问一句"体验，**多轮一开就免费**——模型把问题当普通回复发出、结束本轮，用户下条消息进来同一 agent（带历史）接着走。这恰恰是 clarify 阻塞设计在对抗的异步语义，被现有"per-user agent + 历史"架构白送。
- 所以「引导式澄清」退化成**一行 prompt 放松**（§4.3）。

---

## 4. 设计

### Part 1 — 多轮记忆（地基）

**`bot/agent_pool.py`**：在 `AgentPool` 加 per-user 历史，复用现有 `self._lock` 与 LRU 生命周期。

```python
# 与 agent 实例同生命周期：popitem 驱逐 agent 的同一路径里一起 pop 掉历史
self._history: OrderedDict[str, deque] = OrderedDict()   # user_id -> deque(maxlen=12)
self._history_touch: dict[str, float] = {}               # user_id -> monotonic last-use

def get_history(self, user_id: str) -> list[dict]:
    # 30min 空闲 TTL：超时则清空，返回最近 N 轮 user/assistant dict
def append_turn(self, user_id: str, user_msg: dict, assistant_msg: dict) -> None:
    # 追加；只存纯文本 role=user/assistant，跳过 tool 轮
def clear_history(self, user_id: str) -> None: ...
```

- 窗口：**最近 6 轮**（`deque(maxlen=12)` = 6 user + 6 assistant）。理由：minimax M2.7 长 prompt 遵守差 + system prompt 已大，6 轮够"约/取消/还"多步连续性又不稀释顶部 3 条核心规则。
- 单条 content 截断 **~800 字**，防病态长消息。
- **空闲 TTL 30 分钟**：`get_history` 时 `now - touch > 1800s` 则清空（比旧 car_state 的 10min 长，因为是对话记忆），让久别重来的用户从头开始而非续上陈旧约车线程。
- 只存 **user/assistant 纯文本**，**跳过 tool 轮**——避免手搓 protocol-valid 的 tool_call/tool_result 配对（探针明确警告其脆弱），对展示型卡片 bot 足够。

**`bot/handler.py:_run_agent`**：
```python
hist = agent_pool.get_history(user_id)
result = agent.run_conversation(agent_input, conversation_history=hist, stream_callback=_on_delta)
response = result["final_response"]
# 成功后：
agent_pool.append_turn(user_id,
    {"role": "user", "content": agent_input},
    {"role": "assistant", "content": response})
```
- 替换 `handler.py:225` 的 `agent.chat(...)`；future/timeout/contextvars 包裹方式不变。
- 修 `handler.py:201-202` 假注释 + `CLAUDE.md` 的「自动落盘 state.db」说法。

**fast_path 连续性（小赠送）**：在 `_try_fast_query` 命中返回前，也把这条 Q&A
`append_turn` 进 deque（合成一条 assistant），这样"查可用车"后说"约刚才第一辆"接得上。
仅对 **fast_path 查询**做（问候/身份/管理员命令不入历史）。

> **跨轮 commit 的上下文依赖**：deque 只存文本，所以 dry_run 轮的 assistant 回复
> **必须包含 summary（含 6 字段）**——确认轮模型据此重建 commit args。SKILL 已要求
> "念 summary 给用户"，满足。万一不放心，可改为让 commit 直接用 `dry_run_state` 里
> 存的 args（§Part 2 的硬化选项，本期不做）。

### Part 2 — dry_run→commit 守卫跨轮存活（强制前置）

**新建 `bot/dry_run_state.py`**：内存 per-user store，复活已删 `car_state.py` 机制。
```python
# dict[str, dict] + threading.Lock + monotonic TTL=600s
def save(openid: str, args: dict, ts: float | None = None) -> None  # 只存 6 必填字段 + ts
def get(openid: str) -> dict | None                                  # 过期返 None
def clear(openid: str) -> None
def evict_expired() -> None
```
- key = caller **openid**（`handler` 已注入 CallerIdentity）。
- **只存** `_COMMIT_REQUIRED_FIELDS`（vehicleType/platform/startTime/endTime/taskName/location）+ 时间戳。**绝不存** email/openid/mobile/密钥（与 DMZ 记忆同脱敏铁律）。
- **每次新 dry_run 覆盖**旧条目 → 只有"最新一笔待确认预约"可 commit；用户改主意重新 dry_run 自动替换 → 优雅处理"换一个"。

**`bot/handler.py`**：读完 `captured = tool_capture.read(session_id)`（现 `:227`）后，扫出最近一条
无 `missing_fields` 的 `_dry_run_vehicle_reservation` 结果 → `dry_run_state.save(openid, result["args"])`。
**放在 handler 里**（不只在 hook 里），确保即便 finally 清了 tool_capture 也已落到 dry_run_state。

**`car_tools/handlers.py:_check_dry_run_guard`（41-76）**：双源。
- 先走现有 `tool_capture.read(session_id)` 路径（单轮仍能用）。
- 查不到有效 dry_run（多轮情形）→ **回落 `dry_run_state.get(openid)`**，跑**完全相同**的校验：
  无 missing_fields / 6 字段逐一相等（`_commit_arg_key` camelCase 映射）/ 600s 新鲜度。
- commit 成功（`_commit_single_vehicle_reservation` 末尾 ~`handlers.py:289`）→ `dry_run_state.clear(openid)`。

**🔒 顺带堵洞 —— 收紧 fail-open**（`handlers.py:54-55`）：
- 现状：`session_id` 为空 → `return None`（放行 commit）。本是给已删除的 FSM/卡片 commit 路径用，那些路径现在都没了。
- 多轮后唯一合法 commit 路径 = LLM（一定注入 session_id + openid）。所以"两个身份锚都空就放行"现在是**安全漏洞**。
- **改为**：有 `session_id` 且 tool_capture 有 dry_run，**或** 有 `openid` 且 dry_run_state 有条目 → 校验放行；**两个锚都空 → 拒绝 commit**（视为误配/匿名内部调用，不得下真实单）。仅给显式内部调用标志留窄口，不给"上下文恰好没注入"留口。

**600s 窗口**：保留。多轮下真人确认间隔（几秒~几分钟）才真正用上它（单轮下它几乎瞬时无意义）。

### Part 3 — 引导式澄清（一行 prompt 放松，非 callback）

**不接** native clarify_callback。改 prompt 规则 #1，**两处同步改**：
`bot/agent_pool.py:108`（`_FEISHU_SYSTEM_PROMPT_BASE`）+ `bot/skills/car-booking/SKILL.md:15`（核心原则 #1），并改 `tests/unit/test_system_prompt.py` 期望。

旧（冲突）：
> 用户表达模糊时，立即调工具查，不要反复问。

新（放松但守住反审讯精神）：
> 用户表达模糊但**意图可推断**时，直接选最合理默认并调工具查，不为可自行决定的低风险细节追问
> （如没指定平台/车型 → 直接 `fetch_available_vehicles({})`）。**仅当**请求发散、或缺少无法默认
> 补全的关键信息（缺车辆 / 缺时段 / 一句话含多个互斥意图）时，**最多问一个**聚焦的澄清问题，
> 然后停下等用户回复；得到答复后立即执行，**不得连续追问第二次**。

护栏：① 最多一个澄清/请求，禁止连问；② 默认即走是常态、澄清是窄例外；③ 只枚举这几个触发条件，
不让它问本可自决的事；④ 复用现有"单轮≤2工具调用 / 一条回复 / ≤200字"。

### Part 4 — 时间锚点刷新

- 现状：`_now_cn()` 在 **agent 构造时**写死进 system prompt（`agent_pool.py:186`），池化 agent 活多久就用多旧的 now，相对日期会漂。
- 改：把"当前时间"行从 cached system prompt 移到**每轮 preamble**（`replies.identity_preamble` 旁或 handler 拼接处）。时间永远新鲜，且 system prompt 字节稳定 → KV-cache 更好（双赢）。

---

## 5. 锁定的决策

| 决策 | 选择 | 理由 |
|------|------|------|
| 多轮历史存储 | **方案 B：内存 deque** | 隐私干净、无热路径 DB、完全可控；重启丢失对低频内部 bot 可接受 |
| 历史窗口 | **6 轮**（maxlen=12），单条 ~800 字 | minimax 长 prompt 遵守差，够多步又不稀释规则 |
| 空闲 TTL | **30 分钟** | 久别重来从头开始，不续陈旧线程 |
| dry_run 跨轮 | **新 `dry_run_state.py`**（option 2） | 最低耦合、最贴 repo 习惯（复活 car_state 模式）、不变量端到端保留 |
| fail-open | **收紧**：两锚皆空则拒 commit | 删除 FSM 后旧放行口变成安全漏洞 |
| clarify | **不接 callback，改 prompt** | callback 同步阻塞撞 120s 硬上限；多轮已白送澄清 |

---

## 6. 安全不变量

**保留（一行不改）**：L1 `feishu_acl` pre_tool_call 硬拦 + L2 `guarded()` 兜底；emailAddress/openId/mobile
服务端 contextvars 注入；LLM-facing schema 不含身份字段；ROLE_TOOLS 非线性角色门控；单一 `car` toolset。

**强化**：dry_run→commit 守卫现在跨轮真正生效（600s 窗口实化）；fail-open 漏洞封堵。

**记忆脱敏**：deque 与 dry_run_state 都**不存** email/openid/mobile/密钥/完整原文以外的敏感字段；
deque 存对话文本属内存态、随 TTL/驱逐/重启即焚，不落盘。

---

## 7. 测试计划

- **`tests/unit/test_commit_guard.py`（新增/扩展）**：seed 一条 dry_run → 模拟轮界 `tool_capture.clear(session_id)`
  → 断言 `_check_dry_run_guard` 经 `dry_run_state` **600s 内仍通过**、**超时拒绝**、**字段不符拒绝**、**两锚皆空拒绝**。
- **`bot/dry_run_state` 单测**：save/get/clear/TTL 过期/覆盖语义；脱敏（断言存的 dict 不含 email 等键）。
- **`agent_pool` 历史单测**：append/get/窗口截断/30min TTL 清空/LRU 驱逐连带清史。
- **`test_system_prompt.py`**：更新规则 #1 期望（旧"不要反复问"断言会失败，改为新放松版断言）。
- **`handler` 集成（mock agent + mock 工具）**：两轮约车（dry_run 轮 → 确认轮）端到端 commit 成功；
  跨轮"改平台"覆盖 dry_run_state；fast_path 查询后引用"刚才第一辆"。
- 回归：现有单轮一句话约车仍通过（tool_capture 路径不动）。

---

## 8. 回滚 / 风险

- **回滚**：handler 里 `run_conversation(..., conversation_history=hist)` 退回 `agent.chat(...)`（传 `[]` 即等价旧行为）；prompt 规则 #1 改回；dry_run_state 双源里关掉回落分支。各 Part 独立、可逐个回退。
- **风险 1**：多轮历史让上下文变长，minimax 遵守可能波动 → 用 6 轮小窗 + 顶部规则不动缓解。
- **风险 2**：deque 只存文本，确认轮模型重建 commit args 依赖 dry_run summary 完整 → 守卫**失败即闭**（不符就拒并提示重 dry_run），安全；必要时启用"commit 直接用存的 args"硬化。
- **风险 3**：重启/驱逐丢对话与 dry_run_state → 用户重述；commit 因找不到 dry_run 而**安全拒绝**（不会误下单）。

---

## 9. 不做的事（边界）

- 不写死对话流程 / 不引入新状态机（违背"最大化 LLM"哲学）。
- 不动 fast_path 的查询拦截逻辑（除了把其结果补进 deque）。
- 不接 native clarify_callback / 不引入 GatewayRunner 机制。
- 不重写 system prompt 整体结构、不修 schema required 矛盾、不统一 dry_run/commit 描述措辞（均为 option 3）。
- 不改 max_iterations / timeout 硬上限。
