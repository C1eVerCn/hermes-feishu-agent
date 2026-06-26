"""ocl/tool_guard.py单元测试：contextvars + CallerIdentity + guarded() Layer2兜底权限校验。"""
import json
import threading
import pytest

import ocl.identity as identity
import ocl.tool_guard as guard


@pytest.fixture(autouse=True)
def _fresh(tmp_path, monkeypatch):
 f = tmp_path / "identity_map.json"
 f.write_text(json.dumps({"ou_alice":1}, ensure_ascii=False))
 monkeypatch.setattr(identity, "_MAP_FILE", str(f))
 identity._invalidate_role_overrides()
 monkeypatch.setattr(identity, "_resolve_open_id", lambda oid: ("", ""))
 guard.set_current_caller(guard.CallerIdentity())
 yield


def test_set_and_get_current_user():
 guard.set_current_user("ou_alice")
 assert guard.get_current_user() == "ou_alice"
 guard.set_current_user("")
 assert guard.get_current_user() == ""


def test_email_context_set_and_get():
 guard.set_current_caller(guard.CallerIdentity(openid="ou_alice", email="a@b.com"))
 assert guard.get_current_email() == "a@b.com"
 guard.set_current_caller(guard.CallerIdentity())
 assert guard.get_current_email() == ""


def test_caller_defaults_empty_without_set():
    guard.set_current_caller(guard.CallerIdentity())
    assert guard.get_current_caller() == guard.CallerIdentity()
    assert guard.get_current_caller().openid == ""


def test_caller_identity_as_dict():
    """CallerIdentity.as_dict → MCP args 格式（camelCase）。"""
    c = guard.CallerIdentity(openid="ou_alice", email="a@b.com")
    d = c.as_dict()
    assert d == {"openId": "ou_alice", "emailAddress": "a@b.com"}


def test_caller_identity_as_dict_no_email():
    c = guard.CallerIdentity(openid="ou_alice")
    d = c.as_dict()
    assert d == {"openId": "ou_alice"}
    assert "emailAddress" not in d


def test_caller_identity_as_dict_with_mobile():
    c = guard.CallerIdentity(openid="ou_alice", email="a@b.com", mobile="13800000000")
    d = c.as_dict()
    assert d["mobile"] == "13800000000"


def test_caller_identity_is_authenticated():
    assert guard.CallerIdentity(openid="").is_authenticated is False
    assert guard.CallerIdentity(openid="ou_alice").is_authenticated is True


def test_set_current_caller_replaces_entire_caller():
    guard.set_current_caller(guard.CallerIdentity(openid="ou_alice", email="a@b.com"))
    # 替换会覆盖 openid/email，不能仅设一个
    guard.set_current_caller(guard.CallerIdentity(openid="ou_bob"))
    assert guard.get_current_user() == "ou_bob"
    assert guard.get_current_email() == ""


def test_context_propagates_across_thread_pool_executor():
    """Regression: bot/handler.py runs agent.chat() in a worker thread spawned
    by ThreadPoolExecutor. contextvars do NOT auto-propagate across threads."""
    import concurrent.futures
    import contextvars

    guard.set_current_caller(guard.CallerIdentity(openid="ou_alice", email="alice@immotors.com"))
    ctx = contextvars.copy_context()

    def worker():
        return (
            guard.get_current_user(),
            guard.get_current_email(),
        )

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
        uid, email = ex.submit(ctx.run, worker).result(timeout=2)

    assert uid == "ou_alice"
    assert email == "alice@immotors.com"


def test_guarded_handler_passes_when_permitted():
    """role=1 用户调 level=1 工具 → 通过。"""
    guard.set_current_caller(guard.CallerIdentity(openid="ou_alice", email="a@b.com"))
    calls = []
    inner = lambda args: (calls.append(args), "ok")[1]
    wrapped = guard.guarded("fetch_available_vehicles", inner)
    result = wrapped({"vehicleType": "DM2"})
    assert result == "ok"
    assert len(calls) == 1


def test_guarded_handler_blocks_unknown_tool():
    """工具名不在 ocl.permission.ALL_TOOLS → 直接拦截。"""
    guard.set_current_caller(guard.CallerIdentity(openid="ou_alice", email="a@b.com"))
    inner = lambda args: "should not run"
    wrapped = guard.guarded("drop_database", inner)
    result = wrapped({})
    data = json.loads(result)
    assert "error" in data


def test_guarded_blocks_unregistered_user_from_any_tool():
    """未注册用户 (role=0) 调任何已知工具 → Layer2 拦截。"""
    guard.set_current_caller(guard.CallerIdentity(openid="ou_ghost", email="g@x.com"))
    inner = lambda args: "should not run"
    wrapped = guard.guarded("approval_vehicle_reservation", inner)
    result = wrapped({})
    data = json.loads(result)
    assert "error" in data
    assert "权限不足" in data["error"]


def test_guarded_blocks_role1_from_level2_approval_tool():
    """role=1 工程师调 approval_vehicle_reservation（审批工具，仅 2/3/5）→ 拦截。"""
    guard.set_current_caller(guard.CallerIdentity(openid="ou_alice", email="a@b.com"))
    inner = lambda args: "should not run"
    wrapped = guard.guarded("approval_vehicle_reservation", inner)
    result = wrapped({})
    data = json.loads(result)
    assert "权限不足" in data["error"]


def test_guarded_tolerates_extra_kwargs():
    """registry.dispatch 注入 task_id/user_task — guarded 必须容忍。"""
    guard.set_current_caller(guard.CallerIdentity(openid="ou_alice", email="a@b.com"))
    inner = lambda args: "ok"
    wrapped = guard.guarded("fetch_available_vehicles", inner)
    assert wrapped({"vehicleType": "DM2"}, task_id="abc", user_task="x") == "ok"


def test_guarded_passes_when_no_user_set():
    """无 open_id 时跳过门控（hermes内部/系统调用）。"""
    guard.set_current_caller(guard.CallerIdentity())
    called = []
    inner = lambda args: (called.append(True), "ok")[1]
    wrapped = guard.guarded("approval_vehicle_reservation", inner)
    result = wrapped({})
    assert result == "ok"
    assert called


def test_legacy_set_current_user_preserves_email():
    """旧 API（set_current_user）只改 openid，保留已有 email。"""
    guard.set_current_caller(guard.CallerIdentity(openid="ou_alice", email="a@b.com"))
    guard.set_current_user("ou_bob")
    assert guard.get_current_user() == "ou_bob"
    assert guard.get_current_email() == "a@b.com"  # email 保留


def test_legacy_set_current_email_preserves_user():
    guard.set_current_caller(guard.CallerIdentity(openid="ou_alice", email=""))
    guard.set_current_email("new@x.com")
    assert guard.get_current_user() == "ou_alice"
    assert guard.get_current_email() == "new@x.com"
