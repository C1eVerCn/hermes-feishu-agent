# hermes-feishu-agent

A Feishu bot that receives messages over WebSocket and replies using hermes-agent + Minimax API. Purpose is to explore controlled LLM output (format, content boundary, permissions, tool calling) on a real channel.

## Quick start

```bash
cp .env.example .env        # fill in FEISHU_APP_ID, FEISHU_APP_SECRET, MINIMAX_API_KEY
pip install -e ".[dev]"
python main.py
```

## Key commands

```bash
python main.py              # start the bot (WebSocket + health HTTP on :8080)
pytest tests/unit/          # unit tests (no network)
pytest tests/integration/   # needs .env + running mock_api
curl localhost:8080/health  # check ws_connected, metrics
```

## Architecture — one sentence per layer

- `feishu/` — WebSocket receive + send only; zero business logic
- `bot/` — wraps hermes-agent `AIAgent`; one instance per user, pooled; intent detection
- `ocl/` — Output Control Layer: format, content, length, permissions, double-defense tool ACL
- `mock_tools/` — Mock API tool registration (handlers wrapped with `guarded()` for Layer 2)
- `mock_api/` — Mock enterprise REST API (users, orders, reports with state machine)
- `hermes_plugins/` — hermes plugins: `feishu_acl` pre_tool_call hook (Layer 1 hard block)
- `infra/` — dedup, metrics, health; no domain knowledge
- `config/` — reads `.env`; single source of truth for all settings
- `data/` — permissions.json + pending_requests.json (gitignored)
- `tests/` — unit tests mock everything; integration tests need live env vars

## Invariants that must not break

- WS callback returns immediately — never blocks (queues events instead)
- Each `user_id` maps to exactly one `AIAgent` instance (pool enforces this)
- `MINIMAX_API_KEY` never appears in logs or error messages
- `max_iterations=30` and `timeout=120s` are hard limits — do not raise them without discussion
- Agent pool `get_or_create()` MUST register `session_id` in `ocl.session_map` and evict on LRU
- `ocl/session_map.lookup()` returns `""` on miss — callers must fail-open (plugin returns None, guarded passes)
- Permission enforcement is double-defense: L1 pre_tool_call plugin (hard block) + L2 guarded wrapper (fallback)

## What NOT to do

- Do not add features beyond what a task explicitly requests
- Do not catch exceptions silently — log with context then re-raise or return a user-facing message
- Do not import `feishu` from `agent` or vice versa — they communicate only through `handler.py`
- Do not store user message content in metrics or logs — only IDs and latency
- Do not modify `~/.hermes/config.yaml` at runtime; it is read-only after startup
