"""Handle Feishu interactive-card button callbacks (车辆预约域).

支持的 action：
- select_vehicle    用户在「车辆列表」卡片点 [选N] → 写 car_state（带 dry_run）
- confirm_booking   用户在「确认」卡片点 [确认] → 调 _commit_vehicle_reservation
- cancel_flow       用户在任意卡片点 [取消] → clear car_state

确定性强：不经过 LLM，路径固定。
"""
import json
import logging

from ocl import identity
from ocl.tool_guard import set_current_caller, CallerIdentity
from car_tools import handlers as car_handlers
from car_tools import card_builder as car_card_builder
from bot import car_state
from bot import car_booking_fsm
from feishu import sender

log = logging.getLogger(__name__)

# 卡片回调按钮的特殊文本标记（FSM 内部用）—— 无 value / 需特殊翻译
# 全部集中在这里以便维护；每个 marker 对应 FSM 中的一类按钮
_FSM_MARKERS = {
    "fsm_direct_by_id":  "__fsm_direct_by_id__",
    "fsm_known_yes":     "__fsm_known_yes__",
    "fsm_known_no":      "__fsm_known_no__",
    "fsm_dur_minus":     "__fsm_dur_minus__",
    "fsm_dur_plus":      "__fsm_dur_plus__",
    "fsm_dur_confirm":   "__fsm_dur_confirm__",
}


def handle(open_id: str, value: dict, chat_id: str = "") -> tuple[str, dict | None]:
    """处理飞书卡片按钮回调。

    Returns: (toast_text, updated_card_or_None)
    - toast_text: lark 显示的简短反馈（≤100 字）
    - updated_card_or_None: 替换原卡片的完整 card（None = 不替换）
    """
    action = value.get("action", "")
    email = identity.email_of(open_id)

    # 注入身份（commit 路径走 handler 需要 caller）
    set_current_caller(CallerIdentity(openid=open_id, email=email, mobile=None))

    try:
        # ── 取消流程（任意卡片通用） ─────────────────────────────────
        if action == "cancel_flow" or action == "cancel_reserve":
            car_state.clear(open_id)
            return "已取消本次操作。", None

        # ── FSM 14 状态机按钮（fsm_*） ──────────────────────────────
        if action.startswith("fsm_"):
            return _handle_fsm_button(open_id, chat_id, value)

        # ── 选车（[选N] 按钮） ──────────────────────────────────────
        if action == "select_vehicle":
            vehicle_no = value.get("vehicle_no", "")
            if not vehicle_no:
                return "车辆编号缺失", None
            vehicle_type = value.get("vehicle_type", "")
            platform = value.get("platform", "")
            license_plate = value.get("license_plate", "")
            return _handle_select_vehicle(
                open_id, chat_id, vehicle_no, vehicle_type, platform, license_plate,
                email)

        # ── 确认预约（[确认] 按钮） ─────────────────────────────────
        if action == "confirm_booking":
            return _handle_confirm_booking(open_id, chat_id, value, email)

        return "暂不支持该操作。", None
    finally:
        set_current_caller(CallerIdentity())


# ── FSM 按钮回调（fsm_*） ───────────────────────────────────────────────

def _handle_fsm_button(open_id: str, chat_id: str, value: dict
                        ) -> tuple[str, dict | None]:
    """fsm_* 按钮 → 翻译为文本 → 调 car_booking_fsm.advance() → 渲染响应。

    翻译规则：
    - 默认用 value 字段作为 text（"DM2" / "Xavier" / "1小时" 等）
    - fsm_pick_slot 带 slot_idx → 翻译为 "1" / "2" / "3"
    - fsm_direct_by_id 无 value → 用特殊标记符
    """
    action = value.get("action", "")

    if action == "fsm_pick_slot":
        slot_idx = value.get("slot_idx", 1)
        text = str(slot_idx)
    elif action in _FSM_MARKERS:
        text = _FSM_MARKERS[action]
    else:
        text = str(value.get("value", ""))

    # 调 FSM 主入口
    new_state, response = car_booking_fsm.advance(open_id, text)
    log.info("fsm_button user=%s action=%s new_state=%s", open_id, action, new_state)

    # 渲染：toast_text + 替换卡片
    toast = response.get("text", "")
    card = response.get("card")
    if card is None and (response.get("text") or response.get("buttons")):
        # text + buttons 混合 → 转成 card（飞书 card_action callback 必须返回 card）
        lines = [response.get("text", "")]
        for btn in response.get("buttons", []):
            lines.append(f"  · {btn['text']}")
        card = {"config": {"wide_screen_mode": True},
                "elements": [{"tag": "div",
                              "text": {"tag": "lark_md",
                                       "content": "\n".join(s for s in lines if s)}}]}
    return toast, card


# ── select_vehicle ─────────────────────────────────────────────────────────

def _handle_select_vehicle(open_id: str, chat_id: str, vehicle_no: str,
                           vehicle_type: str, platform: str, license_plate: str,
                           email: str) -> tuple[str, dict | None]:
    """用户点 [选N] → 写 car_state → 调 _dry_run_vehicle_reservation 渲染确认卡
    （缺字段则渲染 missing-fields 卡片）。"""
    car_state.save(
        open_id,
        intent="booking",
        vehicle_no=vehicle_no,
        vehicle_type=vehicle_type,
        platform=platform,
        license_plate=license_plate,
    )

    # 立即跑一次 dry_run（仅含车辆信息，时间/任务/地点都缺 → 触发 missing_fields）
    args = {
        "vehicleNo": vehicle_no,
        "vehicleType": vehicle_type,
        "platform": platform,
        "licensePlate": license_plate,
    }
    set_current_caller(CallerIdentity(openid=open_id, email=email, mobile=None))
    raw = car_handlers._dry_run_reservation(args)
    try:
        parsed = json.loads(raw) if isinstance(raw, str) else raw
    except (json.JSONDecodeError, ValueError):
        parsed = {"error": raw}

    if isinstance(parsed, dict) and "error" in parsed:
        return parsed["error"], car_card_builder.build_fail_card(parsed["error"])

    if isinstance(parsed, dict) and parsed.get("missing_fields"):
        # 渲染缺字段卡片 + 让用户在主对话流补充字段
        return ("已选车辆，请补充其他信息", car_card_builder.build_missing_fields_card(parsed["summary"]))

    if isinstance(parsed, dict) and parsed.get("dry_run"):
        # 全部齐全（极端情况：从 query 阶段已经推断出所有字段）→ 渲染确认卡
        return ("请确认预约信息", car_card_builder.build_confirm_card(parsed["summary"], parsed.get("args", {})))

    return "操作失败", car_card_builder.build_fail_card("未知状态")


# ── confirm_booking ────────────────────────────────────────────────────────

def _handle_confirm_booking(open_id: str, chat_id: str, value: dict,
                            email: str) -> tuple[str, dict | None]:
    """用户点 [确认] → 调 _commit_vehicle_reservation。

    权限门控由 L1 (hermes pre_tool_call 钩子) + L2 (guarded() 包裹) 负责；
    不在调用方再做显式 check。
    """
    args = {k: v for k, v in value.items() if k != "action" and v}
    set_current_caller(CallerIdentity(openid=open_id, email=email, mobile=None))
    raw = car_handlers._commit_single_vehicle_reservation(args)
    car_state.clear(open_id)

    try:
        parsed = json.loads(raw) if isinstance(raw, str) else raw
    except (json.JSONDecodeError, ValueError):
        parsed = {"error": raw}

    if isinstance(parsed, dict) and "error" in parsed:
        return "提交失败", car_card_builder.build_fail_card(parsed["error"], context="提交预约")

    result_dict = parsed.get("data") if isinstance(parsed, dict) and "data" in parsed else parsed
    if not isinstance(result_dict, dict):
        return "提交失败", car_card_builder.build_fail_card("MCP 返回格式异常")

    # 异步通知调度员
    from car_tools import notify_dispatchers
    notify_dispatchers.submit_reservation_dispatchers(result_dict)

    # 持久化 reservation → applicant 映射（用于审批后 DM 申请人）
    from bot import reservation_store
    rid = result_dict.get("reservation_id") or result_dict.get("reservationId") or ""
    key = rid or f"car|{result_dict.get('vehicle_no','')}|{result_dict.get('start_time','')}"
    reservation_store.save(
        key, open_id, email,
        result_dict.get("vehicle_no", ""),
        result_dict.get("start_time", ""),
        result_dict.get("end_time", ""),
        result_dict.get("task_name", ""),
    )

    return "提交成功，等待调度员审批", car_card_builder.build_success_card(result_dict)
