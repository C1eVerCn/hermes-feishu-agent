"""card_action_handler.py FSM 按钮委托单测。"""
import pytest
from bot import card_action_handler, car_state, car_booking_fsm
from ocl.tool_guard import set_current_caller, CallerIdentity


@pytest.fixture(autouse=True)
def setup():
    set_current_caller(CallerIdentity(openid="ou_act", email="a@x.com"))
    yield
    set_current_caller(CallerIdentity())
    car_state.clear("ou_act")


def test_fsm_select_type_translates_value_to_text():
    """fsm_select_type（select_static 下拉选中车型）：value='BM0' → CONFIRM_CHIP。
    2026-06-18 review finding 10：原 fsm_select 已无 caller（_type_card 改用
    fsm_select_type），原测试是死代码；改为新 action 名 + value 模拟 select_static
    回调（feishu/ws_client._extract_card_action 已归一化 option → value['value']）。
    """
    car_state.save("ou_act", state="SELECT_VEHICLE_TYPE")
    toast, card = card_action_handler.handle("ou_act", {"action": "fsm_select_type", "value": "BM0"})
    pending = car_state.get("ou_act")
    assert pending.state == "CONFIRM_CHIP"
    assert pending.vehicle_type_detail == "BM0"
    assert pending.chip == ""  # chip 还没选
    car_state.clear("ou_act")


def test_fsm_select_chip_button():
    """fsm_select_chip 按钮：value='Xavier' → CONFIRM_CHIP 处理 → SELECT_DURATION。"""
    car_state.save("ou_act", state="CONFIRM_CHIP", vehicle_type="大F车")
    toast, card = card_action_handler.handle("ou_act",
                                              {"action": "fsm_select_chip", "value": "Xavier"})
    pending = car_state.get("ou_act")
    # 新流程：芯片确认后直接进 SELECT_DURATION（不再经过 VEHICLE_ENTRY）
    assert pending.state == "SELECT_DURATION"
    assert pending.chip == "Xavier"
    assert pending.duration_minutes == 30  # 默认 30 分钟
    car_state.clear("ou_act")


def test_fsm_dur_confirm_with_no_vehicle_no():
    """fsm_dur_confirm：vehicle_no 空 → SELECT_FROM_LIST（查车）。"""
    car_state.save("ou_act", state="SELECT_DURATION", vehicle_type_detail="DM0", chip="Xavier",
                   duration_minutes=60)
    toast, card = card_action_handler.handle("ou_act", {"action": "fsm_dur_confirm"})
    pending = car_state.get("ou_act")
    assert pending.duration_minutes == 60
    car_state.clear("ou_act")


def test_fsm_pick_slot_button_translates_idx_to_text():
    """fsm_pick_slot：slot_idx=1 → advance('1') → 解析第一个 slot。"""
    slots = [
        {"start": "2026-06-17 14:00", "end": "2026-06-17 16:00", "label": "x"},
        {"start": "2026-06-17 18:00", "end": "2026-06-17 20:00", "label": "y"},
    ]
    car_state.save("ou_act", state="DURATION_CONFIRM", vehicle_no="PNV000",
                   vehicle_type="DM2", chip="Xavier", duration_minutes=120,
                   last_slots=slots)
    toast, card = card_action_handler.handle("ou_act",
                                              {"action": "fsm_pick_slot", "slot_idx": 2})
    pending = car_state.get("ou_act")
    assert pending.state == "INPUT_TASK"
    assert pending.start_time == "2026-06-17 18:00"
    car_state.clear("ou_act")


def test_fsm_input_task_button():
    """fsm_input_task：value='MFF调试' → advance('MFF调试')。"""
    car_state.save("ou_act", state="INPUT_TASK", vehicle_no="PNV000",
                   time_range_start="2026-06-17 14:00",
                   time_range_end="2026-06-17 16:00")
    toast, card = card_action_handler.handle("ou_act",
                                              {"action": "fsm_input_task", "value": "MFF调试"})
    pending = car_state.get("ou_act")
    assert pending.state == "INPUT_LOCATION"
    assert pending.task_name == "MFF调试"
    car_state.clear("ou_act")


def test_fsm_input_location_button():
    """fsm_input_location：value='上海' → advance('上海') → CONFIRM 卡。"""
    car_state.save("ou_act", state="INPUT_LOCATION", vehicle_no="PNV000",
                   vehicle_type="DM2", chip="Xavier", duration_minutes=120,
                   time_range_start="2026-06-17 14:00",
                   time_range_end="2026-06-17 16:00", task_name="MFF调试")
    toast, card = card_action_handler.handle("ou_act",
                                              {"action": "fsm_input_location", "value": "上海"})
    pending = car_state.get("ou_act")
    assert pending.state == "CONFIRM"
    assert pending.location == "上海"
    assert card is not None
    car_state.clear("ou_act")


def test_fsm_confirm_button_cancel():
    """fsm_confirm：value='取消' → clear state → START。"""
    car_state.save("ou_act", state="CONFIRM", vehicle_no="PNV000",
                   task_name="MFF调试", location="上海")
    toast, card = card_action_handler.handle("ou_act",
                                              {"action": "fsm_confirm", "value": "取消"})
    assert car_state.get("ou_act") is None
    # card 是新入口卡
    assert card is not None


def test_fsm_known_yes_button_translates_to_marker():
    """fsm_known_yes（无 value）→ FSM 标记符 → START 时进 DIRECT_BY_ID。"""
    car_state.save("ou_act", state="START")  # 入口状态
    # 直接调 FSM 验证 marker 翻译
    from bot.car_booking_fsm import advance as fsm_advance
    new_state, _ = fsm_advance("ou_act", "__fsm_known_yes__")
    assert new_state == "DIRECT_BY_ID"
    car_state.clear("ou_act")


def test_fsm_known_no_button_translates_to_type_card():
    """fsm_known_no → START 时进 SELECT_VEHICLE_TYPE（带车型卡）。"""
    car_state.save("ou_act", state="START")
    from bot.car_booking_fsm import advance as fsm_advance
    new_state, resp = fsm_advance("ou_act", "__fsm_known_no__")
    assert new_state == "SELECT_VEHICLE_TYPE"
    assert resp.get("card") is not None  # 类型卡
    car_state.clear("ou_act")


def test_fsm_dur_minus_decrements():
    """fsm_dur_minus → duration_minutes 减 30（≥30 限位）。"""
    car_state.save("ou_act", state="SELECT_DURATION", duration_minutes=60)
    from bot.car_booking_fsm import advance as fsm_advance
    new_state, resp = fsm_advance("ou_act", "__fsm_dur_minus__")
    assert new_state == "SELECT_DURATION"
    pending = car_state.get("ou_act")
    assert pending.duration_minutes == 30
    car_state.clear("ou_act")


def test_fsm_dur_minus_min_floor():
    """fsm_dur_minus 在 min (30) 时不再减。"""
    car_state.save("ou_act", state="SELECT_DURATION", duration_minutes=30)
    from bot.car_booking_fsm import advance as fsm_advance
    fsm_advance("ou_act", "__fsm_dur_minus__")
    pending = car_state.get("ou_act")
    assert pending.duration_minutes == 30  # 触底
    car_state.clear("ou_act")


def test_fsm_dur_plus_increments():
    """fsm_dur_plus → duration_minutes 加 30（≤480 限位）。"""
    car_state.save("ou_act", state="SELECT_DURATION", duration_minutes=60)
    from bot.car_booking_fsm import advance as fsm_advance
    fsm_advance("ou_act", "__fsm_dur_plus__")
    pending = car_state.get("ou_act")
    assert pending.duration_minutes == 90
    car_state.clear("ou_act")


def test_fsm_dur_plus_max_cap():
    """fsm_dur_plus 在 max (480) 时不再加。"""
    car_state.save("ou_act", state="SELECT_DURATION", duration_minutes=480)
    from bot.car_booking_fsm import advance as fsm_advance
    fsm_advance("ou_act", "__fsm_dur_plus__")
    pending = car_state.get("ou_act")
    assert pending.duration_minutes == 480
    car_state.clear("ou_act")


def test_cancel_flow_button_clears_state():
    """cancel_flow：清状态。"""
    car_state.save("ou_act", state="SELECT_VEHICLE_TYPE")
    toast, card = card_action_handler.handle("ou_act", {"action": "cancel_flow"})
    assert car_state.get("ou_act") is None
    assert "已取消" in toast


def test_fsm_pick_slot_dropdown_uses_picked_time():
    """下拉选时段：value['value']=选中的 start time → 订到那个时段（修"总是订第1个"的 bug）。"""
    car_state.clear("ou_slot")
    slots = [
        {"start": "2026-06-26 14:00", "end": "2026-06-26 15:00", "label": "06-26 14:00 ~ 15:00"},
        {"start": "2026-06-26 18:00", "end": "2026-06-26 19:00", "label": "06-26 18:00 ~ 19:00"},
    ]
    car_state.save("ou_slot", state="DURATION_CONFIRM", intent="booking",
                   vehicle_no="PNV001", duration_minutes=60, last_slots=slots)
    # 模拟下拉选了第 2 个（18:00），ws_client 把 option 归一化到 value['value']
    card_action_handler.handle("ou_slot",
                               {"action": "fsm_pick_slot", "value": "2026-06-26 18:00"})
    p = car_state.get("ou_slot")
    assert p.start_time == "2026-06-26 18:00", f"应订 18:00，实得 {p.start_time}"
    assert p.end_time == "2026-06-26 19:00"
    car_state.clear("ou_slot")


def test_fsm_known_no_zero_cars_shows_message(monkeypatch):
    """无车组用户（后端 code=200 data:null + 暂无车组）→ 明确提示，不展示车型清单。"""
    from bot import car_booking_fsm as fsm, car_state
    from car_tools import mcp_client as _mc

    class _NoGroup:
        def call(self, tool, args, timeout=10):
            return {"code": 200, "data": None, "message": "当前用户暂无车组，请联系管理员添加"}
    monkeypatch.setattr(_mc, "_client", _NoGroup())
    car_state.save("ou_nogrp", state="START")
    new_state, resp = fsm.advance("ou_nogrp", "__fsm_known_no__")
    assert new_state == "START"
    assert "暂无车组" in resp.get("text", "")
    assert "card" not in resp  # 不展示约不到的车型下拉
    car_state.clear("ou_nogrp")


def test_fsm_known_no_upstream_error_falls_back(monkeypatch):
    """上游报错（code=500）→ 当调用失败，回退固定车型卡（流程不断）。"""
    from bot import car_booking_fsm as fsm, car_state
    from car_tools import mcp_client as _mc

    class _Err:
        def call(self, tool, args, timeout=10):
            return {"code": 500, "data": None, "message": "MCP 调用失败"}
    monkeypatch.setattr(_mc, "_client", _Err())
    car_state.save("ou_err", state="START")
    new_state, resp = fsm.advance("ou_err", "__fsm_known_no__")
    assert new_state == "SELECT_VEHICLE_TYPE"
    assert resp.get("card") is not None
    car_state.clear("ou_err")
