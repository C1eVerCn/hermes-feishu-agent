"""车辆预约对话流 FSM（spec §3.2 / §3.3）。

13 状态机（2026-06-18 review：删 VEHICLE_ENTRY，新流程 CONFIRM_CHIP 直接进 SELECT_DURATION）：
  START → DIRECT_BY_ID / SELECT_VEHICLE_TYPE → CONFIRM_CHIP
  → SELECT_DURATION / SELECT_FROM_LIST → DURATION_CONFIRM ★ → SELECT_TIME
  → INPUT_TASK → INPUT_LOCATION → CONFIRM → COMMIT → SUCCESS

LLM 只在 5 处介入（spec §3.4）：SELECT_VEHICLE_TYPE / SELECT_DURATION /
DIRECT_BY_ID / SELECT_FROM_LIST / INPUT_TASK / INPUT_LOCATION 收自由文本时。
所有按钮渲染、时段匹配、查车、提交都是硬编码 MCP 调用，不经 LLM。
"""
import json
import logging
import re
from datetime import datetime, timedelta

from bot import car_state
from car_tools import normalizers
from car_tools.card_builder import _button_row

log = logging.getLogger(__name__)


# ── 14 状态常量（spec §3.2） ────────────────────────────────────────────────
STATE_START = "START"
STATE_DIRECT_BY_ID = "DIRECT_BY_ID"
STATE_SELECT_VEHICLE_TYPE = "SELECT_VEHICLE_TYPE"
STATE_CONFIRM_CHIP = "CONFIRM_CHIP"
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
    STATE_CONFIRM_CHIP, STATE_SELECT_DURATION,
    STATE_SELECT_FROM_LIST, STATE_DURATION_CONFIRM, STATE_SELECT_TIME,
    STATE_INPUT_TASK, STATE_INPUT_LOCATION, STATE_CONFIRM,
    STATE_COMMIT, STATE_SUCCESS,
})


# ── 按钮定义（spec §3.3） ─────────────────────────────────────────────────
# 车型细分：fmp-app vehicle 表 vehicle_type_detail 列实际值（15 种 active）
# 来自 fmp-mysql SELECT DISTINCT vehicle_type_detail FROM vehicle
#   WHERE del_flag=0 AND status=1
# 按家族分组（每行一类：427-系列 / A-系列 / B-系列 / C-系列 / D-系列 / 大F车）
VEHICLE_TYPE_BUTTONS = [
    # 427-系列
    "427-M0", "427-M1",
    # A-系列 (Acar)
    "AM2", "AM3",
    # B-系列 (Bcar)
    "BM0", "BM1", "BM2",
    # C-系列 (Ccar)
    "CM0", "CM2", "CT1", "CT2",
    # D-系列 (Dcar)
    "DM0", "DM1", "DM2",
    # 大F车
    "?Fcar",
]
CHIP_BUTTONS = ["Xavier", "ADCU", "Orin", "Thor"]
ENTRY_MODE_BUTTONS = ["已知编号", "帮我查"]
TASK_HINT_BUTTONS = ["MFF调试", "路测", "数据采集", "其它"]
# 2026-06-18 改：地点按钮精简到 4 个常用 + 1 个「其它」走自定义输入。
# 新人友好：直接列常用地点 + 兜底自由输入（避免误判"不在列表里"）。
LOCATION_BUTTONS = ["上海", "塔山路", "创新港", "张江", "其它"]

# 时段候选上限（spec §5.2）—— 2026-06-18 改：用户希望看所有可用时段而非 3 个 mock 候选
# 改成 6 个跨 24h（2 小时间隔），让用户有更多选择
MAX_SLOT_CANDIDATES = 6
SLOT_INTERVAL_HOURS = 2
# 单次预约时长（spec §5.4）= 8h 上限，30min 步进
MIN_DURATION_MINUTES = 30
DURATION_STEP_MINUTES = 30
MAX_DURATION_MINUTES = 480  # 8h
DEFAULT_DURATION_MINUTES = 30  # 初始 30 分钟

# 卡片回调按钮的特殊文本标记（card_action_handler._handle_fsm_button 用）
# 2026-06-18 review 删 _FSM_DIRECT_BY_ID_MARKER：orphan marker，emit 端和 consume 端
# 都不存在；用户报编号是 START 状态文本路径（"请直接输入车辆编号"），不需要 callback。
_FSM_DUR_MINUS_MARKER = "__fsm_dur_minus__"
_FSM_DUR_PLUS_MARKER = "__fsm_dur_plus__"
_FSM_DUR_CONFIRM_MARKER = "__fsm_dur_confirm__"
_FSM_KNOWN_YES_MARKER = "__fsm_known_yes__"
_FSM_KNOWN_NO_MARKER = "__fsm_known_no__"
# 「其它」按钮 → 自定义文本输入路径（2026-06-18 新增）
_FSM_TASK_OTHER_MARKER = "__fsm_task_other__"
_FSM_LOCATION_OTHER_MARKER = "__fsm_location_other__"
# SUCCESS 卡快捷按钮 → 2026-06-18 新增
_FSM_DONE_MORE_MARKER = "__fsm_done_more__"
_FSM_DONE_RECORDS_MARKER = "__fsm_done_records__"


def _format_duration(minutes: int) -> str:
    """分钟数 → 'X 小时 Y 分钟' 友好显示。"""
    if minutes < 60:
        return f"{minutes} 分钟"
    h, m = divmod(minutes, 60)
    return f"{h} 小时" + (f" {m} 分钟" if m else "")


# 「其它」按钮走特殊 action → card_action_handler 翻译成对应 marker → FSM 识别后
# 渲染纯文本输入提示（不再展示示例按钮）。普通按钮用 fsm_input_task / fsm_input_location。
def _fsm_input_task_action(value: str) -> str:
    return "fsm_input_task_other" if value == "其它" else "fsm_input_task"


def _fsm_input_location_action(value: str) -> str:
    return "fsm_input_location_other" if value == "其它" else "fsm_input_location"


def _card_wrap(body_elements: list[dict], *, wide: bool = True) -> dict:
    """Card 2.0 schema 包装（2026-06-18 review：select_static 是 Card 2.0
    特性，飞书 Card 1.0 schema 会静默忽略 select_static 元素；之前 _type_card
    /_chip_card 用了 1.0 结构 → 用户只看到 div 文字，看不到下拉）。

    Card 2.0 也不支持 `tag: "action"` 容器（旧 Card 1.0 用 `{"tag":"action","actions":[btn1,btn2]}`
    包多个 button；Card 2.0 要求 button 直接作为 body.elements 元素）—— 飞书
    server 返回 ErrCode 200861 "unsupported tag action"。本 helper 自动展平
    action 容器，调用方写 Card 1.0 风格也能正确发 Card 2.0。

    2026-06-18 修复：之前返回 `{"card": <v2>}` 多包一层，与 _card_base 不一致
    也与飞书 raw callback 期望冲突。飞书回调 raw `data` 期望直接是 Card 2.0 dict
    （{schema, config, body}），多一层 "card" 键会导致飞书找不到 schema 字段，
    卡片内的 button / select_static 都不渲染。改为直接返回 Card 2.0 dict（与
    _card_base 一致）。
    """
    flat: list[dict] = []
    for e in body_elements:
        if isinstance(e, dict) and e.get("tag") == "action" and isinstance(e.get("actions"), list):
            flat.extend(e["actions"])
        else:
            flat.append(e)
    card: dict = {
        "schema": "2.0",
        "config": {},
        "body": {"elements": flat},
    }
    if wide:
        card["config"]["wide_screen_mode"] = True
    return card


def _entry_card() -> dict:
    """START 状态：入口卡。问"您是否知道要约的车辆编号？"，[知道] [不知道] 二选一。

    2026-06-18 引导性增强：明确两条路径的预期行为（老用户/新人各选其一），
    避免新人被"是否知道编号"卡住不知道选什么。
    """
    return _card_wrap([
        {"tag": "div", "text": {"tag": "lark_md",
         "content": ("**🚗 车辆预约**\n\n"
                     "📌 请选择您的约车方式：\n"
                     "  • **知道** —— 您已确定要约哪辆车（记得编号如 SNV018）\n"
                     "  • **不知道** —— 想先看看有哪些车可用\n\n"
                     "💡 整个流程约 1 分钟完成（车型→芯片→时长→选车→时段→任务→地点→确认）\n"
                     "🚪 任何步骤可说「算了/取消」随时退出")}},
        _button_row([
            {"tag": "button", "type": "primary",
             "text": {"tag": "plain_text", "content": "✅ 知道（报编号直接约）"},
             "value": {"action": "fsm_known_yes"}},
            {"tag": "button", "type": "default",
             "text": {"tag": "plain_text", "content": "🔍 不知道（帮我查）"},
             "value": {"action": "fsm_known_no"}},
        ]),
    ])


def _select_card(title: str, placeholder: str, options: list, action: str) -> dict:
    """通用 select_static 卡片骨架（Card 2.0 schema）。

    options 是 list[str]（选项的 value 与 text 同名）。lark-oapi CardBuilder.select
    （verified: channel/card/builder.py:188-208）输出也是这种结构，**不**带
    initial_option 字段（Card 2.0 select_static 不支持）。value 字段携带
    action 标识，callback 时由 feishu/ws_client._extract_card_action 归一化
    option → value['value']。
    """
    return _card_wrap([
        {"tag": "div", "text": {"tag": "lark_md", "content": title}},
        {"tag": "select_static",
         "placeholder": {"tag": "plain_text", "content": placeholder},
         "options": [
             {"text": {"tag": "plain_text", "content": o}, "value": o}
             for o in options
         ],
         "value": {"action": action}}
    ])


def _type_card() -> dict:
    """SELECT_VEHICLE_TYPE：单选下拉（替代原 15 个 button）。

    2026-06-18 引导性增强：标题加步骤进度提示（第 1/8 步）+ 用途说明（按车型查车）。
    """
    return _select_card(
        ("**🚗 第 1/8 步：选择车型**\n\n"
         "📋 列表是车辆型号细分（DM0/CT1/Acar 等），用于查询可用车辆\n"),
        "点击选择车型",
        VEHICLE_TYPE_BUTTONS,
        "fsm_select_type",
    )


def _chip_card(vehicle_type_detail: str = "") -> dict:
    """CONFIRM_CHIP：单选下拉（4 个芯片）。vehicle_type_detail 用于标题回显
    已选车型；review finding 1 修复：原 f-string `{''}` 永远空，现传真实值。

    2026-06-18 引导性增强：加步骤进度 + 芯片平台用途说明。
    """
    detail_suffix = f" {vehicle_type_detail}" if vehicle_type_detail else ""
    return _select_card(
        (f"**🧠 第 2/8 步：选择芯片平台**（已选车型{detail_suffix}）\n\n"
         "🔧 芯片平台是车上的计算单元（Xavier/Orin 较新算力强；ADCU/Thor 专用场景）\n"),
        "点击选择芯片",
        CHIP_BUTTONS,
        "fsm_select_chip",
    )


def _duration_card(pending) -> dict:
    """SELECT_DURATION：当前时长显示 + [-30] [+30] [确认] 按钮。

    2026-06-18 引导性增强：加步骤进度 + 步进规则说明 + 范围限制。
    """
    cur = pending.duration_minutes if pending and pending.duration_minutes > 0 else DEFAULT_DURATION_MINUTES
    return _card_wrap([
        {"tag": "div", "text": {"tag": "lark_md",
         "content": (f"**⏱️ 第 3/8 步：选择用车时长**\n\n"
                     f"📌 当前：**{_format_duration(cur)}**\n"
                     f"📏 范围：{_format_duration(MIN_DURATION_MINUTES)} ~ {_format_duration(MAX_DURATION_MINUTES)} "
                     f"（按 {DURATION_STEP_MINUTES} 分钟步进）\n"
                     f"💡 用 [−30 分] / [+30 分] 调整时长，默认 30 分钟\n"
                     f"✅ 调好后点 [确认] 进入下一步")}},
        _button_row([
            {"tag": "button", "type": "default",
             "text": {"tag": "plain_text", "content": "−30 分"},
             "value": {"action": "fsm_dur_minus"}},
            {"tag": "button", "type": "default",
             "text": {"tag": "plain_text", "content": "+30 分"},
             "value": {"action": "fsm_dur_plus"}},
            {"tag": "button", "type": "primary",
             "text": {"tag": "plain_text", "content": "✅ 确认"},
             "value": {"action": "fsm_dur_confirm"}},
        ]),
    ])


def _do_commit(user_id: str) -> tuple[str, dict]:
    """调 _commit_single_vehicle_reservation 提交预约。2026-06-18 抽公共函数
    让 CONFIRM "确认" 和 STATE_COMMIT 分支复用（同一次 fsm.advance 调用里完成，
    避免切到 STATE_COMMIT 等下一次 advance 时用户没发消息卡住）。
    """
    from car_tools import handlers as _h
    from ocl.tool_guard import set_current_caller, CallerIdentity
    from ocl import identity as _identity
    # 2026-06-18 fix：fsm 同步路径未注入 CallerIdentity，booking_mcp_server
    # 收到 emailAddress='' → 返 {"code":400, "data":None} → normalize 失败
    # "data 字段不是 dict: NoneType"。必须先注入。
    pending_c = car_state.get(user_id)
    user_email = _identity.email_of(user_id)
    set_current_caller(CallerIdentity(openid=user_id, email=user_email or ""))
    try:
        raw = _h._commit_single_vehicle_reservation({
            "vehicleNo":          pending_c.vehicle_no,
            "vehicleType":        pending_c.vehicle_type,
            "vehicleTypeDetail":  pending_c.vehicle_type_detail,
            "platform":           pending_c.chip,
            "startTime":          pending_c.start_time,
            "endTime":            pending_c.end_time,
            "taskName":           pending_c.task_name,
            "location":           pending_c.location,
        })
        result = json.loads(raw) if isinstance(raw, str) else raw
        if isinstance(result, dict) and "error" in result:
            return STATE_START, {"text": f"提交失败：{result['error']}"}
        # 持久化 reservation → applicant 映射（spec §7.2 通知）
        from bot import reservation_store
        key = f"car|{pending_c.vehicle_no}|{pending_c.start_time}"
        reservation_store.save(key, user_id, "", pending_c.vehicle_no,
                              pending_c.start_time, pending_c.end_time,
                              pending_c.task_name)
    except Exception as e:
        log.exception("commit failed")
        return STATE_START, {"text": f"提交异常：{e}"}
    return STATE_SUCCESS, _success_card(user_id)


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
    """spec §5.2 模糊匹配：从"现在 + 30min"起向后生成 MAX_SLOT_CANDIDATES 个候选时段，
    间隔 SLOT_INTERVAL_HOURS 小时。

    2026-06-18 review：原 spec 3 个 / 4 小时间隔 / 从下个整点起 → 改成 6 个 / 2 小时
    间隔 / 从"现在+30min"起（不等下个整点，让 slot 立刻可用，且时段是连续的）。
    MVP mock；Task 5/6 接入真实后端时替换为查 fmp 真可用时段。
    行为：
    - duration > 8h → 空列表（spec §5.4）
    """
    if duration_minutes <= 0 or duration_minutes > MAX_DURATION_MINUTES:
        return []
    now = datetime.now()
    # 从"现在 + 30min"起，向上取整到 30min（用户能立即选一个 slot 试用）
    base = now.replace(second=0, microsecond=0) + timedelta(minutes=30)
    base = base.replace(minute=(base.minute // 30) * 30)
    slots: list[dict] = []
    for i in range(MAX_SLOT_CANDIDATES):
        start = base + timedelta(hours=i * SLOT_INTERVAL_HOURS)
        end = start + timedelta(minutes=duration_minutes)
        slots.append({
            "start": start.strftime("%Y-%m-%d %H:%M"),
            "end": end.strftime("%Y-%m-%d %H:%M"),
            "label": f"{start.strftime('%m-%d %H:%M')} ~ {end.strftime('%H:%M')}",
        })
    return slots


def _resolve_slot_from_text(text: str, slots: list[dict]) -> dict | None:
    """DURATION_CONFIRM 内反查时段。

    接受：
    - "1" / "2" / "3"（1-based 索引）
    - "选1" / "选2"
    - 完整 start 时间 "2026-06-17 14:00"（从 slots 找匹配）
    """
    text = (text or "").strip()
    if text.startswith("选") and len(text) > 1:
        text = text[1:].strip()
    if not slots:
        return None
    if text.isdigit():
        idx = int(text)
        if 1 <= idx <= len(slots):
            return slots[idx - 1]
        return None
    # 完整 start 时间匹配
    for s in slots:
        if s.get("start") == text:
            return s
    return None


# 车辆编号格式：字母 2-5 个 + 数字 3-6 个（spec §3.3 D-1）
_VEHICLE_NO_RE = re.compile(r"^[A-Z]{2,5}\d{3,6}$")


# LLM 抽取的 stub：MVP 简化版。后续 Task 6 替换为真 LLM 调用。
def _llm_extract_task(text: str) -> dict:
    """spec §3.4 ④ LLM 抽 taskName。不补全、不修改（spec §4.2 严禁）。"""
    cleaned = (text or "").strip()
    return {"task_name": cleaned}


def _llm_extract_location(text: str) -> dict:
    """spec §3.4 ⑤ LLM 抽 location。"""
    cleaned = (text or "").strip()
    return {"location": cleaned}


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

    new_state, response = _advance_inner(user_id, text, current_state, pending)
    # 持久化新 state 到 car_state（除非返回 START — START 表示"未挂起"，保持原状态）
    if new_state != STATE_START:
        car_state.save(user_id, state=new_state)
    # 统一 response 形状为 {"text"?: ..., "card"?: <v2_dict>|_build_vehicles_card 结果, "buttons"?: ...}。
    # _advance_inner 状态分支：
    #   (a) 直接返回 Card 2.0 dict（来自 _entry_card/_type_card/_chip_card/_duration_card/
    #       _confirm_card/_success_card 这些 _card_wrap 系，或 _cb.build_*_card 卡构建器）
    #   (b) 返回 {"text":..., "buttons":...} 简单 dict（需要 _render_fsm_response 拼 Card 2.0）
    #   (c) 返回 {"text":..., "card": <v2>}/{"text":..., **_card_wrap(...)} 已是正确形状
    # 包装 (a) 为 {"card": <v2>}；(b) 和 (c) 原样透传。判定用顶层 "schema" == "2.0"。
    if isinstance(response, dict) and response.get("schema") == "2.0" and "card" not in response:
        response = {"card": response}
    return new_state, response


def _advance_inner(user_id: str, text: str, current_state: str, pending) -> tuple[str, dict]:
    """内部 advance 逻辑：返回 (new_state, response)。不持久化 state。"""

    # 全局重置：用户在任何状态说"我想约车"等约车意图时，清状态回到入口
    BOOKING_INTENT_PHRASES = ("我想约车", "我要约车", "帮我约车", "约车", "预约车")
    if text in BOOKING_INTENT_PHRASES and current_state != STATE_START:
        car_state.clear(user_id)
        return STATE_START, _entry_card()

    # 2026-06-18 全局 escape：用户在任何"挂起"状态说"算了/取消/退出/不约了"时清 state
    # 回 START（"等待用户输入"的状态如 DURATION_CONFIRM / INPUT_TASK /
    # INPUT_LOCATION / CONFIRM 会因非相关输入被误判为"未识别时段"等）——
    # 截图 19/20 显示用户发"查询我的审批记录"被误判为"未识别时段"，让用户
    # 能 escape 后重发查询更自然。
    ESCAPE_PHRASES = ("算了", "取消", "退出", "不约了", "放弃", "不选了")
    if text.strip() in ESCAPE_PHRASES and current_state not in ("", STATE_START):
        car_state.clear(user_id)
        return STATE_START, _entry_card()

    # 入口状态：未开始 → 渲染"知道编号吗"问询卡
    # 关键：渲染入口卡后保持 state=START（不切到 SELECT_VEHICLE_TYPE），
    # 这样用户点"不知道/知道"按钮回调时仍在 START 状态，marker 能被正确分发。
    if current_state == STATE_START:
        # 优先识别"报编号"快捷路径（用户在 START 直接报编号）
        if text and _VEHICLE_NO_RE.match(text.strip().upper()):
            car_state.save(user_id, vehicle_no=text.strip().upper())
            return STATE_SELECT_DURATION, _duration_card(car_state.get(user_id))
        # 收到"知道"按钮回调 → DIRECT_BY_ID
        if text == _FSM_KNOWN_YES_MARKER:
            return STATE_DIRECT_BY_ID, {
                "text": "请输入车辆编号（如 SNV018 / PNV000）：",
            }
        # 收到"不知道"按钮回调 → 展示车型卡
        if text == _FSM_KNOWN_NO_MARKER:
            return STATE_SELECT_VEHICLE_TYPE, _type_card()
        # 默认：渲染问询卡（保持 state=START）
        return STATE_START, _entry_card()

    # SELECT_VEHICLE_TYPE：收车型细分按钮（用户从"不知道"路径过来）
    if current_state == STATE_SELECT_VEHICLE_TYPE:
        if text in VEHICLE_TYPE_BUTTONS:
            # 细分（如 DM0/CM0/427-M1）存到 vehicle_type_detail 字段
            car_state.save(user_id, vehicle_type_detail=text)
            # 统一用 _chip_card 渲染（与"重渲染"路径一致），UI 不分叉
            # 2026-06-18 fix：之前用 ** 展开 {"card": <v2>} 会把 card 内的 schema/
            # config/body 三个键混进 response 顶层，导致飞书 raw 回调 data 字段
            # 拿到错误结构（"未识别车型「DM0」" 等场景会复现）。改用显式 "card" 键
            # 包 card_dict 本身。
            return STATE_CONFIRM_CHIP, {
                "text": f"已选车型 {text}。请选择芯片平台：",
                "card": _chip_card(vehicle_type_detail=text),
            }
        return STATE_SELECT_VEHICLE_TYPE, {
            "text": f"未识别车型「{text}」。请从下拉选择：",
            "card": _type_card(),
        }

    # CONFIRM_CHIP：收芯片按钮 → 直接进 SELECT_DURATION（车型+芯片+时长都明确后才查车）
    if current_state == STATE_CONFIRM_CHIP:
        chip = text.strip()
        if chip in CHIP_BUTTONS:
            car_state.save(user_id, chip=chip)
            pending_now = car_state.get(user_id)
            if not pending_now.duration_minutes:
                car_state.save(user_id, duration_minutes=DEFAULT_DURATION_MINUTES)
            return STATE_SELECT_DURATION, _duration_card(car_state.get(user_id))
        # 未知输入（含旧按钮残留 / 文本乱打）→ 错误回显 + 重渲染下拉卡
        # review finding 4 修复：原代码删了"未识别芯片「{text}」"错误信息，
        # 用户看不到自己输错了什么。这里恢复文本反馈 + 卡片（双重显示）。
        pending_now = car_state.get(user_id)
        return STATE_CONFIRM_CHIP, {
            "text": f"未识别芯片「{chip}」。请从下拉选择：",
            "card": _chip_card(vehicle_type_detail=pending_now.vehicle_type_detail if pending_now else ""),
        }

    # SELECT_DURATION：±30min 按钮选择器（30~480 min，30 步进）
    if current_state == STATE_SELECT_DURATION:
        cur = (pending.duration_minutes if pending and pending.duration_minutes > 0
               else DEFAULT_DURATION_MINUTES)
        if text == _FSM_DUR_MINUS_MARKER:
            if cur <= MIN_DURATION_MINUTES:
                return STATE_SELECT_DURATION, {
                    "text": f"已是最小时长 {_format_duration(MIN_DURATION_MINUTES)}，无法再减。",
                    "card": _duration_card(car_state.get(user_id)),
                }
            new_dur = max(MIN_DURATION_MINUTES, cur - DURATION_STEP_MINUTES)
            car_state.save(user_id, duration_minutes=new_dur)
            return STATE_SELECT_DURATION, _duration_card(car_state.get(user_id))
        if text == _FSM_DUR_PLUS_MARKER:
            if cur >= MAX_DURATION_MINUTES:
                return STATE_SELECT_DURATION, {
                    "text": f"已是最大时长 {_format_duration(MAX_DURATION_MINUTES)}，无法再加。",
                    "card": _duration_card(car_state.get(user_id)),
                }
            new_dur = min(MAX_DURATION_MINUTES, cur + DURATION_STEP_MINUTES)
            car_state.save(user_id, duration_minutes=new_dur)
            return STATE_SELECT_DURATION, _duration_card(car_state.get(user_id))
        if text == _FSM_DUR_CONFIRM_MARKER:
            # 已选时长 → 根据是否已选车辆决定下一态
            # 已知编号路径（vehicle_no 已存）→ 直接进 DURATION_CONFIRM 走时段匹配
            # 不知道路径（vehicle_no 空）→ fall through 到 SELECT_FROM_LIST 查车
            if pending and pending.vehicle_no:
                return STATE_DURATION_CONFIRM, {
                    "text": f"已选 {pending.vehicle_no}，时长 {cur} 分钟。正在匹配可用时段…",
                }
            # fall through 到下面的 SELECT_FROM_LIST
            current_state = STATE_SELECT_FROM_LIST
        else:
            # 任何其他文本（包括 re-entry）→ 重渲染 duration_card
            return STATE_SELECT_DURATION, _duration_card(car_state.get(user_id))

    # SELECT_FROM_LIST：调 fetch_available_vehicles → 渲染表格 + 缓存 last_vehicles
    if current_state == STATE_SELECT_FROM_LIST:
        from car_tools import mcp_client as _mc
        from car_tools import card_builder as _cb

        pending_now = car_state.get(user_id)
        try:
            raw = _mc.get_mcp_client().call("fetch_available_vehicles", {
                # vehicleTypeDetail（如 DM0/CM0/427-M1）是 fmp-app SQL 的过滤字段
                # vehicleType（大类）保持兼容（如果细分为空也能按大类筛）
                "vehicleTypeDetail": pending_now.vehicle_type_detail,
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
                vehicles_list = [normalizers._vehicle_to_card_dict(v) for v in items if isinstance(v, dict)]

        # 缓存到 car_state（供 Task 5 文本选车"约第N个"反查）
        car_state.save(user_id,
                       last_vehicles=vehicles_list,
                       last_query={"vehicleTypeDetail": pending_now.vehicle_type_detail,
                                   "vehicleType": pending_now.vehicle_type,
                                   "platform": pending_now.chip})

        # 标题：查询条件（spec §5.3）— 优先显示细分，再显示大类
        ql_parts = []
        if pending_now.vehicle_type_detail:
            ql_parts.append(pending_now.vehicle_type_detail)
        elif pending_now.vehicle_type:
            ql_parts.append(pending_now.vehicle_type)
        if pending_now.chip:
            ql_parts.append(f"{pending_now.chip}芯片")
        query_label = " · ".join(ql_parts) if ql_parts else None
        card = _cb.build_vehicles_card(vehicles_list, query_label=query_label)

        # 2026-06-18 fix：区分"有车可选" vs "该车型×该芯片无车"两种场景，
        # 避免"已选时长...请选车"误导用户，并给具体可换的芯片建议。
        # review finding 5 修复：alt_chips 用 CHIP_BUTTONS 而不是硬编码 4 元组，
        # 单一事实源，新增芯片时自动同步。
        # 2026-06-18 引导性增强：加步骤进度 + 选车方式说明（按钮/报编号 二选一）。
        if vehicles_list:
            text = (f"**🚗 第 4/8 步：选择车辆**（共 {len(vehicles_list)} 辆可用）\n\n"
                    f"📋 您可以：\n"
                    f"  • 点下方 [选 N] 按钮快速选中（推荐新手）\n"
                    f"  • 或直接报车辆编号（如 PNV001）\n"
                    f"💡 时长：{pending_now.duration_minutes} 分钟")
        else:
            alt_chips = [c for c in CHIP_BUTTONS if c != pending_now.chip]
            text = (
                f"📋 **第 4/8 步：选择车辆**（暂无）\n\n"
                f"❌ 已选 {pending_now.vehicle_type_detail or pending_now.vehicle_type} · "
                f"{pending_now.chip or '未选芯片'} 当前没有可用车辆。\n\n"
                f"💡 建议方案（按推荐度排序）：\n"
                f"  ① 换芯片平台（如 {alt_chips[0] if alt_chips else '其他芯片'}）\n"
                f"  ② 换个车型重新查\n"
                f"  ③ 换个时长试试\n\n"
                f"🚪 说「算了/取消」可退出本次约车"
            )
        return STATE_SELECT_FROM_LIST, {
            "text": text,
            "card": card,
        }

    # DURATION_CONFIRM ★ 模糊匹配（spec §5.2）
    if current_state == STATE_DURATION_CONFIRM:
        pending_dc = car_state.get(user_id)
        # 0) text 为空：FSM 重新渲染时段选项（不显示"未识别"）
        #    触发场景：card_action_handler._handle_select_vehicle 写完 vehicle_no
        #    + state=DURATION_CONFIRM 后调 advance("")，期望返回时段选项按钮。
        if not text:
            slots = pending_dc.last_slots or _match_slots(
                pending_dc.vehicle_no, pending_dc.duration_minutes)
            if not slots:
                return STATE_SELECT_TIME, {
                    "text": ("**⏰ 第 5/8 步：选择时段**（预设时间）\n\n"
                             "📋 该车辆当前没有连续可用时段，请选下面 4 个预设时间之一：\n"
                             "💡 或返回上一步换辆车"),
                    "buttons": [{"text": s, "value": {"action": "fsm_select_time", "value": s}}
                                for s in ["1小时后", "2小时后", "明早9点", "明天下午2点"]],
                }
            # 2026-06-18：6+ 个时段改用 select_static 下拉（移动端友好，避免横排
            # 按钮在窄屏上溢出）。option 文本是 "MM-DD HH:MM ~ HH:MM"，value 是
            # 完整 start_time 字符串（card_action_handler 不再需要特殊翻译）。
            return STATE_DURATION_CONFIRM, {
                "text": (f"**⏰ 第 5/8 步：选择用车时段**\n\n"
                         f"📋 已选车辆：**{pending_dc.vehicle_no}**\n"
                         f"⏱️ 用车时长：{_format_duration(pending_dc.duration_minutes)}\n"
                         f"💡 下方下拉显示从「现在 + 30 分钟」起的 {len(slots)} 个候选时段\n"
                         f"   选中后时段会标在调度系统里"),
                "card": _select_card(
                    title=f"⏰ 第 5/8 步：选择时段（{pending_dc.vehicle_no}）",
                    placeholder="点击选择时段",
                    options=[s["start"] for s in slots],
                    action="fsm_pick_slot",
                ),
            }
        # 1) 如果 vehicle_no 还没定（用户刚到 DURATION_CONFIRM）→ 解析选车
        if not pending_dc.vehicle_no:
            chosen = _resolve_vehicle_from_text(text, pending_dc.last_vehicles or [])
            if not chosen:
                n = len(pending_dc.last_vehicles or [])
                return STATE_DURATION_CONFIRM, {
                    "text": (f"❌ 未识别车辆「{text}」。\n\n"
                             f"💡 您可以：\n"
                             f"  • 点击下方车辆卡上的 [选 N] 按钮（推荐新手）\n"
                             f"  • 或直接报车辆编号（1-{n} 的序号 / PNVxxx 完整编号）\n"
                             f"🚪 说「算了」可返回车型选择"),
                }
            car_state.save(user_id,
                           vehicle_no=chosen.get("vehicle_no", ""),
                           vehicle_type=chosen.get("vehicle_type", ""),
                           platform=chosen.get("platform", ""),
                           license_plate=chosen.get("license_plate", ""))
            slots = _match_slots(chosen.get("vehicle_no", ""), pending_dc.duration_minutes)
            if not slots:
                return STATE_SELECT_TIME, {
                    "text": (f"⚠️ 未找到 {_format_duration(pending_dc.duration_minutes)} 连续可用时段\n\n"
                             f"📋 请选下面 4 个预设时间之一，或换辆车："),
                    "buttons": [{"text": s, "value": {"action": "fsm_select_time", "value": s}}
                                for s in ["1小时后", "2小时后", "明早9点", "明天下午2点"]]
                }
            # 缓存候选时段到 car_state（用户后续 "选1" / "选2" / "选3" 反查）
            car_state.save(user_id, last_slots=slots)
            return STATE_DURATION_CONFIRM, {
                "text": (f"**⏰ 第 5/8 步：选择时段**\n\n"
                         f"✅ 已选车辆：**{chosen.get('vehicle_no','')}**\n"
                         f"💡 下方按钮是候选时段（按时间段展示）"),
                "buttons": [{"text": s["label"],
                             "value": {"action": "fsm_pick_slot", "slot_idx": i + 1}}
                            for i, s in enumerate(slots)]
            }
        # 2) vehicle_no 已定 → 解析选时段
        slots = pending_dc.last_slots or _match_slots(pending_dc.vehicle_no, pending_dc.duration_minutes)
        slot = _resolve_slot_from_text(text, slots)
        if not slot:
            return STATE_DURATION_CONFIRM, {
                "text": (f"❌ 未识别时段「{text}」。\n\n"
                         f"💡 您可以：\n"
                         f"  • 点 1-{len(slots)} 序号\n"
                         f"  • 或报起止时间（如「14:00-16:00」）\n"
                         f"🚪 说「算了/取消/退出」可放弃本次约车"),
                "buttons": [{"text": s["label"],
                             "value": {"action": "fsm_pick_slot", "slot_idx": i + 1}}
                            for i, s in enumerate(slots)]
            }
        car_state.save(user_id,
                       time_range_start=slot["start"],
                       time_range_end=slot["end"],
                       start_time=slot["start"],
                       end_time=slot["end"])
        return STATE_INPUT_TASK, {
            "text": (f"**📝 第 6/8 步：输入任务名称**\n\n"
                     f"✅ 已选时段：**{slot['start']} ~ {slot['end']}**\n\n"
                     f"💡 您可以：\n"
                     f"  • 点击下方常用任务快速填入（推荐新手）\n"
                     f"  • 或直接输入自己的任务名称\n"
                     f"  • 点「其它」可跳过示例自由输入"),
            "buttons": [{"text": t, "value": {"action": _fsm_input_task_action(t)}}
                        for t in TASK_HINT_BUTTONS]
        }

    # SELECT_TIME 兜底（spec §3.3 DC-10 retry）
    if current_state == STATE_SELECT_TIME:
        # 用户从预设时段按钮中选 — 保存到 car_state
        if text in ("1小时后", "2小时后", "明早9点", "明天下午2点"):
            slots_now = _match_slots("", 60)  # 复用 mock 生成 4 个候选
            slot_map = {
                "1小时后": 0, "2小时后": 1, "明早9点": 2, "明天下午2点": 3,
            }
            idx = slot_map.get(text, 0)
            if idx < len(slots_now):
                s = slots_now[idx]
                car_state.save(user_id,
                               time_range_start=s["start"],
                               time_range_end=s["end"],
                               start_time=s["start"],
                               end_time=s["end"])
        return STATE_INPUT_TASK, {
            "text": (f"**📝 第 6/8 步：输入任务名称**\n\n"
                     f"✅ 时段已记录\n\n"
                     f"💡 您可以：\n"
                     f"  • 点击下方常用任务快速填入（推荐新手）\n"
                     f"  • 或直接输入自己的任务名称\n"
                     f"  • 点「其它」可跳过示例自由输入"),
            "buttons": [{"text": t, "value": {"action": _fsm_input_task_action(t)}}
                        for t in TASK_HINT_BUTTONS]
        }

    # DIRECT_BY_ID：用户直接报编号（spec §3.3）
    if current_state == STATE_DIRECT_BY_ID:
        vehicle_no = text.strip().upper()
        if not _VEHICLE_NO_RE.match(vehicle_no):
            return STATE_DIRECT_BY_ID, {
                "text": (f"❌ 编号格式不符「{vehicle_no}」\n\n"
                         f"💡 正确格式：字母+数字（如 SNV018 / PNV000）\n"
                         f"🚪 说「算了/取消」可返回车型选择"),
            }
        car_state.save(user_id, vehicle_no=vehicle_no)
        pending_now = car_state.get(user_id)
        if not pending_now.duration_minutes:
            car_state.save(user_id, duration_minutes=DEFAULT_DURATION_MINUTES)
        return STATE_SELECT_DURATION, _duration_card(car_state.get(user_id))

    # INPUT_TASK：LLM 抽 taskName（spec §3.4 ④；spec §4.2 prompt 不补全）
    if current_state == STATE_INPUT_TASK:
        # 2026-06-18 引导性增强：用户点「其它」按钮 → 走自定义输入路径，
        # 渲染纯文本提示（不再展示示例按钮，避免重复）。
        if text == _FSM_TASK_OTHER_MARKER:
            return STATE_INPUT_TASK, {
                "text": "✍️ 请直接输入任务名称（可中文/英文，如「MFF 调试」「路测 03 园区」）",
            }
        task = _llm_extract_task(text)["task_name"]
        if not task:
            return STATE_INPUT_TASK, {
                "text": "❓ 任务名称不能为空。\n"
                        "💡 您可以：\n"
                        "  • 点击下方示例快速填入（推荐新手）\n"
                        "  • 或直接输入自己的任务名称\n"
                        "  • 点「其它」可跳过示例自由输入\n"
                        "🚪 若想取消本次约车，请说「算了」/「取消」/「退出」",
                "buttons": [{"text": t, "value": {"action": _fsm_input_task_action(t)}}
                            for t in TASK_HINT_BUTTONS]
            }
        car_state.save(user_id, task_name=task)
        return STATE_INPUT_LOCATION, {
            "text": f"✅ 任务已记录：**{task}**\n\n"
                    f"📍 第 7/8 步：请输入测试地点\n"
                    f"💡 您可以：\n"
                    f"  • 点击下方常用地点快速填入\n"
                    f"  • 或直接输入自己的地点\n"
                    f"  • 点「其它」可跳过列表自由输入",
            "buttons": [{"text": c, "value": {"action": _fsm_input_location_action(c)}}
                        for c in LOCATION_BUTTONS]
        }

    # INPUT_LOCATION：LLM 抽 location
    if current_state == STATE_INPUT_LOCATION:
        # 2026-06-18 引导性增强：用户点「其它」按钮 → 走自定义输入路径。
        if text == _FSM_LOCATION_OTHER_MARKER:
            return STATE_INPUT_LOCATION, {
                "text": "✍️ 请直接输入测试地点（中文/英文均可，如「园区A3 号楼」「上海临港」）",
            }
        loc = _llm_extract_location(text)["location"]
        if not loc:
            return STATE_INPUT_LOCATION, {
                "text": "❓ 地点不能为空。\n"
                        "💡 您可以：\n"
                        "  • 点击下方常用地点快速填入（推荐新手）\n"
                        "  • 或直接输入自己的地点\n"
                        "  • 点「其它」可跳过列表自由输入\n"
                        "🚪 若想取消本次约车，请说「算了」/「取消」/「退出」",
                "buttons": [{"text": c, "value": {"action": _fsm_input_location_action(c)}}
                            for c in LOCATION_BUTTONS]
            }
        car_state.save(user_id, location=loc)
        return STATE_CONFIRM, _confirm_card(user_id)

    # CONFIRM：收"确认"/"取消"
    if current_state == STATE_CONFIRM:
        text_clean = text.strip()
        if text_clean == "确认":
            # 2026-06-18 修：之前 "确认" 只切到 STATE_COMMIT 等下一次 fsm.advance(text)
            # 才执行 commit，但用户没再次发消息，state 永远卡在 COMMIT。
            # 改为"确认"在同一 fsm.advance 调用里完成 commit，返回 SUCCESS 或失败提示。
            return _do_commit(user_id)
        if text_clean == "取消":
            car_state.clear(user_id)
            return STATE_START, _entry_card()
        if text_clean == "修改":
            return STATE_INPUT_TASK, {
                "text": ("✏️ 请重新输入任务名称\n\n"
                         "💡 您可以：\n"
                         "  • 点击下方常用任务快速填入\n"
                         "  • 或直接输入新的任务名称\n"
                         "  • 点「其它」可跳过示例自由输入"),
                "buttons": [{"text": t, "value": {"action": _fsm_input_task_action(t)}}
                            for t in TASK_HINT_BUTTONS]
            }
        return STATE_CONFIRM, {
            "text": ("❓ 请选择下一步操作：\n\n"
                     "  • ✅ **确认** —— 提交预约，等待调度员审批\n"
                     "  • ✏️ **修改** —— 重新填任务名称\n"
                     "  • ❌ **取消** —— 放弃本次约车\n\n"
                     "⏰ 10 分钟内未确认将自动作废"),
            "buttons": [
                {"text": "✅ 确认", "value": {"action": "fsm_confirm", "value": "确认"}},
                {"text": "✏️ 修改", "value": {"action": "fsm_confirm", "value": "修改"}},
                {"text": "❌ 取消", "value": {"action": "fsm_confirm", "value": "取消"}},
            ]
        }

    # COMMIT：调 _commit_single_vehicle_reservation（保留以兼容 car_state 中残留 state=COMMIT）
    if current_state == STATE_COMMIT:
        return _do_commit(user_id)

    # SUCCESS：终态
    if current_state == STATE_SUCCESS:
        # 2026-06-18 引导性增强：SUCCESS 不再立即清状态回 START —— 等用户看完摘要
        # 点 [再约一辆] / [我的预约] 才清。给"再约一辆"快捷路径，避免重走 13 步。
        # fsm_done_more → 清状态回 START（再走一遍）
        # fsm_done_records → 由 card_action_handler 直接分发到 records 查询（不走 FSM）
        if text == _FSM_DONE_MORE_MARKER:
            car_state.clear(user_id)
            return STATE_START, _entry_card()
        if text == _FSM_DONE_RECORDS_MARKER:
            car_state.clear(user_id)
            return STATE_START, _entry_card()  # 也回 START，让 records 自然分发
        # 默认：重渲染成功卡（用户点其他按钮或发文本时不变）
        return STATE_SUCCESS, _success_card(user_id)

    raise NotImplementedError(f"FSM state handler not implemented: {current_state}")


def _confirm_card(user_id: str) -> dict:
    """CONFIRM 状态：二次确认卡。

    2026-06-18 引导性增强：加步骤进度 + 字段高亮（车牌号/调度规则）+ 后续动作说明。
    """
    from bot import car_state
    p = car_state.get(user_id)
    summary = (
        f"**📋 第 8/8 步：最终确认**\n\n"
        f"🚗 **车辆信息**\n"
        f"  • 编号：**{p.vehicle_no}**（车牌：{p.license_plate or '-'}）\n"
        f"  • 车型：{p.vehicle_type or '-'} · {p.chip or '-'} 芯片\n\n"
        f"⏱️ **时间安排**\n"
        f"  • 时段：**{p.time_range_start} ~ {p.time_range_end}**\n"
        f"  • 时长：{_format_duration(p.duration_minutes)}\n\n"
        f"📝 **任务信息**\n"
        f"  • 任务：{p.task_name}\n"
        f"  • 地点：{p.location}\n\n"
        f"📨 提交后将自动通知调度员审批，通常 10-30 分钟内有结果\n"
        f"⏰ 10 分钟内未点确认，本次预约自动作废"
    )
    return _card_wrap([
        {"tag": "div", "text": {"tag": "lark_md", "content": summary}},
        _button_row([
            {"tag": "button", "type": "primary",
             "text": {"tag": "plain_text", "content": "✅ 确认提交"},
             "value": {"action": "fsm_confirm", "value": "确认"}},
            {"tag": "button", "type": "default",
             "text": {"tag": "plain_text", "content": "✏️ 修改任务"},
             "value": {"action": "fsm_confirm", "value": "修改"}},
            {"tag": "button", "type": "danger",
             "text": {"tag": "plain_text", "content": "❌ 取消"},
             "value": {"action": "fsm_confirm", "value": "取消"}},
        ])
    ])


def _success_card(user_id: str) -> dict:
    """SUCCESS 状态：成功卡。

    2026-06-18 引导性增强：加审批 SLA 说明 + 后续动作快捷入口（[再约一辆] / [我的预约]）。
    不再立即清状态回 START —— 等用户看完摘要点快捷按钮才清。
    """
    from bot import car_state
    p = car_state.get(user_id)
    return _card_wrap([
        {"tag": "div", "text": {"tag": "lark_md",
         "content": (f"**🎉 预约提交成功！**\n\n"
                     f"📋 **预约信息**\n"
                     f"  • 车辆编号：**{p.vehicle_no}**\n"
                     f"  • 车型：{p.vehicle_type or '-'} · {p.chip or '-'} 芯片\n"
                     f"  • 时段：{p.start_time} ~ {p.end_time}\n"
                     f"  • 任务：{p.task_name}\n"
                     f"  • 地点：{p.location}\n\n"
                     f"📨 调度员会收到通知，通常 10-30 分钟内审批\n"
                     f"💡 您可以：\n"
                     f"  • 约另一辆车（点下方 [再约一辆]）\n"
                     f"  • 查看我的所有预约（点下方 [我的预约]）\n"
                     f"  • 审批结果会通过本对话通知您")}},
        _button_row([
            {"tag": "button", "type": "primary",
             "text": {"tag": "plain_text", "content": "🚗 再约一辆"},
             "value": {"action": "fsm_done_more"}},
            {"tag": "button", "type": "default",
             "text": {"tag": "plain_text", "content": "📋 我的预约"},
             "value": {"action": "fsm_done_records"}},
        ]),
    ])


# _normalize_vehicle_keys 委托给 car_tools.normalizers._vehicle_to_card_dict
# （单一事实源；调用方直接 import 使用）。原 wrapper 已在 2026-06-18 review
# finding 9 修复时删除。
