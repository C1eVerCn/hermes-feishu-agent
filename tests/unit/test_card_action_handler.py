"""飞书卡片按钮回调的 handler 测试（车辆预约域）。"""
import json
from unittest.mock import patch
import pytest

import bot.card_action_handler as cah
import ocl.identity as identity
from ocl.tool_guard import CallerIdentity, set_current_caller


@pytest.fixture(autouse=True)
def _ident(tmp_path, monkeypatch):
    f = tmp_path / "identity_map.json"
    f.write_text(json.dumps({
        "ou_user":1,
        "ou_sched":2,
    }, ensure_ascii=False))
    monkeypatch.setattr(identity, "_MAP_FILE", str(f))
    identity._invalidate_role_overrides()
    _emails = {"ou_user": ("zhangsan@example.com", "张三"),
               "ou_sched": ("scheduler1@example.com", "调度员1")}
    monkeypatch.setattr(identity, "_resolve_open_id", lambda oid: _emails.get(oid, ("", "")))
    set_current_caller(CallerIdentity())
    yield
    set_current_caller(CallerIdentity())


# ── select_vehicle ────────────────────────────────────────────────────────

def test_select_vehicle_with_missing_fields_returns_card():
    """用户点 [选N] 但 dry_run 缺字段 → 返回 missing-fields card。"""
    patcher = patch.object(cah.car_handlers, "_dry_run_reservation",
                           return_value=json.dumps({
                               "dry_run": True,
                               "missing_fields": ["start_time", "end_time",
                                                  "task_name", "location"],
                               "summary": "缺少信息",
                               "args": {"vehicle_no": "PNV332"},
                           }, ensure_ascii=False))
    patcher.start()
    toast, card = cah.handle("ou_user", {"action": "select_vehicle",
                                          "vehicle_no": "PNV332",
                                          "vehicle_type": "DM2",
                                          "platform": "Xavier"})
    assert "已选" in toast or "已选车辆" in toast
    assert card is not None
    patcher.stop()


def test_select_vehicle_no_vehicle_no_rejected():
    toast, card = cah.handle("ou_user", {"action": "select_vehicle", "vehicle_no": ""})
    assert "缺失" in toast


# ── confirm_booking ──────────────────────────────────────────────────────

def test_confirm_booking_success_returns_card():
    result_dict = {
        "vehicle_no": "PNV332", "vehicle_type": "DM2", "platform": "Xavier",
        "start_time": "2026-06-16 09:00", "end_time": "2026-06-16 18:00",
        "task_name": "高速测试", "location": "A区",
        "dispatchers": [],
    }
    patcher = patch.object(cah.car_handlers, "_commit_single_vehicle_reservation",
                           return_value=json.dumps({"data": result_dict}, ensure_ascii=False))
    patcher.start()
    toast, card = cah.handle("ou_user", {"action": "confirm_booking",
                                          "vehicleNo": "PNV332",
                                          "vehicleType": "DM2",
                                          "platform": "Xavier",
                                          "startTime": "2026-06-16 09:00",
                                          "endTime": "2026-06-16 18:00",
                                          "taskName": "高速测试",
                                          "location": "A区"})
    assert "成功" in toast or "等待" in toast
    assert card is not None
    patcher.stop()


def test_confirm_booking_business_error():
    patcher = patch.object(cah.car_handlers, "_commit_single_vehicle_reservation",
                           return_value=json.dumps({"error": "MCP 返回格式异常"}))
    patcher.start()
    toast, card = cah.handle("ou_user", {"action": "confirm_booking",
                                          "vehicleNo": "PNV332",
                                          "startTime": "2026-06-16 09:00",
                                          "endTime": "2026-06-16 18:00",
                                          "taskName": "t", "location": "l"})
    assert "失败" in toast or "格式异常" in toast
    assert card is not None
    patcher.stop()


def test_confirm_booking_normal_user_blocked_by_role():
    """ou_ghost (role=0) 试图 confirm → 由 L2 guarded() 拦截（在工具调用边界）。

    之前的 L0 caller-side check (permission.is_tool_permitted) 已删除（与 handler.py
    _commit_confirmed_booking 一致）：L1 (hermes pre_tool_call 钩子) + L2 (guarded()
    包裹) 才是权限门控的真正边界。guarded() 的拦截测试见 test_ocl_tool_guard.py。
    """
    from ocl.tool_guard import guarded
    from car_tools import handlers as _ch
    _wrapped = guarded("_commit_vehicle_reservation", _ch._commit_single_vehicle_reservation)
    set_current_caller(CallerIdentity(openid="ou_ghost", email="", mobile=None))
    try:
        out = _wrapped({"vehicleNo": "PNV332", "startTime": "x", "endTime": "y",
                        "taskName": "t", "location": "l"})
    finally:
        set_current_caller(CallerIdentity())
    parsed = json.loads(out)
    assert "权限不足" in parsed.get("error", "")


# ── cancel_flow ──────────────────────────────────────────────────────────

def test_cancel_flow_clears_state():
    """[取消] 按钮 → 始终返回成功 toast。"""
    toast, card = cah.handle("ou_user", {"action": "cancel_flow"})
    assert "已取消" in toast


# ── unknown action ───────────────────────────────────────────────────────

def test_unknown_action_rejected():
    toast, card = cah.handle("ou_user", {"action": "nuke", "vehicleNo": "PNV332"})
    assert "不支持" in toast


# ── 兼容老 cancel_reserve action（return short tuple toast） ─────────────

def test_legacy_cancel_reserve_action():
    toast, card = cah.handle("ou_user", {"action": "cancel_reserve"})
    assert "已取消" in toast
