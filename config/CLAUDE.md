# config/

Single source of truth for all configuration. Reads `.env` at import time, validates required fields, and exposes typed constants. Nothing else.

## Files

- `settings.py` — all env var reads happen here; every other module imports from here, never from `os.environ` directly

## Rules

- Fail fast: if a required var is missing at import time, raise `RuntimeError` with the var name and a hint — do not use a default that masks a misconfiguration
- Required vars: `FEISHU_APP_ID`, `FEISHU_APP_SECRET`, `MINIMAX_API_KEY`
- Optional vars with defaults: `MINIMAX_BASE_URL` (→ `https://api.minimax.chat/v1`), `MINIMAX_MODEL` (→ `MiniMax-Text-01`), `AGENT_MAX_ITERATIONS` (→ `30`), `AGENT_TIMEOUT_SECONDS` (→ `120`), `AGENT_POOL_MAX_SIZE` (→ `100`), `HTTP_PORT` (→ `8088`), `CAR_MCP_SERVER_NAME` (→ `car_booking`)
- MCP server 连接信息不在 settings（用户在 `~/.hermes/config.yaml::mcp_servers` 单独配置；CAR_MCP_SERVER_NAME 是该 dict 的 key）
- API keys must never appear in `repr()` or `str()` of the settings object — mask them as `***`

## What NOT to do

- Do not cache external data here (user info, token lookups) — `settings.py` is static config only
- Do not put validation logic beyond "is the var present and non-empty" — type coercion (int, bool) is fine
- Do not import from `feishu`, `agent`, or `infra` — this module has no dependencies within the project
- Do not add MCP server connection params (URL/JWT/credentials) — those belong to `~/.hermes/config.yaml`
