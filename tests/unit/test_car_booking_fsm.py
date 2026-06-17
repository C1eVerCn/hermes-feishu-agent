"""bot/car_booking_fsm.py 单元测试：13 状态机。"""
import pytest
from bot.car_booking_fsm import (
    CarBookingFSM,
    STATE_START,
    STATE_DIRECT_BY_ID,
    STATE_SELECT_VEHICLE_TYPE,
    advance,
)


def test_states_defined():
    """13 个状态名必须存在（spec §3.2）。"""
    expected = {
        "STATE_START", "STATE_DIRECT_BY_ID", "STATE_SELECT_VEHICLE_TYPE",
        "STATE_CONFIRM_CHIP", "STATE_VEHICLE_ENTRY", "STATE_SELECT_DURATION",
        "STATE_SELECT_FROM_LIST", "STATE_DURATION_CONFIRM", "STATE_SELECT_TIME",
        "STATE_INPUT_TASK", "STATE_INPUT_LOCATION", "STATE_CONFIRM",
        "STATE_COMMIT", "STATE_SUCCESS",
    }
    from bot import car_booking_fsm as fsm
    actual = {n for n in dir(fsm) if n.startswith("STATE_")}
    assert expected.issubset(actual), f"missing states: {expected - actual}"


def test_fsm_class_instantiable():
    """CarBookingFSM() 不需要参数。"""
    fsm = CarBookingFSM()
    assert fsm is not None


def test_advance_start_returns_entry_card():
    """START → 入口卡（车型按钮 + 直接输入编号按钮）。"""
    from bot import car_state
    car_state.clear("ou_t1")
    new_state, resp = advance("ou_t1", "")
    assert new_state == "SELECT_VEHICLE_TYPE"
    assert "card" in resp or "text" in resp  # 任一渲染形式


def test_advance_select_vehicle_type_button():
    """SELECT_VEHICLE_TYPE 收车型按钮 → CONFIRM_CHIP 或 VEHICLE_ENTRY。"""
    from bot import car_state
    car_state.save("ou_t2", state="SELECT_VEHICLE_TYPE")
    new_state, resp = advance("ou_t2", "大F车")
    assert new_state in ("CONFIRM_CHIP", "VEHICLE_ENTRY")
