"""Per-session capture of this turn's tool results, written by the feishu_acl
post_tool_call hook and read by bot/handler.py after agent.chat().

Keyed by hermes session_id. handler clears before chat and reads after.
Stores raw result (parsed dict when the tool returned a JSON string, else the
raw value) so card_builder can render deterministically.

Phase 0 (2026-06-30) 增加 ``timestamp`` 字段，供 commit 守卫判断 dry_run 是否过期。
时钟来源是模块级可注入函数（默认 :func:`time.time`），单测用 monkeypatch 替换，
不破坏 tests/CLAUDE.md 的「不用 time.sleep」规则（这里 time.time 是瞬时值）。"""
import json
import threading
import time as _time
from typing import Any, Callable

_lock = threading.Lock()
_store: dict[str, list[dict]] = {}
_clock: Callable[[], float] = _time.time


def set_clock(fn: Callable[[], float]) -> None:
    """注入时钟（用于单测 monkeypatch）。"""
    global _clock
    _clock = fn


def _coerce(result: Any):
    if isinstance(result, str):
        try:
            return json.loads(result)
        except (json.JSONDecodeError, ValueError):
            return result
    return result


def record(session_id: str, tool_name: str, result: Any) -> None:
    entry = {"tool": tool_name, "result": _coerce(result), "timestamp": _clock()}
    with _lock:
        _store.setdefault(session_id, []).append(entry)


def read(session_id: str) -> list[dict]:
    with _lock:
        return list(_store.get(session_id, []))


def clear(session_id: str) -> None:
    with _lock:
        _store.pop(session_id, None)
