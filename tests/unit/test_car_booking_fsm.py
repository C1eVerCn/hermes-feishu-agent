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
