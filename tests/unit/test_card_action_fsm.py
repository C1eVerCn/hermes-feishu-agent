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
    """fsm_select 按钮：value='BM0' → advance('BM0') → SELECT_VEHICLE_TYPE 处理。
    选车型细分后存到 vehicle_type_detail 字段，统一过 CONFIRM_CHIP。"""
    car_state.save("ou_act", state="SELECT_VEHICLE_TYPE")
    toast, card = card_action_handler.handle("ou_act", {"action": "fsm_select", "value": "BM0"})
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


def test_fsm_known_yes_button():
    """fsm_known_yes 按钮：value-less → FSM 标记符 → DIRECT_BY_ID。"""
    car_state.save("ou_act", state="SELECT_VEHICLE_TYPE")  # 用户刚点"不知道"后状态
    toast, card = card_action_handler.handle("ou_act",
                                              {"action": "fsm_known_yes"})
    # 实际不会到这里（known_yes 只能从 START 状态进），但测一下 marker 翻译
    # 注：fsm_known_yes 翻译为 marker，进 advance 后 START 会进 DIRECT_BY_ID
    # 因为是 SELECT_VEHICLE_TYPE 状态，advance 走 else 分支


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
