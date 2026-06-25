"""tests for 手机号作为第二识别符（mobile as a secondary identifier）。

覆盖：identity_admin 存/查 mobile；ocl.identity.mobile_of / open_id_of_mobile；
admin「查看用户 <手机号>」查人。邮箱仍为主识别符、上游 fmp 仍按 emailAddress 鉴权。
"""
import json
import pytest

import ocl.identity as identity
from bot.identity_admin import IdentityAdmin


# ── identity_admin：存 / 查 mobile ───────────────────────────────────────
@pytest.fixture
def admin(tmp_path):
    return IdentityAdmin(str(tmp_path / "m.json"), str(tmp_path / "a.jsonl"))


def test_auto_register_stores_mobile(admin):
    admin.auto_register("ou_1", email="a@x.com", name="张三", mobile="13800138000")
    rec = admin.get("ou_1")
    assert rec["mobile"] == "13800138000"
    assert rec["email"] == "a@x.com"


def test_update_profile_sets_mobile(admin):
    admin.auto_register("ou_2", email="b@x.com", name="李四")
    admin.update_profile("ou_2", mobile="13900139000")
    assert admin.get("ou_2")["mobile"] == "13900139000"
    # email 不被 mobile 更新覆盖
    assert admin.get("ou_2")["email"] == "b@x.com"


def test_find_by_mobile(admin):
    admin.auto_register("ou_3", email="c@x.com", name="王五", mobile="13700137000")
    hit = admin.find_by_mobile("13700137000")
    assert hit is not None and hit[0] == "ou_3"
    assert admin.find_by_mobile("00000000000") is None
    assert admin.find_by_mobile("") is None


def test_find_by_email(admin):
    admin.auto_register("ou_4", email="d@x.com", name="赵六", mobile="13600136000")
    hit = admin.find_by_email("d@x.com")
    assert hit is not None and hit[0] == "ou_4"


# ── ocl.identity：mobile_of / open_id_of_mobile（读 identity_map）─────────
@pytest.fixture
def map_with_mobile(tmp_path, monkeypatch):
    f = tmp_path / "identity_map.json"
    f.write_text(json.dumps({
        "ou_a": {"role": 1, "email": "a@x.com", "mobile": "13800138000"},
        "ou_b": {"role": 2, "email": "b@x.com"},  # 无 mobile
    }, ensure_ascii=False))
    monkeypatch.setattr(identity, "_MAP_FILE", str(f))
    identity._invalidate_role_overrides()
    monkeypatch.setattr(identity, "_resolve_open_id", lambda oid: ("", ""))
    yield
    identity._invalidate_role_overrides()


def test_mobile_of_reads_map(map_with_mobile):
    assert identity.mobile_of("ou_a") == "13800138000"
    assert identity.mobile_of("ou_b") == ""   # 无 mobile
    assert identity.mobile_of("ou_unknown") == ""
    assert identity.mobile_of("") == ""


def test_open_id_of_mobile_reverse(map_with_mobile):
    assert identity.open_id_of_mobile("13800138000") == "ou_a"
    assert identity.open_id_of_mobile("00000000000") == ""
    assert identity.open_id_of_mobile("") == ""


def test_email_still_primary(map_with_mobile):
    # 邮箱解析与反查不受 mobile 改动影响（兼容）
    assert identity.open_id_of("a@x.com") == "ou_a"
    assert identity.role_of("ou_a") == 1


# ── replies：查看用户 <手机号> ─────────────────────────────────────────────
def test_admin_view_user_by_mobile(monkeypatch, tmp_path):
    from bot import replies, identity_admin
    a = IdentityAdmin(str(tmp_path / "m2.json"), str(tmp_path / "a2.jsonl"))
    a.auto_register("ou_x", email="x@x.com", name="孙七", mobile="13500135000")
    a.set_role("ou_x", 2, operator="test")
    monkeypatch.setattr(identity_admin, "_singleton", a)
    out = replies._format_user_list("13500135000")
    assert "孙七" in out and "13500135000" in out and "调度员" in out


def test_admin_set_mobile_command(monkeypatch, tmp_path):
    from bot import replies, identity_admin
    a = IdentityAdmin(str(tmp_path / "m3.json"), str(tmp_path / "a3.jsonl"))
    a.auto_register("ou_y", email="y@x.com", name="周八")
    a.set_role("ou_y", 1, operator="test")
    monkeypatch.setattr(identity_admin, "_singleton", a)
    # 管理员身份（OCL_ADMIN_USER_IDS）
    monkeypatch.setattr(replies, "is_admin", lambda uid: True)
    out = replies.handle_admin_command("设置手机 ou_y 138-0013-8000", "ou_admin")
    assert "已设置" in out and "13800138000" in out  # 归一化去连字符
    assert a.get("ou_y")["mobile"] == "13800138000"


def test_admin_set_mobile_creates_if_absent(monkeypatch, tmp_path):
    from bot import replies, identity_admin
    a = IdentityAdmin(str(tmp_path / "m4.json"), str(tmp_path / "a4.jsonl"))
    monkeypatch.setattr(identity_admin, "_singleton", a)
    monkeypatch.setattr(replies, "is_admin", lambda uid: True)
    replies.handle_admin_command("绑定手机 ou_new 13900139000", "ou_admin")
    assert a.get("ou_new")["mobile"] == "13900139000"


def test_dmz_memory_strips_mobile():
    from bot.dmz_memory import _strip_sensitive
    cleaned = _strip_sensitive({"mobile": "13800138000", "openId": "ou_x",
                                "vehicleNo": "PNV332"})
    assert "mobile" not in cleaned and "openId" not in cleaned
    assert cleaned.get("vehicleNo") == "PNV332"  # 非敏感字段保留
