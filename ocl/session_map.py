"""
Session ID to Feishu user ID mapping for hermes plugin pre_tool_call hooks.

Bridges the threading gap: the event consumer thread knows user_id from the
Feishu event; hermes internal tool-execution threads only know session_id.
This module provides the lookup that the feishu_acl plugin (Layer 1) uses,
while ocl/tool_guard.py (Layer 2) handles the thread-local fallback.
"""
import threading
from typing import Dict

_lock = threading.Lock()
_map: Dict[str, str] = {}


def register(session_id: str, user_id: str) -> None:
    """Map a hermes session_id to a Feishu user_id.

    Called by agent_pool when creating a new AIAgent instance.
    Idempotent — overwrites any existing mapping for the same session_id.
    """
    with _lock:
        _map[session_id] = user_id


def lookup(session_id: str) -> str:
    """Return the user_id for a session_id, or '' if unknown.

    Called by the pre_tool_call plugin callback (runs in hermes worker thread).
    Returns '' when session_id is unknown — the plugin uses this to fail-open
    and delegate to Layer 2 (guarded).
    """
    with _lock:
        return _map.get(session_id, "")


def evict(session_id: str) -> None:
    """Remove a session mapping. Called when agent_pool evicts an AIAgent.

    Idempotent — silently no-ops if session_id not found.
    """
    with _lock:
        _map.pop(session_id, None)


def size() -> int:
    """Return number of active mappings. Exposed for tests and metrics."""
    with _lock:
        return len(_map)
