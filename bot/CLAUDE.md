# bot/

hermes-agent integration layer. Owns `AIAgent` lifecycle and the bridge from Feishu events to LLM responses.

## Files

- `handler.py` — **路由总枢纽（单路径全对话流，2026-06-30 重构）**：消费事件队列 → 输入校验 → `replies.match_simple_intent`（零延迟问候/帮助）→ 身份闸 `_resolve_identity` → `set_current_caller` / `set_current_session` → role==0 陌生人提示 → `_try_fast_query`（固定查询正则短路，命中直调 car_tools，结果写入多轮历史）→ `replies.handle_identity_query` / `handle_admin_command` → `_run_agent`（统一兜底，多轮 `run_conversation` + `_now_preamble` 时间注入 + `append_turn`）。**FSM / 两档路由已彻底拆除**，所有业务消息进入单一 LLM 路径。
- `intent.py` — **意图识别单一事实源**：escape / confirm / booking 意图 / 车辆编号 / 车型平台关键字 / 查询模式。
- `replies.py` — 无 agent 的确定性文案：问候/帮助（`match_simple_intent`）、身份查询（`handle_identity_query`/`identity_reply`）、管理员命令（`handle_admin_command`）、agent 路径身份前导词（`identity_preamble`）。
- `agent_pool.py` — LRU pool of `AIAgent` instances keyed by `user_id`; max 100 entries; thread-safe. Generates stable `session_id = f"feishu_{user_id}"`, registers/evicts `ocl.session_map` mappings for the feishu_acl plugin. Mounts DMZMemoryProvider per user. **同时维护 per-user 多轮对话历史 deque（最近 6 轮 / 30分钟空闲 TTL / 800字 content 截断）**；接口：`get_history(user_id)` / `append_turn(user_id, user_msg, assistant_msg)` / `clear_history(user_id)`；随 agent 一起按 LRU 驱逐。
- `dry_run_state.py` — **跨轮次 dry_run 快照（内存，per-openid）**：由 `car_tools/handlers._dry_run_reservation` 完成 dry_run 后写入，在多轮对话中当 `ocl.tool_capture` 缓存为空时由 commit 守卫 `_check_dry_run_guard` 回落读取，保证 dry_run→commit 不变量跨 turn 成立。TTL=600s，仅存 6 个去敏字段，不含 emailAddress/openId/mobile。
- `reservation_store.py` — 持久化 reservation_id → applicant_open_id，用于审批后 DM 申请人（vehicle_no 字段替代旧 bench_no）。
- `dmz_memory.py` — DMZMemoryProvider（hermes MemoryProvider 协议实现，跨会话持久化偏好 / 操作 / 错误模式；30 天 TTL）。
- `curator_runner.py` — Phase 3 自进化 Curator（巡检工具 schema，**只**生成 Skill / 工具描述建议，不改业务代码）。
- `feedback.py` — 卡片按钮回调记录用户操作模式（Phase 2 metadata-only）。
- `identity_admin.py` — open_id → {email, name, role} 自动建档 / 角色分配 / 审计。

## AIAgent rules

- One `AIAgent` instance per `user_id` — never share across users (hermes-agent session state is per-instance)
- Always create with `quiet_mode=True`, `max_iterations=30` — these are not configurable at runtime
- Pool eviction: LRU, evict oldest when `len > max_size`; evicted instances are discarded. **注意**：本 bot 未传 `session_db` 给 `AIAgent`，hermes **不**自动持久化对话轮次到 `~/.hermes/state.db`；多轮历史保存在内存 `agent_pool` per-user deque（方案 B，最近 6 轮 / 30分钟 TTL，非磁盘）。
- Thread pool: `max_workers=5` — limits concurrent Minimax API calls to stay within rate limit

## handler.py 两档路由（2026-06-25 重构）

> ⚠️ 已过时（2026-06-30）：FSM/两档路由已拆除，改为单路径 LLM 多轮对话；以 handler.py 顶部 docstring 为准。

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

> ⚠️ 已过时（2026-06-30）：`car_state.py` / `car_booking_fsm.py` / `return_fsm.py` / `intent_router.py` / `fast_path.py` / `card_action_handler.py` 均已删除。以下为历史参考。

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
- Do not add new tools to car_tools without registering them in `ocl/permission.py::ROLE_TOOLS`
