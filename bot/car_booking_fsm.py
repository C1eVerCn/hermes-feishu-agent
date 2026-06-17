"""车辆预约对话流 FSM（spec §3.2 / §3.3）。

14 状态机：
  START → DIRECT_BY_ID / SELECT_VEHICLE_TYPE → CONFIRM_CHIP → VEHICLE_ENTRY
  → SELECT_DURATION / SELECT_FROM_LIST → DURATION_CONFIRM ★ → SELECT_TIME
  → INPUT_TASK → INPUT_LOCATION → CONFIRM → COMMIT → SUCCESS

LLM 只在 5 处介入（spec §3.4）：SELECT_VEHICLE_TYPE / SELECT_DURATION /
DIRECT_BY_ID / SELECT_FROM_LIST / INPUT_TASK / INPUT_LOCATION 收自由文本时。
所有按钮渲染、时段匹配、查车、提交都是硬编码 MCP 调用，不经 LLM。
"""
import logging

log = logging.getLogger(__name__)


# ── 14 状态常量（spec §3.2） ────────────────────────────────────────────────
STATE_START = "START"
STATE_DIRECT_BY_ID = "DIRECT_BY_ID"
STATE_SELECT_VEHICLE_TYPE = "SELECT_VEHICLE_TYPE"
STATE_CONFIRM_CHIP = "CONFIRM_CHIP"
STATE_VEHICLE_ENTRY = "VEHICLE_ENTRY"
STATE_SELECT_DURATION = "SELECT_DURATION"
STATE_SELECT_FROM_LIST = "SELECT_FROM_LIST"
STATE_DURATION_CONFIRM = "DURATION_CONFIRM"
STATE_SELECT_TIME = "SELECT_TIME"
STATE_INPUT_TASK = "INPUT_TASK"
STATE_INPUT_LOCATION = "INPUT_LOCATION"
STATE_CONFIRM = "CONFIRM"
STATE_COMMIT = "COMMIT"
STATE_SUCCESS = "SUCCESS"

ALL_STATES = frozenset({
    STATE_START, STATE_DIRECT_BY_ID, STATE_SELECT_VEHICLE_TYPE,
    STATE_CONFIRM_CHIP, STATE_VEHICLE_ENTRY, STATE_SELECT_DURATION,
    STATE_SELECT_FROM_LIST, STATE_DURATION_CONFIRM, STATE_SELECT_TIME,
    STATE_INPUT_TASK, STATE_INPUT_LOCATION, STATE_CONFIRM,
    STATE_COMMIT, STATE_SUCCESS,
})


# ── 按钮定义（spec §3.3） ─────────────────────────────────────────────────
VEHICLE_TYPE_BUTTONS = ["DM2", "CT1", "大F车", "CM0", "BM2"]
CHIP_BUTTONS = ["Xavier", "ADCU", "Orin", "Thor"]
ENTRY_MODE_BUTTONS = ["已知编号", "帮我查"]
DURATION_BUTTONS = ["30分钟", "1小时", "2小时", "3小时", "半天", "1天", "其它"]
TASK_HINT_BUTTONS = ["MFF调试", "路测", "数据采集"]
LOCATION_BUTTONS = ["上海", "北京", "广州", "深圳"]


def _entry_card() -> dict:
    """START 状态：入口卡。"""
    return {
        "card": {
            "config": {"wide_screen_mode": True},
            "elements": [
                {"tag": "div", "text": {"tag": "lark_md",
                 "content": "**🚗 车辆预约**\n请选择您要预约的车型，或直接输入车辆编号："}},
                {"tag": "action", "actions": [
                    {"tag": "button", "type": "primary",
                     "text": {"tag": "plain_text", "content": t},
                     "value": {"action": "fsm_select", "value": t}}
                    for t in VEHICLE_TYPE_BUTTONS
                ] + [{"tag": "button", "type": "default",
                       "text": {"tag": "plain_text", "content": "直接输入编号"},
                       "value": {"action": "fsm_direct_by_id"}}]}
            ]
        }
    }


def _single_chip(vehicle_type: str) -> str | None:
    """单芯片车型直接跳 VEHICLE_ENTRY（spec §3.3）；多芯片进 CONFIRM_CHIP。

    Returns: chip 名（单芯片时）或 None（多芯片需用户选）。
    实际 chip 映射在生产环境应来自后端（getCommonDictionary），此处硬编码示例。
    """
    single_chip_map = {"DM2": "Xavier", "CT1": "Orin", "CM0": "Thor", "BM2": "ADCU"}
    return single_chip_map.get(vehicle_type)


class CarBookingFSM:
    """per-user FSM 实例（无状态；所有状态在 car_state.CarPendingState 里）。"""

    def __init__(self):
        pass


def advance(user_id: str, text: str = "") -> tuple[str, dict]:
    """FSM 主入口：从当前 state 推进。

    Returns: (new_state, response_dict)
    response_dict 形如 {"text": "..."} 或 {"card": {...}} 或 {"buttons": [...]}
    """
    from bot import car_state
    pending = car_state.get(user_id)
    current_state = pending.state if pending else STATE_START
    # 校验：car_state 里持久化出来的 state 必须是已知状态名（防 typo）
    if current_state not in ALL_STATES and current_state != "":
        log.warning("fsm_advance unknown state user=%s state=%r", user_id, current_state)
        current_state = STATE_START
    log.info("fsm_advance user=%s state=%s text=%r", user_id, current_state, text[:40])

    # 入口状态：未开始 → 渲染入口卡
    if current_state == STATE_START:
        return STATE_SELECT_VEHICLE_TYPE, _entry_card()

    # SELECT_VEHICLE_TYPE：收车型按钮或 LLM 抽取的车型
    if current_state == STATE_SELECT_VEHICLE_TYPE:
        vehicle_type = text.strip()
        if vehicle_type in VEHICLE_TYPE_BUTTONS:
            chip = _single_chip(vehicle_type)
            car_state.save(user_id, vehicle_type=vehicle_type, chip=chip or "")
            if chip:
                return STATE_VEHICLE_ENTRY, {
                    "text": f"已选车型 {vehicle_type}（{chip} 芯片）。请选择查车方式：",
                    "buttons": [{"text": t, "value": {"action": "fsm_entry", "value": t}}
                               for t in ENTRY_MODE_BUTTONS]
                }
            return STATE_CONFIRM_CHIP, {
                "text": f"已选车型 {vehicle_type}。该车型支持多个芯片，请选择：",
                "buttons": [{"text": t, "value": {"action": "fsm_select_chip", "value": t}}
                           for t in CHIP_BUTTONS]
            }
        return STATE_SELECT_VEHICLE_TYPE, {
            "text": f"未识别车型「{text}」。请从按钮选择：",
            "buttons": [{"text": t, "value": {"action": "fsm_select_type", "value": t}}
                       for t in VEHICLE_TYPE_BUTTONS]
        }

    # CONFIRM_CHIP：收芯片按钮
    if current_state == STATE_CONFIRM_CHIP:
        chip = text.strip()
        if chip in CHIP_BUTTONS:
            car_state.save(user_id, chip=chip)
            return STATE_VEHICLE_ENTRY, {
                "text": f"已选芯片 {chip}。请选择查车方式：",
                "buttons": [{"text": t, "value": {"action": "fsm_entry", "value": t}}
                           for t in ENTRY_MODE_BUTTONS]
            }
        return STATE_CONFIRM_CHIP, {
            "text": f"未识别芯片「{text}」。请从按钮选择：",
            "buttons": [{"text": t, "value": {"action": "fsm_select_chip", "value": t}}
                       for t in CHIP_BUTTONS]
        }

    # VEHICLE_ENTRY：选"已知编号" / "帮我查"
    if current_state == STATE_VEHICLE_ENTRY:
        text_clean = text.strip()
        if text_clean == "已知编号":
            return STATE_DIRECT_BY_ID, {
                "text": "请直接输入车辆编号（如 SNV018）：",
            }
        if text_clean == "帮我查":
            return STATE_SELECT_DURATION, {
                "text": "请选择您需要用车的时长：",
                "buttons": [{"text": t, "value": {"action": "fsm_select_duration", "value": t}}
                           for t in DURATION_BUTTONS]
            }
        return STATE_VEHICLE_ENTRY, {
            "text": "请选择查车方式：",
            "buttons": [{"text": t, "value": {"action": "fsm_entry", "value": t}}
                       for t in ENTRY_MODE_BUTTONS]
        }

    # SELECT_DURATION：选时长按钮（Task 4 完整实现 + 接入 SELECT_FROM_LIST）
    if current_state == STATE_SELECT_DURATION:
        mapping = {"30分钟": 30, "1小时": 60, "2小时": 120,
                   "3小时": 180, "半天": 240, "1天": 480}
        if text in mapping:
            car_state.save(user_id, duration_minutes=mapping[text])
            return STATE_SELECT_FROM_LIST, {
                "text": f"已记录时长 {text}。正在查可用车辆…",
            }
        return STATE_SELECT_DURATION, {
            "text": "请选择时长：",
            "buttons": [{"text": t, "value": {"action": "fsm_select_duration", "value": t}}
                       for t in DURATION_BUTTONS]
        }

    raise NotImplementedError(f"FSM state handler not implemented: {current_state}")
