# ocl/

Output Control Layer (Phase 3 + 3.5). Intercepts LLM responses and per-user tool calls
before they reach the user. Single entry point: `pipeline.apply(response, user_id)`.

## Files

- `pipeline.py`      — orchestrates all checks; `apply(response, user_id, captured=None) → OclResult`
- `format_control.py` — strip whitespace, collapse blank lines, detect empty responses
- `content_filter.py` — keyword/regex hard-blocks (political, key leaks) + warn-only patterns
- `identity.py`       — open_id → {email, name, role} map; JSON persistence; `set_role` for admins
- `permission.py`     — role-based tool ACL (`TOOL_MIN_ROLE`); `is_tool_permitted(open_id, tool)`
- `tool_guard.py`     — thread-local user + email context; `guarded()` wraps handlers (Layer 2 fallback)
- `length_limiter.py` — truncate at `OCL_MAX_OUTPUT_CHARS`, preserve sentence boundary
- `session_map.py`    — session_id → user_id mapping for hermes plugin (Layer 1) lookup
- `tool_capture.py`   — (Plan B) per-session capture of this turn's raw tool results
- `markdown_to_lark.py` — (Plan B) markdown → Feishu lark_md text
- `card_builder.py`   — (Plan B) build interactive card (summary + data block + buttons)

## Permission / role model

Roles come from `data/identity_map.json` (open_id → {email, name, role}); `role_of`
returns 0 for non-platform users. OCL gates each tool by a minimum role:

| Min role | Tools |
|----------|-------|
| 1 普通用户 | list_architectures, list_available_benches, reserve_bench, cancel_reservation, return_bench, list_my_reservations |
| 2 调度员   | approve_reservation, list_my_approvals |
| 3 管理员   | (all of the above; no group restriction at the API layer) |

OCL is the **coarse** gate (can this role call this tool). Fine-grained rules
(same-group, reservation status, time validity) are enforced by the mock API via
`emailAddress`. The two gates are independent.

## Identity & role assignment

`data/identity_map.json` is gitignored (see `.example`). Admins assign roles in Feishu
with `设置角色 <open_id> <1|2|3>` → `identity.set_role`. No self-service application flow.
`emailAddress` for every API call is injected from the current user's open_id via
`tool_guard.set_current_email`; it is never an LLM-facing tool argument.

## Tool boundary wiring (double defense)

**Layer 1 — pre_tool_call plugin (primary):**  
`hermes_plugins/feishu_acl/` registers a `pre_tool_call` hook with hermes. On each
tool call, the hook receives `session_id` → `session_map.lookup()` → `user_id` →
`permission.is_tool_permitted()`. Returns `{"action":"block","message":"..."}`
to hard-block the tool inside hermes. Fails open (returns None) on any error.
The same plugin also registers a `post_tool_call` hook (Plan B) that records each
raw tool result into `tool_capture` for deterministic card rendering.

**Layer 2 — guarded() wrapper (fallback):**  
`mock_tools/register.py` wraps every handler with `tool_guard.guarded(name, handler)`.
`bot/handler.py` calls `set_current_user(user_id)` + `set_current_email(email)` before
`agent.chat()` and clears them in `finally`. Handlers check permission via thread-local
inside `guarded()` and read the injected email.

**Layer 1 blocks before Layer 2 runs.** Layer 2 activates only when Layer 1 fails
(plugin not loaded, session_map miss, permission check exception).

## Invariants

- `apply()` must return in < 100ms — no network calls, no LLM calls, no disk I/O in pipeline
  (card_builder is pure CPU, satisfies this)
- `apply()` never raises — exceptions are caught and logged; fail-open (pass through)
- Never log response content — only `user_id`, `block_reason` (string key), and lengths
- Thread-safe: `tool_guard` uses `threading.local`; `tool_capture`/`session_map`/`identity` use locks

## What NOT to do

- Do not add ML classifiers or external API calls to content_filter.py
- Do not add role persistence beyond the JSON files (no DB, no Redis)
- Do not summarise truncated responses — truncate + note only
- Do not add permission enforcement to pipeline.py — it happens at tool invocation time
- Do not let `emailAddress` become an LLM-facing tool argument — always inject it
