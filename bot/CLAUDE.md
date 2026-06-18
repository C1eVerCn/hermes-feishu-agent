# bot/

hermes-agent integration layer. Owns `AIAgent` lifecycle and the bridge from Feishu events to LLM responses.

## Files

- `handler.py` — consumes from the event queue; calls `agent_pool`, runs `AIAgent.chat()` in thread pool, clears/reads `ocl.tool_capture` around the call, runs OCL pipeline, sends an interactive card (or text fallback) via `sender`. 车辆预约业务子分支（状态机 + 快速路径 + 卡片回调）也在此文件。
- `card_action_handler.py` — deterministic Feishu card-button callback: `handle(open_id, value)` → 注入 CallerIdentity → 走 car_tools 业务（select_vehicle / confirm_booking / cancel_flow） → 返回 (toast_text, updated_card)。No LLM. Injected into `feishu.ws_client` by `main.py`.
- `agent_pool.py` — LRU pool of `AIAgent` instances keyed by `user_id`; max 100 entries; thread-safe. Generates stable `session_id = f"feishu_{user_id}"`, registers/evicts `ocl.session_map` mappings for the feishu_acl plugin. Mounts DMZMemoryProvider per user.
- `car_state.py` — per-user 车辆预约状态机（10min TTL）：save / update / get / clear / as_dict。记录用户当前正在进行的 booking / cancel / return / approve / records 意图与已收集的槽位。
- `dry_run_state.py` — legacy _dry_run_reservation 状态 fallback（与 car_state 并存）。
- `reservation_store.py` — 持久化 reservation_id → applicant_open_id，用于审批后 DM 申请人（vehicle_no 字段替代旧 bench_no）。
- `dmz_memory.py` — DMZMemoryProvider（hermes MemoryProvider 协议实现，跨会话持久化偏好 / 操作 / 错误模式；30 天 TTL）。
- `curator_runner.py` — Phase 3 自进化 Curator（巡检工具 schema，**只**生成 Skill / 工具描述建议，不改业务代码）。
- `feedback.py` — 卡片按钮回调记录用户操作模式（Phase 2 metadata-only）。
- `identity_admin.py` — open_id → {email, name, role} 自动建档 / 角色分配 / 审计。

## AIAgent rules

- One `AIAgent` instance per `user_id` — never share across users (hermes-agent session state is per-instance)
- Always create with `quiet_mode=True`, `max_iterations=30` — these are not configurable at runtime
- Pool eviction: LRU, evict oldest when `len > max_size`; evicted instances are discarded (hermes-agent persists to `~/.hermes/state.db` automatically)
- Thread pool: `max_workers=5` — limits concurrent Minimax API calls to stay within rate limit

## handler.py flow

```
event → extract (user_id, chat_id, text) → validate input
      → resolve identity (email + role) → set_current_caller(CallerIdentity)
      → Layer 0    闲聊/帮助/身份查询 (instant reply)
      → Layer 0.5/0.6 快速路径 (直接调 car_tools handlers，<1s)
      → car_state 状态机 (用户在挂起状态时收字段补充 / escape)
      → identity / admin 命令 (bypass agent)
      → pool.get_or_create(user_id)  # agent_pool registers session_map
      → contextvars.copy_context()  # 跨线程传播 identity
      → executor.submit(agent.chat, text).result(timeout=120)
      → ocl.pipeline.apply(response, user_id)  # format → content → intent → length
      → sender.send_card / sender.send_text_as_card
      → 状态机持久化：_dry_run_reservation → car_state.save()
```

Permission requests and admin commands are keyword-matched (regex, no LLM) for reliability.
Empty or whitespace-only text → reply with a static prompt string, skip LLM.

## car_state 状态机（10min TTL）

- IDLE → 用户说「查车」→ QUERY_PENDING → fetch_available_vehicles
- QUERY_PENDING → 用户点 [选N] → BOOKING_DRY_RUN → _dry_run_reservation
- BOOKING_DRY_RUN 缺字段 → 用户补字段 → 重新 dry_run → BOOKING_DRY_RUN
- BOOKING_DRY_RUN 全齐 → 用户点 [确认] → BOOKING_SUBMIT → _commit_vehicle_reservation
- (any) → 用户说「算了/换个/不订了」→ IDLE（clear state）
- escape 关键词："算了", "换个", "不订了", "取消", "放弃", "不要了"

## What NOT to do

- Do not catch `TimeoutError` silently — reply with the standard timeout message and log the event
- Do not hold the pool lock while calling `AIAgent.chat()` — acquire lock only to get/create the instance
- Do not retry `AIAgent.chat()` on failure — hermes-agent has internal retry; double-retry amplifies Minimax costs
- Do not put business logic here beyond state machine + 快速路径 dispatch — car_tools/handlers 是业务核心
- Do not modify car_state without updating car_state tests (test_car_state.py)
