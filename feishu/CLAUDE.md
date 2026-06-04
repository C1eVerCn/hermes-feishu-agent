# feishu/

Feishu transport layer. Owns the WebSocket connection and message send/receive. Nothing else.

## Files

- `ws_client.py` — lark-oapi `WSClient` + supervision loop; puts events onto a `queue.Queue`. Also registers the `p2_card_action_trigger` callback (synchronous) and delegates to an injected handler via `set_card_action_handler()` — feishu/ must not import bot/, so main.py injects `bot.card_action_handler.handle`.
- `sender.py` — wraps Feishu CreateMessage API; `send`/`send_to_user` (text) and `send_card` (interactive); rate limiting (5 msg/s), chunking (4096 chars), retry
- `typing_indicator.py` — sends a placeholder message after 5s then updates it when the reply arrives

## Invariants

- Event callback must return in < 50 ms — enqueue and return, never call Agent or LLM here
- Deduplication is in `infra/dedup.py`, not here — do not add a second dedup layer
- Sender never truncates text — it chunks at word boundaries and labels chunks `[1/N]`
- Rate limiter is a token bucket at 4 msg/s (below Feishu's 5 msg/s limit for safety margin)

## Supervision loop (ws_client.py)

lark-oapi retries ~7 times internally then exits. The outer loop restarts it with exponential backoff: 2 s → 4 s → … → 60 s cap. Reset delay to 2 s on clean restart.

## What NOT to do

- Do not call `agent.handler` directly from here — publish to queue, let the consumer thread call handler
- Do not parse message content (JSON fields inside `msg.content`) beyond extracting plain text
- Do not implement retry logic in `typing_indicator.py` — if the placeholder send fails, skip silently
- Do not log the actual text content of messages — log `message_id` and `chat_id` only
- Card action callback is SYNCHRONOUS (lark expects a quick toast/card response) — the injected handler must not block on I/O; do not import `bot/` here, use `set_card_action_handler()` injection
