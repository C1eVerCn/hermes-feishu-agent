"""bot/handler — 飞书消息 → 业务处理 → 飞书卡片回复。

层级（车辆预约域）：
- Layer 0      闲聊/打招呼/帮助（无 agent，<1ms）
- Layer 0.5    快速路径：查车 / 我的预约 / 我的待审批
- Layer 0.6    快速路径：预约 dry_run
- Layer "car"  状态机：用户在挂起状态时收到字段补充、escape 等
- Identity / admin commands (bypass agent)
- Agent path   复杂自由文本 → AIAgent → OCL pipeline → 卡片
"""
import json
import logging
import re
import time
import contextvars
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout
from datetime import datetime, timedelta
from typing import Optional

from lark_oapi.api.im.v1 import P2ImMessageReceiveV1

from config.settings import settings
from feishu.ws_client import event_queue
from feishu import sender
from feishu import notify
from bot.agent_pool import agent_pool
from bot import car_state
from infra.metrics import metrics
from ocl.pipeline import apply as ocl_apply
from ocl.tool_guard import (
    set_current_caller, set_current_user, set_current_email,
    CallerIdentity, get_current_caller,
)
from ocl import identity
from bot.identity_admin import get_admin as get_identity_admin
from ocl import tool_capture
from car_tools import handlers as car_handlers
from car_tools import card_builder as car_card_builder

log = logging.getLogger(__name__)

_executor = ThreadPoolExecutor(max_workers=5, thread_name_prefix="agent-worker")

# 状态机：escape 关键词（用户说「算了/换个/不订了」→ clear state）
_ESCAPE_PHRASES = ("算了", "换个", "不订了", "取消", "放弃", "不要了")

# 状态机：confirm 文本路径（用户在挂起状态回复「确认」→ 走 commit）
_CONFIRM_PHRASES = ("确认", "确定", "ok", "yes", "yep", "yeah")

_EMPTY_REPLY = "您好，请输入文字消息，我来为您解答。"
_TIMEOUT_REPLY = "抱歉，响应超时（>120s），请稍后重试。"
_ERROR_REPLY = "抱歉，处理您的消息时出现了错误，请稍后再试。"
_INPUT_TOO_LONG_REPLY = "抱歉，消息过长（超过 8000 字），请分段发送。"
_MAX_INPUT_CHARS = 8000

# ── Layer 0: Simple intent ────────────────────────────────────────────────

_SIMPLE_REPLIES: list[tuple[re.Pattern, str]] = [
    (re.compile(r'^(你好|hi|hello|hey|嗨|哈[啰咯]|早上好|下午好|晚上好|good\s*morning|good\s*afternoon|good\s*evening)[\s!！。.]*$', re.IGNORECASE),
     "你好！我是约车助手，专注于车辆预约管理（查询 / 预约 / 取消 / 归还 / 审批）。\n输入「帮助」了解我能做什么。"),
    (re.compile(r'^(谢谢|感谢|thanks|thank\s*you|3q|多谢|谢了|辛苦)[\s!！。.]*$', re.IGNORECASE),
     "不客气！有需要随时找我。"),
    (re.compile(r'^(再见|bye|拜拜|88|回见|下次聊)[\s!！。.]*$', re.IGNORECASE),
     "再见！有需要随时找我。"),
    (re.compile(r'^(在吗|在不在|在线吗)[\s!！。.?？]*$'),
     "在的！有什么可以帮您的？"),
    (re.compile(r'^(你是谁|你叫什么|你是做什么的|你能做什么|你能帮我做什么|你能帮我吗|你能干什么|介绍一下你自己)[\s!！。.?？]*$'),
     "我是约车助手，可以帮您：\n• 查询可用车辆\n• 预约 / 取消 / 归还车辆\n• 查询我的预约\n• （调度员/管理员）审批预约、查询待审批\n\n输入「我的权限」查看当前角色。"),
    (re.compile(r'^(帮助|help|怎么用|怎么操作|使用说明|功能)[\s!！。.?？]*$', re.IGNORECASE),
     "📋 我能帮你做的事情：\n\n🔍 查询类\n• 查询可用车辆（指定时间 + 平台 + 类型）\n• 查询我的预约\n• （调度员）查询待审批列表\n\n✏️ 操作类\n• 预约车辆（两步流程：选车 → 确认）\n• 取消待审批的预约\n• 归还已批准的车辆\n\n🛡️ 调度员/管理员\n• 审批预约\n\n💡 输入「我的权限」查看当前角色"),
    (re.compile(r'^(好[的]?|ok|嗯|哦|知道了|明白了|懂了|收到|了解|got\s*it)[\s!！。.]*$', re.IGNORECASE),
     "好的，有问题随时找我。"),
]

_MY_PERMS = re.compile(r'我的权限|查看.*权限|我的角色')
_ADMIN_SET_ROLE = re.compile(r'^设置角色\s+(\S+)\s+([123])$')
_ADMIN_LIST_USERS = re.compile(r'^查看用户(?:\s+(\S+))?$')

_ROLE_NAME = {0: "非平台用户", 1: "普通用户", 2: "调度员", 3: "管理员"}
_ROLE_BY_NAME = {"待审核": 0, "非平台用户": 0, "普通用户": 1, "调度员": 2, "管理员": 3}


def _admin_ids() -> set[str]:
    raw = getattr(settings, "OCL_ADMIN_USER_IDS", "")
    return {uid.strip() for uid in raw.split(",") if uid.strip()}


def _is_admin(user_id: str) -> bool:
    return user_id in _admin_ids() or identity.role_of(user_id) == 3


def _resolve_role_with_env_admin(admin, user_id: str, role: int) -> int:
    if role < 3 and user_id in _admin_ids():
        admin.set_role(user_id, 3, operator="ocl_admin_env",
                       note="auto-elevated from OCL_ADMIN_USER_IDS")
        return 3
    return role


_ROLE_CAPS = {
    1: "可查询可用车辆、预约/取消/归还车辆、查询自己的预约记录。",
    2: "在普通用户基础上，可审批本组车辆预约、查询本组待审批列表。",
    3: "拥有全部权限（含跨组审批等系统级操作）。",
}


def _identity_preamble(user_id: str, role: int, name: str) -> str:
    role_name = _ROLE_NAME.get(role, "未知")
    caps = _ROLE_CAPS.get(role, "")
    who = f"当前对话用户：{name}（角色：{role_name}，role={role}）。" if name \
        else f"当前对话用户角色：{role_name}（role={role}）。"
    return (
        "［系统已核验的用户身份，以此为准，不要自行推断或质疑］\n"
        f"{who}\n"
        f"权限范围：{caps}\n"
        "回答涉及「你是谁/我的权限/我能做什么」时，必须依据上述角色，"
        "不得默认对方是普通用户。\n"
        "———\n"
        "用户消息："
    )


def _handle_identity_query(text: str, user_id: str) -> str:
    if _MY_PERMS.search(text):
        admin = get_identity_admin()
        role = admin.get_role(user_id)
        if role == 0:
            return (f"您当前是【待审核】用户，无平台权限。\n"
                    f"请联系管理员开通，并提供您的 open_id：\n"
                    f"{user_id}")
        role_name = {1: "普通用户", 2: "调度员", 3: "管理员"}.get(role, "未知")
        caps = {
            1: "可查询可用车辆、预约/取消/归还车辆、查询自己的预约记录。",
            2: "可审批本组车辆预约、查询本组待审批列表。",
            3: "拥有全部权限（含跨组审批等系统级操作）。",
        }.get(role, "")
        return f"您是【{role_name}】。\n{caps}"
    return ""


def _handle_admin_command(text: str, user_id: str) -> str:
    if not _is_admin(user_id):
        return ""
    m = _ADMIN_SET_ROLE.match(text)
    if m:
        target, role = m.group(1), int(m.group(2))
        admin = get_identity_admin()
        ok, msg = admin.set_role(target, role, operator=user_id, note="via_feishu_admin_command")
        if not ok:
            return f"设置失败：{msg}"
        return f"已设置 {target} 的角色为 {_ROLE_NAME[role]}。"
    m = _ADMIN_LIST_USERS.match(text)
    if m:
        return _format_user_list(m.group(1))
    return ""


def _format_user_list(filter_arg: str | None) -> str:
    admin = get_identity_admin()
    users = admin.list_all()
    if not users:
        return "当前没有任何用户记录。"
    if filter_arg and filter_arg in users:
        rec = users[filter_arg]
        return (f"用户 {filter_arg}：\n"
                f"• 角色：{_ROLE_NAME.get(int(rec.get('role', 0)), '未知')}\n"
                f"• 姓名：{rec.get('name', '') or '(未知)'}\n"
                f"• 邮箱：{rec.get('email', '') or '(未知)'}\n"
                f"• 建档方式：{rec.get('registered_via', '') or '(未知)'}")
    role_filter = _ROLE_BY_NAME.get(filter_arg) if filter_arg else None
    if filter_arg and role_filter is None and filter_arg not in users:
        return (f"未找到用户或角色「{filter_arg}」。\n"
                "用法：「查看用户」全部 / 「查看用户 调度员」按角色 / 「查看用户 ou_xxx」按 open_id。")
    lines: list[str] = []
    by_role: dict[int, list[str]] = {0: [], 1: [], 2: [], 3: []}
    for oid, rec in users.items():
        r = int(rec.get("role", 0))
        if role_filter is not None and r != role_filter:
            continue
        by_role.setdefault(r, []).append(
            f"  • {rec.get('name', '') or '(无名)'} | {rec.get('email', '') or '(无邮箱)'} | {oid}")
    total = sum(len(v) for v in by_role.values())
    header = f"📋 用户列表（共 {total} 人）" + (f"，筛选：{filter_arg}" if filter_arg else "")
    lines.append(header)
    for r in (3, 2, 1, 0):
        if by_role.get(r):
            lines.append(f"\n【{_ROLE_NAME[r]}】{len(by_role[r])} 人")
            lines.extend(by_role[r])
    return "\n".join(lines)


# ── 入口 ─────────────────────────────────────────────────────────────────

def start_consumer() -> None:
    log.info("Event consumer started")
    while True:
        data: P2ImMessageReceiveV1 = event_queue.get()
        try:
            _handle(data)
        except Exception:
            log.exception("Unhandled error in consumer for message_id=%s",
                          data.event.message.message_id)
        finally:
            event_queue.task_done()


def _handle(data: P2ImMessageReceiveV1) -> None:
    msg = data.event.message
    sender_info = data.event.sender
    user_id = sender_info.sender_id.open_id
    chat_id = msg.chat_id

    if user_id:
        notify.remember_open_id(user_id, email="")

    log.info("received message_id=%s chat_id=%s user_id=%s", msg.message_id, chat_id, user_id)

    text = _extract_text(msg)

    if not text:
        sender.send_text_as_card(chat_id, _EMPTY_REPLY)
        return

    if len(text) > _MAX_INPUT_CHARS:
        sender.send_text_as_card(chat_id, _INPUT_TOO_LONG_REPLY)
        return

    # ── Layer 0: Simple intent（先于身份闸，避免问候语被 18s 飞书 API 阻塞） ──
    instant = _match_simple_intent(text)
    if instant:
        sender.send_text_as_card(chat_id, instant)
        return

    # ── 身份闸 ─────────────────────────────────────────────────────────
    admin = get_identity_admin()
    email = identity.email_of(user_id)
    name = identity.name_of(user_id)
    if user_id:
        admin.auto_register(user_id, email=email, name=name)
        if email:
            existing = admin.get(user_id) or {}
            if existing.get("email") != email:
                admin.update_profile(user_id, email=email, name=name or existing.get("name", ""),
                                     operator="auto_email_sync")
        if admin.get_role(user_id) == 0:
            admin.set_role(user_id, 1, operator="auto_in_scope",
                           note=f"in-visible-scope default; email={'yes' if email else 'none'}")
    role = admin.get_role(user_id)
    role = _resolve_role_with_env_admin(admin, user_id, role)
    if admin.get(user_id):
        email = admin.get(user_id).get("email", "") or email
        name = admin.get(user_id).get("name", "") or name

    # ── 注入 CallerIdentity 到 contextvars（agent 路径和工具路径共用） ───
    caller = CallerIdentity(openid=user_id, email=email, mobile=None)
    set_current_caller(caller)

    # ── 状态机：escape 关键词（用户在挂起状态时说「算了/换个」→ clear） ─
    if car_state.get(user_id) is not None:
        norm = text.strip().strip("「」『』[]\"'").lower()
        if norm in _ESCAPE_PHRASES:
            car_state.clear(user_id)
            sender.send_text_as_card(chat_id, "已取消本次操作。")
            return

    # ── Layer 0.5/0.6: Fast paths（直接调工具，<1s） ─────────────────
    fast = _try_fast_path(text, user_id, role)
    if fast is not None:
        if fast.get("blocked"):
            sender.send_text_as_card(chat_id, fast["text"])
        elif fast.get("card"):
            sender.send_card(chat_id, fast["card"])
        else:
            sender.send_text_as_card(chat_id, fast.get("text") or _ERROR_REPLY)
        return

    # ── FSM 入口：用户在挂起状态 OR 表达约车意图（spec §3.2） ─────────
    from bot import car_booking_fsm
    pending_state = car_state.get(user_id)
    in_fsm = pending_state and pending_state.state not in ("", "START")

    BOOKING_INTENT = ("我想约车", "我要约车", "帮我约车", "约车", "预约车",
                      "帮我预约", "我想预约")
    norm = text.strip()
    # "报编号"快捷路径：字母+数字 5-9 字符（spec §3.3 START 路径）
    is_vehicle_id = bool(re.match(r"^[A-Za-z]{2,5}\d{3,6}$", norm))
    is_booking_intent = (
        norm in BOOKING_INTENT
        or norm.startswith(("我要约", "帮我约"))
        or _is_type_keyword(norm)
        or is_vehicle_id
    )

    if in_fsm or is_booking_intent:
        new_state, response = car_booking_fsm.advance(user_id, text)
        # 渲染响应
        if response.get("card"):
            sender.send_card(chat_id, response["card"])
        elif response.get("text") or response.get("buttons"):
            # 拼接 text + buttons（spec §3.3 各状态用 text 描述 + 按钮组）
            text_lines = [response.get("text", "")]
            for btn in response.get("buttons", []):
                text_lines.append(f"  · {btn['text']}")
            sender.send_text_as_card(chat_id, "\n".join(s for s in text_lines if s))
        else:
            sender.send_text_as_card(chat_id, _ERROR_REPLY)
        return

    # ── Identity query / admin command ────────────────────────────────
    identity_response = _handle_identity_query(text, user_id)
    if identity_response:
        sender.send_text_as_card(chat_id, identity_response)
        return

    admin_response = _handle_admin_command(text, user_id)
    if admin_response:
        sender.send_text_as_card(chat_id, admin_response)
        return

    if role == 0:
        sender.send_text_as_card(chat_id,
            "无法识别您的身份（未获取到 open_id），请在飞书私聊中直接发消息后重试。")
        return

    # ── Agent 路径 ────────────────────────────────────────────────────
    session_id = f"feishu_{user_id}"
    start = time.monotonic()
    captured: list[dict] = []
    try:
        t0 = time.monotonic()
        agent = agent_pool.get_or_create(user_id)
        log.info("trace[agent_pool.get_or_create] took=%.2fs", time.monotonic() - t0)
        # contextvars 不会跨线程自动传播 —— 必须 copy_context。
        ctx = contextvars.copy_context()
        tool_capture.clear(session_id)
        agent_input = _identity_preamble(user_id, role, name) + text
        t1 = time.monotonic()
        future = _executor.submit(ctx.run, agent.chat, agent_input)
        log.info("trace[executor.submit] took=%.2fs", time.monotonic() - t1)
        t2 = time.monotonic()
        response: str = future.result(timeout=settings.AGENT_TIMEOUT_SECONDS)
        log.info("trace[future.result] took=%.2fs", time.monotonic() - t2)
        captured = tool_capture.read(session_id)
        latency = time.monotonic() - start
        metrics.record("llm_latency_seconds", latency)
        metrics.inc("messages_processed")
        log.info("processed message_id=%s latency=%.2fs", msg.message_id, latency)
    except FuturesTimeout:
        log.error("Agent timeout for user_id=%s message_id=%s", user_id, msg.message_id)
        metrics.inc("errors_timeout")
        response = _TIMEOUT_REPLY
    except Exception:
        log.exception("Agent error for user_id=%s message_id=%s", user_id, msg.message_id)
        metrics.inc("errors_agent")
        response = _ERROR_REPLY
    finally:
        set_current_caller(CallerIdentity())
        tool_capture.clear(session_id)

    # ── OCL pipeline ──────────────────────────────────────────────────
    ocl_result = ocl_apply(response or "", user_id, captured=captured)
    if ocl_result.blocked:
        metrics.inc("errors_ocl_blocked")

    if ocl_result.card is not None and not ocl_result.blocked:
        try:
            sender.send_card(chat_id, ocl_result.card)
        except Exception:
            log.exception("send_card failed, falling back to text-as-card")
            sender.send_text_as_card(chat_id, ocl_result.text or _ERROR_REPLY)
    else:
        sender.send_text_as_card(chat_id, ocl_result.text or _ERROR_REPLY)

    # ── 状态机持久化：_dry_run 完成后写入 car_state ───────────────────
    for entry in reversed(captured):
        if entry.get("tool") == "_dry_run_vehicle_reservation":
            res = entry.get("result") or {}
            if isinstance(res, dict) and res.get("dry_run") and not res.get("missing_fields"):
                args = res.get("args") or {}
                car_state.save(
                    user_id,
                    intent="booking",
                    vehicle_no=args.get("vehicle_no", ""),
                    vehicle_type=args.get("vehicle_type", ""),
                    platform=args.get("platform", ""),
                    license_plate=args.get("license_plate", ""),
                    start_time=args.get("start_time", ""),
                    end_time=args.get("end_time", ""),
                    task_name=args.get("task_name", ""),
                    location=args.get("location", ""),
                    remark=args.get("remark", ""),
                    vin=args.get("vin", ""),
                )
                break

    # ── 审批完成通知申请人：扫描 captured 里的 approval_vehicle_reservation ──
    # 调度员审批 → applicant DM 是车辆域头条功能；approval_vehicle_reservation
    # 成功完成时通过 reservation_store 反查申请人 open_id 异步发飞书通知。
    _notify_applicants_from_captured(captured)


def _notify_applicants_from_captured(captured: list) -> None:
    """扫描本轮 captured，找到 approval_vehicle_reservation 成功结果，
    给对应申请人发 DM（异步、fire-and-forget）。"""
    from car_tools import notify_applicant as _notify_applicant
    for entry in captured:
        if entry.get("tool") != "approval_vehicle_reservation":
            continue
        res = entry.get("result")
        if not isinstance(res, dict):
            continue
        if "error" in res:
            continue
        # 兼容 {"data": {...}} 包装；与 normalizers 输出对齐
        result_dict = res.get("data") if isinstance(res.get("data"), dict) else res
        if not isinstance(result_dict, dict) or "approved" not in result_dict:
            continue
        rid = result_dict.get("reservation_id") or result_dict.get("reservationId") or ""
        vehicle_no = result_dict.get("vehicle_no", "")
        start_time = result_dict.get("start_time", "")
        try:
            _notify_applicant.submit_approval_to_applicant(
                result_dict,
                reservation_id=rid,
                vehicle_no=vehicle_no,
                start_time=start_time,
            )
        except Exception:
            log.exception("notify_applicant_dispatch_failed rid=%s", rid)


def _commit_confirmed_booking(chat_id: str, user_id: str) -> None:
    """用户在挂起状态回复「确认」→ 调 _commit_vehicle_reservation。"""
    pending = car_state.get(user_id)
    if not pending or pending.intent != "booking":
        sender.send_text_as_card(chat_id, _ERROR_REPLY)
        return
    car_state.clear(user_id)

    args = {
        "vehicleNo": pending.vehicle_no,
        "vehicleType": pending.vehicle_type,
        "platform": pending.platform,
        "licensePlate": pending.license_plate,
        "startTime": pending.start_time,
        "endTime": pending.end_time,
        "taskName": pending.task_name,
        "location": pending.location,
        "remark": pending.remark,
        "vin": pending.vin,
    }
    args = {k: v for k, v in args.items() if v}

    # 权限门控由 L1 (hermes pre_tool_call 钩子) + L2 (guarded() 包裹) 负责；
    # 这里不再做第三重硬编码 check（与 card_action_handler._handle_confirm_booking 一致）。

    set_current_caller(CallerIdentity(openid=user_id, email=identity.email_of(user_id)))
    try:
        raw = car_handlers._commit_single_vehicle_reservation(args)
        try:
            parsed = json.loads(raw) if isinstance(raw, str) else raw
        except (json.JSONDecodeError, ValueError):
            parsed = {"error": raw}

        if isinstance(parsed, dict) and "error" in parsed:
            sender.send_card(chat_id, car_card_builder.build_fail_card(
                parsed["error"], context="提交预约"))
            return

        # 解析 ReservationResult 并触发调度员通知
        from car_tools import notify_dispatchers
        result_dict = parsed.get("data") if isinstance(parsed, dict) and "data" in parsed else parsed
        if not isinstance(result_dict, dict):
            sender.send_card(chat_id, car_card_builder.build_success_card({"args": args}))
            return

        sender.send_card(chat_id, car_card_builder.build_success_card(result_dict))
        notify_dispatchers.submit_reservation_dispatchers(result_dict)

        # 与 card_action_handler 路径保持一致：持久化 (reservation_id → applicant_open_id)
        # 映射，审批通过时由 notify_applicant 反查。
        from bot import reservation_store
        rid = result_dict.get("reservation_id") or result_dict.get("reservationId") or ""
        key = rid or f"car|{result_dict.get('vehicle_no','')}|{result_dict.get('start_time','')}"
        reservation_store.save(
            key, user_id, identity.email_of(user_id),
            result_dict.get("vehicle_no", ""),
            result_dict.get("start_time", ""),
            result_dict.get("end_time", ""),
            result_dict.get("task_name", ""),
        )
    finally:
        set_current_caller(CallerIdentity())


def _match_simple_intent(text: str) -> str:
    for pattern, reply in _SIMPLE_REPLIES:
        if pattern.search(text):
            return reply
    return ""


# ── Layer 0.5 / 0.6 Fast path（车辆业务） ─────────────────────────────────

# 类型/平台关键字白名单（fast-path 用，避免误匹配"我想约车"等通用短语）
_TYPE_KEYWORDS = (
    # 车辆类型（车型）
    "DM2", "CT1", "BM2", "CM0", "大F车", "小F车", "中F车",
    # 平台（芯片）
    "Xavier", "ADCU", "Orin", "Thor",
    # 英文拼写
    "大Fcar",
)


def _empty_args(m) -> dict:
    return {}


def _args_with_type(m) -> dict:
    """从 fast-path 命中里抽 vehicleType。"""
    if m.lastindex and m.group(1):
        return {"vehicleType": m.group(1).strip()}
    return {}


def _args_with_platform(m) -> dict:
    """从 fast-path 命中里抽 platform（芯片）。"""
    if m.lastindex and m.group(1):
        return {"platform": m.group(1).strip()}
    return {}


def _is_type_keyword(s: str) -> bool:
    """判断字符串是否是已知车型/平台关键字。"""
    return s.strip() in _TYPE_KEYWORDS


_FAST_PATH_PATTERNS: list[tuple[re.Pattern, str, "callable"]] = [
    # ── fetch_available_vehicles ──
    (re.compile(r'^(查询|查看|看看|有什么|列出|看|查)(\s*(所有|可用))?\s*(车辆|车)(\s*(列表|号))?[\s!！。.]*$'),
     "fetch_available_vehicles", _empty_args),
    (re.compile(r'^车辆(\s*(列表))?[\s!！。.]*$'),
     "fetch_available_vehicles", _empty_args),
    # 带车型/平台过滤的查询（用白名单匹配，避免误匹配通用短语）
    # 模式：现在 DM2 有什么车可以约 / 大Fcar 有什么车 / Xavier 平台的车
    (re.compile(
        r'^(?:现在|查|看|查询)?\s*(' + "|".join(re.escape(k) for k in _TYPE_KEYWORDS) + r')\s*'
        r'(?:有(?:什么|哪些))?\s*车\s*(?:可以)?\s*(?:约|查询|看)?\s*[\s!！。.]*$'),
     "fetch_available_vehicles", _args_with_type),

    # ── fetch_user_reservation ──
    (re.compile(r'^(查询|查看|查一下|查|看看|看下|帮我查|帮我看)?\s*我的\s*(预约记录|预约|所有预约)[\s!！。.]*$'),
     "fetch_user_reservation", _empty_args),

    # ── fetch_user_approval ──
    (re.compile(r'^(查询|查看|查|看看|帮我查|帮我看)?\s*(我的)?\s*(待审批列表|待审批|待我审批|审批列表|审批记录)[\s!！。.]*$'),
     "fetch_user_approval", _empty_args),
]


def _try_fast_path(text: str, user_id: str, role: int) -> Optional[dict]:
    norm = text.strip()
    if not norm:
        return None

    from ocl.permission import TOOL_MIN_ROLE

    for pattern, tool_name, args_fn in _FAST_PATH_PATTERNS:
        m = pattern.match(norm)
        if not m:
            continue

        if TOOL_MIN_ROLE.get(tool_name, 99) > role:
            return None

        set_current_caller(CallerIdentity(openid=user_id, email=identity.email_of(user_id)))
        try:
            handler = getattr(car_handlers, tool_name, None)
            if handler is None:
                return None

            args = args_fn(m)
            t0 = time.monotonic()
            try:
                raw = handler(args)
            except Exception:
                log.exception("fast_path tool_failed tool=%s user=%s", tool_name, user_id)
                return None
            latency_ms = (time.monotonic() - t0) * 1000
            metrics.inc("fast_path_hits")
            log.info("fast_path hit tool=%s user=%s latency=%.0fms",
                     tool_name, user_id, latency_ms)

            try:
                parsed = json.loads(raw) if isinstance(raw, str) else raw
            except (json.JSONDecodeError, ValueError):
                parsed = {}

            if not isinstance(parsed, (dict, list)):
                parsed = {}

            if isinstance(parsed, dict) and "error" in parsed:
                return {"text": f"❌ 查询失败：{parsed['error']}", "blocked": False, "card": None}

            # 业务专用卡片
            if tool_name == "fetch_available_vehicles":
                # parsed 可能是 list[dict]（vehicle 列表）或 dict（错误）
                vehicles = parsed if isinstance(parsed, list) else []
                # 缓存到 car_state（供"约第N个" / "约XX" 文本选择路径反查）
                car_state.save(
                    user_id,
                    intent="",
                    last_vehicles=vehicles,
                    last_query={},
                )
                # 把用户的过滤条件（如 vehicleType / platform）拼成 query_label 显示在卡片
                ql_parts = []
                if (args.get("vehicleType") or args.get("vehicle_type")):
                    ql_parts.append(str(args.get("vehicleType") or args.get("vehicle_type")))
                if args.get("platform"):
                    ql_parts.append(f"{args['platform']}芯片")
                query_label = " · ".join(ql_parts) if ql_parts else None
                card = car_card_builder.build_vehicles_card(
                    vehicles, query_label=query_label)
                return {"card": card, "text": None, "blocked": False}
            if tool_name == "fetch_user_reservation":
                n = len(parsed) if isinstance(parsed, list) else 0
                card = _build_records_card(parsed, title=f"📋 我的预约（共 {n} 条）")
                return {"card": card, "text": None, "blocked": False}
            if tool_name == "fetch_user_approval":
                n = len(parsed) if isinstance(parsed, list) else 0
                card = _build_records_card(parsed, title=f"📋 我的待审批（共 {n} 条）")
                return {"card": card, "text": None, "blocked": False}

            return {"text": "📋 查询成功。", "blocked": False, "card": None}
        finally:
            # 防止 caller 跨消息泄漏：fast path 不会走到 handler.py 主 finally
            set_current_caller(CallerIdentity())
    return None


def _build_records_card(records: list, *, title: str) -> dict:
    if not records:
        return {"config": {"wide_screen_mode": True},
                "elements": [{"tag": "div",
                              "text": {"tag": "lark_md",
                                       "content": f"{title}\n\n（暂无记录）"}}]}
    lines = ["| 车辆 | 平台 | 时间 | 任务 | 状态 |",
             "|------|------|------|------|------|"]
    for r in records:
        if not isinstance(r, dict):
            continue
        status = r.get("status", "")
        if status in ("待审批", "已批准", "已驳回", "已取消", "已归还"):
            badge_map = {
                "待审批": "🟡待审批", "已批准": "🟢已批准", "已驳回": "🔴已驳回",
                "已取消": "⚪已取消", "已归还": "✅已归还",
            }
            status = badge_map.get(status, status)
        lines.append(
            f"| `{r.get('vehicle_no','')}` "
            f"| {r.get('platform') or '-'} "
            f"| {r.get('start_time','')} ~ {r.get('end_time','')} "
            f"| {r.get('task_name') or '-'} "
            f"| {status} |"
        )
    return {"config": {"wide_screen_mode": True},
            "elements": [{"tag": "div",
                          "text": {"tag": "lark_md",
                                   "content": f"{title}\n\n" + "\n".join(lines)}}]}


def _extract_text(msg) -> str:
    if msg.message_type != "text":
        return ""
    try:
        content = json.loads(msg.content)
        return content.get("text", "").strip()
    except (json.JSONDecodeError, AttributeError):
        return ""
