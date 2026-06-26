"""tests for bot/handler._resolve_identity — 角色解析（identity_map 唯一事实源）。

2026-06-26：后端 0617 把 role 从 get_user_context 拿掉后，handler 删除了后端角色
同步路径（连同 test_backend_role.py）。本文件补回对 _resolve_identity 的直接覆盖：
- 可见范围内未建档用户 → auto-in-scope 默认 role=1
- OCL_ADMIN_USER_IDS 命中 → 提到 role=3
均离线（monkeypatch ocl.identity 的 Feishu 查询 + tmp IdentityAdmin 单例）。
"""
import pytest

from bot import handler, identity_admin, replies
from ocl import identity


@pytest.fixture
def fresh_admin(tmp_path, monkeypatch):
    """把 identity_admin 单例换成 tmp 文件的全新实例，测试后还原。"""
    saved = identity_admin._singleton
    admin = identity_admin.IdentityAdmin(
        str(tmp_path / "identity_map.json"),
        str(tmp_path / "identity_audit.jsonl"),
    )
    monkeypatch.setattr(identity_admin, "_singleton", admin)
    yield admin
    identity_admin._singleton = saved


def _stub_feishu(monkeypatch, *, email="", name="", mobile=""):
    """让 _resolve_identity 不打飞书 API：身份字段从 stub 返回。"""
    monkeypatch.setattr(identity, "email_of", lambda uid: email)
    monkeypatch.setattr(identity, "name_of", lambda uid: name)
    monkeypatch.setattr(identity, "mobile_of", lambda uid: mobile)


def test_resolve_identity_in_scope_defaults_role1(fresh_admin, monkeypatch):
    """可见范围内未建档用户经 _resolve_identity → 默认普通用户(role=1)。"""
    _stub_feishu(monkeypatch, email="newbie@x.com", name="新人")
    monkeypatch.setattr(replies, "admin_ids", lambda: set())

    role, email, name, mobile = handler._resolve_identity("ou_newbie")

    assert role == 1
    assert email == "newbie@x.com"
    # 已落档，再次解析仍是 role=1（幂等）
    assert fresh_admin.get_role("ou_newbie") == 1


def test_resolve_identity_env_admin_elevates_to_3(fresh_admin, monkeypatch):
    """OCL_ADMIN_USER_IDS 命中 → 自动提到管理员(role=3)。"""
    _stub_feishu(monkeypatch, email="boss@x.com", name="头儿")
    monkeypatch.setattr(replies, "admin_ids", lambda: {"ou_boss"})

    role, _email, _name, _mobile = handler._resolve_identity("ou_boss")

    assert role == 3


def test_resolve_identity_no_email_still_in_scope(fresh_admin, monkeypatch):
    """无 email（飞书没拿到）仍按可见范围默认 role=1，不降级为陌生人。"""
    _stub_feishu(monkeypatch, email="", name="")
    monkeypatch.setattr(replies, "admin_ids", lambda: set())

    role, _email, _name, _mobile = handler._resolve_identity("ou_no_email")

    assert role == 1
