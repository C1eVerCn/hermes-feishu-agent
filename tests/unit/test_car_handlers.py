"""car_tools/handlers.py 单元测试：mock mcp_client + identity 注入。

模式与之前一致 —— monkeypatch car_tools.mcp_client._client 为 FakeMcp，
handlers._call_mcp → mcp_client.get_mcp_client().call(...)
"""
import json
import pytest

from car_tools import handlers
from car_tools import mcp_client
from car_tools.mcp_client import McpError, McpToolNotFound
from ocl.tool_guard import set_current_caller, set_current_session, CallerIdentity


# ── fixtures ──────────────────────────────────────────────────────────────

class FakeMcp:
    """fake CarMcpClient —— 记录每次 call，return preconfigured result。"""
    def __init__(self, results=None, raise_exc=None):
        self.calls: list[tuple[str, dict]] = []
        self.results = results or {}
        self.raise_exc = raise_exc

    def call(self, tool_name, args, timeout=30):
        self.calls.append((tool_name, args))
        if self.raise_exc is not None:
            raise self.raise_exc
        if tool_name not in self.results:
            return {}
        return self.results[tool_name]


@pytest.fixture(autouse=True)
def _fresh_caller():
    import time as _t
    from bot import dry_run_state
    set_current_caller(CallerIdentity())
    set_current_session("")
    with dry_run_state._lock:
        dry_run_state._store.clear()
    dry_run_state.set_clock(_t.time)
    yield
    set_current_caller(CallerIdentity())
    set_current_session("")
    with dry_run_state._lock:
        dry_run_state._store.clear()
    dry_run_state.set_clock(_t.time)


def _seed_dry_run_state(openid: str, args: dict | None = None) -> None:
    """往 dry_run_state 灌一条完整快照（让守卫通过）。"""
    from bot import dry_run_state
    default_args = {
        "vehicle_type": "DM2", "platform": "Xavier",
        "start_time": "2026-06-16 09:00", "end_time": "2026-06-16 18:00",
        "task_name": "test", "location": "loc",
    }
    dry_run_state.save(openid, args if args is not None else default_args)


@pytest.fixture
def fake_mcp(monkeypatch):
    fm = FakeMcp()
    monkeypatch.setattr(mcp_client, "_client", fm)
    return fm


@pytest.fixture
def auth_caller():
    set_current_caller(CallerIdentity(openid="ou_alice", email="alice@x.com"))
    return CallerIdentity(openid="ou_alice", email="alice@x.com")


def test_commit_surfaces_business_error_message(monkeypatch):
    """上游 {code:200, data:null, message:...} 业务拒绝 → 把 message 当 error 返回，
    而非吐"上游返回格式异常: data 字段不是 dict"。"""
    fm = FakeMcp(results={"single_vehicle_reservation": {
        "code": 200, "data": None,
        "message": "当前用户在该时间段内已存在未完成的预约记录，请选择其他时间段",
    }})
    monkeypatch.setattr(mcp_client, "_client", fm)
    set_current_caller(CallerIdentity(openid="ou_a", email="a@x.com"))
    _seed_dry_run_state("ou_a", {
        "vehicle_type": "DM2", "platform": "Xavier",
        "start_time": "2026-06-26 09:50", "end_time": "2026-06-26 10:50",
        "task_name": "MFF调试", "location": "上海",
    })
    raw = handlers._commit_single_vehicle_reservation({
        "vehicleNo": "AATI25SNV639", "vehicleType": "DM2", "platform": "Xavier",
        "startTime": "2026-06-26 09:50",
        "endTime": "2026-06-26 10:50", "taskName": "MFF调试", "location": "上海"})
    res = json.loads(raw)
    assert "已存在未完成的预约记录" in res.get("error", "")
    assert "格式异常" not in res.get("error", "")


def test_commit_success_still_normalizes(monkeypatch):
    """data 是 dict（成功）→ 正常 normalize，不被 _business_error 误判。"""
    fm = FakeMcp(results={"single_vehicle_reservation": {
        "code": 200, "data": {"vehicleNo": "PNV1", "startTime": "2026-06-26 09:50",
                               "endTime": "2026-06-26 10:50", "taskName": "t", "location": "上海"}}})
    monkeypatch.setattr(mcp_client, "_client", fm)
    set_current_caller(CallerIdentity(openid="ou_a", email="a@x.com"))
    _seed_dry_run_state("ou_a", {
        "vehicle_type": "DM2", "platform": "Xavier",
        "start_time": "2026-06-26 09:50", "end_time": "2026-06-26 10:50",
        "task_name": "t", "location": "上海",
    })
    raw = handlers._commit_single_vehicle_reservation({
        "vehicleNo": "PNV1", "vehicleType": "DM2", "platform": "Xavier",
        "startTime": "2026-06-26 09:50", "endTime": "2026-06-26 10:50",
        "taskName": "t", "location": "上海"})
    res = json.loads(raw)
    assert "error" not in res
    assert res.get("vehicle_no") == "PNV1"


def test_commit_success_with_data_null_message(monkeypatch):
    """上游成功也可能 data:null，成功信息在 message（"预约成功，请联系调度员审批"）→
    必须当成功（无 error），而不是误报"提交失败"。"""
    fm = FakeMcp(results={"single_vehicle_reservation": {
        "code": 200, "data": None,
        "message": "车辆AATI25SNV639预约成功，请联系调度员审批，车辆调度员：谌一航(chenyihang@immotors.com)",
    }})
    monkeypatch.setattr(mcp_client, "_client", fm)
    set_current_caller(CallerIdentity(openid="ou_a", email="a@x.com"))
    _seed_dry_run_state("ou_a", {
        "vehicle_type": "DM2", "platform": "Xavier",
        "start_time": "2026-06-26 09:50", "end_time": "2026-06-26 10:50",
        "task_name": "MFF调试", "location": "上海",
    })
    raw = handlers._commit_single_vehicle_reservation({
        "vehicleNo": "AATI25SNV639", "vehicleType": "DM2", "platform": "Xavier",
        "startTime": "2026-06-26 09:50", "endTime": "2026-06-26 10:50",
        "taskName": "MFF调试", "location": "上海"})
    res = json.loads(raw)
    assert "error" not in res, f"成功不应有 error: {res}"
    assert res.get("success") is True
    assert "预约成功" in res.get("message", "")  # 保留 message 供 notify_dispatchers 提 email


# ── 身份注入 ──────────────────────────────────────────────────────────────

def test_caller_injected_into_args(fake_mcp, auth_caller):
    """身份注入：2026-06-18 移除 openId 注入后，只剩 emailAddress。
    9 个 MCP @Tool 函数签名都不接受 openId（注入会导致 TypeError）。"""
    handlers.fetch_available_vehicles({"vehicle_type": "DM2", "platform": "Xavier",
                                       "start_time": "2026-06-16 09:00",
                                       "end_time": "2026-06-16 18:00"})
    assert fake_mcp.calls, "mcp_client.call 未被调用"
    tool, args = fake_mcp.calls[0]
    assert tool == "fetch_available_vehicles"
    assert "openId" not in args  # 已删除
    assert args["emailAddress"] == "alice@x.com"


def test_no_email_in_args_when_empty(fake_mcp):
    """email 为空时不应注入 emailAddress 字段。openId 也不再注入。"""
    set_current_caller(CallerIdentity(openid="ou_alice"))
    handlers.fetch_available_vehicles({"vehicle_type": "DM2", "platform": "Xavier",
                                       "start_time": "2026-06-16 09:00",
                                       "end_time": "2026-06-16 18:00"})
    _, args = fake_mcp.calls[0]
    assert "openId" not in args  # 已删除
    assert "emailAddress" not in args


def test_no_mobile_field_when_none(fake_mcp):
    set_current_caller(CallerIdentity(openid="ou_alice", email="a@x.com", mobile=None))
    handlers.fetch_available_vehicles({"vehicle_type": "DM2", "platform": "Xavier",
                                       "start_time": "2026-06-16 09:00",
                                       "end_time": "2026-06-16 18:00"})
    _, args = fake_mcp.calls[0]
    assert "mobile" not in args


def test_prefers_mobile_over_email(fake_mcp):
    """2026-06-30 改造：优先 mobile —— qiongchi-dev 上游按 emailAddress 过滤，
    但很多用户在 fmp 端 email 字段是空（"chenyihang"实测 emailAddress=""）。
    只发 mobile 不会丢用户，且能查到 86 辆车；同时发反而把用户顶成"非平台用户"。
    """
    set_current_caller(CallerIdentity(openid="ou_alice", email="a@x.com", mobile="13800138000"))
    handlers.fetch_available_vehicles({"vehicle_type": "DM2"})
    _, args = fake_mcp.calls[0]
    assert args.get("mobile") == "13800138000"
    assert "emailAddress" not in args  # 关键：有 mobile 时绝不带 email（防上游按 email 过滤误杀）


def test_email_fallback_when_no_mobile(fake_mcp):
    """无 mobile、有 email → 用 email 兜底（覆盖无手机号但有邮箱的用户）。"""
    set_current_caller(CallerIdentity(openid="ou_alice", email="a@x.com", mobile=None))
    handlers.fetch_available_vehicles({"vehicle_type": "DM2"})
    _, args = fake_mcp.calls[0]
    assert "mobile" not in args
    assert args.get("emailAddress") == "a@x.com"


# ── fetch_available_vehicles ──────────────────────────────────────────────

def test_fetch_available_vehicles_happy(fake_mcp, auth_caller):
    fake_mcp.results["fetch_available_vehicles"] = [
        {"vehicleNo": "PNV332", "vehicleType": "DM2", "platform": "Xavier"},
    ]
    raw = handlers.fetch_available_vehicles({"vehicle_type": "DM2", "platform": "Xavier",
                                              "start_time": "2026-06-16 09:00",
                                              "end_time": "2026-06-16 18:00"})
    parsed = json.loads(raw)
    assert isinstance(parsed, list)
    assert parsed[0]["vehicle_no"] == "PNV332"
    assert parsed[0]["platform"] == "Xavier"


def test_fetch_available_vehicles_mcp_error(fake_mcp, auth_caller):
    """MCP 工具返回 {"error": ...} → 透传。"""
    fake_mcp.results["fetch_available_vehicles"] = {"error": "tool down"}
    raw = handlers.fetch_available_vehicles({"vehicle_type": "DM2"})
    parsed = json.loads(raw)
    assert "error" in parsed


# ── _commit_single_vehicle_reservation ───────────────────────────────────

def test_commit_calls_mcp_with_args(fake_mcp, auth_caller):
    """_commit_single_vehicle_reservation 调 MCP；返回 snake_case 序列化的 ReservationResult。"""
    fake_mcp.results["single_vehicle_reservation"] = {
        "success": True, "vehicleNo": "PNV332", "vehicleType": "DM2", "platform": "Xavier",
        "startTime": "2026-06-16 09:00", "endTime": "2026-06-16 18:00",
        "taskName": "test", "location": "loc",
    }
    _seed_dry_run_state("ou_alice")  # 守卫：注入 dry_run 快照（默认值与 commit args 对齐）
    raw = handlers._commit_single_vehicle_reservation({
        "vehicleNo": "PNV332", "vehicleType": "DM2", "platform": "Xavier",
        "startTime": "2026-06-16 09:00", "endTime": "2026-06-16 18:00",
        "taskName": "test", "location": "loc",
    })
    assert fake_mcp.calls
    tool, args = fake_mcp.calls[0]
    assert tool == "single_vehicle_reservation"
    assert args["vehicleNo"] == "PNV332"
    assert "openId" not in args  # 2026-06-18 已删除
    assert args["emailAddress"] == "alice@x.com"
    # 关键契约：返回值为 snake_case 序列化的 ReservationResult
    parsed = json.loads(raw)
    assert parsed["vehicle_no"] == "PNV332"
    assert parsed["start_time"] == "2026-06-16 09:00"
    assert parsed["task_name"] == "test"


# ── cancel / approve / return / fetch_user_* ──────────────────────────────

def test_cancel_vehicle_reservation(fake_mcp, auth_caller):
    fake_mcp.results["cancel_vehicle_reservation"] = {"vehicleNo": "PNV332"}
    handlers.cancel_vehicle_reservation({"vehicleNo": "PNV332"})
    tool, args = fake_mcp.calls[0]
    assert tool == "cancel_vehicle_reservation"
    assert args["vehicleNo"] == "PNV332"


def test_cancel_with_reservation_id(fake_mcp, auth_caller):
    fake_mcp.results["cancel_vehicle_reservation"] = {}
    handlers.cancel_vehicle_reservation({"vehicleNo": "PNV332", "reservationId": "RES123"})
    _, args = fake_mcp.calls[0]
    assert args["reservationId"] == "RES123"


def test_approval_vehicle_reservation(fake_mcp, auth_caller):
    fake_mcp.results["approval_vehicle_reservation"] = {
        "approved": True, "vehicleNo": "PNV332",
        "startTime": "2026-06-16 09:00", "endTime": "2026-06-16 18:00",
        "taskName": "test", "reviewer": "Alice",
    }
    raw = handlers.approval_vehicle_reservation({"vehicleNo": "PNV332", "approved": True})
    parsed = json.loads(raw)
    assert parsed["approved"] is True


def test_return_vehicle(fake_mcp, auth_caller):
    fake_mcp.results["return_vehicle"] = {
        "vehicleNo": "PNV332", "returnLocation": "A区",
        "keyPosition": "抽屉", "changeModule": "无", "vehicleStatus": "1",
    }
    raw = handlers.return_vehicle({
        "vehicleNo": "PNV332", "returnLocation": "A区",
        "keyPosition": "抽屉", "changeModule": "无", "vehicleStatus": "1",
    })
    parsed = json.loads(raw)
    assert parsed["vehicle_no"] == "PNV332"


def test_fetch_user_reservation(fake_mcp, auth_caller):
    fake_mcp.results["fetch_user_reservation"] = [
        {"vehicleNo": "PNV332", "startTime": "2026-06-16 09:00",
         "endTime": "2026-06-16 18:00", "status": "待审批"},
    ]
    raw = handlers.fetch_user_reservation({})
    parsed = json.loads(raw)
    assert parsed[0]["vehicle_no"] == "PNV332"
    assert parsed[0]["status"] == "待审批"


def test_fetch_user_approval(fake_mcp, auth_caller):
    fake_mcp.results["fetch_user_approval"] = [
        {"vehicleNo": "PNV332", "startTime": "2026-06-16 09:00",
         "endTime": "2026-06-16 18:00", "status": "待审批"},
    ]
    raw = handlers.fetch_user_approval({})
    parsed = json.loads(raw)
    assert len(parsed) == 1


def test_get_user_context(fake_mcp, auth_caller):
    fake_mcp.results["get_user_context"] = {"department": "智驾"}
    raw = handlers.get_user_context({})
    _, args = fake_mcp.calls[0]
    assert args.get("emailAddress") == "alice@x.com"
    assert "openId" not in args  # 2026-06-18 已删除


def test_get_common_dictionary_via_mcp(fake_mcp, auth_caller):
    """MCP 端返回有效 items → 透传。"""
    fake_mcp.results["get_common_dictionary"] = {"items": [{"code": "Xavier", "name": "Xavier 芯片"}]}
    raw = handlers.get_common_dictionary({"typeCode": "VEHICLE_CHIP"})
    parsed = json.loads(raw)
    assert parsed["items"][0]["code"] == "Xavier"


def test_get_common_dictionary_fallback_on_tool_not_found(fake_mcp, auth_caller):
    """MCP 端未暴露该工具 → 内置 fallback 字典。"""
    fake_mcp.raise_exc = McpToolNotFound("not registered")
    raw = handlers.get_common_dictionary({"typeCode": "VEHICLE_CHIP"})
    parsed = json.loads(raw)
    assert parsed["items"][0]["code"] == "Xavier"
    assert "Xavier 芯片" in parsed["items"][0]["name"]


def test_get_common_dictionary_fallback_on_unknown_type(fake_mcp, auth_caller):
    """MCP 端无该字典类型 + fallback 也没有 → error。"""
    fake_mcp.raise_exc = McpToolNotFound("not registered")
    raw = handlers.get_common_dictionary({"typeCode": "UNKNOWN"})
    parsed = json.loads(raw)
    assert "error" in parsed
    assert "UNKNOWN" in parsed["error"]


# ── _dry_run_reservation（纯本地逻辑，不打 MCP） ────────────────────────

def test_dry_run_reservation_missing_fields(auth_caller):
    """缺 vehicle_type / start_time → 返回 missing_fields + summary。"""
    raw = handlers._dry_run_reservation({"vehicleNo": "PNV332"})
    parsed = json.loads(raw)
    assert parsed["dry_run"] is True
    assert "vehicle_type" in parsed["missing_fields"]
    assert "缺少以下信息" in parsed["summary"]


def test_dry_run_reservation_no_call_out_to_mcp(monkeypatch, auth_caller):
    """_dry_run 永远不应该调 MCP（缺字段时直接走本地缺失字段路径）。"""
    called = []
    class _NoCall:
        def call(self, *a, **k):
            called.append(a)
            return {}
    monkeypatch.setattr(mcp_client, "_client", _NoCall())
    handlers._dry_run_reservation({"vehicleNo": "PNV332"})
    assert not called, "_dry_run 不应触发任何 MCP 调用"
