"""bot/car_state.py 单元测试：save / get / clear / TTL 过期。"""
import pytest

from bot import car_state


@pytest.fixture(autouse=True)
def _clean():
    car_state._state.clear()
    yield
    car_state._state.clear()


def test_save_creates_new_entry():
    car_state.save("ou_alice", intent="booking", vehicle_no="PNV332")
    s = car_state.get("ou_alice")
    assert s is not None
    assert s.intent == "booking"
    assert s.vehicle_no == "PNV332"
    assert s.user_id == "ou_alice"


def test_save_overwrites():
    car_state.save("ou_alice", intent="booking", vehicle_no="PNV332")
    car_state.save("ou_alice", intent="approve", vehicle_no="SVV027")
    s = car_state.get("ou_alice")
    assert s.intent == "approve"
    assert s.vehicle_no == "SVV027"


def test_get_returns_none_when_missing():
    assert car_state.get("ou_unknown") is None


def test_clear_removes_entry():
    car_state.save("ou_alice", intent="booking")
    car_state.clear("ou_alice")
    assert car_state.get("ou_alice") is None


def test_clear_missing_is_noop():
    car_state.clear("ou_unknown")  # should not raise


def test_update_modifies_existing():
    car_state.save("ou_alice", intent="booking", vehicle_no="PNV332")
    car_state.update("ou_alice", task_name="高速测试", location="A区")
    s = car_state.get("ou_alice")
    assert s.task_name == "高速测试"
    assert s.location == "A区"
    assert s.vehicle_no == "PNV332"  # 旧字段保留


def test_update_missing_noop():
    """无 entry 时 update 不创建（caller 应该先 save）。"""
    car_state.update("ou_unknown", task_name="X")
    assert car_state.get("ou_unknown") is None


def test_ttl_expiry(monkeypatch):
    """过期 entry 在 get 时被清除。"""
    car_state.save("ou_alice", intent="booking")
    s = car_state.get("ou_alice")
    # 模拟过期：手动把 expires_at 调到过去（>0 且 < time.monotonic）
    monkeypatch.setattr(s, "expires_at", 1.0)
    assert car_state.get("ou_alice") is None


def test_as_dict():
    car_state.save("ou_alice", intent="booking", vehicle_no="PNV332",
                   task_name="高速测试", location="A区")
    d = car_state.as_dict("ou_alice")
    assert d["intent"] == "booking"
    assert d["vehicle_no"] == "PNV332"
    assert "expires_at" not in d  # 不暴露 internal 字段


def test_as_dict_missing_returns_empty():
    assert car_state.as_dict("ou_unknown") == {}


def test_is_expired():
    """expires_at=0 → 不算过期（默认/未初始化）；expires_at>now → 不过期；<now → 过期。"""
    s0 = car_state.CarPendingState(user_id="ou_alice", expires_at=0.0)
    assert s0.is_expired() is False  # 0 是 sentinel
    s_future = car_state.CarPendingState(user_id="ou_alice", expires_at=99999999999.0)
    assert s_future.is_expired() is False
    s_past = car_state.CarPendingState(user_id="ou_alice", expires_at=1.0)
    assert s_past.is_expired() is True


def test_state_independent_per_user():
    car_state.save("ou_alice", intent="booking")
    car_state.save("ou_bob", intent="approve")
    assert car_state.get("ou_alice").intent == "booking"
    assert car_state.get("ou_bob").intent == "approve"


def test_save_partial_fields():
    """只传部分字段 → 默认值生效。"""
    car_state.save("ou_alice", intent="booking")
    s = car_state.get("ou_alice")
    assert s.vehicle_no == ""
    assert s.task_name == ""
    assert s.platform == ""


def test_update_ignores_unknown_field():
    car_state.save("ou_alice", intent="booking")
    car_state.update("ou_alice", task_name="test", ghost_field=1)
    s = car_state.get("ou_alice")
    assert s.task_name == "test"
    assert not hasattr(s, "ghost_field") or s.ghost_field is None


def test_car_booking_state_new_fields():
    """v2 FSM 扩展字段：state/vehicle_type/chip/duration_minutes/.../retry_count。"""
    car_state.save(
        "ou_test_v2",
        state="DURATION_CONFIRM",
        vehicle_type="大F车",
        chip="Xavier",
        duration_minutes=120,
        time_range_start="2026-06-17 14:00",
        time_range_end="2026-06-17 16:00",
        task_name="MFF调试",
        location="上海",
        retry_count=1,
    )
    s = car_state.get("ou_test_v2")
    assert s is not None
    assert s.state == "DURATION_CONFIRM"
    assert s.vehicle_type == "大F车"
    assert s.chip == "Xavier"
    assert s.duration_minutes == 120
    assert s.retry_count == 1
    car_state.clear("ou_test_v2")
