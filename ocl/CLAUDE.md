# ocl/

Output Control Layer (Phase 3 + 3.5). Intercepts LLM responses and per-user tool calls
before they reach the user. Single entry point: `pipeline.apply(response, user_id)`.

## Files

- `pipeline.py`      — orchestrates all checks; `apply(response, user_id) → OclResult`
- `format_control.py` — strip whitespace, collapse blank lines, detect empty responses
- `content_filter.py` — keyword/regex hard-blocks (political, key leaks) + warn-only patterns
- `permission.py`     — read/write/report group management; JSON-file persistence; approval flow
- `tool_guard.py`     — thread-local user context; `guarded()` wraps tool handlers (Layer 2 fallback)
- `length_limiter.py` — truncate at `OCL_MAX_OUTPUT_CHARS`, preserve sentence boundary
- `session_map.py`    — session_id → user_id mapping for hermes plugin (Layer 1) lookup

## Permission model

Three groups — every user starts with `read` only:

| Group   | Tools                                              | How to get        |
|---------|----------------------------------------------------|-------------------|
| read    | list_users, get_user, list_orders, get_order       | default           |
| write   | create_user, create_order, pay_order, ship_order   | apply in Feishu   |
| report  | create_report_job, get_report_status, get_report_data | apply in Feishu |

Permission data lives in `data/permissions.json` and `data/pending_requests.json`.
Changes are effective immediately (no restart needed). Files are excluded from git.

## Feishu approval flow

Users send: "申请写入权限" or "申请报表权限" → admin notified → admin replies
"批准 <open_id> write" or "拒绝 <open_id>" → applicant notified.
Admin open_ids come from `OCL_ADMIN_USER_IDS` env var (comma-separated).

## Tool boundary wiring (double defense)

**Layer 1 — pre_tool_call plugin (primary):**  
`hermes_plugins/feishu_acl/` registers a `pre_tool_call` hook with hermes. On each
tool call, the hook receives `session_id` → `session_map.lookup()` → `user_id` →
`permission.is_tool_permitted()`. Returns `{"action":"block","message":"..."}`
to hard-block the tool inside hermes. Fails open (returns None) on any error.

**Layer 2 — guarded() wrapper (fallback):**  
`mock_tools/register.py` wraps every handler with `tool_guard.guarded(name, handler)`.
`bot/handler.py` calls `set_current_user(user_id)` before `agent.chat()` and clears
it in `finally`. Handler functions check permission via thread-local inside `guarded()`.

**Layer 1 blocks before Layer 2 runs.** Layer 2 activates only when Layer 1 fails
(plugin not loaded, session_map miss, permission check exception).

## Invariants

- `apply()` must return in < 100ms — no network calls, no LLM calls, no disk I/O in pipeline
- `apply()` never raises — exceptions are caught and logged; fail-open (pass through)
- Never log response content — only `user_id`, `block_reason` (string key), and lengths
- Thread-safe: `tool_guard` uses `threading.local`; other modules are stateless

## What NOT to do

- Do not add ML classifiers or external API calls to content_filter.py
- Do not add role persistence beyond the JSON files (no DB, no Redis)
- Do not summarise truncated responses — truncate + note only
- Do not add permission enforcement to pipeline.py — it happens at tool invocation time
