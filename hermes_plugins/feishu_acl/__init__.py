"""
feishu_acl — per-user tool call access control for Feishu bot.

Layer 1 of the double-defense architecture. Registered as a hermes
pre_tool_call hook plugin. On every tool call dispatched by the agent,
the hook receives the hermes session_id, looks up the Feishu user_id via
ocl.session_map, and checks ocl.permission.is_tool_permitted().

Returns {"action": "block", "message": "..."} to block the tool call,
or None to allow it. The first non-None block directive returned by any
plugin wins.

Layer 2 (ocl.tool_guard.guarded) remains active as a fallback for cases
where the plugin path is bypassed or session_id propagation fails.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from ocl import permission
from ocl.session_map import lookup as session_lookup
from ocl import tool_capture

logger = logging.getLogger(__name__)

_BLOCK_MESSAGE = "权限不足：请联系管理员申请相应权限"


def _on_pre_tool_call(
    tool_name: str = "",
    args: Optional[Dict[str, Any]] = None,
    task_id: str = "",
    session_id: str = "",
    tool_call_id: str = "",
    **kwargs: Any,
) -> Optional[Dict[str, str]]:
    """Check whether the calling user is permitted to use this tool.

    Called by hermes's get_pre_tool_call_block_message before executing
    any tool. The first plugin returning a valid block directive wins.
    Return None to allow the tool to proceed.

    Keyword Arguments (from hermes internals):
        tool_name: registry name of the tool being dispatched.
        args: tool arguments dict (may be {}).
        task_id: terminal/browser session isolation key.
        session_id: AIAgent session_id.
        tool_call_id: unique id for this specific call.
    """
    if not session_id:
        # No session_id — cannot determine user. Let Layer 2 handle it.
        return None

    user_id = session_lookup(session_id)
    if not user_id:
        # Mapping not found (race condition, agent created before session_map
        # introduced, or pool eviction race). Layer 2 will handle it.
        return None

    try:
        allowed = permission.is_tool_permitted(user_id, tool_name)
    except Exception:
        logger.exception(
            "feishu_acl: permission check failed user=%s tool=%s",
            user_id, tool_name,
        )
        return None  # fail-open

    if not allowed:
        logger.warning(
            "feishu_acl: BLOCKED tool=%s user=%s session=%s",
            tool_name, user_id, session_id,
        )
        return {"action": "block", "message": _BLOCK_MESSAGE}

    return None


def _on_post_tool_call(
    tool_name: str = "",
    args: Optional[Dict[str, Any]] = None,
    result: Any = None,
    task_id: str = "",
    session_id: str = "",
    tool_call_id: str = "",
    **kwargs: Any,
) -> None:
    """Observational: capture each tool's raw result for deterministic card
    rendering. Keyed by session_id. Fail-open — never raise into hermes."""
    if not session_id:
        return
    try:
        tool_capture.record(session_id, tool_name, result)
    except Exception:
        logger.exception(
            "feishu_acl: capture failed tool=%s session=%s", tool_name, session_id,
        )


def register(ctx) -> None:
    """插件入口，由 hermes 的 PluginManager 调用一次。"""
    ctx.register_hook("pre_tool_call", _on_pre_tool_call)
    ctx.register_hook("post_tool_call", _on_post_tool_call)
    logger.info("feishu_acl plugin registered: pre_tool_call + post_tool_call hooks active")
