
"""bot/identity_admin.py 单元测试。

覆盖：schema 转换 / 角色边界 / 写盘 / audit / 防重写 / bulk_upsert / 迁移 v1。
"""
import json
from pathlib import Path

import pytest

from bot.identity_admin import IdentityAdmin, get_admin, ALLOWED_ROLES, ROLE_NAMES


@pytest.fixture
def admin(tmp_path):
    map_file = tmp_path / "identity_map.json"
    audit_file = tmp_path / "identity_audit.jsonl"
    return IdentityAdmin(str(map_file), str(audit_file))


def test_initial_empty(admin):
    assert admin.list_all() == {}
    assert admin.get_role("ou_nobody") == 0


def test_auto_register_creates_pending_user(admin):
    ok, msg = admin.auto_register("ou_alice", email="a@b.com", name="Alice")
    assert ok is True
    rec = admin.get("ou_alice")
    assert rec["role"] == 0
    assert rec["email"] == "a@b.com"
    assert rec["name"] == "Alice"
    assert rec["registered_via"] == "auto_first_contact"
    assert msg == "created"


def test_auto_register_does_not_overwrite_existing(admin):
    admin.set_role("ou_alice", 3, operator="admin_root")
    ok, msg = admin.auto_register("ou_alice", email="new@b.com")
    assert ok is False
    assert msg == "already_registered"
    rec = admin.get("ou_alice")
    assert rec["role"] == 3
    assert rec["email"] == ""
    assert "role unchanged" in str(rec) or rec.get("role") == 3


def test_set_role_validates_range(admin):
    admin.auto_register("ou_alice")
    ok, msg = admin.set_role("ou_alice", 6)   # 6 越界（fmp 内置角色到 5）
    assert ok is False
    assert "invalid_role" in msg
    ok, msg = admin.set_role("ou_alice", -1)
    assert ok is False
    assert "invalid_role" in msg


def test_set_role_valid_values(admin):
    admin.auto_register("ou_alice")
    for r in (0, 1, 2, 3, 4, 5):   # 与 fmp sys_role 对齐：含司机(4)/组管理员(5)
        ok, msg = admin.set_role("ou_alice", r, operator="admin_root")
        assert ok is True
        assert admin.get_role("ou_alice") == r


def test_set_role_creates_if_not_exists(admin):
    ok, msg = admin.set_role("ou_brand_new", 2, operator="admin_root", note="promoted")
    assert ok is True
    rec = admin.get("ou_brand_new")
    assert rec["role"] == 2
    assert rec["note"] == "promoted"
    assert rec["registered_via"] == "admin_assign"


def test_set_role_requires_open_id(admin):
    ok, msg = admin.set_role("", 1, operator="admin")
    assert ok is False
    assert "missing_open_id" in msg


def test_update_profile_keeps_role(admin):
    admin.set_role("ou_alice", 2, operator="admin")
    ok, _ = admin.update_profile("ou_alice", email="alice@new.com", name="Alice New", operator="bot")
    assert ok is True
    rec = admin.get("ou_alice")
    assert rec["role"] == 2
    assert rec["email"] == "alice@new.com"
    assert rec["name"] == "Alice New"


def test_update_profile_rejects_unknown_user(admin):
    ok, msg = admin.update_profile("ou_ghost", email="x@y.com")
    assert ok is False
    assert msg == "not_found"


def test_list_by_role(admin):
    admin.set_role("ou_a", 1, operator="admin")
    admin.set_role("ou_b", 2, operator="admin")
    admin.set_role("ou_c", 3, operator="admin")
    admin.set_role("ou_d", 0, operator="admin")
    assert set(admin.list_by_role(1).keys()) == {"ou_a"}
    assert set(admin.list_by_role(2).keys()) == {"ou_b"}
    assert set(admin.list_by_role(3).keys()) == {"ou_c"}
    assert set(admin.list_by_role(0).keys()) == {"ou_d"}


def test_list_pending(admin):
    admin.set_role("ou_pending1", 0, operator="admin")
    admin.set_role("ou_pending2", 0, operator="admin")
    admin.set_role("ou_active", 1, operator="admin")
    pending = admin.list_pending()
    assert set(pending.keys()) == {"ou_pending1", "ou_pending2"}


def test_is_platform_user(admin):
    admin.set_role("ou_real", 1, operator="admin")
    assert admin.is_platform_user("ou_real") is True
    admin.set_role("ou_pending", 0, operator="admin")
    assert admin.is_platform_user("ou_pending") is False
    assert admin.is_platform_user("ou_ghost") is False


def test_bulk_upsert_creates_new(admin):
    members = [
        {"open_id": "ou_a", "email": "a@b.com", "name": "Alice"},
        {"open_id": "ou_b", "email": "b@b.com", "name": "Bob"},
    ]
    result = admin.bulk_upsert_from_feishu_org(members)
    assert result == {"created": 2, "updated": 0}
    assert admin.get_role("ou_a") == 1
    assert admin.get_role("ou_b") == 1


def test_bulk_upsert_preserves_existing_role(admin):
    admin.set_role("ou_alice", 3, operator="admin")
    members = [{"open_id": "ou_alice", "email": "new@b.com", "name": "Alice New"}]
    result = admin.bulk_upsert_from_feishu_org(members)
    assert result == {"created": 0, "updated": 1}
    rec = admin.get("ou_alice")
    assert rec["role"] == 3
    assert rec["email"] == "new@b.com"
    assert rec["name"] == "Alice New"


def test_bulk_upsert_skips_empty_open_id(admin):
    members = [{"open_id": "", "email": "x@y.com", "name": "X"}]
    result = admin.bulk_upsert_from_feishu_org(members)
    assert result == {"created": 0, "updated": 0}


def test_persistence_across_instances(tmp_path):
    map_file = tmp_path / "identity_map.json"
    audit_file = tmp_path / "identity_audit.jsonl"
    a1 = IdentityAdmin(str(map_file), str(audit_file))
    a1.set_role("ou_alice", 2, operator="admin")
    a2 = IdentityAdmin(str(map_file), str(audit_file))
    assert a2.get_role("ou_alice") == 2


def test_audit_log_recorded(admin):
    admin.set_role("ou_alice", 2, operator="admin_root", note="promoted for project X")
    lines = admin._audit_file.read_text(encoding="utf-8").splitlines()
    assert len(lines) >= 1
    rec = json.loads(lines[0])
    assert rec["op"] == "set_role"
    assert rec["open_id"] == "ou_alice"
    assert rec["operator"] == "admin_root"
    assert rec["after"]["role"] == 2
    assert rec["note"] == "promoted for project X"


def test_full_export_contains_summary(admin):
    admin.set_role("ou_a", 1, operator="admin")
    admin.set_role("ou_b", 2, operator="admin")
    export = admin.full_export()
    assert export["count"] == 2
    assert export["by_role"][1] == 1
    assert export["by_role"][2] == 1
    assert "ou_a" in export["users"]


def test_v1_format_migrated_on_load(tmp_path):
    map_file = tmp_path / "identity_map.json"
    map_file.write_text(json.dumps({"ou_v1_user": 2, "ou_v2_user": {"role": 1}}))
    audit_file = tmp_path / "audit.jsonl"
    admin = IdentityAdmin(str(map_file), str(audit_file))
    rec_v1 = admin.get("ou_v1_user")
    rec_v2 = admin.get("ou_v2_user")
    assert rec_v1["role"] == 2
    assert rec_v1["registered_via"] == "manual"
    assert rec_v2["role"] == 1


def test_role_names_constant():
    assert ROLE_NAMES[1] == "工程师"
    assert ROLE_NAMES[2] == "调度员"
    assert ROLE_NAMES[3] == "管理员"
    assert ROLE_NAMES[4] == "司机"
    assert ROLE_NAMES[5] == "组管理员"
    assert ROLE_NAMES[0] == "待审核"
    assert set(ALLOWED_ROLES) == {0, 1, 2, 3, 4, 5}


def test_get_admin_singleton():
    """get_admin 返同实例（默认路径下）。"""
    a1 = get_admin()
    a2 = get_admin()
    assert a1 is a2
