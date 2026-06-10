"""飞书卡片按钮回调的 handler 测试（已切到 bench_tools）。"""
import json
from unittest.mock import patch
import pytest

import bot.card_action_handler as cah
import ocl.identity as identity


@pytest.fixture(autouse=True)
def _ident(tmp_path, monkeypatch):
 f = tmp_path / "identity_map.json"
 f.write_text(json.dumps({
 "ou_user":1,
 "ou_sched":2,
 }, ensure_ascii=False))
 monkeypatch.setattr(identity, "_MAP_FILE", str(f))
 identity._invalidate_role_overrides()
 _emails = {"ou_user": ("zhangsan@example.com", "张三"), "ou_sched": ("scheduler1@example.com", "调度员1")}
 monkeypatch.setattr(identity, "_resolve_open_id", lambda oid: _emails.get(oid, ("", "")))
 yield


def test_cancel_calls_handler_and_returns_toast():
 patcher = patch.object(cah.handlers, "cancel_reservation", return_value='{"code":200,"message":"取消预约成功","data":null}')
 m = patcher.start()
 toast, card = cah.handle("ou_user", {"action": "cancel", "benchNo": "TJ001", "startTime": "2099-01-0109:00:00", "endTime": "2099-01-0110:00:00"})
 assert "取消" in toast
 assert m.call_count ==1
 patcher.stop()


def test_approve_by_normal_user_blocked_by_ocl():
 """OCL Layer2拦截 — approve_reservation 要求 role>=2，普通用户(role=1) 直接被拒绝。"""
 patcher = patch.object(cah.handlers, "approve_reservation", return_value='{"code":200,"message":"ok","data":null}')
 m = patcher.start()
 toast, card = cah.handle("ou_user", {"action": "approve", "benchNo": "TJ001", "approvalResult":1})
 assert "权限不足" in toast or "权限" in toast
 assert m.call_count ==0
 patcher.stop()


def test_approve_allowed_for_scheduler():
 patcher = patch.object(cah.handlers, "approve_reservation", return_value='{"code":200,"message":"审批成功:批准1条预约记录","data":null}')
 m = patcher.start()
 toast, card = cah.handle("ou_sched", {"action": "approve", "benchNo": "TJ001", "approvalResult":1})
 assert "审批" in toast or "批准" in toast
 assert m.call_count ==1
 patcher.stop()


def test_unknown_user_rejected():
 toast, card = cah.handle("ou_ghost", {"action": "cancel", "benchNo": "TJ001"})
 assert "平台用户" in toast


def test_api_business_error_surfaced_in_toast():
 patcher = patch.object(cah.handlers, "cancel_reservation", return_value='{"error":"HTTP400: 未找到待审批状态的预约记录"}')
 patcher.start()
 toast, card = cah.handle("ou_user", {"action": "cancel", "benchNo": "TJ001"})
 assert "未找到" in toast or "失败" in toast
 patcher.stop()


def test_return_without_location_requests_followup():
 toast, card = cah.handle("ou_user", {"action": "return", "benchNo": "TJ006"})
 assert "地点" in toast


def test_unknown_action_rejected():
 toast, card = cah.handle("ou_user", {"action": "nuke", "benchNo": "TJ001"})
 assert "不支持" in toast
