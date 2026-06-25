"""Tests for ocl/identity.py — Feishu API resolution + role overrides."""
import json
import types
import pytest

import ocl.identity as identity


@pytest.fixture(autouse=True)
def _fresh(tmp_path, monkeypatch):
    f = tmp_path / "identity_map.json"
    f.write_text(json.dumps({"ou_admin": 3, "ou_sched": 2}))
    monkeypatch.setattr(identity, "_MAP_FILE", str(f))
    identity._invalidate_role_overrides()
    identity._email_cache.clear()
    identity._name_cache.clear()
    identity._mobile_api_cache.clear()
    identity._miss_cache.clear()
    # prevent real API calls by default
    monkeypatch.setattr(identity, "_resolve_open_id", lambda oid: ("", "", ""))
    yield


def _feishu_user(email, name="测试用户"):
    return types.SimpleNamespace(
        email=email, name=name,
    )


def _fake_resp(email, name="测试用户"):
    r = types.SimpleNamespace()
    r.success = lambda: True
    r.data = types.SimpleNamespace(user=_feishu_user(email, name))
    return r


def test_role_from_override_file():
    assert identity.role_of("ou_admin") == 3
    assert identity.role_of("ou_sched") == 2


def test_role_zero_without_override(monkeypatch):
    # role_of no longer consults fake_db; only overrides count
    monkeypatch.setattr(identity, "_resolve_open_id",
                        lambda oid: ("zhangsan@example.com", "张三"))
    assert identity.role_of("ou_unknown") == 0


def test_role_zero_for_unknown_user(monkeypatch):
    monkeypatch.setattr(identity, "_resolve_open_id",
                        lambda oid: ("ghost@nowhere.com", "Ghost"))
    assert identity.role_of("ou_ghost") == 0


def test_email_of_cached(monkeypatch):
    calls = []

    def _resolve(oid):
        calls.append(oid)
        return ("a@b.com", "Alice")

    monkeypatch.setattr(identity, "_resolve_open_id", _resolve)
    assert identity.email_of("ou_x") == "a@b.com"
    assert len(calls) == 1
    # second call hits cache, no API call
    assert identity.email_of("ou_x") == "a@b.com"
    assert len(calls) == 1


def test_email_of_miss_cached():
    identity._miss_cache.add("ou_bad")
    assert identity.email_of("ou_bad") == ""


def test_lookup_returns_dict(monkeypatch):
    monkeypatch.setattr(identity, "_resolve_open_id",
                        lambda oid: ("zhangsan@example.com", "张三"))
    info = identity.lookup("ou_x")
    assert info["email"] == "zhangsan@example.com"
    assert info["name"] == "张三"
    assert info["role"] == 0  # no override → 0 (real API decides perms)


def test_set_role_persists():
    identity.set_role("ou_new", 2)
    assert identity.role_of("ou_new") == 2
    # verify it's written to file
    from ocl.identity import _load_role_overrides
    assert _load_role_overrides()["ou_new"] == 2


def test_set_role_empty_open_id_is_noop():
    identity.set_role("", 3)  # must not raise


def test_email_of_empty_open_id():
    assert identity.email_of("") == ""


def test_role_of_empty_open_id():
    assert identity.role_of("") == 0


def test_role_picks_up_external_write_via_mtime(tmp_path, monkeypatch):
    """ocl.identity must reflect writes made by bot.identity_admin to the same
    file (regression: cache loaded once → newly-registered colleague stayed
    role 0 → tool calls wrongly blocked)."""
    import os, time as _t
    f = tmp_path / "identity_map.json"
    f.write_text(json.dumps({"ou_admin": 3}))
    monkeypatch.setattr(identity, "_MAP_FILE", str(f))
    identity._invalidate_role_overrides()
    assert identity.role_of("ou_colleague") == 0          # not yet present
    # external writer (e.g. identity_admin) adds the colleague as role 1
    f.write_text(json.dumps({"ou_admin": 3, "ou_colleague": {"email": "c@x.com", "role": 1}}))
    os.utime(f, (_t.time() + 1, _t.time() + 1))           # bump mtime
    assert identity.role_of("ou_colleague") == 1          # reload picks it up
