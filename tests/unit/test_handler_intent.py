"""Unit tests for handler.py intent detection — identity, admin, simple intents."""
import json
import pytest
from unittest.mock import patch


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    monkeypatch.setenv("FEISHU_APP_ID", "test")
    monkeypatch.setenv("FEISHU_APP_SECRET", "test")
    monkeypatch.setenv("MINIMAX_API_KEY", "test")
    monkeypatch.setenv("OCL_ADMIN_USER_IDS", "ou_admin1,ou_admin2")
    import ocl.identity as identity
    f = tmp_path / "identity_map.json"
    f.write_text(json.dumps({
        "ou_user":  {"email": "zhangsan@example.com", "name": "张三", "role": 1},
        "ou_sched": {"email": "scheduler1@example.com", "name": "调度员1", "role": 2},
        "ou_admin": {"email": "admin@example.com", "name": "王五", "role": 3},
    }, ensure_ascii=False))
    monkeypatch.setattr(identity, "_MAP_FILE", str(f))
    identity._invalidate_cache()


def _make_handler():
    import importlib
    import bot.handler
    importlib.reload(bot.handler)
    return bot.handler


# ── Identity query ───────────────────────────────────────────────────────────

def test_my_permission_reports_role():
    handler = _make_handler()
    assert "调度员" in handler._handle_identity_query("我的权限", "ou_sched")
    assert "普通用户" in handler._handle_identity_query("我的权限", "ou_user")


def test_identity_query_empty_for_other_text():
    handler = _make_handler()
    assert handler._handle_identity_query("帮我预约台架", "ou_user") == ""


# ── Admin set-role ─────────────────────────────────────────────────────────────

def test_set_role_admin_command():
    handler = _make_handler()
    out = handler._handle_admin_command("设置角色 ou_target 2", "ou_admin")  # role-3 admin
    assert "已设置" in out
    import ocl.identity as identity
    assert identity.role_of("ou_target") == 2


def test_set_role_rejected_for_non_admin():
    handler = _make_handler()
    assert handler._handle_admin_command("设置角色 ou_target 2", "ou_user") == ""


def test_set_role_bad_format_returns_empty():
    handler = _make_handler()
    assert handler._handle_admin_command("设置角色 ou_target 9", "ou_admin") == ""


# ── Identity gate ───────────────────────────────────────────────────────────────

def test_non_platform_user_reply_constant():
    handler = _make_handler()
    assert handler._NON_PLATFORM_REPLY_TEMPLATE.startswith("您还不是平台用户")


# ── Layer 0: Simple intent instant replies ──────────────────────────────────

def test_simple_greeting_matched():
    handler = _make_handler()
    assert handler._match_simple_intent("你好")
    assert handler._match_simple_intent("hi")
    assert handler._match_simple_intent("Hello")


def test_simple_help_matched():
    handler = _make_handler()
    assert handler._match_simple_intent("帮助")
    assert handler._match_simple_intent("help")


def test_thanks_matched():
    handler = _make_handler()
    assert handler._match_simple_intent("谢谢")
    assert handler._match_simple_intent("thanks")


def test_identity_matched():
    handler = _make_handler()
    assert handler._match_simple_intent("你是谁")
    assert handler._match_simple_intent("你能做什么")


def test_complex_query_not_matched():
    handler = _make_handler()
    assert not handler._match_simple_intent("帮我查一下可用台架")
    assert not handler._match_simple_intent("预约台架 TJ002")
    assert not handler._match_simple_intent("你好啊，最近台架忙吗")  # trailing content
