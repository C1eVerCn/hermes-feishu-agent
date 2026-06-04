# hermes-feishu-agent

A Feishu bot that receives messages over WebSocket and replies using hermes-agent + Minimax API. Purpose is to explore controlled LLM output (format, content boundary, permissions, tool calling) on a real channel. The mock backend is a **test-bench reservation** system (台架预约).

## Quick start

```bash
cp .env.example .env        # fill in FEISHU_APP_ID, FEISHU_APP_SECRET, MINIMAX_API_KEY
cp data/identity_map.json.example data/identity_map.json   # map open_id → email/role
pip install -e ".[dev]"
python -m uvicorn mock_api.main:app --port 9013   # start the mock test-bench API
python main.py
```

## Key commands

```bash
python main.py              # start the bot (WebSocket + health HTTP)
pytest tests/unit/          # unit tests (no network)
pytest tests/integration/   # mock_api e2e via FastAPI TestClient
curl localhost:8080/health  # check ws_connected, metrics
```

## Architecture — one sentence per layer

- `feishu/` — WebSocket receive + send only; zero business logic
- `bot/` — wraps hermes-agent `AIAgent`; one instance per user, pooled; intent detection; resolves email from identity
- `ocl/` — Output Control Layer: format, content, length, role-based tool ACL, identity map, double-defense
- `mock_tools/` — test-bench reservation tools (handlers wrapped with `guarded()` for Layer 2); emailAddress injected server-side, never LLM-facing
- `mock_api/` — Mock test-bench reservation REST API (8 endpoints, reservation FSM, role/group business rules)
- `hermes_plugins/` — hermes plugins: `feishu_acl` pre_tool_call hook (Layer 1 hard block)
- `infra/` — dedup, metrics, health; no domain knowledge
- `config/` — reads `.env`; single source of truth for all settings
- `data/` — `identity_map.json` (open_id → email/role/name; gitignored, see `.example`)
- `tests/` — unit tests mock everything; integration tests use TestClient

## Permission / role model

Roles come from `data/identity_map.json` (open_id → role): **1 普通用户 / 2 调度员 / 3 管理员**.
OCL gates tools by minimum role (coarse); the mock API enforces fine-grained rules
(same-group, status, time) by `emailAddress`. Two independent gates.

| Min role | Tools |
|----------|-------|
| 1 | list_architectures, list_available_benches, reserve_bench, cancel_reservation, return_bench, list_my_reservations |
| 2 | approve_reservation, list_my_approvals |

Admins assign roles in Feishu: `设置角色 <open_id> <1|2|3>`. There is no self-service application flow.

## Invariants that must not break

- WS callback returns immediately — never blocks (queues events instead)
- Each `user_id` maps to exactly one `AIAgent` instance (pool enforces this)
- `MINIMAX_API_KEY` never appears in logs or error messages
- `max_iterations` and `timeout=120s` are hard limits — do not raise them without discussion
- Agent pool `get_or_create()` MUST register `session_id` in `ocl.session_map` and evict on LRU
- `ocl/session_map.lookup()` returns `""` on miss — callers must fail-open (plugin returns None, guarded passes)
- Permission enforcement is double-defense: L1 pre_tool_call plugin (hard block) + L2 guarded wrapper (fallback)
- `emailAddress` is injected from the current user's open_id; it is never a tool argument and the LLM must not supply it
- Non-platform users (identity miss) cannot reach the agent — handler replies with a friendly prompt

## What NOT to do

- Do not add features beyond what a task explicitly requests
- Do not catch exceptions silently — log with context then re-raise or return a user-facing message
- Do not import `feishu` from `agent` or vice versa — they communicate only through `handler.py`
- Do not store user message content in metrics or logs — only IDs and latency
- Do not modify `~/.hermes/config.yaml` at runtime; it is read-only after startup
