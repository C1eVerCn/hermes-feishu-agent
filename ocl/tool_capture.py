"""Per-session capture of this turn's tool results, written by the feishu_acl
post_tool_call hook and read by bot/handler.py after agent.chat().

Keyed by hermes session_id. handler clears before chat and reads after.
Stores raw result (parsed dict when the tool returned a JSON string, else the
raw value) so card_builder can render deterministically."""
import json
import threading
from typing import Any

_lock = threading.Lock()
_store: dict[str, list[dict]] = {}


def _coerce(result: Any):
    if isinstance(result, str):
        try:
            return json.loads(result)
        except (json.JSONDecodeError, ValueError):
            return result
    return result


def record(session_id: str, tool_name: str, result: Any) -> None:
    entry = {"tool": tool_name, "result": _coerce(result)}
    with _lock:
        _store.setdefault(session_id, []).append(entry)


def read(session_id: str) -> list[dict]:
    with _lock:
        return list(_store.get(session_id, []))


def clear(session_id: str) -> None:
    with _lock:
        _store.pop(session_id, None)
