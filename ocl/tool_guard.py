"""Tool boundary enforcement + per-call user context via contextvars.

Uses `contextvars.ContextVar` (not `threading.local`) so that when the agent
runs in a worker thread spawned by `concurrent.futures.ThreadPoolExecutor`,
the per-request user/email context is automatically copied across the thread
boundary (Python 3.7+ does this in `Executor.submit` via `copy_context`).
"""
import contextvars
import json
import logging
from typing import Callable

from ocl import permission

log = logging.getLogger(__name__)

_user_id: contextvars.ContextVar[str] = contextvars.ContextVar("ocl_user_id", default="")
_email: contextvars.ContextVar[str] = contextvars.ContextVar("ocl_email", default="")


def set_current_user(user_id: str) -> None:
    _user_id.set(user_id)


def get_current_user() -> str:
    return _user_id.get()


def set_current_email(email: str) -> None:
    _email.set(email)


def get_current_email() -> str:
    return _email.get()


def guarded(tool_name: str, inner_handler: Callable) -> Callable:
    """Wrap a tool handler to enforce per-user permission before execution."""
    def _wrapper(args: dict, **_) -> str:
        uid = get_current_user()
        if uid and not permission.is_tool_permitted(uid, tool_name):
            log.warning("tool_blocked tool=%s user_id=%s", tool_name, uid)
            return json.dumps({"error": "权限不足：请联系管理员申请相应权限"}, ensure_ascii=False)
        return inner_handler(args)
    return _wrapper
