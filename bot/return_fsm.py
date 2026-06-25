"""bot/return_fsm — 还车表单 FSM（收集 5 个必填字段 + 二次确认）。

新版上游 return_vehicle 需要：还车地点 / 钥匙位置 / 变更模块 / 车辆状态 / 状态描述
（外加车辆编号）。一键确认不足以收集，故做成像约车 FSM 那样一步步走的表单流。

状态（car_state.intent="return"，state="RET_*"）：
  RET_VEHICLE → RET_LOCATION → RET_KEY → RET_MODULE → RET_STATUS → RET_DESC
  → RET_CONFIRM → RET_DONE

每步给"快捷按钮 + 可直接输入"两条路径：按钮点击（card_action_handler 的 ret_* 分支）
或自由文本（handler 在 intent=="return" 挂起态路由到本模块）都进 advance。
"""
import json
import logging

from bot import car_state
from bot import intent
from bot.car_booking_fsm import _card_wrap, _select_card, LOCATION_BUTTONS
from car_tools.card_builder import _button_row

log = logging.getLogger(__name__)

RET_VEHICLE = "RET_VEHICLE"
RET_LOCATION = "RET_LOCATION"
RET_KEY = "RET_KEY"
RET_MODULE = "RET_MODULE"
RET_STATUS = "RET_STATUS"
RET_DESC = "RET_DESC"
RET_CONFIRM = "RET_CONFIRM"
RET_DONE = "RET_DONE"

ALL_RETURN_STATES = frozenset({
    RET_VEHICLE, RET_LOCATION, RET_KEY, RET_MODULE, RET_STATUS, RET_DESC,
    RET_CONFIRM, RET_DONE,
})

KEY_BUTTONS = ["车内", "前台保安", "钥匙柜", "项目组"]
MODULE_BUTTONS = ["无变更", "传感器", "域控制器", "线束"]
STATUS_BUTTONS = ["可用", "故障", "维保", "报废"]
_STATUS_CODE = {"可用": 1, "故障": 2, "维保": 3, "报废": 4}
DESC_BUTTONS = ["车况正常", "有轻微故障", "需维保"]


def _quick_buttons(action: str, options: list[str]) -> list[dict]:
    return [{"text": o, "value": {"action": action, "value": o}} for o in options]


def is_return_state(state: str) -> bool:
    return state in ALL_RETURN_STATES


def start(user_id: str, slots: dict | None = None) -> tuple[str, dict]:
    """进入还车流程：用 slots 播种 vehicle_no，跳到第一个缺口。"""
    slots = slots or {}
    car_state.clear(user_id)
    seed = {"intent": "return"}
    vno = (slots.get("vehicle_no") or "").strip()
    if vno:
        seed["vehicle_no"] = vno
    car_state.save(user_id, **seed)
    return _resume(user_id)


def _resume(user_id: str) -> tuple[str, dict]:
    """根据已填字段定位下一个缺口并渲染。"""
    p = car_state.get(user_id)
    if not p.vehicle_no:
        car_state.save(user_id, state=RET_VEHICLE)
        return RET_VEHICLE, {"text": ("**🔧 还车 · 第 1 步：车辆编号**\n\n"
                                      "请输入要归还的车辆编号（如 PNV332 / 苏EAM0769）：")}
    if not p.return_location:
        return _ask_location(user_id)
    if not p.key_position:
        return _ask_key(user_id)
    if not p.change_module:
        return _ask_module(user_id)
    if not p.vehicle_status:
        return _ask_status(user_id)
    if not p.vehicle_status_description:
        return _ask_desc(user_id)
    return _to_confirm(user_id)


def _ask_location(user_id: str) -> tuple[str, dict]:
    car_state.save(user_id, state=RET_LOCATION)
    return RET_LOCATION, {
        "text": ("**🔧 还车 · 第 2 步：还车地点**\n\n"
                 "💡 点下方常用地点，或直接输入："),
        "buttons": _quick_buttons("ret_loc", LOCATION_BUTTONS[:-1]),  # 去掉"其它"（可直接输入）
    }


def _ask_key(user_id: str) -> tuple[str, dict]:
    car_state.save(user_id, state=RET_KEY)
    return RET_KEY, {
        "text": ("**🔧 还车 · 第 3 步：钥匙位置**\n\n"
                 "💡 点下方常用位置，或直接输入："),
        "buttons": _quick_buttons("ret_key", KEY_BUTTONS),
    }


def _ask_module(user_id: str) -> tuple[str, dict]:
    car_state.save(user_id, state=RET_MODULE)
    return RET_MODULE, {
        "text": ("**🔧 还车 · 第 4 步：变更模块**\n\n"
                 "💡 本次用车有无硬件变更？点下方或直接输入（无变更填「无变更」）："),
        "buttons": _quick_buttons("ret_module", MODULE_BUTTONS),
    }


def _ask_status(user_id: str) -> tuple[str, dict]:
    car_state.save(user_id, state=RET_STATUS)
    return RET_STATUS, {
        "text": ("**🔧 还车 · 第 5 步：车辆状态**\n\n"
                 "请选择归还时的车辆状态："),
        "card": _select_card(
            title="🔧 第 5 步：车辆状态",
            placeholder="点击选择车辆状态",
            options=STATUS_BUTTONS,
            action="ret_status",
        ),
    }


def _ask_desc(user_id: str) -> tuple[str, dict]:
    car_state.save(user_id, state=RET_DESC)
    return RET_DESC, {
        "text": ("**🔧 还车 · 第 6 步：状态描述**\n\n"
                 "💡 点下方快捷描述，或直接输入车况说明："),
        "buttons": _quick_buttons("ret_desc", DESC_BUTTONS),
    }


def _confirm_card(user_id: str) -> dict:
    p = car_state.get(user_id)
    status_name = next((k for k, v in _STATUS_CODE.items() if str(v) == str(p.vehicle_status)),
                       p.vehicle_status or "-")
    summary = (
        f"**🔧 第 7 步：确认归还信息**\n\n"
        f"🚗 车辆编号：**{p.vehicle_no}**\n"
        f"📍 还车地点：{p.return_location}\n"
        f"🔑 钥匙位置：{p.key_position}\n"
        f"🧩 变更模块：{p.change_module}\n"
        f"📋 车辆状态：**{status_name}**\n"
        f"📝 状态描述：{p.vehicle_status_description}\n\n"
        f"确认无误后点 [确认归还]。"
    )
    return _card_wrap([
        {"tag": "div", "text": {"tag": "lark_md", "content": summary}},
        _button_row([
            {"tag": "button", "type": "primary",
             "text": {"tag": "plain_text", "content": "✅ 确认归还"},
             "value": {"action": "ret_confirm", "value": "确认"}},
            {"tag": "button", "type": "danger",
             "text": {"tag": "plain_text", "content": "❌ 取消"},
             "value": {"action": "ret_confirm", "value": "取消"}},
        ]),
    ])


def _to_confirm(user_id: str) -> tuple[str, dict]:
    car_state.save(user_id, state=RET_CONFIRM)
    return RET_CONFIRM, {"card": _confirm_card(user_id)}


def _do_return(user_id: str) -> tuple[str, dict]:
    """执行 return_vehicle（注入 CallerIdentity）。"""
    from car_tools import handlers as _h
    from ocl.tool_guard import set_current_caller, CallerIdentity
    from ocl import identity as _identity
    p = car_state.get(user_id)
    set_current_caller(CallerIdentity(
        openid=user_id, email=_identity.email_of(user_id) or "",
        mobile=_identity.mobile_of(user_id) or None))
    try:
        raw = _h.return_vehicle({
            "vehicleNo": p.vehicle_no,
            "returnLocation": p.return_location,
            "keyPosition": p.key_position,
            "changeModule": p.change_module,
            "vehicleStatus": int(_STATUS_CODE.get(_status_name(p.vehicle_status),
                                                  p.vehicle_status or 1)),
            "vehicleStatusDescription": p.vehicle_status_description,
        })
        result = json.loads(raw) if isinstance(raw, str) else raw
    except Exception as e:
        log.exception("return failed")
        return RET_DONE, {"text": f"❌ 归还失败：{e}"}
    finally:
        set_current_caller(CallerIdentity())
    car_state.clear(user_id)
    if isinstance(result, dict) and "error" in result:
        return RET_DONE, {"text": f"❌ 归还失败：{result['error']}"}
    return RET_DONE, {"text": f"✅ 已归还车辆 **{p.vehicle_no}**，状态已更新。"}


def _status_name(stored) -> str:
    """car_state.vehicle_status 可能存中文名或数字，统一回中文名给 _STATUS_CODE 查。"""
    s = str(stored or "")
    if s in _STATUS_CODE:
        return s
    for k, v in _STATUS_CODE.items():
        if str(v) == s:
            return k
    return "可用"


def advance(user_id: str, text: str = "") -> tuple[str, dict]:
    """还车 FSM 主入口。"""
    text = (text or "").strip()
    p = car_state.get(user_id)
    state = p.state if p else RET_VEHICLE

    # escape：算了/取消/退出 → 清状态退出（CONFIRM 的"取消"走自己的分支）
    if intent.is_escape(text) and state != RET_CONFIRM:
        car_state.clear(user_id)
        return RET_DONE, {"text": "已取消本次归还。"}

    if state == RET_VEHICLE:
        vno = text.upper()
        if not intent.looks_like_vehicle_id(vno):
            return RET_VEHICLE, {"text": ("❌ 编号格式不符。\n"
                                          "请输入车辆编号（字母/汉字开头+含数字，如 PNV332）：")}
        car_state.save(user_id, vehicle_no=vno)
        return _ask_location(user_id)

    if state == RET_LOCATION:
        if not text:
            return _ask_location(user_id)
        car_state.save(user_id, return_location=text)
        return _ask_key(user_id)

    if state == RET_KEY:
        if not text:
            return _ask_key(user_id)
        car_state.save(user_id, key_position=text)
        return _ask_module(user_id)

    if state == RET_MODULE:
        if not text:
            return _ask_module(user_id)
        car_state.save(user_id, change_module=text)
        return _ask_status(user_id)

    if state == RET_STATUS:
        if text not in _STATUS_CODE:
            return RET_STATUS, {
                "text": f"❌ 未识别状态「{text}」，请从下拉选择（可用/故障/维保/报废）：",
                "card": _select_card(title="🔧 车辆状态", placeholder="点击选择",
                                     options=STATUS_BUTTONS, action="ret_status"),
            }
        car_state.save(user_id, vehicle_status=text)
        return _ask_desc(user_id)

    if state == RET_DESC:
        if not text:
            return _ask_desc(user_id)
        car_state.save(user_id, vehicle_status_description=text)
        return _to_confirm(user_id)

    if state == RET_CONFIRM:
        if text == "确认":
            return _do_return(user_id)
        if text == "取消":
            car_state.clear(user_id)
            return RET_DONE, {"text": "已取消本次归还。"}
        return RET_CONFIRM, {"card": _confirm_card(user_id)}

    # 未知 → 重新定位缺口
    return _resume(user_id)
