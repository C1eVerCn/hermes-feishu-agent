"""Tests for bot/reservation_store.py — reservation_id ↔ applicant mapping (车辆预约域)."""
import json
from pathlib import Path

import pytest

from bot import reservation_store


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    """Use a tmp file so we never touch data/reservation_applicants.json."""
    f = tmp_path / "reservation_applicants.json"
    monkeypatch.setattr(reservation_store, "_FILE", f)
    yield


def test_save_then_get():
    reservation_store.save(
        "rid-1", "ou_alice", "alice@x.com",
        "PNV332", "2026-06-16 09:00",
        "2026-06-16 18:00", "高速测试",
    )
    rec = reservation_store.get("rid-1")
    assert rec is not None
    assert rec["applicant_open_id"] == "ou_alice"
    assert rec["applicant_email"] == "alice@x.com"
    assert rec["vehicle_no"] == "PNV332"
    assert rec["start_time"] == "2026-06-16 09:00"
    assert rec["task_name"] == "高速测试"


def test_get_missing_returns_none():
    assert reservation_store.get("nope") is None


def test_save_is_idempotent_on_reservation_id():
    """Re-saving the same reservation_id overwrites."""
    reservation_store.save("rid-1", "ou_a", "a@x.com", "PNV332", "t1")
    reservation_store.save("rid-1", "ou_a", "a@x.com", "PNV332", "t1",
                           "t2", "updated_task")
    rec = reservation_store.get("rid-1")
    assert rec["end_time"] == "t2"
    assert rec["task_name"] == "updated_task"


def test_save_empty_id_is_noop():
    reservation_store.save("", "ou_a", "a@x.com", "PNV332", "t1")
    assert reservation_store.get("") is None


def test_find_by_vehicle_and_time_returns_latest():
    reservation_store.save("rid-1", "ou_a", "a@x.com", "PNV332", "2026-06-16 09:00:00")
    reservation_store.save("rid-2", "ou_b", "b@x.com", "PNV332", "2026-06-16 09:00:00",
                           "2026-06-16 18:00", "高速测试2")
    rec = reservation_store.find_by_vehicle_and_time("PNV332", "2026-06-16 09:00:00")
    assert rec is not None
    assert rec["applicant_email"] == "b@x.com"
    assert rec["task_name"] == "高速测试2"


def test_find_by_vehicle_and_time_no_match():
    reservation_store.save("rid-1", "ou_a", "a@x.com", "PNV332", "2026-06-16 09:00:00")
    assert reservation_store.find_by_vehicle_and_time("SVV027", "2026-06-16 09:00:00") is None


def test_corrupt_file_returns_empty():
    """If the file is malformed JSON, treat as empty rather than crash."""
    Path(reservation_store._FILE).parent.mkdir(parents=True, exist_ok=True)
    Path(reservation_store._FILE).write_text("{ not json", encoding="utf-8")
    assert reservation_store.get("anything") is None
    assert reservation_store.list_all() == {}


def test_list_all_returns_copy():
    reservation_store.save("rid-1", "ou_a", "a@x.com", "PNV332", "t1")
    snapshot = reservation_store.list_all()
    assert "rid-1" in snapshot
    # Mutating snapshot doesn't affect the store
    snapshot["rid-1"]["vehicle_no"] = "mutated"
    assert reservation_store.get("rid-1")["vehicle_no"] == "PNV332"


def test_find_by_vehicle_and_time_format_tolerant():
    """Format 容差：MCP 返回的 startTime 格式可能与申请时不同。"""
    reservation_store.save("rid-1", "ou_a", "a@x.com", "PNV332",
                           "2026-06-16 09:00:00", "2026-06-16 18:00:00", "测试")
    for echoed in ("2026-06-16T09:00:00", "2026-06-16 09:00",
                   "2026-06-16T09:00:00+08:00"):
        rec = reservation_store.find_by_vehicle_and_time("PNV332", echoed)
        assert rec is not None, echoed
        assert rec["applicant_email"] == "a@x.com"


def test_find_by_vehicle_and_time_different_time_no_match():
    reservation_store.save("rid-1", "ou_a", "a@x.com", "PNV332",
                           "2026-06-16 09:00:00")
    assert reservation_store.find_by_vehicle_and_time("PNV332", "2026-06-16 18:00:00") is None


# ── Backwards compat alias ─────────────────────────────────────────────────

def test_find_by_bench_and_time_alias():
    """旧别名保留（card_action_handler 等老代码可能调用）。"""
    reservation_store.save("rid-1", "ou_a", "a@x.com", "PNV332", "2026-06-16 09:00")
    rec = reservation_store.find_by_bench_and_time("PNV332", "2026-06-16 09:00")
    assert rec is not None
