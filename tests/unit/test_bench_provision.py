"""Tests for bench_tools.provision — best-effort台架平台用户自动开通。"""
import pytest
from unittest.mock import patch, MagicMock


@pytest.fixture(autouse=True)
def _reset_cache():
    from bench_tools import provision
    with provision._lock:
        provision._provisioned.clear()
    yield


def test_placeholder_mobile_is_unique_and_stable():
    from bench_tools.provision import _placeholder_mobile
    a = _placeholder_mobile("alice@x.com")
    b = _placeholder_mobile("alice@x.com")
    c = _placeholder_mobile("bob@x.com")
    assert a == b  # 同邮箱恒定（用 sha256）
    assert a != c  # 跨邮箱唯一
    assert len(a) == 11 and a[0] == "9"  # 占位号段


def test_skips_empty_email():
    from bench_tools.provision import provision_now
    assert provision_now("", "x")["ok"] is False


def test_creates_employee_on_201(monkeypatch):
    from bench_tools import provision
    ok = MagicMock(); ok.json.return_value = {"code": 200, "message": "新增人员成功"}
    with patch.object(provision.httpx, "post", return_value=ok):
        r = provision.provision_now("alice@x.com", "Alice")
    assert r == {"ok": True, "status": "created", "message": "新增人员成功"}
    # 进程内已缓存
    assert "alice@x.com" in provision._provisioned


def test_treats_already_registered_as_ok(monkeypatch):
    from bench_tools import provision
    # dmz-fmp 行为：重复 email → code 500 + message「邮箱已被注册」
    bad = MagicMock(); bad.json.return_value = {"code": 500, "message": "邮箱已被注册"}
    with patch.object(provision.httpx, "post", return_value=bad):
        r = provision.provision_now("alice@x.com", "Alice")
    assert r["ok"] is True
    assert r["status"] == "already"
    assert "alice@x.com" in provision._provisioned  # 已缓存，下次不再调


def test_does_not_cache_on_rejection(monkeypatch):
    from bench_tools import provision
    bad = MagicMock(); bad.json.return_value = {"code": 403, "message": "没有权限"}
    with patch.object(provision.httpx, "post", return_value=bad):
        r = provision.provision_now("alice@x.com", "Alice")
    assert r["ok"] is False
    assert "alice@x.com" not in provision._provisioned  # 下次重试


def test_ensure_skips_when_cached():
    from bench_tools import provision
    with provision._lock:
        provision._provisioned.add("cached@x.com")
    # 即便后端挂掉也不调
    with patch.object(provision.httpx, "post", side_effect=AssertionError("must not call")):
        provision.ensure_bench_user("cached@x.com", "X")
    with patch.object(provision.httpx, "post", side_effect=AssertionError("must not call")):
        provision.ensure_bench_user("", "")  # 空 email 也不调
