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
        "ou_user":  1,
        "ou_sched": 2,
        "ou_admin": 3,
    }, ensure_ascii=False))
    monkeypatch.setattr(identity, "_MAP_FILE", str(f))
    identity._invalidate_role_overrides()
    monkeypatch.setattr(identity, "_resolve_open_id", lambda oid: ("", ""))


def _make_handler():
    import importlib
    import bot.handler
    importlib.reload(bot.handler)
    return bot.handler


# ── Identity query ───────────────────────────────────────────────────────────

def test_my_permission_reports_platform_user(monkeypatch):
    """Identity reply shows role name + capabilities but NOT PII
    (open_id / email / name). Privacy hardening 2026-06-10."""
    handler = _make_handler()
    monkeypatch.setattr(handler.identity, "email_of", lambda oid: "zhangsan@example.com")
    # Replace get_identity_admin to return role=1 for ou_user
    admin = handler.get_identity_admin()
    admin.set_role("ou_user", 1, operator="test", note="fixture")
    out = handler._handle_identity_query("我的权限", "ou_user")
    assert "普通用户" in out
    # PII must NOT leak to the user
    assert "zhangsan@example.com" not in out
    assert "ou_user" not in out
    # Capabilities must still be present
    assert "可查询" in out or "预约" in out


def test_my_permission_non_platform(monkeypatch):
    handler = _make_handler()
    monkeypatch.setattr(handler.identity, "email_of", lambda oid: "")
    out = handler._handle_identity_query("我的权限", "ou_ghost")
    assert "待审核" in out
    # role=0 case is the EXCEPTION: open_id must be visible so user can
    # relay it to the admin for manual role assignment.
    assert "ou_ghost" in out


def test_identity_query_empty_for_other_text():
    handler = _make_handler()
    assert handler._handle_identity_query("帮我预约台架", "ou_user") == ""


# ── Admin set-role ─────────────────────────────────────────────────────────────

def test_set_role_admin_command(tmp_path, monkeypatch):
    """用 identity_admin 验证 set_role 行为。"""
    import importlib
    import bot.identity_admin as ia_mod
    importlib.reload(ia_mod)
    from bot.identity_admin import IdentityAdmin
    # 注入 tmp identity_admin
    admin_inst = IdentityAdmin(str(tmp_path / "im.json"), str(tmp_path / "audit.jsonl"))
    monkeypatch.setattr(ia_mod, "get_admin", lambda: admin_inst)
    admin_inst.set_role("ou_admin", 3, operator="root")
    # 同样把 handler 里的引用替换
    handler = _make_handler()
    monkeypatch.setattr(handler, "get_identity_admin", lambda: admin_inst)
    out = handler._handle_admin_command("设置角色 ou_target 2", "ou_admin")
    assert "已设置" in out
    assert admin_inst.get_role("ou_target") == 2


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


# ── Fast path (single-tool query bypass) ───────────────────────────────────

class TestFastPathMatching:
    """Pure regex matching — no mocks needed."""

    def setup_method(self):
        import importlib, bot.handler
        importlib.reload(bot.handler)
        self.handler = bot.handler

    def _hit(self, text):
        return self.handler._try_fast_path(text, "ou_x", "x@y.com", 1)

    def test_query_benches_matched(self):
        assert self._hit("查询可用台架") is not None
        assert self._hit("查看台架") is not None
        assert self._hit("看看台架") is not None
        assert self._hit("有什么台架") is not None
        assert self._hit("台架列表") is not None
        assert self._hit("查询台架") is not None

    def test_query_architecture_matched(self):
        assert self._hit("查询架构") is not None
        assert self._hit("台架架构") is not None
        assert self._hit("架构列表") is not None

    def test_my_reservations_matched(self):
        assert self._hit("我的预约") is not None
        assert self._hit("看看我的预约") is not None
        assert self._hit("我的记录") is not None
        assert self._hit("我的所有预约") is not None

    def test_my_approvals_matched(self):
        assert self._hit("我的待审批") is not None
        assert self._hit("待审批") is not None
        assert self._hit("审批列表") is not None

    def test_architecture_specific_query_matched(self):
        # "查询 1.0 架构台架" — should hit list_available_benches with arg
        assert self._hit("查询 1.0 架构台架") is not None
        assert self._hit("查询 1.5 架构") is not None
        assert self._hit("L3 架构的台架") is not None

    def test_complex_queries_not_matched(self):
        # These need LLM understanding — must NOT be fast-pathed
        assert self._hit("帮我预约 TJ002 明天上午跑高速") is None
        assert self._hit("查询可用台架的剩余数量") is None  # has trailing content
        assert self._hit("为什么我的预约还没审批") is None
        assert self._hit("把 TJ001 改成下午") is None
        assert self._hit("你好") is None
        assert self._hit("") is None

    def test_case_insensitive_punctuation(self):
        assert self._hit("查询可用台架。") is not None
        assert self._hit("查询可用台架!") is not None
        assert self._hit("查询可用台架  ") is not None  # trailing whitespace


class TestFastPathPermission:
    """Fast path must respect OCL TOOL_MIN_ROLE (matches L1 plugin / L2 guarded)."""

    def setup_method(self):
        import importlib, bot.handler
        importlib.reload(bot.handler)
        self.handler = bot.handler
        from ocl.pipeline import OclResult
        self.OclResult = OclResult

    def test_role_0_user_bypassed(self, monkeypatch):
        # Role 0 (non-platform) — fast path must NOT execute tool,
        # so handler doesn't even get a chance to fail-open
        out = self.handler._try_fast_path("查询可用台架", "ou_guest", "g@x.com", 0)
        # Falls through (None) — existing role=0 reject handles it
        assert out is None

    def test_normal_user_can_query_benches(self, monkeypatch):
        fake_card = {"elements": [{"tag": "div", "text": {"content": "data"}}]}
        fake_ocl = self.OclResult(text="", blocked=False, card=fake_card)
        captured_args = {}
        with patch.object(self.handler, "ocl_apply", return_value=fake_ocl) as mock_apply, \
             patch.object(self.handler.bench_handlers, "list_available_benches",
                         side_effect=lambda a: captured_args.setdefault("args", a) or
                                                '{"code": 200, "data": ["TJ001"]}'):
            out = self.handler._try_fast_path("查询可用台架", "ou_user", "u@x.com", 1)
        assert out is not None
        assert out.card == fake_card
        assert captured_args["args"] == {}
        # Email was injected via context
        from ocl.tool_guard import get_current_email
        assert get_current_email() == "u@x.com"

    def test_architecture_specific_query_passes_architecture_arg(self, monkeypatch):
        fake_card = {"elements": []}
        fake_ocl = self.OclResult(text="", blocked=False, card=fake_card)
        captured = {}
        with patch.object(self.handler, "ocl_apply", return_value=fake_ocl), \
             patch.object(self.handler.bench_handlers, "list_available_benches",
                         side_effect=lambda a: captured.setdefault("args", a) or
                                                '{"code": 200, "data": []}'):
            self.handler._try_fast_path("查询 1.0 架构台架", "ou_user", "u@x.com", 1)
        assert captured["args"] == {"architecture": "1.0架构"}

    def test_l3_architecture_arg_built(self, monkeypatch):
        fake_ocl = self.OclResult(text="", blocked=False, card={"elements": []})
        captured = {}
        with patch.object(self.handler, "ocl_apply", return_value=fake_ocl), \
             patch.object(self.handler.bench_handlers, "list_available_benches",
                         side_effect=lambda a: captured.setdefault("args", a) or
                                                '{"code": 200, "data": []}'):
            self.handler._try_fast_path("L3 架构的台架", "ou_user", "u@x.com", 1)
        assert captured["args"] == {"architecture": "L3架构"}


class TestFastPathErrorHandling:
    """Errors in fast path must not break the bot — fall through to LLM."""

    def setup_method(self):
        import importlib, bot.handler
        importlib.reload(bot.handler)
        self.handler = bot.handler
        from ocl.pipeline import OclResult
        self.OclResult = OclResult

    def test_tool_raises_returns_none(self, monkeypatch):
        """If the bench handler raises, fast path returns None (LLM retries)."""
        with patch.object(self.handler.bench_handlers, "list_available_benches",
                         side_effect=RuntimeError("bench API down")):
            out = self.handler._try_fast_path("查询可用台架", "ou_user", "u@x.com", 1)
        assert out is None

    def test_tool_returns_error_code_returns_text_not_card(self, monkeypatch):
        """If tool returns code != 200, return a text result (not empty card)."""
        error_result = json.dumps({"code": 500, "msg": "bench API temporarily unavailable"})
        fake_ocl = self.OclResult(text="", blocked=False, card={"elements": []})
        with patch.object(self.handler, "ocl_apply", return_value=fake_ocl), \
             patch.object(self.handler.bench_handlers, "list_available_benches",
                         return_value=error_result):
            out = self.handler._try_fast_path("查询可用台架", "ou_user", "u@x.com", 1)
        assert out is not None
        assert out.card is None  # switched to text
        assert "bench API temporarily unavailable" in out.text

    def test_tool_returns_success_returns_card(self, monkeypatch):
        """If tool returns code=200, return the OCL card."""
        success = json.dumps({"code": 200, "data": ["TJ001", "TJ002"]})
        fake_card = {"elements": [{"tag": "div", "text": {"content": "data"}}]}
        fake_ocl = self.OclResult(text="", blocked=False, card=fake_card)
        with patch.object(self.handler, "ocl_apply", return_value=fake_ocl), \
             patch.object(self.handler.bench_handlers, "list_available_benches",
                         return_value=success):
            out = self.handler._try_fast_path("查询可用台架", "ou_user", "u@x.com", 1)
        assert out is not None
        assert out.card == fake_card


class TestFastPathSummary:
    """Summary text must be non-empty or OCL format_control returns
    _EMPTY_MESSAGE ('抱歉，未能生成有效回复'). The summary mirrors the
    one-line + next-step format the LLM is expected to write.
    """

    def setup_method(self):
        import importlib, bot.handler
        importlib.reload(bot.handler)
        self.handler = bot.handler

    def test_summary_contains_count_for_benches(self):
        s = self.handler._fast_path_summary(
            "list_available_benches", {"code": 200, "data": ["TJ001", "TJ002", "TJ003"]})
        assert "3" in s
        assert "可用台架" in s

    def test_summary_for_empty_bench_list(self):
        s = self.handler._fast_path_summary(
            "list_available_benches", {"code": 200, "data": []})
        assert "0" in s
        assert "可用台架" in s

    def test_summary_contains_architecture_names(self):
        s = self.handler._fast_path_summary(
            "list_architectures", {"code": 200, "data": ["1.0架构", "1.5架构", "L3架构"]})
        assert "1.0架构" in s
        assert "L3架构" in s

    def test_summary_contains_count_for_reservations(self):
        s = self.handler._fast_path_summary(
            "list_my_reservations", {"code": 200, "data": [{}, {}]})
        assert "2" in s
        assert "预约" in s

    def test_summary_contains_count_for_approvals(self):
        s = self.handler._fast_path_summary(
            "list_my_approvals", {"code": 200, "data": [{}]})
        assert "1" in s
        assert "待审批" in s

    def test_summary_falls_back_gracefully(self):
        s = self.handler._fast_path_summary("unknown_tool", {"code": 200})
        assert s  # non-empty
        assert "查询" in s

    def test_success_response_passes_non_empty_summary_to_ocl(self, monkeypatch):
        """Verify the success path passes a non-empty summary to ocl_apply,
        so OCL doesn't return _EMPTY_MESSAGE."""
        from ocl.pipeline import OclResult
        captured_summary = {}
        def fake_apply(response, user_id, captured=None):
            captured_summary["response"] = response
            return OclResult(text=response, blocked=False,
                             card={"elements": [{"tag": "div", "text": {"content": "data"}}]})
        monkeypatch.setattr(self.handler, "ocl_apply", fake_apply)
        success = json.dumps({"code": 200, "data": ["TJ001"]})
        with patch.object(self.handler.bench_handlers, "list_available_benches",
                         return_value=success):
            out = self.handler._try_fast_path("查询可用台架", "ou_user", "u@x.com", 1)
        # Summary is non-empty (would be passed to OCL as "response")
        assert captured_summary["response"], "OCL must receive a non-empty summary"
        assert "1" in captured_summary["response"]
        assert out is not None
        assert out.card is not None
