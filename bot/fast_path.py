"""bot/fast_path — 确定性查询快速路径（不经 LLM，<1s）。

从 handler 拆出（2026-06-25 重构）。命中 :func:`bot.intent.match_query` 的精确
查询短语（查可用车辆 / 我的预约 / 我的待审批）时，直接调对应 car_tools handler
并构建飞书卡片，绕过 agent。意图模式收口在 :mod:`bot.intent`（单一事实源）。
"""
import json
import logging
import time
from typing import Optional

from bot import car_state
from bot import intent
from ocl import identity
from ocl.permission import TOOL_MIN_ROLE
from ocl.tool_guard import set_current_caller, CallerIdentity
from infra.metrics import metrics
from car_tools import handlers as car_handlers
from car_tools import card_builder as car_card_builder

log = logging.getLogger(__name__)


def _fetch_vehicles_recommendations(user_id: str) -> list:
    """查可用车辆空态推荐：调 fetch_available_vehicles（无 filter）拿车组全量。"""
    try:
        raw = car_handlers.fetch_available_vehicles({})
        if isinstance(raw, str):
            parsed = json.loads(raw) if raw else {}
        else:
            parsed = raw
        if isinstance(parsed, dict):
            items = (parsed.get("items") or parsed.get("vehicles")
                     or parsed.get("data") or [])
        elif isinstance(parsed, list):
            items = parsed
        else:
            items = []
        return [v for v in items if isinstance(v, dict)]
    except Exception:
        return []


def try_fast_path(text: str, user_id: str, role: int) -> Optional[dict]:
    """命中查询快速路径则执行并返回 {"card"|"text"|"blocked"}，否则 None。"""
    matched = intent.match_query(text)
    if matched is None:
        return None
    tool_name, args, _m = matched
    return run_tool(tool_name, user_id, role, args)


def run_tool(tool_name: str, user_id: str, role: int, args: dict | None = None) -> Optional[dict]:
    """执行一个查询工具并构建卡片。供 fast-path（精确短语）与 Tier-2 路由器共用。

    返回 {"card"|"text"|"blocked"}；权限不足/工具缺失/异常 → None（调用方落 agent）。
    """
    args = args or {}
    if TOOL_MIN_ROLE.get(tool_name, 99) > role:
        return None

    set_current_caller(CallerIdentity(
        openid=user_id,
        email=identity.email_of(user_id),
        mobile=identity.mobile_of(user_id) or None,
    ))
    try:
        handler = getattr(car_handlers, tool_name, None)
        if handler is None:
            return None

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

        if tool_name == "fetch_available_vehicles":
            return _render_vehicles(parsed, args, user_id)
        if tool_name == "fetch_user_reservation":
            # 默认只显示「待审批」「已批准」（隐藏已取消/已驳回/已归还等历史，减少干扰）
            recs = [r for r in parsed if isinstance(r, dict)
                    and r.get("status") in ("待审批", "已批准")] if isinstance(parsed, list) else []
            card = car_card_builder.build_records_card(
                recs, title=f"我的预约（共 {len(recs)} 条）", show_cancel=True)
            return {"card": card, "text": None, "blocked": False}
        if tool_name == "fetch_user_approval":
            # 待审批列表只列真正「待审批」的（已处理的不再显示）
            recs = [r for r in parsed if isinstance(r, dict)
                    and r.get("status") == "待审批"] if isinstance(parsed, list) else []
            card = car_card_builder.build_records_card(recs, title=f"我的待审批（共 {len(recs)} 条）")
            return {"card": card, "text": None, "blocked": False}

        return {"text": "📋 查询成功。", "blocked": False, "card": None}
    finally:
        # 防止 caller 跨消息泄漏：fast path 不走 handler.py 主 finally。
        set_current_caller(CallerIdentity())


_MUTATION_TOOL = {
    "cancel": "cancel_vehicle_reservation",
    "return": "return_vehicle",
    "approve": "approval_vehicle_reservation",
}


def run_mutation(intent_name: str, slots: dict, user_id: str, role: int) -> Optional[dict]:
    """Tier-2 mutation 确定性分发：cancel / return / approve。

    返回 {"text"|"card"|"blocked"}；缺必要识别符 / 权限不足 / 异常 → None（落 agent 追问）。
    安全：必须有明确识别符（vehicle_no 或 reservation_id）才执行，否则交 agent；后端按
    emailAddress + 归属再做细粒度鉴权（取消只能取消自己待审批、审批只能审本组）。
    """
    tool = _MUTATION_TOOL.get(intent_name)
    if not tool:
        return None
    args = _build_mutation_args(intent_name, slots)
    if args is None:
        return None  # 缺识别符 → agent
    if TOOL_MIN_ROLE.get(tool, 99) > role:
        return None  # 权限不足 → agent（L1/L2 也会拦）

    set_current_caller(CallerIdentity(
        openid=user_id,
        email=identity.email_of(user_id),
        mobile=identity.mobile_of(user_id) or None,
    ))
    try:
        handler = getattr(car_handlers, tool, None)
        if handler is None:
            return None
        try:
            raw = handler(args)
        except Exception:
            log.exception("tier2_mutation_failed tool=%s user=%s", tool, user_id)
            return None
        metrics.inc("tier2_mutation_dispatch")
        try:
            parsed = json.loads(raw) if isinstance(raw, str) else raw
        except (json.JSONDecodeError, ValueError):
            parsed = {}
        if isinstance(parsed, dict) and "error" in parsed:
            return {"text": f"❌ 操作失败：{parsed['error']}", "card": None, "blocked": False}
        return {"text": _mutation_success_text(intent_name, slots), "card": None, "blocked": False}
    finally:
        set_current_caller(CallerIdentity())


def build_mutation_args(intent_name: str, slots: dict) -> Optional[dict]:
    """从 slots 构造 mutation 工具入参；缺 vehicle_no 返回 None。

    2026-06-25 新版上游：cancel/approval 去掉了 reservationId，统一以 vehicleNo(或 vin)
    为识别符。我们从自然语言只抽 vehicle_no，故三者都要求 vehicle_no。
    """
    vno = (slots.get("vehicle_no") or "").strip()
    if intent_name in ("cancel", "return"):
        return {"vehicleNo": vno} if vno else None
    if intent_name == "approve":
        approved = slots.get("approved")
        if not vno or approved is None:
            return None
        args = {"vehicleNo": vno, "approved": approved}
        if slots.get("review_comment"):
            args["reviewComment"] = slots["review_comment"]
        return args
    return None


# 向后兼容别名（旧测试引用 _build_mutation_args）
_build_mutation_args = build_mutation_args


def _mutation_success_text(intent_name: str, slots: dict) -> str:
    ident = (slots.get("vehicle_no") or slots.get("reservation_id") or "").strip()
    suffix = f" {ident}" if ident else ""
    if intent_name == "cancel":
        return f"✅ 已取消预约{suffix}。"
    if intent_name == "return":
        return f"✅ 已归还车辆{suffix}。"
    if intent_name == "approve":
        decision = "已批准" if slots.get("approved") else "已驳回"
        return f"✅ {decision}预约{suffix}，已通知申请人。"
    return "✅ 操作成功。"


def _render_vehicles(parsed, args: dict, user_id: str) -> dict:
    """fetch_available_vehicles 结果 → 车辆卡 / 空态推荐卡。"""
    vehicles = parsed if isinstance(parsed, list) else []
    recommendations = []
    if not vehicles:
        recommendations = _fetch_vehicles_recommendations(user_id)
    # 缓存到 car_state（供"约第N个"/"约XX"文本选择路径反查）
    car_state.save(user_id, intent="", last_vehicles=vehicles, last_query={})
    # 过滤条件 → query_label
    ql_parts = []
    if args.get("vehicleType") or args.get("vehicle_type"):
        ql_parts.append(str(args.get("vehicleType") or args.get("vehicle_type")))
    if args.get("platform"):
        ql_parts.append(f"{args['platform']}芯片")
    query_label = " · ".join(ql_parts) if ql_parts else None

    if vehicles:
        card = car_card_builder.build_vehicles_card(vehicles, query_label=query_label)
        return {"card": card, "text": None, "blocked": False}
    if recommendations:
        from bot.car_booking_fsm import _build_recommendation_card
        fake_pending = type("_P", (), {
            "vehicle_type_detail": args.get("vehicleTypeDetail") or args.get("vehicle_type_detail") or "",
            "vehicle_type": args.get("vehicleType") or args.get("vehicle_type") or "",
            "chip": args.get("platform") or "",
        })()
        card = _build_recommendation_card(recommendations, fake_pending)
        return {"card": card, "text": None, "blocked": False}
    card = car_card_builder.build_vehicles_card(vehicles, query_label=query_label)
    return {"card": card, "text": None, "blocked": False}


# build_records_card 已统一到 car_tools.card_builder.build_records_card（Card 2.0，
# 与 [我的预约] 按钮、其它记录卡同一渲染器，避免样式不一致）。本模块不再自带。
