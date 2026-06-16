"""每个用户挂起dry_run_reserve_bench状态。

当LLM调用“dry_run_reserve_bench”时，用户会看到一个确认消息
卡片。当用户正在考虑时，
startTime、endTime、taskName、testPurpose）存储在内存中
由user_id键控。当用户回复“确认”时，处理程序会查看
在州内，调用真正的reserve_bench，并清除条目。

条目将在几分钟后过期，因此被遗忘的确认不会永远逗留，并在以后意外预订意图。
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
