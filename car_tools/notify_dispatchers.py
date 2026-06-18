"""车辆预约 — 异步通知调度员。

预约提交成功后，给 ReservationResult.dispatchers 里每个调度员发飞书 DM。
复用 feishu/notify.submit_dispatchers_by_email_blocking（在后台线程池跑，
不阻塞调用方）。

为什么不用 subprocess：参考项目用 subprocess.run(lark_message_tool.py) 有 30s
阻塞 + 进程管理成本。feishu/notify.py 已经在异步线程池里跑过 —— 直接复用。
"""
import logging
from typing import Optional

from feishu import notify

log = logging.getLogger(__name__)


def submit_reservation_dispatchers(reservation_result: dict) -> "Future":
    """预约成功后异步通知所有 dispatcher。fire-and-forget。"""
    dispatchers = reservation_result.get("dispatchers") or []
    if not dispatchers:
        log.info("notify_dispatchers_skip: empty dispatchers list")
        from concurrent.futures import Future
        fut: Future = Future()
        fut.set_result(0)
        return fut

    emails = [d.get("email", "") for d in dispatchers if d.get("email")]
    if not emails:
        log.info("notify_dispatchers_skip: no emails parsed")
        from concurrent.futures import Future
        fut = Future()
        fut.set_result(0)
        return fut

    applicant = reservation_result.get("applicant_name") or reservation_result.get("applicant_email") or "申请人"
    subject = "📋 新预约待审批"
    body = (
        f"申请人：{applicant}\n"
        f"车辆编号：{reservation_result.get('vehicle_no','')}\n"
        f"开始时间：{reservation_result.get('start_time','')}\n"
        f"结束时间：{reservation_result.get('end_time','')}\n"
        f"任务：{reservation_result.get('task_name','')}\n"
        f"地点：{reservation_result.get('location','')}\n"
        "请尽快审批。"
    )
    return notify.submit_dispatchers_by_email(emails, subject, body)
