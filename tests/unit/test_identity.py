import json
import pytest
import ocl.identity as identity


@pytest.fixture(autouse=True)
def _fresh(tmp_path, monkeypatch):
    f = tmp_path / "identity_map.json"
    f.write_text(json.dumps({
        "ou_zhang": {"email": "zhangsan@example.com", "name": "张三", "role": 1},
        "ou_admin": {"email": "admin@example.com", "name": "王五", "role": 3},
    }, ensure_ascii=False))
    monkeypatch.setattr(identity, "_MAP_FILE", str(f))
    identity._invalidate_cache()
    yield


def test_lookup_known_user():
    info = identity.lookup("ou_zhang")
    assert info["email"] == "zhangsan@example.com"
    assert info["role"] == 1


def test_lookup_unknown_returns_none():
    assert identity.lookup("ou_ghost") is None


def test_email_of_known():
    assert identity.email_of("ou_zhang") == "zhangsan@example.com"


def test_email_of_unknown_is_empty():
    assert identity.email_of("ou_ghost") == ""


def test_role_of_unknown_is_zero():
    assert identity.role_of("ou_ghost") == 0


def test_set_role_persists():
    identity.set_role("ou_new", "new@example.com", "新人", 2)
    assert identity.role_of("ou_new") == 2
    assert identity.email_of("ou_new") == "new@example.com"
