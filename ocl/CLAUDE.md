# ocl/

Output Control Layer (Phase 3 + 3.5). Intercepts LLM responses and per-user tool calls
before they reach the user. Single entry point: `pipeline.apply(response, user_id)`.

## Files

- `pipeline.py`      — orchestrates all checks; `apply(response, user_id, captured=None) → OclResult`
- `format_control.py` — strip whitespace, collapse blank lines, detect empty responses
- `content_filter.py` — keyword/regex hard-blocks (political, key leaks) + warn-only patterns
- `identity.py`       — open_id → {email, name, role} map; JSON persistence; `set_role` for admins; `build_caller_identity(openid) → CallerIdentity`
- `permission.py`     — role-based tool ACL (`ROLE_TOOLS` / `role_allows` / `is_tool_permitted`)（车辆预约域；角色与 fmp sys_role 对齐 1~5）
- `tool_guard.py`     — contextvars-based CallerIdentity 注入；`guarded()` wraps handlers (Layer 2 fallback)
- `length_limiter.py` — truncate at `OCL_MAX_OUTPUT_CHARS`, preserve sentence boundary
- `session_map.py`    — session_id → user_id mapping for hermes plugin (Layer 1) lookup
- `tool_capture.py`   — (Plan B) per-session capture of this turn's raw tool results
- `markdown_to_lark.py` — (Plan B) markdown → Feishu lark_md text
- `card_builder.py`   — (Plan B) build interactive card (summary + data block + buttons)

## Permission / role model

Roles come from `data/identity_map.json` (open_id → {email, name, role}); `role_of`
returns 0 for non-platform users. Roles align with fmp backend RBAC (`sys_role`).
OCL gates each tool by an explicit per-role tool set (`ROLE_TOOLS`), **not** a linear
`role>=min_role` — fmp's 5 roles are non-linear (driver(4) has fewer perms than
engineer(1); group_manager(5) ≈ dispatcher, not admin):

| Role | Tools (== car_tools/register.py registered names) |
|------|-------|
| 1 工程师 | fetch_available_vehicles, _dry_run_vehicle_reservation, _commit_vehicle_reservation, cancel_vehicle_reservation, return_vehicle, fetch_user_reservation, get_user_context, get_common_dictionary |
| 2 调度员 / 5 组管理员 | 工程师全部 + approval_vehicle_reservation, fetch_user_approval |
| 3 管理员   | （全部） |
| 4 司机     | get_user_context, get_common_dictionary（仅助手；fmp 司机无约车菜单） |

OCL is the **coarse** gate (can this role call this tool). Fine-grained rules
(same-group, reservation status, time validity) are enforced by the MCP server via
`openid + emailAddress`. The two gates are independent.

## Identity & role assignment

`data/identity_map.json` is gitignored (see `.example`). Admins assign roles in Feishu
with `设置角色 <open_id> <1-5>` → `identity.set_role` (whitelist `ALLOWED_ROLES`).
fmp custom group-roles (e.g. sys_role.id=100) collapse to role 5 here. No self-service flow.
`emailAddress` for every MCP call is injected from the current user's open_id via
`tool_guard.set_current_caller(CallerIdentity(...))`; it is never an LLM-facing tool argument.

**CallerIdentity**（2026-06-16 业务域合并新增）：
- 单个 contextvars 变量（替代旧的 set_current_user + set_current_email 两个）
- 字段：openid / email / mobile（mobile 当前 stub 为 None，2026 Q3 接入）
- `as_dict()` 输出 MCP 入参（camelCase）：`{"openId":..., "emailAddress":..., "mobile":...}`
- 旧 API（set_current_user / set_current_email）保留为 alias（不破坏现有调用方）

## Tool boundary wiring (double defense)

**Layer 1 — pre_tool_call plugin (primary):**  
`hermes_plugins/feishu_acl/` registers a `pre_tool_call` hook with hermes. On each
tool call, the hook receives `session_id` → `session_map.lookup()` → `user_id` →
`permission.is_tool_permitted()`. Returns `{"action":"block","message":"..."}`
to hard-block the tool inside hermes. Fails open (returns None) on any error.
The same plugin also registers a `post_tool_call` hook (Plan B) that records each
raw tool result into `tool_capture` for deterministic card rendering.

**Layer 2 — guarded() wrapper (fallback):**  
`car_tools/register.py` wraps every handler with `tool_guard.guarded(name, handler)`.
`bot/handler.py` calls `set_current_caller(CallerIdentity(openid, email))` before
`agent.chat()` and clears them in `finally`. Handlers check permission via contextvar
inside `guarded()` and read the injected email via `get_current_caller()`.

**Layer 1 blocks before Layer 2 runs.** Layer 2 activates only when Layer 1 fails
(plugin not loaded, session_map miss, permission check exception).

## Invariants

- `apply()` must return in < 100ms — no network calls, no LLM calls, no disk I/O in pipeline
  (card_builder is pure CPU, satisfies this)
- `apply()` never raises — exceptions are caught and logged; fail-open (pass through)
- Never log response content — only `user_id`, `block_reason` (string key), and lengths
- Thread-safe: `tool_guard` uses `contextvars.ContextVar`; `tool_capture`/`session_map`/`identity` use locks

## What NOT to do

- Do not add ML classifiers or external API calls to content_filter.py
- Do not add role persistence beyond the JSON files (no DB, no Redis)
- Do not summarise truncated responses — truncate + note only
- Do not add permission enforcement to pipeline.py — it happens at tool invocation time
- Do not let `emailAddress` / `openId` / `mobile` become LLM-facing tool arguments — always inject via CallerIdentity
