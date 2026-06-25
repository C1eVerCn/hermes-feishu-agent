"""tests for bot/car_booking_fsm.start_booking — Tier-2 槽位播种 + 跳到第一个缺口。

验证"说全了的人跳过按钮步骤"的自由度，同时流程仍由 FSM 执行（不变量不破）。
"""
from bot.car_booking_fsm import start_booking
from bot import car_state
from car_tools import mcp_client as _mc
import json


class _FakeMcpWithCars:
    def call(self, tool_name, args, timeout=10):
        if tool_name == "fetch_available_vehicles":
            return {"items": [
                {"vehicleNo": f"PNV{i:03d}", "vehicleType": "DM2",
                 "platform": "Xavier", "licensePlate": f"沪X{i:03d}"}
                for i in range(3)
            ]}
        return {}


def test_seed_empty_goes_to_entry():
    car_state.clear("ou_s1")
    state, resp = start_booking("ou_s1", {})
    assert state == "START"
    assert "card" in resp
    car_state.clear("ou_s1")


def test_seed_type_only_goes_to_chip():
    car_state.clear("ou_s2")
    state, resp = start_booking("ou_s2", {"vehicle_type_detail": "DM2"})
    assert state == "CONFIRM_CHIP"
    p = car_state.get("ou_s2")
    assert p.vehicle_type_detail == "DM2"
    car_state.clear("ou_s2")


def test_seed_type_and_chip_goes_to_duration():
    car_state.clear("ou_s3")
    state, resp = start_booking("ou_s3", {"vehicle_type_detail": "DM2", "platform": "Orin"})
    assert state == "SELECT_DURATION"
    p = car_state.get("ou_s3")
    assert p.chip == "Orin"          # router 'platform' → car_state.chip
    assert p.duration_minutes == 30  # 默认补齐
    car_state.clear("ou_s3")


def test_seed_vehicle_no_skips_to_time(monkeypatch):
    """报了车辆编号 → 跳过车型/芯片/选车，直接进时段选择（DURATION_CONFIRM）。"""
    car_state.clear("ou_s4")
    state, resp = start_booking("ou_s4", {"vehicle_no": "PNV332", "duration_minutes": 60})
    assert state == "DURATION_CONFIRM"
    p = car_state.get("ou_s4")
    assert p.vehicle_no == "PNV332"
    assert p.duration_minutes == 60
    car_state.clear("ou_s4")


def test_seed_vehicle_and_time_skips_to_task():
    car_state.clear("ou_s5")
    state, resp = start_booking("ou_s5", {
        "vehicle_no": "PNV332", "duration_minutes": 60,
        "start_time": "2026-06-26 09:00", "end_time": "2026-06-26 10:00",
    })
    assert state == "INPUT_TASK"
    car_state.clear("ou_s5")


def test_seed_complete_goes_straight_to_confirm():
    """全槽位齐 → 直接落到最终确认卡（最大化跳过 8 步 march），且时段不为空。"""
    car_state.clear("ou_s6")
    state, resp = start_booking("ou_s6", {
        "vehicle_no": "PNV332", "duration_minutes": 60,
        "start_time": "2026-06-26 09:00", "end_time": "2026-06-26 10:00",
        "task_name": "MFF调试", "location": "张江",
    })
    assert state == "CONFIRM"
    assert "card" in resp
    p = car_state.get("ou_s6")
    assert p.task_name == "MFF调试" and p.location == "张江"
    # 时段镜像到 time_range_*，确认卡才显示得出（fix：之前 CONFIRM 卡时段空白）
    assert p.time_range_start == "2026-06-26 09:00"
    assert "2026-06-26 09:00" in json.dumps(resp["card"], ensure_ascii=False)
    car_state.clear("ou_s6")


def test_seed_clears_prior_state():
    """start_booking 应清掉旧的挂起状态再播种（避免脏数据串场）。"""
    car_state.save("ou_s7", state="INPUT_LOCATION", vehicle_no="OLD999", task_name="旧任务")
    state, resp = start_booking("ou_s7", {"vehicle_type_detail": "DM2", "platform": "Orin"})
    p = car_state.get("ou_s7")
    assert p.vehicle_no == ""       # 旧 vehicle_no 被清
    assert p.task_name == ""        # 旧 task 被清
    car_state.clear("ou_s7")
