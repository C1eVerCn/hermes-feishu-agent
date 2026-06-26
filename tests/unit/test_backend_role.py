"""tests for bot/backend_role — 后端角色事实源（mock mcp_client，离线）。"""
import pytest

from bot import backend_role
from car_tools import mcp_client


class _FakeMcp:
    def __init__(self, resp):
        self.resp = resp
        self.calls = 0

    def call(self, tool_name, args, timeout=30):
        self.calls += 1
        return self.resp


@pytest.fixture(autouse=True)
def _clear():
    backend_role.clear_cache()
    yield
    backend_role.clear_cache()


def test_role_from_backend(monkeypatch):
    fm = _FakeMcp({"code": 200, "data": {"role": 2, "emailAddress": "a@x.com"}})
    monkeypatch.setattr(mcp_client, "_client", fm)
    assert backend_role.role_of_backend("a@x.com") == 2


def test_caches_within_ttl(monkeypatch):
    fm = _FakeMcp({"code": 200, "data": {"role": 3}})
    monkeypatch.setattr(mcp_client, "_client", fm)
    assert backend_role.role_of_backend("b@x.com") == 3
    assert backend_role.role_of_backend("b@x.com") == 3
    assert fm.calls == 1  # 第二次走缓存


def test_data_null_returns_none(monkeypatch):
    fm = _FakeMcp({"code": 200, "data": None, "message": "不是平台用户"})
    monkeypatch.setattr(mcp_client, "_client", fm)
    assert backend_role.role_of_backend("c@x.com") is None  # 不降级，交调用方


def test_invalid_role_returns_none(monkeypatch):
    fm = _FakeMcp({"code": 200, "data": {"role": 9}})  # 非 1/2/3
    monkeypatch.setattr(mcp_client, "_client", fm)
    assert backend_role.role_of_backend("d@x.com") is None
    # bool 也不算合法 role
    fm2 = _FakeMcp({"code": 200, "data": {"role": True}})
    monkeypatch.setattr(mcp_client, "_client", fm2)
    backend_role.clear_cache()
    assert backend_role.role_of_backend("e@x.com") is None


def test_exception_returns_none(monkeypatch):
    class _Boom:
        def call(self, *a, **k):
            raise RuntimeError("mcp down")
    monkeypatch.setattr(mcp_client, "_client", _Boom())
    assert backend_role.role_of_backend("f@x.com") is None


def test_empty_email():
    assert backend_role.role_of_backend("") is None
    assert backend_role.role_of_backend(None) is None


# ── handler._resolve_identity 同步后端角色 ─────────────────────────────────
def test_handler_syncs_backend_role(monkeypatch, tmp_path):
    from bot import handler, identity_admin
    from bot import backend_role as br
    a = identity_admin.IdentityAdmin(str(tmp_path / "m.json"), str(tmp_path / "a.jsonl"))
    monkeypatch.setattr(identity_admin, "_singleton", a)
    monkeypatch.setattr("ocl.identity.email_of", lambda uid: "u@x.com")
    monkeypatch.setattr("ocl.identity.name_of", lambda uid: "U")
    monkeypatch.setattr("ocl.identity.mobile_of", lambda uid: "")
    # 后端说该用户是调度员(2)
    monkeypatch.setattr(br, "role_of_backend", lambda email: 2)
    role, email, name, mobile = handler._resolve_identity("ou_sync")
    assert role == 2  # 同步自后端
    assert a.get_role("ou_sync") == 2  # 写进了 identity_map


def test_handler_backend_unknown_keeps_default(monkeypatch, tmp_path):
    """后端不认识（None）+ 本地 0 → 默认普通用户（保持原行为，不降级）。"""
    from bot import handler, identity_admin
    from bot import backend_role as br
    a = identity_admin.IdentityAdmin(str(tmp_path / "m2.json"), str(tmp_path / "a2.jsonl"))
    monkeypatch.setattr(identity_admin, "_singleton", a)
    monkeypatch.setattr("ocl.identity.email_of", lambda uid: "u2@x.com")
    monkeypatch.setattr("ocl.identity.name_of", lambda uid: "U2")
    monkeypatch.setattr("ocl.identity.mobile_of", lambda uid: "")
    monkeypatch.setattr(br, "role_of_backend", lambda email: None)
    role, *_ = handler._resolve_identity("ou_unknown")
    assert role == 1  # auto-in-scope 默认
