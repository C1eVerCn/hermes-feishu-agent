"""commit 守卫 (car_tools/handlers._check_dry_run_guard) 单元测试。

覆盖 4 个失败分支 + 1 个 pass 分支 + 1 个 fail-open 分支。
mock 模式：直接调用 _check_dry_run_guard（不调 _commit），避免 mcp_client。
"""
import json
import time as _time

import pytest

from car_tools import handlers
from car_tools import mcp_client
from car_tools.mcp_client import McpError, McpToolNotFound
from ocl import tool_capture
from ocl.tool_guard import set_current_caller, set_current_session, CallerIdentity


@pytest.fixture(autouse=True)
def _fresh_context():
    import time as _t
    from bot import dry_run_state
    set_current_caller(CallerIdentity())
    set_current_session("")
    tool_capture.clear("test_session")
    with dry_run_state._lock:
        dry_run_state._store.clear()
    dry_run_state.set_clock(_t.time)
    yield
    set_current_caller(CallerIdentity())
    set_current_session("")
    tool_capture.clear("test_session")
    with dry_run_state._lock:
        dry_run_state._store.clear()
    dry_run_state.set_clock(_t.time)


def _seed_dry_run(session_id, *, args, missing=None, dry_run=True, ts=None):
    """往 tool_capture 灌一条 _dry_run 记录。"""
    entry_result = {"dry_run": dry_run, "args": args}
    if missing:
        entry_result["missing_fields"] = missing
    # 直接用 _store（绕过 record 的 clock）以控制 timestamp
    with tool_capture._lock:
        ts = ts if ts is not None else _time.time()
        tool_capture._store.setdefault(session_id, []).append({
            "tool": "_dry_run_vehicle_reservation",
            "result": entry_result,
            "timestamp": ts,
        })


# ── 1. 收紧 fail-open：无 session 且无 dry_run_state → 拒绝 ───────────────

def test_guard_rejects_when_no_session_and_no_state():
    """删除 FSM/卡片路径后：openid 在但既无 session 又无 dry_run_state → 拒绝（堵漏）。"""
    set_current_caller(CallerIdentity(openid="ou_a", email="a@x.com"))
    # 不调 set_current_session；dry_run_state 为空
    err = handlers._check_dry_run_guard({"vehicleNo": "PNV1"})
    assert err is not None
    assert "_dry_run" in err


def test_guard_rejects_when_both_anchors_empty():
    """session 与 openid 都为空（匿名/误配）→ 拒绝下单。"""
    set_current_caller(CallerIdentity())  # openid=""
    err = handlers._check_dry_run_guard({"vehicleNo": "PNV1"})
    assert err is not None
    assert "缺失" in err


# ── 多轮：tool_capture 已清，回落 dry_run_state（按 openid）──────────────

def test_guard_passes_via_dry_run_state_across_turns():
    """模拟多轮：本轮 tool_capture 空（已清），但 dry_run_state 有上一轮完整快照 → 通过。"""
    from bot import dry_run_state
    dry_run_state.set_clock(lambda: 5000.0)
    set_current_caller(CallerIdentity(openid="ou_a", email="a@x.com"))
    set_current_session("test_session")  # session 在，但 capture 是空的
    dry_run_state.save("ou_a", {
        "vehicle_type": "DM2", "platform": "Xavier",
        "start_time": "2026-06-30 09:00", "end_time": "2026-06-30 10:00",
        "task_name": "MFF调试", "location": "上海",
    }, ts=5000.0)
    err = handlers._check_dry_run_guard({
        "vehicleType": "DM2", "platform": "Xavier",
        "startTime": "2026-06-30 09:00", "endTime": "2026-06-30 10:00",
        "taskName": "MFF调试", "location": "上海",
    })
    assert err is None


def test_guard_state_rejects_on_args_mismatch():
    from bot import dry_run_state
    set_current_caller(CallerIdentity(openid="ou_a", email="a@x.com"))
    set_current_session("test_session")
    dry_run_state.save("ou_a", {
        "vehicle_type": "DM2", "platform": "Xavier",
        "start_time": "2026-06-30 09:00", "end_time": "2026-06-30 10:00",
        "task_name": "MFF调试", "location": "上海",
    })
    err = handlers._check_dry_run_guard({
        "vehicleType": "CT1",  # ← 与快照不一致
        "platform": "Xavier",
        "startTime": "2026-06-30 09:00", "endTime": "2026-06-30 10:00",
        "taskName": "MFF调试", "location": "上海",
    })
    assert err is not None
    assert "不一致" in err


# ── 2. 显式注入 session 但无 dry_run → 拒绝 ──────────────────────────────

def test_guard_rejects_when_no_dry_run():
    set_current_caller(CallerIdentity(openid="ou_a", email="a@x.com"))
    set_current_session("test_session")
    # 没灌记录
    err = handlers._check_dry_run_guard({"vehicleNo": "PNV1"})
    assert err is not None
    assert "_dry_run" in err


# ── 3. dry_run 有 missing_fields → 拒绝 ─────────────────────────────────

def test_guard_rejects_when_dry_run_has_missing():
    set_current_caller(CallerIdentity(openid="ou_a", email="a@x.com"))
    set_current_session("test_session")
    _seed_dry_run("test_session",
                  args={"vehicle_type": "", "platform": "", "start_time": "", "end_time": "",
                        "task_name": "", "location": ""},
                  missing=["vehicle_type", "platform", "start_time", "end_time",
                           "task_name", "location"])
    err = handlers._check_dry_run_guard({"vehicleNo": "PNV1"})
    assert err is not None
    assert "缺字段" in err


# ── 4. args 不一致 → 拒绝 ──────────────────────────────────────────────

def test_guard_rejects_on_args_mismatch():
    set_current_caller(CallerIdentity(openid="ou_a", email="a@x.com"))
    set_current_session("test_session")
    _seed_dry_run("test_session", args={
        "vehicle_type": "DM2", "platform": "Xavier",
        "start_time": "2026-06-30 09:00", "end_time": "2026-06-30 10:00",
        "task_name": "MFF调试", "location": "上海",
    })
    err = handlers._check_dry_run_guard({
        "vehicleNo": "PNV1", "vehicleType": "CT1",  # ← 与 dry_run 不一致
        "platform": "Xavier",
        "startTime": "2026-06-30 09:00", "endTime": "2026-06-30 10:00",
        "taskName": "MFF调试", "location": "上海",
    })
    assert err is not None
    assert "不一致" in err


# ── 5. 超过 10 分钟 → 拒绝 ─────────────────────────────────────────────

def test_guard_rejects_when_dry_run_stale(monkeypatch):
    set_current_caller(CallerIdentity(openid="ou_a", email="a@x.com"))
    set_current_session("test_session")
    stale_ts = _time.time() - 700  # 11+ 分钟前
    _seed_dry_run("test_session",
                  args={"vehicle_type": "DM2", "platform": "Xavier",
                        "start_time": "2026-06-30 09:00", "end_time": "2026-06-30 10:00",
                        "task_name": "MFF调试", "location": "上海"},
                  ts=stale_ts)
    err = handlers._check_dry_run_guard({
        "vehicleType": "DM2", "platform": "Xavier",
        "startTime": "2026-06-30 09:00", "endTime": "2026-06-30 10:00",
        "taskName": "MFF调试", "location": "上海",
    })
    assert err is not None
    assert "超过" in err


# ── 6. 完整流程：args 一致 + 未过期 + 缺字段为空 → 通过 ───────────────────

def test_guard_passes_with_complete_dry_run():
    set_current_caller(CallerIdentity(openid="ou_a", email="a@x.com"))
    set_current_session("test_session")
    _seed_dry_run("test_session", args={
        "vehicle_type": "DM2", "platform": "Xavier",
        "start_time": "2026-06-30 09:00", "end_time": "2026-06-30 10:00",
        "task_name": "MFF调试", "location": "上海",
    })
    err = handlers._check_dry_run_guard({
        "vehicleType": "DM2", "platform": "Xavier",
        "startTime": "2026-06-30 09:00", "endTime": "2026-06-30 10:00",
        "taskName": "MFF调试", "location": "上海",
    })
    assert err is None


# ── 7. commit handler 集成：被守卫拒绝时不调 MCP、返回 error ───────────

class _NoMcp:
    def call(self, *a, **k):
        raise AssertionError("MCP 不应在守卫拒绝时被调用")


def test_commit_returns_guard_error_without_calling_mcp(monkeypatch):
    set_current_caller(CallerIdentity(openid="ou_a", email="a@x.com"))
    set_current_session("test_session")
    monkeypatch.setattr(mcp_client, "_client", _NoMcp())
    raw = handlers._commit_single_vehicle_reservation({
        "vehicleNo": "PNV1", "vehicleType": "DM2", "platform": "Xavier",
        "startTime": "2026-06-30 09:00", "endTime": "2026-06-30 10:00",
        "taskName": "MFF调试", "location": "上海",
    })
    parsed = json.loads(raw)
    assert "error" in parsed
    assert "_dry_run" in parsed["error"]


# ── 8. commit handler 集成：dry_run 完整 + args 一致 → 调 MCP + 触发副作用 ──

class FakeMcp:
    def __init__(self, result):
        self.calls = []
        self.result = result

    def call(self, tool_name, args, timeout=30):
        self.calls.append((tool_name, args))
        return self.result


def test_commit_with_guard_pass_triggers_side_effects(monkeypatch):
    """dry_run 完整 + 一致 → 调 MCP + 触发 reservation_store.save（dispatcher DM mock）。"""
    from car_tools import notify_dispatchers
    from bot import reservation_store

    set_current_caller(CallerIdentity(openid="ou_a", email="a@x.com"))
    set_current_session("test_session")
    _seed_dry_run("test_session", args={
        "vehicle_type": "DM2", "platform": "Xavier",
        "start_time": "2026-06-30 09:00", "end_time": "2026-06-30 10:00",
        "task_name": "MFF调试", "location": "上海",
    })

    fm = FakeMcp(result={
        "code": 200, "data": {
            "vehicleNo": "PNV1", "vehicleType": "DM2", "platform": "Xavier",
            "startTime": "2026-06-30 09:00", "endTime": "2026-06-30 10:00",
            "taskName": "MFF调试", "location": "上海",
        },
    })
    monkeypatch.setattr(mcp_client, "_client", fm)
    monkeypatch.setattr(reservation_store, "_store",
                        type("_S", (), {"put": lambda self, k, v: None, "all": lambda self: {}})())
    # 还要 mock notify_dispatchers，避免真发飞书
    class _Fut:
        def set_result(self, x): pass
    monkeypatch.setattr(notify_dispatchers, "submit_reservation_dispatchers",
                        lambda r: type("F", (), {"set_result": lambda s, x: None})())

    raw = handlers._commit_single_vehicle_reservation({
        "vehicleNo": "PNV1", "vehicleType": "DM2", "platform": "Xavier",
        "startTime": "2026-06-30 09:00", "endTime": "2026-06-30 10:00",
        "taskName": "MFF调试", "location": "上海",
    })
    parsed = json.loads(raw)
    assert "error" not in parsed, parsed
    assert parsed["vehicle_no"] == "PNV1"
    assert len(fm.calls) == 1
    assert fm.calls[0][0] == "single_vehicle_reservation"


# ── 9. status coerce 单元测试（normalizers.coerce_vehicle_status_to_int） ──

def test_coerce_vehicle_status_to_int_chinese():
    from car_tools.normalizers import coerce_vehicle_status_to_int
    assert coerce_vehicle_status_to_int("可用") == 1
    assert coerce_vehicle_status_to_int("故障") == 2
    assert coerce_vehicle_status_to_int("维保") == 3
    assert coerce_vehicle_status_to_int("报废") == 4


def test_coerce_vehicle_status_to_int_digit_string():
    from car_tools.normalizers import coerce_vehicle_status_to_int
    assert coerce_vehicle_status_to_int("1") == 1
    assert coerce_vehicle_status_to_int("4") == 4
    assert coerce_vehicle_status_to_int(1) == 1
    assert coerce_vehicle_status_to_int(2) == 2


def test_coerce_vehicle_status_to_int_invalid():
    from car_tools.normalizers import coerce_vehicle_status_to_int
    assert coerce_vehicle_status_to_int("") is None
    assert coerce_vehicle_status_to_int(None) is None
    assert coerce_vehicle_status_to_int("5") is None  # 越界
    assert coerce_vehicle_status_to_int("乱填") is None
    assert coerce_vehicle_status_to_int(True) is None  # bool 不接受
