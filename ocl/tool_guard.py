"""Tool boundary enforcement + per-call user context via thread-local.
Carries both the open_id (for permission) and the resolved email (for API injection).
"""
import json
import threading
import logging
from typing import Callable

from ocl import permission

log = logging.getLogger(__name__)

_current = threading.local()


def set_current_user(user_id: str) -> None:
    _current.user_id = user_id


def get_current_user() -> str:
    return getattr(_current, "user_id", "")


def set_current_email(email: str) -> None:
    _current.email = email


def get_current_email() -> str:
    return getattr(_current, "email", "")


def guarded(tool_name: str, inner_handler: Callable) -> Callable:
    """Wrap a tool handler to enforce per-user permission before execution."""
    def _wrapper(args: dict, **_) -> str:
        uid = get_current_user()
        if uid and not permission.is_tool_permitted(uid, tool_name):
            log.warning("tool_blocked tool=%s user_id=%s", tool_name, uid)
            return json.dumps({"error": "权限不足：请联系管理员申请相应权限"}, ensure_ascii=False)
        return inner_handler(args)
    return _wrapper
