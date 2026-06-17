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
from datetime import datetime, timedelta

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

# 时段候选上限（spec §5.2）
MAX_SLOT_CANDIDATES = 3
# 单次预约时长上限（spec §5.4）= 8h
MAX_DURATION_MINUTES = 480


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


def _resolve_vehicle_from_text(text: str, candidates: list) -> dict | None:
    """从 candidates 反查车辆（spec §3.4 ⑤：序号 → 完整编号 → 后缀匹配）。

    Args:
        text: 用户文本（如 "1" / "选1" / "PNV003" / "V003"）。
        candidates: 上次查车的车辆列表（snake_case dict）。

    Returns:
        匹配到的车辆 dict，或 None。
    """
    text = (text or "").strip()
    # "选 N" → 去前缀
    if text.startswith("选") and len(text) > 1:
        text = text[1:].strip()
    if not candidates:
        return None
    # 1) 纯数字 → 视作 1-based 索引
    if text.isdigit():
        idx = int(text)
        if 1 <= idx <= len(candidates):
            return candidates[idx - 1]
        # 越界则 fall-through 到后缀匹配（"003" 这种不是索引而是编号段）
    upper = text.upper()
    # 2) 完整匹配（大小写不敏感）
    for v in candidates:
        if (v.get("vehicle_no") or "").upper() == upper:
            return v
    # 3) 后缀匹配（≥3 字符，避免误匹配）
    if len(upper) >= 3:
        for v in candidates:
            if (v.get("vehicle_no") or "").upper().endswith(upper):
                return v
    return None


def _match_slots(vehicle_no: str, duration_minutes: int) -> list[dict]:
    """spec §5.2 模糊匹配：今天起向后生成 ≤3 个候选时段。

    MVP：mock 实现。Task 5/6 接入真实后端时替换。
    行为：
    - duration > 8h → 空列表（spec §5.4）
    - 否则从下个整点开始，间隔 4h 取 3 个
    """
    if duration_minutes <= 0 or duration_minutes > MAX_DURATION_MINUTES:
        return []
    now = datetime.now()
    base = now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    slots: list[dict] = []
    for i in range(MAX_SLOT_CANDIDATES):
        start = base + timedelta(hours=i * 4)
        end = start + timedelta(minutes=duration_minutes)
        slots.append({
            "start": start.strftime("%Y-%m-%d %H:%M"),
            "end": end.strftime("%Y-%m-%d %H:%M"),
            "label": f"{start.strftime('%m-%d %H:%M')} ~ {end.strftime('%H:%M')}",
        })
    return slots


class CarBookingFSM:
    """per-user FSM 实例（无状态；所有状态在 car_state.CarPendingState 里）。"""

    def __init__(self):
        pass


def advance(user_id: str, text: str = "") -> tuple[str, dict]:
    """FSM 主入口：从当前 state 推进。

    Returns: (new_state, response_dict)
    response_dict 形如 {"text": "..."} 或 {"card": {...}} 或 {"buttons": [...]}
    """
    # 防御：卡片回调可能传 None
    if text is None:
        text = ""
    text = text.strip()
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
        if text in VEHICLE_TYPE_BUTTONS:
            chip = _single_chip(text)
            car_state.save(user_id, vehicle_type=text, chip=chip or "")
            if chip:
                return STATE_VEHICLE_ENTRY, {
                    "text": f"已选车型 {text}（{chip} 芯片）。请选择查车方式：",
                    "buttons": [{"text": t, "value": {"action": "fsm_entry", "value": t}}
                               for t in ENTRY_MODE_BUTTONS]
                }
            return STATE_CONFIRM_CHIP, {
                "text": f"已选车型 {text}。该车型支持多个芯片，请选择：",
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

    # SELECT_DURATION：选时长按钮 → 直接串联 SELECT_FROM_LIST（单次 advance 完成查车）
    if current_state == STATE_SELECT_DURATION:
        # 6 个固定时长 → 30/60/120/180/240/480 分钟。"其它" 不在 mapping 中：
        # PR5 会决定 custom-duration 流程（可能走自由文本输入）。
        mapping = {"30分钟": 30, "1小时": 60, "2小时": 120,
                   "3小时": 180, "半天": 240, "1天": 480}
        if text in mapping:
            car_state.save(user_id, duration_minutes=mapping[text])
        elif not text:
            # re-entry: state 已是 SELECT_DURATION + duration_minutes 已存
            if (pending and pending.duration_minutes > 0):
                pass  # fall through 到 SELECT_FROM_LIST
            else:
                return STATE_SELECT_DURATION, {
                    "text": "请先选择用车时长：",
                    "buttons": [{"text": t, "value": {"action": "fsm_select_duration", "value": t}}
                                for t in DURATION_BUTTONS]
                }
        else:
            return STATE_SELECT_DURATION, {
                "text": f"未识别时长「{text}」。请从按钮选择：",
                "buttons": [{"text": t, "value": {"action": "fsm_select_duration", "value": t}}
                            for t in DURATION_BUTTONS]
            }
        # 落到 SELECT_FROM_LIST 处理
        current_state = STATE_SELECT_FROM_LIST

    # SELECT_FROM_LIST：调 fetch_available_vehicles → 渲染表格 + 缓存 last_vehicles
    if current_state == STATE_SELECT_FROM_LIST:
        from car_tools import mcp_client as _mc
        from car_tools import card_builder as _cb

        pending_now = car_state.get(user_id)
        try:
            raw = _mc.get_mcp_client().call("fetch_available_vehicles", {
                "vehicleType": pending_now.vehicle_type,
                "platform": pending_now.chip,
            })
        except Exception as e:
            log.warning("select_from_list fetch failed: %s", e)
            raw = {"items": []}

        # 解析响应：MCP 边界格式可能是 list / {"items": [...]} / {"data": [...]}。
        # car_tools.card_builder.build_vehicles_card 接受 list[dict]（snake_case）。
        vehicles_list: list[dict] = []
        if isinstance(raw, list):
            vehicles_list = raw
        elif isinstance(raw, dict):
            items = raw.get("items") or raw.get("vehicles") or raw.get("data") or []
            if isinstance(items, list):
                # 字段名归一化：camelCase → snake_case（card_builder 期望 snake_case）
                vehicles_list = [_normalize_vehicle_keys(v) for v in items if isinstance(v, dict)]

        # 缓存到 car_state（供 Task 5 文本选车"约第N个"反查）
        car_state.save(user_id,
                       last_vehicles=vehicles_list,
                       last_query={"vehicleType": pending_now.vehicle_type,
                                   "platform": pending_now.chip})

        # 标题：查询条件（spec §5.3）
        ql_parts = []
        if pending_now.vehicle_type:
            ql_parts.append(pending_now.vehicle_type)
        if pending_now.chip:
            ql_parts.append(f"{pending_now.chip}芯片")
        query_label = " · ".join(ql_parts) if ql_parts else None
        card = _cb.build_vehicles_card(vehicles_list, query_label=query_label)
        return STATE_SELECT_FROM_LIST, {
            "text": f"已选时长 {pending_now.duration_minutes}分钟。请从下方车辆列表选车：",
            "card": card,
        }

    # DURATION_CONFIRM ★ 模糊匹配（spec §5.2）
    if current_state == STATE_DURATION_CONFIRM:
        pending_dc = car_state.get(user_id)
        chosen = _resolve_vehicle_from_text(text, pending_dc.last_vehicles or [])
        if not chosen:
            n = len(pending_dc.last_vehicles or [])
            return STATE_DURATION_CONFIRM, {
                "text": f"未识别车辆「{text}」。请选编号（1-{n}）或报车辆编号：",
            }
        # 写 car_state（vehicle_no / vehicle_type / license_plate）
        car_state.save(user_id,
                       vehicle_no=chosen.get("vehicle_no", ""),
                       vehicle_type=chosen.get("vehicle_type", ""),
                       platform=chosen.get("platform", ""),
                       license_plate=chosen.get("license_plate", ""))
        slots = _match_slots(chosen.get("vehicle_no", ""), pending_dc.duration_minutes)
        if not slots:
            return STATE_SELECT_TIME, {
                "text": f"未找到 {pending_dc.duration_minutes} 分钟连续可用时段，"
                        f"请选预设时间或换车：",
                "buttons": [{"text": s, "value": {"action": "fsm_select_time", "value": s}}
                            for s in ["1小时后", "2小时后", "明早9点", "明天下午2点"]]
            }
        return STATE_INPUT_TASK, {
            "text": f"已选 {chosen.get('vehicle_no','')}。可选时段：",
            "buttons": [{"text": s["label"],
                         "value": {"action": "fsm_pick_slot",
                                   "start": s["start"], "end": s["end"]}}
                        for s in slots]
        }

    # SELECT_TIME 兜底（spec §3.3 DC-10 retry）
    if current_state == STATE_SELECT_TIME:
        return STATE_INPUT_TASK, {
            "text": "已记录时段。请输入任务名称：",
        }

    raise NotImplementedError(f"FSM state handler not implemented: {current_state}")


def _normalize_vehicle_keys(v: dict) -> dict:
    """MCP 返回的 camelCase 字段归一化为 snake_case，供 build_vehicles_card 消费。

    build_vehicles_card 期望 vehicle_no / vehicle_type / platform / license_plate / vin。
    MCP 边界可能传 vehicleNo / vehicleType / platform / licensePlate / vin。
    """
    return {
        "vehicle_no":    v.get("vehicleNo") or v.get("vehicle_no", ""),
        "vehicle_type":  v.get("vehicleType") or v.get("vehicle_type", ""),
        "platform":      v.get("platform", ""),
        "license_plate": v.get("licensePlate") or v.get("license_plate", ""),
        "vin":           v.get("vin", ""),
    }
