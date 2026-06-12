"""Handle Feishu interactive-card button callbacks.
Deterministic (no LLM): inject identity → OCL role gate → call the tool handler.
Returns (toast_text, updated_card_or_None). value never carries email — the
clicker's open_id resolves it, so a forged value cannot impersonate."""
import json
import logging

from ocl import identity, permission
from ocl.tool_guard import set_current_email, set_current_user
from bench_tools import handlers
from bot import reservation_store
from feishu import notify

log = logging.getLogger(__name__)

# action → tool注册名（handler 按同名在调用时解析）
_ACTION_TOOL = {
    "cancel":  "cancel_reservation",
    "approve": "approve_reservation",
    "return":  "return_bench",
}


def _notify_dispatchers_for_reservation(bench_no: str, start_time: str,
                                         applicant_name: str,
                                         applicant_email: str,
                                         api_message: str) -> int:
    """Best-effort: DM every dispatcher whose email appears in the success
    message, with a heads-up that a new reservation is awaiting their
    approval. Returns the number of dispatchers successfully notified.

    Uses `submit_dispatchers_by_email_blocking` so the caller gets an
    accurate count to show in the applicant-confirmation card. Safe
    here because this is called from the confirm-text path
    (bot/handler.py), not the WS card-callback hot path.
    """
    emails = notify.extract_scheduler_emails(api_message)
    if not emails:
        log.info("notify_dispatchers_skip: no scheduler emails parsed from api_message")
        return 0
    subject = "📋 新预约待审批"
    body = (f"申请人：{applicant_name or applicant_email}\n"
            f"台架编号：{bench_no}\n"
            f"开始时间：{start_time}\n"
            "请尽快登录系统审批。")
    return notify.submit_dispatchers_by_email_blocking(
        emails, subject, body, timeout=30.0)


def _find_reservation_id(bench_no: str, start_time: str, end_time: str,
                          email: str) -> str:
    """Look up the most recent reservation matching (benchNo, startTime, endTime)
    via the bench API's myReservations endpoint, so we can persist a mapping
    for later approval notification. Caller MUST ensure set_current_email() is
    still in scope (we do that by keeping the try/finally wrapping this)."""
    try:
        from bench_tools import handlers as bench_handlers
        raw = bench_handlers.list_my_reservations(
            {"benchNo": bench_no, "startTime": start_time, "endTime": end_time}
        )
        data = json.loads(raw).get("data") or []
        if not data:
            return ""
        # most recent (by createTime desc if present, else by id)
        data.sort(key=lambda r: (r.get("createTime") or "", r.get("id") or ""),
                  reverse=True)
        return data[0].get("id", "")
    except Exception:
        log.exception("find_reservation_id_failed bench=%s start=%s",
                      bench_no, start_time)
        return ""


def _notify_applicant_of_approval(bench_no: str, start_time: str, end_time: str) -> None:
    """If we recorded the applicant's open_id for this reservation, DM them
    that the reservation is approved. Dispatched fire-and-forget on the
    notify pool so the WS card-action callback (which has a <50ms return
    budget) is never blocked by the rate-limit sleep + network round-trip."""
    rec = reservation_store.find_by_bench_and_time(bench_no, start_time)
    if not rec:
        return
    oid = rec.get("applicant_open_id", "")
    if not oid:
        return
    text = (f"✅ 您的台架预约已通过审批\n"
            f"台架编号：{bench_no}\n"
            f"开始时间：{start_time}\n"
            f"结束时间：{end_time}\n"
            f"任务：{rec.get('task_name', '')}\n"
            "请按时使用，使用完毕请归还。")
    notify.submit_text_to_user(oid, text)
    log.info("applicant_notify_submitted oid=%s bench=%s", oid, bench_no)


def handle(open_id: str, value: dict, chat_id: str = "") -> tuple[str, dict | None]:
    action = value.get("action", "")
    email = identity.email_of(open_id)
    if not email:
        return "您还不是平台用户，请联系管理员开通。", None

    if action == "cancel_reserve":
        return "已取消预约，请重新告知预约信息。", None

    # ── Existing: return / cancel / approve (deterministic) ──────────────
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
        # Approval: notify the original applicant
        if action == "approve" and args.get("approvalResult") in (1, "1"):
            _notify_applicant_of_approval(
                args.get("benchNo", ""),
                args.get("startTime", ""),
                args.get("endTime", ""))
        return parsed.get("message", "操作成功"), None
    return parsed.get("message", "操作失败"), None
