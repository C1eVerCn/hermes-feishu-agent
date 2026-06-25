# bot/

hermes-agent integration layer. Owns `AIAgent` lifecycle and the bridge from Feishu events to LLM responses.

## Files

- `handler.py` — **路由总枢纽（slim orchestrator，2026-06-25 重构后只做编排）**：从事件队列消费 → 身份闸 → 两档路由 → 渲染发送。Tier-1 命中即返回；Tier-2 调 `intent_router`；兜底走 agent。
- `intent.py` — **意图识别单一事实源**：escape / confirm / booking 意图 / 车辆编号 / 车型平台关键字 / 查询模式。handler 与 car_booking_fsm 都从这里导入（消除了原来 6 处分散、互相漂移的副本）。
- `intent_router.py` — **Tier-2 LLM 意图分类器**：Tier-1 未命中的消息 → 一次轻量结构化分类（intent + slots + confidence）。结构化输出防漂移；fail-open（异常→unknown→agent）。`_complete` 是唯一网络出口（单测 monkeypatch）。
- `replies.py` — 无 agent 的确定性文案：问候/帮助（`match_simple_intent`）、身份查询（`handle_identity_query`/`identity_reply`）、管理员命令（`handle_admin_command`）、agent 路径身份前导词（`identity_preamble`）。
- `fast_path.py` — 确定性查询快速路径：命中 `intent.match_query` 的精确短语 → `run_tool` 直接调 car_tools handler 并建卡（绕过 LLM）。Tier-1 与 Tier-2 query 共用 `run_tool`。
- `card_action_handler.py` — deterministic Feishu card-button callback: `handle(open_id, value)` → 注入 CallerIdentity → 走 car_tools 业务（select_vehicle / confirm_booking / cancel_flow） → 返回 (toast_text, updated_card)。No LLM. Injected into `feishu.ws_client` by `main.py`.
- `car_booking_fsm.py` — 13 状态约车 FSM。`advance()` 逐状态推进；`start_booking(user_id, slots)` 用 Tier-2 抽到的槽位**播种**并跳到第一个缺口状态（"说全了的人直接到确认卡"）。意图/编号识别复用 `bot.intent`。
- `agent_pool.py` — LRU pool of `AIAgent` instances keyed by `user_id`; max 100 entries; thread-safe. Generates stable `session_id = f"feishu_{user_id}"`, registers/evicts `ocl.session_map` mappings for the feishu_acl plugin. Mounts DMZMemoryProvider per user.
- `car_state.py` — per-user 车辆预约状态机（10min TTL）：save / update / get / clear / as_dict。记录用户当前正在进行的 booking / cancel / return / approve / records 意图与已收集的槽位。
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

## handler.py 两档路由（2026-06-25 重构）

```
event → extract (user_id, chat_id, text) → validate input
  Tier 1（确定性，零歧义，瞬回）
      → Layer 0 即时文案 replies.match_simple_intent（先于身份闸，避免被飞书 API 阻塞）
      → 身份闸 _resolve_identity → set_current_caller(CallerIdentity)
      → FSM 续答：挂起态 escape(intent.is_escape) / 查询逃逸 / 继续推进
      → fast_path.try_fast_path（intent.match_query 精确短语 → 直接调工具）
      → FSM 入口：in_fsm OR intent.is_booking_intent → car_booking_fsm.advance
      → replies.handle_identity_query / handle_admin_command（精确正则）
      → role==0 stranger 提示
  Tier 2（LLM 结构化分类，理解措辞多样/表达有问题的消息）
      → intent_router.classify → RouteResult{intent, slots, confidence}
          book(高置信)        → car_booking_fsm.start_booking(slots) 播种 + 跳缺口
          query_*(高置信)     → fast_path.run_tool
          identity            → replies.identity_reply
          chitchat            → intent_filter.REDIRECT_MESSAGE
          cancel/return/approve/unknown/低置信 → 落到 agent
  Agent 路径（兜底，最大自由度）
      → pool.get_or_create(user_id) → contextvars.copy_context()
      → executor.submit(agent.chat, preamble+text).result(timeout=120)
      → ocl.pipeline.apply → sender.send_card / send_text_as_card
      → _persist_dry_run_state（_dry_run_vehicle_reservation → car_state.save）
      → _notify_applicants_from_captured（审批成功 → DM 申请人）
```

设计哲学：**意图识别交给 LLM（Tier-2），多步流程执行交给确定性 FSM/handler**。LLM 提议
intent+slots，确定性层处置（权限/必填校验/dry_run→commit 不变量全保留）→ 给自由但不失控、
不漂移。详见 `docs/intent-routing-redesign.md`。
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
