"""bot/dry_run_state 单测：跨轮 dry_run 快照（内存 / 600s TTL / 脱敏）。"""
import time

import pytest

from bot import dry_run_state


@pytest.fixture(autouse=True)
def _fresh():
    with dry_run_state._lock:
        dry_run_state._store.clear()
    dry_run_state.set_clock(lambda: 1000.0)  # 固定时钟
    yield
    with dry_run_state._lock:
        dry_run_state._store.clear()
    dry_run_state.set_clock(time.time)


_ARGS = {
    "vehicle_type": "DM2", "platform": "Xavier",
    "start_time": "2026-06-30 09:00", "end_time": "2026-06-30 10:00",
    "task_name": "MFF调试", "location": "上海",
}


def test_save_then_get_roundtrip():
    dry_run_state.save("ou_a", dict(_ARGS))
    snap = dry_run_state.get("ou_a")
    assert snap is not None
    assert snap["args"]["platform"] == "Xavier"
    assert snap["ts"] == 1000.0


def test_get_returns_none_when_absent():
    assert dry_run_state.get("ou_missing") is None


def test_save_redacts_identity_and_extra_fields():
    """只保留 6 个业务字段；email/openid/mobile/任意多余键必须被剔除。"""
    polluted = dict(_ARGS)
    polluted.update({"emailAddress": "a@x.com", "openId": "ou_a",
                     "mobile": "13800000000", "secret": "sk-xxx"})
    dry_run_state.save("ou_a", polluted)
    stored = dry_run_state.get("ou_a")["args"]
    assert set(stored.keys()) <= set(dry_run_state._ALLOWED_FIELDS)
    for leaked in ("emailAddress", "openId", "mobile", "secret"):
        assert leaked not in stored


def test_get_evicts_after_ttl():
    dry_run_state.save("ou_a", dict(_ARGS), ts=1000.0)
    dry_run_state.set_clock(lambda: 1000.0 + 601)  # 超过 600s
    assert dry_run_state.get("ou_a") is None
    with dry_run_state._lock:
        assert "ou_a" not in dry_run_state._store  # 顺手清除


def test_get_passes_within_ttl():
    dry_run_state.save("ou_a", dict(_ARGS), ts=1000.0)
    dry_run_state.set_clock(lambda: 1000.0 + 599)
    assert dry_run_state.get("ou_a") is not None


def test_save_overwrites_previous():
    dry_run_state.save("ou_a", dict(_ARGS))
    dry_run_state.save("ou_a", {**_ARGS, "platform": "Orin"})
    assert dry_run_state.get("ou_a")["args"]["platform"] == "Orin"


def test_save_ignores_empty_openid():
    dry_run_state.save("", dict(_ARGS))
    with dry_run_state._lock:
        assert dry_run_state._store == {}


def test_clear_removes_entry():
    dry_run_state.save("ou_a", dict(_ARGS))
    dry_run_state.clear("ou_a")
    assert dry_run_state.get("ou_a") is None


def test_get_returns_independent_copy():
    """get() 返回的 args 是独立拷贝，调用方改它不污染存储。"""
    dry_run_state.save("ou_a", dict(_ARGS))
    snap = dry_run_state.get("ou_a")
    snap["args"]["platform"] = "MUTATED"
    assert dry_run_state.get("ou_a")["args"]["platform"] == "Xavier"
