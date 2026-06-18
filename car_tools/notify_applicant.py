"""车辆预约 — 审批后异步通知申请人。

调度员审批完成后（approved=true/false），从 reservation_store 反查申请人 open_id，
发飞书 DM。复用 feishu/notify.submit_text_to_user（在后台线程池跑）。
"""
import logging
from typing import Optional

from feishu import notify
from bot import reservation_store

log = logging.getLogger(__name__)


def submit_approval_to_applicant(approval_result: dict, reservation_id: str = "",
                                 vehicle_no: str = "", start_time: str = "") -> Optional["Future"]:
    """审批后异步通知预约人。

    优先级：reservation_id > (vehicle_no + start_time) 反查。
    返回 Future；找不到申请人 → 返回 None（不通知）。
    """
    rec = None
    if reservation_id:
        rec = reservation_store.get(reservation_id)
    if not rec and vehicle_no and start_time:
        rec = reservation_store.find_by_vehicle_and_time(vehicle_no, start_time)

    if not rec:
        log.info("notify_applicant_skip: reservation not found rid=%s vehicle=%s",
                 reservation_id, vehicle_no)
        return None

    oid = rec.get("applicant_open_id", "")
    if not oid:
        log.info("notify_applicant_skip: no open_id rid=%s", reservation_id)
        return None

    approved = bool(approval_result.get("approved"))
    title = "✅ 您的车辆预约已通过审批" if approved else "❌ 您的车辆预约已被拒绝"
    body = (
        f"{title}\n"
        f"车辆编号：{approval_result.get('vehicle_no','')}\n"
        f"开始时间：{approval_result.get('start_time','')}\n"
        f"结束时间：{approval_result.get('end_time','')}\n"
        f"任务：{approval_result.get('task_name','')}\n"
        f"审批人：{approval_result.get('reviewer','')}\n"
        f"审批意见：{approval_result.get('review_comment') or '（无）'}\n\n"
        "请按时使用，使用完毕请归还。"
        if approved else
        f"{title}\n"
        f"车辆编号：{approval_result.get('vehicle_no','')}\n"
        f"审批人：{approval_result.get('reviewer','')}\n"
        f"审批意见：{approval_result.get('review_comment') or '（无）'}"
    )
    log.info("applicant_notify_submit rid=%s oid=%s approved=%s",
             reservation_id, oid[:8], approved)
    return notify.submit_text_to_user(oid, body)
