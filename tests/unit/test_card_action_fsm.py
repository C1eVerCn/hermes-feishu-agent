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


def test_fsm_select_button_translates_value_to_text():
    """fsm_select 按钮：value='DM2' → advance('DM2') → SELECT_VEHICLE_TYPE 处理。
    所有车型统一过 CONFIRM_CHIP（即使单芯片车型也确认一次）。"""
    car_state.save("ou_act", state="SELECT_VEHICLE_TYPE")
    toast, card = card_action_handler.handle("ou_act", {"action": "fsm_select", "value": "DM2"})
    pending = car_state.get("ou_act")
    assert pending.state == "CONFIRM_CHIP"
    assert pending.vehicle_type == "DM2"
    assert pending.chip == ""  # chip 还没选
    car_state.clear("ou_act")


def test_fsm_select_chip_button():
    """fsm_select_chip 按钮：value='Xavier' → CONFIRM_CHIP 处理。"""
    car_state.save("ou_act", state="CONFIRM_CHIP", vehicle_type="大F车")
    toast, card = card_action_handler.handle("ou_act",
                                              {"action": "fsm_select_chip", "value": "Xavier"})
    pending = car_state.get("ou_act")
    assert pending.state == "VEHICLE_ENTRY"
    assert pending.chip == "Xavier"
    car_state.clear("ou_act")


def test_fsm_entry_button_known_id():
    """fsm_entry 按钮：value='已知编号' → DIRECT_BY_ID。"""
    car_state.save("ou_act", state="VEHICLE_ENTRY", vehicle_type="DM2", chip="Xavier")
    toast, card = card_action_handler.handle("ou_act",
                                              {"action": "fsm_entry", "value": "已知编号"})
    pending = car_state.get("ou_act")
    assert pending.state == "DIRECT_BY_ID"
    car_state.clear("ou_act")


def test_fsm_select_duration_button():
    """fsm_select_duration：value='1小时' → SELECT_DURATION 处理 → SELECT_FROM_LIST。"""
    car_state.save("ou_act", state="SELECT_DURATION", vehicle_type="DM2", chip="Xavier")
    toast, card = card_action_handler.handle("ou_act",
                                              {"action": "fsm_select_duration", "value": "1小时"})
    pending = car_state.get("ou_act")
    # duration=60 已存，state 进 SELECT_FROM_LIST
    assert pending.state == "SELECT_FROM_LIST"
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


def test_fsm_direct_by_id_button():
    """fsm_direct_by_id（无 value）→ FSM 特殊标记符 → DIRECT_BY_ID。"""
    car_state.save("ou_act", state="SELECT_VEHICLE_TYPE")
    toast, card = card_action_handler.handle("ou_act",
                                              {"action": "fsm_direct_by_id"})
    pending = car_state.get("ou_act")
    assert pending.state == "DIRECT_BY_ID"
    car_state.clear("ou_act")


def test_cancel_flow_button_clears_state():
    """cancel_flow：清状态。"""
    car_state.save("ou_act", state="SELECT_VEHICLE_TYPE")
    toast, card = card_action_handler.handle("ou_act", {"action": "cancel_flow"})
    assert car_state.get("ou_act") is None
    assert "已取消" in toast
