"""Tests for bot/reservation_store.py — reservation_id ↔ applicant mapping."""
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
        "CT001", "2026-06-11 17:00:00",
        "2026-06-11 20:00:00", "测试",
    )
    rec = reservation_store.get("rid-1")
    assert rec is not None
    assert rec["applicant_open_id"] == "ou_alice"
    assert rec["applicant_email"] == "alice@x.com"
    assert rec["bench_no"] == "CT001"
    assert rec["start_time"] == "2026-06-11 17:00:00"
    assert rec["task_name"] == "测试"


def test_get_missing_returns_none():
    assert reservation_store.get("nope") is None


def test_save_is_idempotent_on_reservation_id():
    """Re-saving the same reservation_id overwrites (e.g. if applicant
    later amends task_name)."""
    reservation_store.save("rid-1", "ou_a", "a@x.com", "CT001", "t1")
    reservation_store.save("rid-1", "ou_a", "a@x.com", "CT001", "t1",
                           "t2", "updated_task")
    rec = reservation_store.get("rid-1")
    assert rec["end_time"] == "t2"
    assert rec["task_name"] == "updated_task"


def test_save_empty_id_is_noop():
    reservation_store.save("", "ou_a", "a@x.com", "CT001", "t1")
    assert reservation_store.get("") is None


def test_find_by_bench_and_time_returns_latest():
    reservation_store.save("rid-1", "ou_a", "a@x.com", "CT001", "2026-06-11 17:00:00")
    reservation_store.save("rid-2", "ou_b", "b@x.com", "CT001", "2026-06-11 17:00:00",
                           "2026-06-11 20:00:00", "测试2")
    rec = reservation_store.find_by_bench_and_time("CT001", "2026-06-11 17:00:00")
    # Last-wins: most recently saved matching record
    assert rec is not None
    assert rec["applicant_email"] == "b@x.com"
    assert rec["task_name"] == "测试2"


def test_find_by_bench_and_time_no_match():
    reservation_store.save("rid-1", "ou_a", "a@x.com", "CT001", "2026-06-11 17:00:00")
    assert reservation_store.find_by_bench_and_time("TJ001", "2026-06-11 17:00:00") is None


def test_corrupt_file_returns_empty():
    """If the file is malformed JSON, treat as empty rather than crash."""
    Path(reservation_store._FILE).parent.mkdir(parents=True, exist_ok=True)
    Path(reservation_store._FILE).write_text("{ not json", encoding="utf-8")
    assert reservation_store.get("anything") is None
    assert reservation_store.list_all() == {}


def test_list_all_returns_copy():
    reservation_store.save("rid-1", "ou_a", "a@x.com", "CT001", "t1")
    snapshot = reservation_store.list_all()
    assert "rid-1" in snapshot
    # Mutating snapshot doesn't affect the store
    snapshot["rid-1"]["bench_no"] = "mutated"
    assert reservation_store.get("rid-1")["bench_no"] == "CT001"


# ── BUGFIX: LLM key-name normalization (added 2026-06-10) ──────────────────

def test_normalize_task_alias_to_task_name():
    from bench_tools.handlers import _normalize_args
    args = {"benchNo": "CT001", "task": "测试",
            "startTime": "2026-06-12 17:00:00",
            "endTime": "2026-06-12 20:00:00"}
    normalized, rewrites = _normalize_args(args)
    assert "taskName" in normalized
    assert normalized["taskName"] == "测试"
    assert "task" not in normalized
    assert any("task" in r for r in rewrites)


def test_normalize_purpose_alias_to_test_purpose():
    from bench_tools.handlers import _normalize_args
    args = {"test_purpose": "感知压测"}
    normalized, rewrites = _normalize_args(args)
    assert normalized["testPurpose"] == "感知压测"
    assert "test_purpose" not in normalized
    assert any("test_purpose" in r for r in rewrites)


def test_normalize_snake_case_time_keys():
    from bench_tools.handlers import _normalize_args
    args = {"start_time": "2026-06-12 17:00:00", "end_time": "2026-06-12 20:00:00"}
    normalized, rewrites = _normalize_args(args)
    assert normalized["startTime"] == "2026-06-12 17:00:00"
    assert normalized["endTime"] == "2026-06-12 20:00:00"
    assert len(rewrites) == 2


def test_normalize_passthrough_for_canonical_keys():
    from bench_tools.handlers import _normalize_args
    args = {"benchNo": "CT001", "taskName": "测试",
            "testPurpose": "测试", "startTime": "t", "endTime": "e"}
    normalized, rewrites = _normalize_args(args)
    assert rewrites == []  # no rewrites needed
    assert normalized == args


def test_normalize_unwraps_single_key_json_string():
    """LLM sometimes wraps the whole payload as a JSON string under a single
    key (e.g. `{"reservation": "{...all fields...}"}`). The normaliser
    should unwrap and rename.
    """
    import json as _json
    from bench_tools.handlers import _normalize_args
    inner = {"benchNo": "CT001", "task": "测试",
             "purpose": "感知压测", "start_time": "2026-06-12 17:00:00",
             "end_time": "2026-06-12 20:00:00"}
    args = {"reservation": _json.dumps(inner, ensure_ascii=False)}
    normalized, rewrites = _normalize_args(args)
    # Inner fields surface at top level, aliases renamed
    assert normalized["benchNo"] == "CT001"
    assert normalized["taskName"] == "测试"
    assert normalized["testPurpose"] == "感知压测"
    assert normalized["startTime"] == "2026-06-12 17:00:00"
    assert normalized["endTime"] == "2026-06-12 20:00:00"
    # The wrapper and aliases are listed in rewrites for debug
    assert any("reservation" in r for r in rewrites)
    assert any("task->taskName" in r for r in rewrites)
    assert any("purpose->testPurpose" in r for r in rewrites)


def test_dry_run_reserve_bench_handles_task_alias():
    """End-to-end: LLM passes 'task' instead of 'taskName' — dry_run
    payload should contain the canonical key, normalised. With testPurpose
    also supplied, the summary should include the task."""
    from bench_tools import handlers
    raw = handlers.dry_run_reserve_bench({
        "benchNo": "CT001",
        "startTime": "2026-06-12 17:00:00",
        "endTime": "2026-06-12 20:00:00",
        "task": "测试",  # wrong key
        "testPurpose": "感知压测",
    })
    import json as _json
    payload = _json.loads(raw)
    assert "任务：测试" in payload["summary"]
    assert payload["args"]["taskName"] == "测试"


def test_dry_run_signals_missing_fields_when_test_purpose_absent():
    """If testPurpose is missing/empty, the dry_run payload should include
    `missing_fields: ["testPurpose"]` so the LLM can prompt the user."""
    from bench_tools import handlers
    raw = handlers.dry_run_reserve_bench({
        "benchNo": "CT001",
        "startTime": "2026-06-12 17:00:00",
        "endTime": "2026-06-12 20:00:00",
        "taskName": "测试",
        # testPurpose deliberately missing
    })
    import json as _json
    payload = _json.loads(raw)
    assert payload.get("missing_fields") == ["testPurpose"]
    assert "测试目的" in payload["summary"]
    # TaskName still echoed back so the LLM knows what's already filled
    assert payload["args"]["taskName"] == "测试"


def test_dry_run_missing_fields_treats_whitespace_as_empty():
    """A field with whitespace-only is treated as missing (defensive)."""
    from bench_tools import handlers
    raw = handlers.dry_run_reserve_bench({
        "benchNo": "CT001",
        "startTime": "2026-06-12 17:00:00",
        "endTime": "2026-06-12 20:00:00",
        "taskName": "测试",
        "testPurpose": "   ",  # whitespace only
    })
    import json as _json
    payload = _json.loads(raw)
    assert "testPurpose" in payload.get("missing_fields", [])


def test_build_missing_fields_card_renders_without_action_buttons():
    """The missing-fields card should NOT have an action block — the user
    is expected to type a free-text reply, not click a button."""
    from ocl import card_builder
    captured = [{"tool": "dry_run_reserve_bench", "result": {
        "dry_run": True,
        "missing_fields": ["testPurpose"],
        "summary": "📝 预约信息不完整，请补充以下字段后再确认：\n• 测试目的",
        "args": {"benchNo": "CT001", "taskName": "测试"},
    }}]
    card = card_builder.build_card("已检测到缺字段。", captured)
    actions = [e for e in card["elements"] if e.get("tag") == "action"]
    assert actions == []  # no buttons — user replies via chat
    # Summary line present
    divs = [e for e in card["elements"] if e.get("tag") == "div"]
    assert any("测试目的" in d.get("text", {}).get("content", "") for d in divs)

def test_find_by_bench_and_time_format_tolerant():
    """The approve button's startTime (echoed by the bench API) may differ in
    format from the value the applicant submitted. Lookup must still match.
    (Regression: code-review fix #5.)"""
    reservation_store.save("rid-1", "ou_a", "a@x.com", "CT001",
                           "2026-06-11 17:00:00", "2026-06-11 20:00:00", "测试")
    # ISO 'T' separator, no seconds, trailing timezone — all must match.
    for echoed in ("2026-06-11T17:00:00", "2026-06-11 17:00",
                   "2026-06-11T17:00:00+08:00"):
        rec = reservation_store.find_by_bench_and_time("CT001", echoed)
        assert rec is not None, echoed
        assert rec["applicant_email"] == "a@x.com"


def test_find_by_bench_and_time_different_time_no_match():
    """A genuinely different time must NOT match (don't over-normalize)."""
    reservation_store.save("rid-1", "ou_a", "a@x.com", "CT001",
                           "2026-06-11 17:00:00")
    assert reservation_store.find_by_bench_and_time("CT001", "2026-06-11 18:00:00") is None
