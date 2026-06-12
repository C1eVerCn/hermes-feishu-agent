"""Feishu proactive notification helpers.

Sends outbound Feishu DMs (text only) for two flows:
- After successful reserve_bench, fan out to every dispatcher in the bench's
  group (best-effort) so they can approve in a timely manner.
- After a dispatcher approves a reservation, notify the original applicant.

DESIGN (post code-review fixes #2/#3/#6):
- Transport is delegated to `feishu.sender` (chunking, 429 retry, single
  rate-limiter). This module owns ONLY the orchestration: email→open_id
  resolution, dispatcher fan-out, and async dispatch.
- All sends are submitted to a background ThreadPoolExecutor and return
  immediately. This is critical because notify is called from the WS
  card-callback path which has a < 50ms return budget (feishu/CLAUDE.md).
- `notify_dispatchers_by_email` returns a Future so callers can wait for
  completion (e.g. tests) or fire-and-forget (production).
"""
import logging
import re
import threading
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Iterable

from feishu import sender
from ocl import identity

log = logging.getLogger(__name__)

_EXECUTOR = ThreadPoolExecutor(
    max_workers=2, thread_name_prefix="feishu-notify")
# NOTE: this is intentionally a SEPARATE pool from `bot.handler._executor`.
# The handler pool is sized for blocking LLM calls (max_workers=5); mixing
# fire-and-forget notification fan-out into it would let notification
# bursts starve the LLM hot path. Keep the pools independent.

# ── Email → open_id resolution (cached, threaded) ─────────────────────────
# Falls back to ocl.identity's own cache when possible; here we add a
# *process-local* short-circuit so we never re-hit Contact API for an email
# we've already resolved (or already known to fail).

_OPEN_ID_CACHE: dict[str, str] = {}  # email → open_id
_OPEN_ID_NEG: set[str] = set()       # emails known to be unresolvable
_IN_FLIGHT: set[str] = set()         # emails with a Contact API call in progress (per-email dedup)
_OPEN_ID_LOCK = threading.Lock()


def remember_open_id(open_id: str, email: str = "") -> None:
    """Seed the email→open_id cache. Called by handler.py whenever a user
    sends the bot a message (Feishu delivers open_id in the event; if we
    also resolved their email via Contact API at some point, we have the
    pair and never need to re-query).

    BUGFIX (2026-06-10): Feishu v3 contact API no longer supports
    user_id_type=email. Email→open_id lookup is effectively impossible
    via the official SDK. The only reliable path is to seed the cache
    from message events.
    """
    if not open_id:
        return
    with _OPEN_ID_LOCK:
        # Skip the email→open_id leg when email is empty: an earlier user
        # would have written the same "" key, and the second call's
        # `_OPEN_ID_CACHE.get("", open_id)` returns the stale value,
        # silently overwriting the new open_id. Reverse lookup (open_id→open_id)
        # is the only leg we want in that case.
        if email:
            _OPEN_ID_CACHE[email] = open_id
        # also key by open_id for reverse lookup
        _OPEN_ID_CACHE.setdefault(open_id, open_id)


def _email_to_open_id(email: str) -> str:
    """Resolve email → open_id. Cache; tolerate misses with negative cache.

    Lookup order:
      1. ocl.identity.open_id_of(email) — local index built from
         identity_map.json v2 schema. Bypasses Feishu v3's broken
         email→open_id lookup (99992402). Works for all admin-assigned
         dispatchers (which carry an `email` field in the v2 schema).
      2. _OPEN_ID_CACHE (in-process, populated by remember_open_id when
         the dispatcher sends the bot a message).
      3. Feishu BatchGetIdUser API as a last-resort fallback (often fails
         on v3 but kept for users outside the local identity_map).
    """
    if not email:
        return ""
    # 1) Local identity index — built from identity_map.json v2 schema.
    #    No Feishu API call, no quota burn.
    from ocl import identity as _identity
    oid = _identity.open_id_of(email)
    if oid:
        with _OPEN_ID_LOCK:
            _OPEN_ID_CACHE[email] = oid
        return oid
    with _OPEN_ID_LOCK:
        if email in _OPEN_ID_CACHE:
            log.debug("email_to_open_id HIT email=%s oid=%s", email, _OPEN_ID_CACHE[email])
            return _OPEN_ID_CACHE[email]
        if email in _OPEN_ID_NEG:
            log.debug("email_to_open_id NEG hit email=%s", email)
            return ""
        if email in _IN_FLIGHT:
            log.debug("email_to_open_id IN-FLIGHT dedup email=%s", email)
            return ""
        _IN_FLIGHT.add(email)
    # Use the dedicated batch_get_id endpoint which accepts emails.
    # (The single-user GetUser endpoint rejects user_id_type=email with
    # 99992402 "field validation failed" — see lark SDK quirk.)
    try:
        from lark_oapi.api.contact.v3 import (
            BatchGetIdUserRequest, BatchGetIdUserRequestBody,
        )
        client = sender._client  # BUGFIX: was `sender._client()` — but `_client`
        # is already a constructed Client instance, not a callable.
        body = (BatchGetIdUserRequestBody.builder()
                .emails([email])
                .include_resigned(True)
                .build())
        req = (BatchGetIdUserRequest.builder()
                .user_id_type("email")
                .request_body(body)
                .build())
        resp = client.contact.v3.user.batch_get_id(req)
        if resp.success() and resp.data and resp.data.user_list:
            for entry in resp.data.user_list:
                if getattr(entry, "email", "") == email:
                    oid = getattr(entry, "open_id", "") or ""
                    if oid:
                        with _OPEN_ID_LOCK:
                            _OPEN_ID_CACHE[email] = oid
                        log.debug("email_to_open_id RESOLVED email=%s oid=%s", email, oid)
                        return oid
        log.debug("email_to_open_id MISS email=%s code=%s msg=%s", email, resp.code, resp.msg)
    except Exception as e:
        log.debug("email_to_open_id EXCEPTION email=%s type=%s msg=%s", email, type(e).__name__, e)
        log.exception("email_to_open_id_failed email=%s", email)
    finally:
        with _OPEN_ID_LOCK:
            _IN_FLIGHT.discard(email)

    with _OPEN_ID_LOCK:
        _OPEN_ID_NEG.add(email)
    return ""


# ── Public API ─────────────────────────────────────────────────────────────

def send_text_to_user(open_id: str, text: str) -> bool:
    """Send a plain text DM to one user. Returns True on success.

    Delegates to sender.send_to_user (chunking + 429 retry + shared
    rate limiter). Synchronous because the underlying call already
    rate-limits via the shared token bucket; the WS callback is safe to
    call this only when a single notification is needed. For fan-out
    use `submit_dispatchers_by_email` instead.
    """
    if not open_id:
        return False
    return sender.send_to_user(open_id, text)


def submit_text_to_user(open_id: str, text: str) -> Future:
    """Fire-and-forget single DM on the background notify pool. Use this
    from the WS card-action callback (which has a <50ms return budget) so
    the blocking send (rate-limit sleep + network round-trip + possible 429
    retry) never stalls the lark callback. Returns a Future[bool]."""
    return _EXECUTOR.submit(send_text_to_user, open_id, text)


def submit_dispatchers_by_email(emails: Iterable[str], subject: str, body: str) -> Future:
    """Fan-out dispatcher notifications on a background thread.

    Returns a Future[int] (count of successful sends). The caller can
    `result()` to wait (tests) or ignore (production).
    """
    return _EXECUTOR.submit(_notify_dispatchers_sync, list(emails or []), subject, body)


def _notify_dispatchers_sync(emails: list[str], subject: str, body: str) -> int:
    """Best-effort: DM each unique dispatcher email. Runs in a worker
    thread so the WS callback is never blocked.
    """
    n_ok = 0
    n_attempted = 0
    n_no_openid = 0
    text = f"{subject}\n\n{body}"
    log.debug("_notify_dispatchers_sync emails=%s", emails)
    seen: set[str] = set()
    for raw in emails:
        email = (raw or "").strip()
        if not email or email in seen:
            continue
        seen.add(email)
        n_attempted += 1
        oid = _email_to_open_id(email)
        if not oid:
            log.info("notify_dispatcher_skip email=%s (no open_id)", email)
            n_no_openid += 1
            continue
        if sender.send_to_user(oid, text):
            n_ok += 1
            log.debug("notify_dispatcher_sent email=%s oid=%s", email, oid)
        else:
            log.warning("notify_dispatcher_send_failed email=%s oid=%s", email, oid)
    log.debug("_notify_dispatchers_sync DONE attempted=%d ok=%d no_openid=%d",
              n_attempted, n_ok, n_no_openid)
    log.info("notify_dispatchers_done attempted=%d ok=%d no_openid=%d",
             n_attempted, n_ok, n_no_openid)
    return n_ok


# Backwards-compatible sync wrapper (for tests that want to assert count).
def notify_dispatchers_by_email(emails: Iterable[str], subject: str, body: str) -> int:
    """Synchronous fan-out. Returns count of successful sends.

    NOTE: this blocks on the underlying rate limiter. For production
    paths, prefer `submit_dispatchers_by_email` which runs in a worker
    thread.
    """
    return _notify_dispatchers_sync(list(emails or []), subject, body)


def submit_dispatchers_by_email_blocking(emails: Iterable[str], subject: str, body: str, timeout: float = 30.0) -> int:
    """Submit the fan-out on a worker thread and block until it finishes
    (or timeout). Returns the number of successful sends so the caller
    can present an accurate "注: 调度员通知成功/失败" message to the
    applicant. Safe to call from non-callback contexts (e.g. the
    confirm-text path in bot/handler.py)."""
    future = submit_dispatchers_by_email(list(emails or []), subject, body)
    try:
        return future.result(timeout=timeout)
    except Exception:
        log.exception("submit_dispatchers_by_email_blocking failed")
        return 0


# ── Email extraction (moved from card_action_handler) ──────────────────────
# Parses "姓名：张三，邮箱：zhangsan@x.com" out of the bench API's success
# message: '预约成功！调度员信息：\\n姓名：A，邮箱：a@x\\n姓名：B，邮箱：b@y'
_EMAIL_RE = re.compile(r"邮箱[：:]\s*([\w.+-]+@[\w.-]+)")


def extract_scheduler_emails(api_message: str) -> list[str]:
    """Return de-duplicated list of emails in the bench API success message,
    preserving order. Empty list if no emails found."""
    out: list[str] = []
    seen: set[str] = set()
    for m in _EMAIL_RE.finditer(api_message or ""):
        e = m.group(1).strip()
        if e and e not in seen:
            seen.add(e)
            out.append(e)
    return out
