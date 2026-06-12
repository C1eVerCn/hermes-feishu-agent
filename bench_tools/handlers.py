"""台架预约工具 handler。

通过 httpx 直接调用真实台架预约后端（Docker容器，端口9013），无 mock 层。
emailAddress 由服务端从 thread-local注入（不暴露给 LLM）——参考 ocl.tool_guard.get_current_email()。

需要 Authorization: Bearer <JWT>，由 bench_tools.jwt_auth 提供。JwtAuthFilter
会按 JWT sub → employee.id 查 user 找员工记录。dev 默认 sub=234234（zxs admin）。
"""
import json
import logging

import httpx

from config.settings import settings
from ocl.tool_guard import get_current_email
from bench_tools.jwt_auth import auth_headers

log = logging.getLogger(__name__)

BASE = settings.BENCH_API_BASE_URL
_PREFIX = "/fmp/testBenchReservationForAgent"


# ── LLM key-name normalization (BUGFIX: LLM keeps inventing wrong key
# names like "task" / "purpose" despite repeated prompt + schema warnings).
# This table maps commonly-misused variants to the canonical schema keys.
# Applied to ALL bench handlers (dry_run + real) so the call works
# regardless of which LLM emitted the args.
_KEY_ALIASES: dict[str, str] = {
    # taskName aliases
    "task":          "taskName",
    "task_name":     "taskName",
    "taskname":      "taskName",
    "name":          "taskName",  # ambiguous but LLM uses it; bench API rejects "name" too
    # testPurpose aliases
    "purpose":       "testPurpose",
    "test_purpose":  "testPurpose",
    "testpurpose":   "testPurpose",
    # startTime / endTime snake_case aliases
    "start_time":    "startTime",
    "starttime":     "startTime",
    "end_time":      "endTime",
    "endtime":       "endTime",
    # benchNo
    "bench_no":      "benchNo",
    "benchno":       "benchNo",
    "bench":         "benchNo",
}

# Canonical schema keys for the reserve handlers. Used to tell a genuine
# JSON-wrapper payload (inner dict carries real fields) apart from a
# legitimate single field whose value just happens to be valid JSON.
_CANONICAL_KEYS: frozenset[str] = frozenset({
    "benchNo", "startTime", "endTime", "taskName", "testPurpose", "remark",
    "approvalResult", "status",
})


def _looks_like_payload(d: dict) -> bool:
    """True if `d` carries at least one recognized schema key (canonical or
    a known alias) — i.e. it is a real argument payload, not arbitrary JSON
    that happened to appear in a string field value."""
    return any((k in _CANONICAL_KEYS or k in _KEY_ALIASES) for k in d)


def _is_blank(v) -> bool:
    """True when a required field value is missing or whitespace-only.
    Type-safe: coerces non-strings (e.g. an int the LLM emitted, or a value
    produced by the JSON-unwrap path) via str() so .strip() never raises."""
    return not str(v or "").strip()


def _normalize_args(args: dict) -> tuple[dict, list[str]]:
    """Translate LLM-misused key names to schema names. Returns the
    normalized dict and a list of rewrites (for debug logging).

    Defensive depth: also unwraps single-key JSON-string payloads (e.g.
    `{"reservation": "{...all fields...}"}`) which the LLM occasionally
    emits when its function-calling is malformed.
    """
    import json as _json

    rewrites: list[str] = []

    # Pass 1: unwrap single-key JSON-string wrapping — but ONLY when the
    # parsed inner object actually looks like an argument payload. Otherwise
    # a legitimate single field whose value is brace-wrapped text (e.g.
    # {"taskName": '{"foo":1}'}) would be silently mangled into {"foo":1}.
    if (len(args) == 1
            and isinstance(next(iter(args.values())), str)):
        wrapper_key, wrapper_val = next(iter(args.items()))
        try:
            inner = _json.loads(wrapper_val)
        except (ValueError, _json.JSONDecodeError):
            inner = None
        if isinstance(inner, dict) and _looks_like_payload(inner):
            rewrites.append(f"{wrapper_key}->unwrapped")
            args = inner

    # Pass 2: rename known-alias keys to canonical schema names.
    normalized: dict = {}
    for k, v in args.items():
        canonical = _KEY_ALIASES.get(k, k)
        if canonical != k:
            rewrites.append(f"{k}->{canonical}")
        normalized[canonical] = v
    return normalized, rewrites


def _ok(resp) -> str:
    return json.dumps(resp.json(), ensure_ascii=False) if resp.is_success \
        else json.dumps({"error": f"HTTP {resp.status_code}: {resp.text[:300]}"}, ensure_ascii=False)


def _http_error_body(exc: Exception) -> str:
    """Render a connection-level failure as a JSON error body (so the
    caller can json.loads it the same way as a successful response)."""
    return json.dumps(
        {"error": f"bench backend unavailable: {type(exc).__name__}: {exc}"},
        ensure_ascii=False,
    )


def list_architectures(args: dict, **_) -> str:
 try:
  r = httpx.get(f"{BASE}{_PREFIX}/architectures", headers=auth_headers(), timeout=10)
 except httpx.HTTPError as e:
  log.warning("list_architectures connect_failed err=%s", e)
  return _http_error_body(e)
 return _ok(r)


def list_available_benches(args: dict, **_) -> str:
 body = {"emailAddress": get_current_email()}
 if args.get("architecture"):
  body["architecture"] = args["architecture"]
 if args.get("needParkingTest") is not None:
  body["needParkingTest"] = args["needParkingTest"]
 try:
  r = httpx.post(f"{BASE}{_PREFIX}/availableTestBenches", headers=auth_headers(), json=body, timeout=10)
 except httpx.HTTPError as e:
  log.warning("list_available_benches connect_failed err=%s", e)
  return _http_error_body(e)
 return _ok(r)


def dry_run_reserve_bench(args: dict, **_) -> str:
 """Always-dry variant exposed to the LLM. BUGFIX (#10): the destructive
 `reserve_bench` is no longer in the LLM-facing toolset at all — the LLM
 can only call this dry variant. The real call happens only from
 `bot.card_action_handler` after the user clicks "确认" on the confirm
 card.

 NEW behaviour: if required fields are missing, return a payload with
 `missing_fields` so card_builder can render a "please supply X" prompt
 card instead of the confirm card. The LLM should then ask the user
 for those fields and re-call this tool.
 """
 REQUIRED = ("benchNo", "startTime", "endTime", "taskName", "testPurpose")
 normalized, rewrites = _normalize_args(args)
 if rewrites:
     log.warning("dry_run_reserve_bench key_normalization rewrites=%s", rewrites)
 body = {"emailAddress": get_current_email(), **normalized}

 # Detect missing required fields. The LLM is responsible for asking
 # the user for them; we surface a structured signal in the payload
 # so the card_builder can render a "missing-field" card.
 missing = [k for k in REQUIRED if _is_blank(body.get(k))]
 if missing:
     # Mirror the 车辆预约 reference (image 23): list the missing fields
     # in **Chinese** for the user, then provide a fully-filled example
     # sentence the user can copy verbatim. Schema names stay in the
     # payload for the LLM's benefit, but the user-facing card uses
     # Chinese labels (per user requirement "不要用schema名就用中文翻译").
     FIELD_LABELS_CN = {
         "benchNo":    "台架编号",
         "startTime":   "开始时间",
         "endTime":     "结束时间",
         "taskName":    "任务名称",
         "testPurpose": "测试目的",
     }
     FIELD_EXAMPLES = {
         "benchNo":    "<台架编号,如CT001>",
         "startTime":  "<开始时间,如2026-06-12 17:00:00>",
         "endTime":    "<结束时间,如2026-06-12 20:00:00>",
         "taskName":   "测试",
         "testPurpose": "感知压测",
     }
     missing_cn = [FIELD_LABELS_CN[k] for k in missing]
     already = {k: v for k, v in normalized.items() if v and k not in missing}
     ex_bench, ex_start, ex_end, ex_task, ex_purp = (
         already.get(k) or FIELD_EXAMPLES[k] for k in REQUIRED
     )
     summary = (
         f"我还缺少以下信息：{', '.join(missing_cn)}\n"
         f"请补充后重新发送,例如:\"预约{ex_bench}，从{ex_start}到{ex_end}，"
         f"任务是{ex_task}，目的是{ex_purp}\""
     )
     return json.dumps({
         "dry_run": True,
         "missing_fields": missing,
         "summary": summary,
         "args": normalized,
         "already_filled": already,
     }, ensure_ascii=False)

 # Build summary, omitting fields that are blank so the user only sees
 # what the LLM actually filled in. Bench API requires both taskName
 # and testPurpose, but we don't surface "任务：" with nothing after it.
 parts = [
     f"台架编号：{body.get('benchNo','')}",
     f"开始：{body.get('startTime','')}",
     f"结束：{body.get('endTime','')}",
 ]
 if body.get("taskName"):
     parts.append(f"任务：{body['taskName']}")
 if body.get("testPurpose"):
     parts.append(f"目的：{body['testPurpose']}")
 if body.get("remark"):
     parts.append(f"备注：{body['remark']}")
 payload = {
     "dry_run": True,
     "summary": "\n".join(parts),
     "args": normalized,
 }
 return json.dumps(payload, ensure_ascii=False)


def reserve_bench(args: dict, **_) -> str:
 """REAL reservation — NOT exposed to the LLM. Only callable from
 bot.card_action_handler.confirm_reserve after the user clicks 确认.

 Adding it to the LLM-facing toolset would allow prompt-injection /
 model-regression attacks to bypass the confirm card. See BUGFIX #10.
 """
 normalized, rewrites = _normalize_args(args)
 if rewrites:
     log.warning("reserve_bench key_normalization rewrites=%s", rewrites)
 body = {"emailAddress": get_current_email(), **normalized}
 # Required-field guard runs only on the REAL call path.
 # The LLM sometimes invents wrong key names (e.g. "task" instead of
 # "taskName"); catch them here with a clear error rather than a generic
 # 400 from the API.
 missing = [k for k in ("benchNo", "startTime", "endTime", "taskName", "testPurpose")
            if _is_blank(body.get(k))]
 if missing:
     err = {"error": f"reserve_bench 缺少必填字段: {missing}。请按 schema 字段名（benchNo/startTime/endTime/taskName/testPurpose）调用。", "received_args": list(args.keys())}
     return json.dumps(err, ensure_ascii=False)

 real_body = {k: v for k, v in body.items() if k != "dry_run"}
 try:
  r = httpx.post(f"{BASE}{_PREFIX}/reserveTestBench", headers=auth_headers(), json=real_body, timeout=10)
 except httpx.HTTPError as e:
  log.warning("reserve_bench connect_failed err=%s", e)
  return _http_error_body(e)
 return _ok(r)


def cancel_reservation(args: dict, **_) -> str:
 body = {"emailAddress": get_current_email(), **args}
 try:
  r = httpx.post(f"{BASE}{_PREFIX}/cancel", headers=auth_headers(), json=body, timeout=10)
 except httpx.HTTPError as e:
  log.warning("cancel_reservation connect_failed err=%s", e)
  return _http_error_body(e)
 return _ok(r)


def approve_reservation(args: dict, **_) -> str:
 body = {"emailAddress": get_current_email(), **args}
 try:
  r = httpx.post(f"{BASE}{_PREFIX}/approve", headers=auth_headers(), json=body, timeout=10)
 except httpx.HTTPError as e:
  log.warning("approve_reservation connect_failed err=%s", e)
  return _http_error_body(e)
 return _ok(r)


def list_my_reservations(args: dict, **_) -> str:
 # `status` is filtered CLIENT-SIDE: we never forward it to the backend,
 # so the call is safe regardless of whether the bench API accepts a scalar
 # int, an array, or no status param at all. The schema/prompt asks the LLM
 # for a list (default [0,1,4]) to hide cancelled/rejected records.
 args = dict(args)
 status_filter = args.pop("status", None)
 body = {"emailAddress": get_current_email(), **args}
 try:
  r = httpx.post(f"{BASE}{_PREFIX}/myReservations", headers=auth_headers(), json=body, timeout=10)
 except httpx.HTTPError as e:
  log.warning("list_my_reservations connect_failed err=%s", e)
  return _http_error_body(e)
 out = _ok(r)
 if status_filter is None:
  return out
 # Normalize the requested filter to a set of ints, then drop records whose
 # status isn't wanted. Tolerate single-int, list, or stringified values.
 wanted: set = set()
 for s in (status_filter if isinstance(status_filter, (list, tuple, set)) else [status_filter]):
  try:
   wanted.add(int(s))
  except (TypeError, ValueError):
   continue
 if not wanted:
  return out
 try:
  parsed = json.loads(out)
  data = parsed.get("data")
  if isinstance(data, list):
   parsed["data"] = [d for d in data
                     if isinstance(d, dict) and d.get("status") in wanted]
   return json.dumps(parsed, ensure_ascii=False)
 except (ValueError, AttributeError):
  pass
 return out


def list_my_approvals(args: dict, **_) -> str:
 body = {"emailAddress": get_current_email(), **args}
 try:
  r = httpx.post(f"{BASE}{_PREFIX}/myApprovals", headers=auth_headers(), json=body, timeout=10)
 except httpx.HTTPError as e:
  log.warning("list_my_approvals connect_failed err=%s", e)
  return _http_error_body(e)
 return _ok(r)


def return_bench(args: dict, **_) -> str:
 body = {"emailAddress": get_current_email(), **args}
 try:
  r = httpx.post(f"{BASE}{_PREFIX}/returnTestBench", headers=auth_headers(), json=body, timeout=10)
 except httpx.HTTPError as e:
  log.warning("return_bench connect_failed err=%s", e)
  return _http_error_body(e)
 return _ok(r)

