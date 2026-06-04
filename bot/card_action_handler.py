"""Handle Feishu interactive-card button callbacks.
Deterministic (no LLM): inject identity → OCL role gate → call the tool handler.
Returns (toast_text, updated_card_or_None). value never carries email — the
clicker's open_id resolves it, so a forged value cannot impersonate."""
import json
import logging

from ocl import identity, permission
from ocl.tool_guard import set_current_email, set_current_user
from mock_tools import handlers

log = logging.getLogger(__name__)

# action → tool registry name (handler resolved by the same name at call time)
_ACTION_TOOL = {
    "cancel":  "cancel_reservation",
    "approve": "approve_reservation",
    "return":  "return_bench",
}


def handle(open_id: str, value: dict) -> tuple[str, dict | None]:
    action = value.get("action", "")
    email = identity.email_of(open_id)
    if not email:
        return "您还不是平台用户，请联系管理员开通。", None

    if action == "return" and not value.get("returnLocation"):
        bench = value.get("benchNo", "")
        return f"请在对话中告诉我 {bench} 的还台地点，例如「归还 {bench}，地点安亭广场」。", None

    tool_name = _ACTION_TOOL.get(action)
    if tool_name is None:
        return "暂不支持该操作。", None
    fn = getattr(handlers, tool_name)  # resolve at call time (patch/reload-safe)

    if not permission.is_tool_permitted(open_id, tool_name):
        return "权限不足：该操作需要更高角色。", None

    args = {k: v for k, v in value.items() if k != "action"}
    set_current_user(open_id)
    set_current_email(email)
    try:
        raw = fn(args)
    finally:
        set_current_user("")
        set_current_email("")

    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        parsed = {"error": raw}

    if "error" in parsed:
        detail = parsed["error"].split(":", 1)[-1].strip()
        return f"操作失败：{detail}", None
    if parsed.get("code") == 200:
        return parsed.get("message", "操作成功"), None
    return parsed.get("message", "操作失败"), None
