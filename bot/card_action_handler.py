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
# 2026-06-18 review 删 'fsm_direct_by_id'：orphan marker（FSM 中无 emit 端）。
# 2026-06-18 增 'fsm_input_task_other' / 'fsm_input_location_other'：「其它」按钮
# 走自定义输入路径（区别于普通按钮 value=MFF调试/上海 等）。
_FSM_MARKERS = {
    "fsm_known_yes":            "__fsm_known_yes__",
    "fsm_known_no":             "__fsm_known_no__",
    "fsm_dur_minus":            "__fsm_dur_minus__",
    "fsm_dur_plus":             "__fsm_dur_plus__",
    "fsm_dur_confirm":          "__fsm_dur_confirm__",
    "fsm_input_task_other":     "__fsm_task_other__",
    "fsm_input_location_other": "__fsm_location_other__",
    # 2026-06-18 新增：SUCCESS 卡 [再约一辆] / [我的预约] 按钮
    "fsm_done_more":            "__fsm_done_more__",
    "fsm_done_records":         "__fsm_done_records__",
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

def _render_fsm_response(response: dict) -> tuple[str, dict | None]:
    """FSM response → (toast_text, card_for_lark) 渲染。

    复用 _handle_fsm_button 与 _handle_select_vehicle 的渲染逻辑。
    - response.get("card") 直接是 LARK card dict（FSM 内部用 _card_wrap 包装）
    - response 仅有 text + buttons → 拼成 LARK card（div + column_set 横排按钮）

    2026-06-18 横排化：按钮用 _button_row（column_set + column）横排，
    移动端自动降级为多行。Card 2.0 不支持 `tag:"action"` 容器（飞书 ErrCode
    200861）；_card_wrap 会自动展平但这里直接用 column_set 更清爽。
    """
    from bot.car_booking_fsm import _card_wrap
    from car_tools.card_builder import _button_row
    toast = response.get("text", "")
    card = response.get("card")
    if card is None and (response.get("text") or response.get("buttons")):
        elements: list[dict] = []
        if response.get("text"):
            elements.append({"tag": "div",
                             "text": {"tag": "lark_md",
                                      "content": response["text"]}})
        if response.get("buttons"):
            btns = [
                {"tag": "button",
                 "type": btn.get("type", "default"),
                 "text": {"tag": "plain_text", "content": btn["text"]},
                 "value": btn["value"]}
                for btn in response["buttons"]
            ]
            # 单一按钮放独立一行（不需要 column_set）；2+ 用 _button_row 横排
            if len(btns) >= 2:
                elements.append(_button_row(btns))
            else:
                elements.extend(btns)
        card = _card_wrap(elements)
    return toast, card


def _handle_fsm_button(open_id: str, chat_id: str, value: dict
                        ) -> tuple[str, dict | None]:
    """fsm_* 按钮 → 翻译为文本 → 调 car_booking_fsm.advance() → 渲染响应。

    翻译规则：
    - 默认用 value 字段作为 text（"DM2" / "Xavier" / "1小时" 等）
    - fsm_pick_slot 带 slot_idx → 翻译为 "1" / "2" / "3"
    - fsm_direct_by_id 无 value → 用特殊标记符

    2026-06-18 增：fsm_done_records 单独走 records 渲染（不进 FSM），
    避免 FSM 返回 entry_card 后又被 records 覆盖导致用户看不懂。
    """
    action = value.get("action", "")

    if action == "fsm_pick_slot":
        slot_idx = value.get("slot_idx", 1)
        text = str(slot_idx)
    elif action in _FSM_MARKERS:
        text = _FSM_MARKERS[action]
    else:
        # 2026-06-18 form submit（fsm_input_task_form / fsm_input_location_form）：
        # ws_client 已把 form_value[input_name] 扁平化到 value['value']，
        # 直接用作 text（用户实际输入）。
        text = str(value.get("value", ""))

    # fsm_done_records：让用户看自己的预约（不进 FSM；FSM 只负责清 booking 状态）
    if action == "fsm_done_records":
        # 先清 booking state（FSM 在 advance 里清；这里直接调 advance 让它清）
        car_booking_fsm.advance(open_id, _FSM_MARKERS["fsm_done_records"])
        # 再渲染 records —— 走 fetch_user_reservation 工具
        from car_tools.handlers import fetch_user_reservation
        from ocl.tool_guard import set_current_caller, CallerIdentity
        from ocl import identity as _ident
        email = _ident.email_of(open_id)
        set_current_caller(CallerIdentity(openid=open_id, email=email or ""))
        try:
            raw = fetch_user_reservation({})
            result = json.loads(raw) if isinstance(raw, str) else raw
            # 2026-06-18 fix：fetch_user_reservation 返 List[Reservation]（直接 list），
            # 不再是 dict 包 "items"/"data"。兼容两种格式。
            if isinstance(result, dict) and "error" in result:
                return (f"查询预约失败：{result['error']}", None)
            if isinstance(result, list):
                items = result
            elif isinstance(result, dict):
                items = (result.get("items") or result.get("data")
                         or result.get("reservations") or [])
            else:
                items = []
            if not isinstance(items, list):
                items = []
            if not items:
                return ("📋 暂无预约记录",
                        {"text": "📋 您当前没有预约记录。\n\n💡 可以说「约车」开始新预约。"})
            lines = [f"📋 您共有 {len(items)} 条预约：\n"]
            for r in items:
                if not isinstance(r, dict):
                    continue
                vno = r.get("vehicle_no") or r.get("车辆编号") or "?"
                st = r.get("start_time") or r.get("开始时间") or "?"
                et = r.get("end_time") or r.get("结束时间") or "?"
                stt = r.get("status") or r.get("状态") or "未知"
                lines.append(f"• {vno} {st} ~ {et}（{stt}）")
            return ("📋 您的预约", {"text": "\n".join(lines)})
        except Exception as e:
            log.warning("fsm_done_records failed: %s", e)
            return (f"查询预约失败：{e}", None)

    # 调 FSM 主入口
    new_state, response = car_booking_fsm.advance(open_id, text)
    log.info("fsm_button user=%s action=%s new_state=%s", open_id, action, new_state)
    return _render_fsm_response(response)


# ── select_vehicle ─────────────────────────────────────────────────────────

def _handle_select_vehicle(open_id: str, chat_id: str, vehicle_no: str,
                           vehicle_type: str, platform: str, license_plate: str,
                           email: str) -> tuple[str, dict | None]:
    """用户点 [选N] → 写 car_state → 推进 FSM 到 DURATION_CONFIRM 选时段。

    2026-06-18 改：原代码调 _dry_run_reservation 走 LLM 工具路径，用户体验
    断裂（选完车突然要写「开始时间/结束时间/任务名称/地点」）。现在直接推进
    FSM：先 DURATION_CONFIRM（选时段）→ INPUT_TASK（任务）→ INPUT_LOCATION
    （地点）→ CONFIRM（确认）→ COMMIT（提交）。_dry_run_reservation 仍是
    LLM-facing 工具（car_tools/handlers.py 保留），仅在 LLM agent 路径用。
    """
    car_state.save(
        open_id,
        intent="booking",
        vehicle_no=vehicle_no,
        vehicle_type=vehicle_type,
        platform=platform,
        license_plate=license_plate,
    )
    set_current_caller(CallerIdentity(openid=open_id, email=email, mobile=None))
    # 推进 FSM：把 state 切到 DURATION_CONFIRM（vehicle_no 已存）让 FSM 渲染时段选项
    car_state.save(open_id, state="DURATION_CONFIRM")
    new_state, response = car_booking_fsm.advance(open_id, "")
    log.info("select_vehicle_fsm user=%s vehicle_no=%s new_state=%s",
             open_id, vehicle_no, new_state)
    return _render_fsm_response(response)


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
