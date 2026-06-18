"""Tool boundary enforcement + per-call identity context via contextvars.

设计原则：
- identity 是一个整体对象 CallerIdentity（openid + email + mobile），而不是分散在
  多个 ContextVar 里。这避免「email 设置了但 openid 没设置」的不一致。
- 老 API（set_current_user / set_current_email / get_current_user / get_current_email）
  保留为 alias，内部转发到 _caller。
- guarded() wrapper 是 L2 兜底；hermes pre_tool_call plugin 是 L1 硬拦截。
- 通过 contextvars（不是 threading.local）—— worker 线程跨边界自动 copy_context。
"""
import contextvars
import json
import logging
from dataclasses import dataclass
from typing import Callable, Optional

from ocl import permission

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class CallerIdentity:
    """调用方身份。openid 必填（无 openid 视为匿名内部调用）；email 必填但允许 ""；
    mobile Q3 接入飞书 contact v3 mobile 权限后启用，stub 为 None。"""
    openid: str = ""
    email: str = ""
    mobile: Optional[str] = None

    def as_dict(self) -> dict:
        """扁平化为 MCP 入参（snake_case → camelCase）。"""
        d: dict = {"openId": self.openid}
        if self.email:
            d["emailAddress"] = self.email
        if self.mobile:
            d["mobile"] = self.mobile
        return d

    @property
    def is_authenticated(self) -> bool:
        return bool(self.openid)


_caller: contextvars.ContextVar[CallerIdentity] = contextvars.ContextVar(
    "ocl_caller", default=CallerIdentity()
)


def set_current_caller(caller: CallerIdentity) -> None:
    """Inject identity for the current call context. Pass an empty CallerIdentity()
    to clear (anonymous)."""
    _caller.set(caller)


def get_current_caller() -> CallerIdentity:
    """Return the current caller's identity. Default is anonymous."""
    return _caller.get()


def get_caller_dict() -> dict:
    """Convenience: identity fields in MCP-arg shape."""
    return get_current_caller().as_dict()


# ── Backwards-compatible aliases (现有调用方仍可用) ─────────────────────────

def set_current_user(user_id: str) -> None:
    """Legacy alias: derive CallerIdentity from open_id alone (email/mobile lost).
    Prefer set_current_caller in new code."""
    cur = _caller.get()
    _caller.set(CallerIdentity(openid=user_id, email=cur.email, mobile=cur.mobile))


def get_current_user() -> str:
    return _caller.get().openid


def set_current_email(email: str) -> None:
    cur = _caller.get()
    _caller.set(CallerIdentity(openid=cur.openid, email=email, mobile=cur.mobile))


def get_current_email() -> str:
    return _caller.get().email


# ── L2 guarded wrapper ──────────────────────────────────────────────────────

def guarded(tool_name: str, inner_handler: Callable) -> Callable:
    """Wrap a tool handler to enforce per-user permission before execution.

    L2 fallback: blocks when caller.openid present but lacks permission.
    Anonymous callers (openid == "") pass through (internal/system call).
    """
    def _wrapper(args: dict, **_) -> str:
        caller = get_current_caller()
        if caller.openid and not permission.is_tool_permitted(caller.openid, tool_name):
            log.warning("tool_blocked tool=%s user_id=%s", tool_name, caller.openid)
            return json.dumps({"error": "权限不足：请联系管理员申请相应权限"}, ensure_ascii=False)
        return inner_handler(args)
    return _wrapper
