# bot/

hermes-agent integration layer. Owns `AIAgent` lifecycle and the bridge from Feishu events to LLM responses.

## Files

- `handler.py` — consumes from the event queue; calls `agent_pool`, runs `AIAgent.chat()` in thread pool, calls `sender`
- `agent_pool.py` — LRU pool of `AIAgent` instances keyed by `user_id`; max 100 entries; thread-safe. Generates stable `session_id = f"feishu_{user_id}"`, registers/evicts `ocl.session_map` mappings for the feishu_acl plugin.

## AIAgent rules

- One `AIAgent` instance per `user_id` — never share across users (hermes-agent session state is per-instance)
- Always create with `quiet_mode=True`, `max_iterations=30` — these are not configurable at runtime
- Pool eviction: LRU, evict oldest when `len > max_size`; evicted instances are discarded (hermes-agent persists to `~/.hermes/state.db` automatically)
- Thread pool: `max_workers=5` — limits concurrent Minimax API calls to stay within rate limit

## handler.py flow

```
event → extract (user_id, chat_id, text) → validate input
      → intent check (permission request / admin command? → bypass agent)
      → pool.get_or_create(user_id)  # agent_pool registers session_map
      → set_current_user(user_id)    # Layer 2 fallback context
      → executor.submit(agent.chat, text).result(timeout=120)
      → ocl.pipeline.apply(response, user_id)  # format → content → length
      → sender.send(chat_id, response)
```

Permisson requests and admin commands are keyword-matched (regex, no LLM) for reliability.
Empty or whitespace-only text → reply with a static prompt string, skip LLM.

## What NOT to do

- Do not catch `TimeoutError` silently — reply with the standard timeout message and log the event
- Do not hold the pool lock while calling `AIAgent.chat()` — acquire lock only to get/create the instance
- Do not retry `AIAgent.chat()` on failure — hermes-agent has internal retry; double-retry amplifies Minimax costs
- Do not put business logic here — `handler.py` is glue only; OCL layers come in Phase 3
