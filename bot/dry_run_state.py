"""Per-user pending dry_run_reserve_bench state.

When the LLM calls `dry_run_reserve_bench`, the user is shown a confirm
card. While the user is deliberating, the bench args (benchNo,
startTime, endTime, taskName, testPurpose) live in this in-memory store
keyed by user_id. When the user replies with "确认", the handler looks
up the state, calls the real reserve_bench, and clears the entry.

The entry expires after a few minutes so a forgotten confirm doesn't
hang around forever and cause an accidental reservation on a later
intent.
"""
import logging
import threading
import time
from typing import Optional

log = logging.getLogger(__name__)

_TTL_SECONDS = 600  # 10 minutes
_state: dict[str, dict] = {}  # user_id → {"args": {...}, "expires_at": float}
_lock = threading.Lock()


def save(user_id: str, args: dict) -> None:
    """Record a pending dry_run for the user. Overwrites any prior entry."""
    with _lock:
        _state[user_id] = {
            "args": args,
            "expires_at": time.monotonic() + _TTL_SECONDS,
        }
        log.info("dry_run_state_saved user=%s bench=%s",
                 user_id, args.get("benchNo"))


def get(user_id: str) -> Optional[dict]:
    """Return the pending dry_run args if not expired, else None. Clears
    expired entries as a side effect."""
    with _lock:
        entry = _state.get(user_id)
        if not entry:
            return None
        if entry["expires_at"] < time.monotonic():
            del _state[user_id]
            log.info("dry_run_state_expired user=%s", user_id)
            return None
        return entry["args"]


def clear(user_id: str) -> None:
    """Remove the entry (used after a successful real reserve or after cancel)."""
    with _lock:
        _state.pop(user_id, None)
