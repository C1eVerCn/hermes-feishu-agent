"""car_tools/schemas.py 单元测试：Pydantic strict + Platform 枚举。"""
import pytest
from pydantic import ValidationError

from car_tools.schemas import (
    Vehicle, Reservation, ReservationResult, ApprovalResult,
    CancelResult, ReturnResult, Dispatcher, Platform,
)


# ── Platform 枚举 ──────────────────────────────────────────────────────────

def test_platform_accepts_known():
    v = Vehicle(vehicle_no="PNV332", vehicle_type="DM2", platform="Xavier")
    assert v.platform == "Xavier"


@pytest.mark.parametrize("p", ["Xavier", "ADCU", "Orin", "Thor"])
def test_platform_all_enum_values(p):
    v = Vehicle(vehicle_no="PNV332", vehicle_type="DM2", platform=p)
    assert v.platform == p


def test_platform_rejects_unknown():
    with pytest.raises(ValidationError):
        Vehicle(vehicle_no="PNV332", vehicle_type="DM2", platform="UnknownPlatform")


# ── extra=forbid 严格模式 ─────────────────────────────────────────────────

def test_vehicle_extra_forbid():
    with pytest.raises(ValidationError):
        Vehicle(vehicle_no="PNV332", vehicle_type="DM2", platform="Xavier",
                extra_field="should_fail")


def test_reservation_extra_forbid():
    with pytest.raises(ValidationError):
        Reservation(vehicle_no="PNV332", start_time="2026-06-16 09:00",
                    end_time="2026-06-16 18:00", status="待审批", unknown=1)


def test_reservation_result_extra_forbid():
    with pytest.raises(ValidationError):
        ReservationResult(
            success=True, vehicle_no="PNV332", vehicle_type="DM2", platform="Xavier",
            start_time="2026-06-16 09:00", end_time="2026-06-16 18:00",
            task_name="test", location="loc", ghost_field=42)


# ── 必填字段缺失 ──────────────────────────────────────────────────────────

def test_vehicle_minimal_required():
    v = Vehicle(vehicle_no="PNV332", vehicle_type="DM2", platform="Orin")
    assert v.vehicle_no == "PNV332"
    assert v.vin is None
    assert v.license_plate is None
    assert v.platform == "Orin"


def test_reservation_minimal_required():
    r = Reservation(vehicle_no="PNV332", start_time="2026-06-16 09:00",
                    end_time="2026-06-16 18:00", status="待审批")
    assert r.vehicle_type is None
    assert r.task_name is None
    assert r.reviewer is None


def test_vehicle_missing_required_platform():
    with pytest.raises(ValidationError):
        Vehicle(vehicle_no="PNV332", vehicle_type="DM2")  # platform 缺失


# ── 嵌套 Dispatcher ────────────────────────────────────────────────────────

def test_reservation_result_dispatchers_list():
    r = ReservationResult(
        success=True, vehicle_no="PNV332", vehicle_type="DM2", platform="Xavier",
        start_time="2026-06-16 09:00", end_time="2026-06-16 18:00",
        task_name="test", location="loc",
        dispatchers=[Dispatcher(name="Alice", email="a@x.com"),
                     Dispatcher(name="Bob", email="b@x.com")],
    )
    assert len(r.dispatchers) == 2
    assert r.dispatchers[0].name == "Alice"
    assert r.dispatchers[1].email == "b@x.com"


def test_reservation_result_dispatchers_default_empty():
    r = ReservationResult(
        success=True, vehicle_no="PNV332", vehicle_type="DM2", platform="Xavier",
        start_time="2026-06-16 09:00", end_time="2026-06-16 18:00",
        task_name="test", location="loc",
    )
    assert r.dispatchers == []


def test_dispatcher_extra_forbid():
    with pytest.raises(ValidationError):
        Dispatcher(name="Alice", email="a@x.com", unknown_field=1)


# ── 模型 dump ─────────────────────────────────────────────────────────────

def test_vehicle_dump_roundtrip():
    v = Vehicle(vehicle_no="PNV332", vehicle_type="DM2", platform="Orin",
                vin="LSGUC52H8RS000001", license_plate="沪A12345")
    d = v.model_dump()
    assert d["vehicle_no"] == "PNV332"
    assert d["vin"] == "LSGUC52H8RS000001"
    assert d["license_plate"] == "沪A12345"


# ── ReturnResult / CancelResult / ApprovalResult 字段 ────────────────────

def test_return_result_required_fields():
    r = ReturnResult(
        vehicle_no="PNV332", return_location="A区", key_position="前台抽屉",
        change_module="无", vehicle_status="1",
    )
    assert r.vehicle_status == "1"
    assert r.vehicle_status_description is None
    assert r.return_time is None


def test_cancel_result_optional():
    c = CancelResult(vehicle_no="PNV332")
    assert c.start_time is None
    assert c.operator is None


def test_approval_result_approved_bool():
    a = ApprovalResult(approved=True, vehicle_no="PNV332",
                       start_time="2026-06-16 09:00", end_time="2026-06-16 18:00",
                       task_name="test", reviewer="Alice")
    assert a.approved is True
    a2 = ApprovalResult(approved=False, vehicle_no="PNV332",
                        start_time="2026-06-16 09:00", end_time="2026-06-16 18:00",
                        task_name="test", reviewer="Alice")
    assert a2.approved is False
