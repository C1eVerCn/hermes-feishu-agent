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


# ── Admin list-users (查看用户) ─────────────────────────────────────────────────

def _admin_with_users(tmp_path, monkeypatch):
    import importlib
    import bot.identity_admin as ia_mod
    importlib.reload(ia_mod)
    from bot.identity_admin import IdentityAdmin
    admin_inst = IdentityAdmin(str(tmp_path / "im.json"), str(tmp_path / "audit.jsonl"))
    monkeypatch.setattr(ia_mod, "get_admin", lambda: admin_inst)
    admin_inst.set_role("ou_admin", 3, operator="root")
    admin_inst.auto_register("ou_zhang", email="zhang@x.com", name="张三")
    admin_inst.set_role("ou_zhang", 1, operator="root")
    admin_inst.auto_register("ou_diao", email="diao@x.com", name="调度李")
    admin_inst.set_role("ou_diao", 2, operator="root")
    admin_inst.auto_register("ou_pending", email="p@x.com", name="待审王")
    handler = _make_handler()
    monkeypatch.setattr(handler, "get_identity_admin", lambda: admin_inst)
    return handler


def test_list_users_all(tmp_path, monkeypatch):
    handler = _admin_with_users(tmp_path, monkeypatch)
    out = handler._handle_admin_command("查看用户", "ou_admin")
    assert "用户列表" in out
    assert "张三" in out and "调度李" in out
    assert "管理员" in out and "普通用户" in out and "非平台用户" in out


def test_list_users_role_filter(tmp_path, monkeypatch):
    handler = _admin_with_users(tmp_path, monkeypatch)
    out = handler._handle_admin_command("查看用户 调度员", "ou_admin")
    assert "调度李" in out
    assert "张三" not in out


def test_list_users_by_open_id(tmp_path, monkeypatch):
    handler = _admin_with_users(tmp_path, monkeypatch)
    out = handler._handle_admin_command("查看用户 ou_zhang", "ou_admin")
    assert "张三" in out
    assert "zhang@x.com" in out


def test_list_users_rejected_for_non_admin():
    handler = _make_handler()
    assert handler._handle_admin_command("查看用户", "ou_user") == ""


# ── Identity preamble injected into the LLM (role-awareness fix) ─────────────

def test_identity_preamble_states_admin_role():
    handler = _make_handler()
    pre = handler._identity_preamble("ou_admin", 3, "王五")
    assert "role=3" in pre
    assert "管理员" in pre
    assert "王五" in pre
    assert pre.endswith("用户消息：")
    # must NOT leak open_id into the LLM context
    assert "ou_admin" not in pre


def test_identity_preamble_role1_no_name():
    handler = _make_handler()
    pre = handler._identity_preamble("ou_user", 1, "")
    assert "role=1" in pre
    assert "普通用户" in pre


def test_identity_preamble_prepended_before_user_text():
    """The agent must receive identity + the original text, in that order."""
    handler = _make_handler()
    pre = handler._identity_preamble("ou_admin", 3, "王五")
    composed = pre + "我是管理员权限"
    assert composed.startswith("［系统已核验")
    assert composed.endswith("用户消息：我是管理员权限")


# ── Env-admin role elevation (OCL_ADMIN_USER_IDS → role 3 everywhere) ────────

def test_env_admin_elevated_to_role3(tmp_path, monkeypatch):
    handler = _make_handler()
    monkeypatch.setattr(handler, "_admin_ids", lambda: {"ou_envadmin"})
    from bot.identity_admin import IdentityAdmin
    admin = IdentityAdmin(str(tmp_path / "im.json"), str(tmp_path / "audit.jsonl"))
    # store says role=1, but the env lists them as admin
    admin.auto_register("ou_envadmin", email="a@x.com", name="管理者")
    admin.set_role("ou_envadmin", 1, operator="test")
    role = handler._resolve_role_with_env_admin(admin, "ou_envadmin", 1)
    assert role == 3
    # persisted to the store (single source of truth for later turns)
    assert admin.get_role("ou_envadmin") == 3


def test_non_env_admin_role_unchanged(tmp_path, monkeypatch):
    handler = _make_handler()
    monkeypatch.setattr(handler, "_admin_ids", lambda: {"ou_someoneelse"})
    from bot.identity_admin import IdentityAdmin
    admin = IdentityAdmin(str(tmp_path / "im.json"), str(tmp_path / "audit.jsonl"))
    admin.auto_register("ou_normal", email="n@x.com", name="普通")
    admin.set_role("ou_normal", 1, operator="test")
    role = handler._resolve_role_with_env_admin(admin, "ou_normal", 1)
    assert role == 1
    assert admin.get_role("ou_normal") == 1


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


class TestParseChineseTime:
    """Test the CN-time parser used by the reservation fast path."""

    def setup_method(self):
        from datetime import datetime
        # Fixed "now" so tests are deterministic
        self.now = datetime(2026, 6, 12, 10, 0, 0)  # 2026-06-12 10:00

    def _t(self, text):
        from bot.handler import _parse_chinese_time
        return _parse_chinese_time(text, self.now)

    def test_tomorrow_afternoon_5(self):
        dt = self._t("明天下午5点")
        assert dt.year == 2026 and dt.month == 6 and dt.day == 13
        assert dt.hour == 17 and dt.minute == 0

    def test_today_morning_9(self):
        dt = self._t("今天上午9点")
        assert dt.day == 12
        assert dt.hour == 9

    def test_day_after_tomorrow_evening_8(self):
        dt = self._t("后天晚上8点")
        assert dt.day == 14
        assert dt.hour == 20

    def test_afternoon_12_no_shift(self):
        # 下午12点 should stay 12, not become 24
        dt = self._t("明天下午12点")
        assert dt.hour == 12

    def test_evening_12_no_shift(self):
        # 晚上12点 = midnight of next day, but parser leaves hour=12 (caller's call)
        dt = self._t("今天晚上12点")
        assert dt.hour == 12

    def test_specific_date_with_time(self):
        dt = self._t("7月1号下午3点")
        assert dt.month == 7 and dt.day == 1
        assert dt.hour == 15

    def test_tonight_morning_etc_default(self):
        # "明早" defaults to 9:00
        dt = self._t("明早")
        assert dt.day == 13 and dt.hour == 9

    def test_ambiguous_returns_none(self):
        assert self._t("") is None
        assert self._t("今天") is None  # day only, no time
        assert self._t("几点") is None

    def test_early_morning_is_am_not_pm(self):
        # 凌晨 = 0–5 AM, must NOT be shifted +12 (regression: fuzz engine found
        # "凌晨2点" parsed as 14:00). 晚上/夜里 still shift.
        assert self._t("明天凌晨2点").hour == 2
        assert self._t("今天凌晨5点").hour == 5
        assert self._t("明天晚上8点").hour == 20   # 晚上 still +12
        assert self._t("明天夜里11点").hour == 23   # 夜里 still +12

    def test_chinese_numerals(self):
        # 中文数字时间（fuzz engine: 「下午五点」曾无法识别）
        assert self._t("明天下午五点").hour == 17
        assert self._t("后天晚上八点").hour == 20
        dt = self._t("明天上午十一点")
        assert dt.hour == 11
        assert self._t("明天下午两点").hour == 14  # 两 = 2

    def test_minutes_preserved(self):
        # 分钟不再被丢弃（fuzz engine: 17:30 / 5点半 曾解析成整点）
        dt = self._t("明天17:30")
        assert dt.hour == 17 and dt.minute == 30
        dt = self._t("明天下午5点半")
        assert dt.hour == 17 and dt.minute == 30
        dt = self._t("明天下午5点30分")
        assert dt.hour == 17 and dt.minute == 30

    def test_end_inherits_start_period(self):
        # 范围结束继承开始的上午/下午（「下午2点到4点」→ end 16:00）
        from bot.handler import _parse_chinese_time
        assert self._t("4点").hour == 4                       # 无继承 → 04:00
        dt = _parse_chinese_time("4点", self.now, inherit_period="下午")
        assert dt.hour == 16                                  # 继承下午 → 16:00

    def test_bare_period_time_no_day_word(self):
        """Day-less '晚上8点' / '下午5点' must resolve (to the `now` day) —
        the docstring promised this but no regex implemented it.
        Regression for the '从明天下午5点到晚上8点' report."""
        dt = self._t("晚上8点")
        assert dt is not None and dt.hour == 20 and dt.day == 12
        dt2 = self._t("下午5点")
        assert dt2 is not None and dt2.hour == 17 and dt2.day == 12
        dt3 = self._t("8点")
        assert dt3 is not None and dt3.hour == 8


class TestReservationRangeDayInheritance:
    """The end of a range without an explicit day inherits the start's day."""

    def setup_method(self):
        import importlib, bot.handler
        importlib.reload(bot.handler)
        self.handler = bot.handler

    def _args(self, text, monkeypatch):
        import json
        from ocl.pipeline import OclResult
        captured = {}
        monkeypatch.setattr(self.handler.bench_handlers, "dry_run_reserve_bench",
                            lambda a: (captured.__setitem__("args", a),
                                       json.dumps({"dry_run": True, "summary": "x", "args": a}))[1])
        monkeypatch.setattr(self.handler, "ocl_apply",
                            lambda r, u, captured=None: OclResult(text=r, blocked=False, card={"ok": 1}))
        monkeypatch.setattr(self.handler, "set_current_user", lambda *a: None)
        monkeypatch.setattr(self.handler, "set_current_email", lambda *a: None)
        out = self.handler._try_reserve_fast_path(text, "ou_x", "x@y.com")
        assert out is not None
        return captured["args"]

    def test_end_inherits_start_day(self, monkeypatch):
        """'从明天下午5点到晚上8点' → start and end on the SAME (tomorrow) day."""
        a = self._args("预约CT001，从明天下午5点到晚上8点，任务是测试，目的是压测", monkeypatch)
        assert "17:00:00" in a["startTime"]
        assert "20:00:00" in a["endTime"]
        # Same calendar day — end is not stuck on "today".
        assert a["startTime"][:10] == a["endTime"][:10]

    def test_range_without_从_prefix(self, monkeypatch):
        """'明天5点到8点'（无『从』）也要能抽出范围（fuzz engine: 60 条曾漏抽）。"""
        a = self._args("预约CT001，明天下午5点到后天晚上8点，任务是测试，目的是压测", monkeypatch)
        assert a["benchNo"] == "CT001"
        assert "17:00:00" in a["startTime"]
        assert "20:00:00" in a["endTime"]

    def test_range_end_inherits_period(self, monkeypatch):
        """'从今天下午2点到4点' → end 16:00（继承下午），不是次日 04:00。"""
        a = self._args("预约TJ001，从今天下午2点到4点，任务是A，目的是B", monkeypatch)
        assert "14:00:00" in a["startTime"]
        assert "16:00:00" in a["endTime"]
        assert a["startTime"][:10] == a["endTime"][:10]

    def test_range_chinese_numerals(self, monkeypatch):
        """中文数字范围 '下午五点到晚上八点'。"""
        a = self._args("预约CT001，从明天下午五点到后天晚上八点，任务是A，目的是B", monkeypatch)
        assert "17:00:00" in a["startTime"]
        assert "20:00:00" in a["endTime"]


class TestReservationFastPath:
    """End-to-end: text → extracted args → dry_run_reserve_bench → OCL card."""

    def setup_method(self):
        import importlib, bot.handler
        importlib.reload(bot.handler)
        self.handler = bot.handler

    def _try(self, text):
        return self.handler._try_reserve_fast_path(text, "ou_x", "x@y.com")

    def test_reservation_完整_args_extracted(self, monkeypatch):
        """User provides all 5 args — dry_run called with full payload."""
        from ocl.pipeline import OclResult
        captured = {}
        def fake_dry(args):
            captured["args"] = args
            return json.dumps({"dry_run": True, "summary": "确认", "args": args})
        monkeypatch.setattr(self.handler.bench_handlers, "dry_run_reserve_bench", fake_dry)
        monkeypatch.setattr(self.handler, "ocl_apply",
            lambda resp, uid, captured=None: OclResult(text=resp, blocked=False, card={"ok": True}))
        out = self._try("预约 TJ001，从明天下午5点到后天晚上8点，任务是测试，目的是感知压测")
        assert out is not None
        assert "TJ001" in captured["args"]["benchNo"]
        assert "17:00" in captured["args"]["startTime"]
        assert "20:00" in captured["args"]["endTime"]
        assert captured["args"]["taskName"] == "测试"
        assert captured["args"]["testPurpose"] == "感知压测"

    def test_reservation_missing_task_still_called(self, monkeypatch):
        """Task is optional — dry_run handles missing fields."""
        from ocl.pipeline import OclResult
        captured = {}
        def fake_dry(args):
            captured["args"] = args
            return json.dumps({"dry_run": True, "missing_fields": ["taskName", "testPurpose"],
                              "summary": "请补充", "args": args})
        monkeypatch.setattr(self.handler.bench_handlers, "dry_run_reserve_bench", fake_dry)
        monkeypatch.setattr(self.handler, "ocl_apply",
            lambda resp, uid, captured=None: OclResult(text=resp, blocked=False, card={"ok": True}))
        out = self._try("预约 TJ001 从明天下午3点到明天下午4点")
        assert out is not None
        assert "TJ001" in captured["args"]["benchNo"]
        assert "taskName" not in captured["args"]

    def test_reservation_no_bench_asks_user(self, monkeypatch):
        """No bench number → ask user directly (don't waste 30s on LLM)."""
        from ocl.pipeline import OclResult
        called = []
        def fake_dry(args):
            called.append(args)
            return json.dumps({"dry_run": True, "args": args})
        monkeypatch.setattr(self.handler.bench_handlers, "dry_run_reserve_bench", fake_dry)
        out = self._try("我想预约一个台架，明天下午")
        # dry_run is NOT called — we ask user first
        assert called == []
        # Returns an ask-user reply, not None
        assert out is not None
        assert out.card is None  # text-only
        assert "台架编号" in out.text
        assert "TJ001" in out.text  # example

    def test_reservation_no_time_range_asks_user(self, monkeypatch):
        """Bench given but no time → ask for time range."""
        from ocl.pipeline import OclResult
        called = []
        monkeypatch.setattr(self.handler.bench_handlers, "dry_run_reserve_bench",
                          lambda a: called.append(a) or json.dumps({"dry_run": True, "args": a}))
        out = self._try("预约 TJ001")
        assert called == []
        assert out is not None
        assert out.card is None
        assert "TJ001" in out.text  # references the bench
        assert "时间" in out.text

    def test_reservation_ambiguous_time_asks_user(self, monkeypatch):
        """"下午" without hour → ask user for specific time."""
        from ocl.pipeline import OclResult
        called = []
        monkeypatch.setattr(self.handler.bench_handlers, "dry_run_reserve_bench",
                          lambda a: called.append(a) or json.dumps({"dry_run": True, "args": a}))
        out = self._try("预约 TJ001 明天下午到晚上")
        assert called == []
        assert out is not None
        assert out.card is None
        assert "无法识别" in out.text or "请" in out.text

    def test_reservation_invalid_time_range_asks_user(self, monkeypatch):
        """end < start → ask user to fix, with both times shown."""
        from ocl.pipeline import OclResult
        called = []
        monkeypatch.setattr(self.handler.bench_handlers, "dry_run_reserve_bench",
                          lambda a: called.append(a) or json.dumps({"dry_run": True, "args": a}))
        out = self._try("预约 TJ001 从今天晚上8点到今天下午5点")
        assert called == []
        assert out is not None
        assert out.card is None
        # Should reference both times so user can fix
        assert "20:00" in out.text or "下午" in out.text
        assert "17:00" in out.text or "晚上" in out.text

    def test_reservation_not_a_reservation_request_returns_none(self):
        """Non-reservation text never matches."""
        assert self._try("查询可用台架") is None
        assert self._try("我的预约") is None
        assert self._try("你好") is None
        assert self._try("") is None


class TestReservationFastPathSavesDryRunState:
    """After the fast path sends a dry_run confirm card, the next '确认'
    text MUST hit _execute_confirmed_reserve (which fires dispatcher
    notifications). The save must happen in the fast path itself,
    because the LLM-driven capture loop is bypassed.
    """

    def setup_method(self):
        import importlib, bot.handler
        importlib.reload(bot.handler)
        self.handler = bot.handler

    def test_saves_dry_run_state_after_hit(self, monkeypatch):
        from ocl.pipeline import OclResult
        saved = {}
        monkeypatch.setattr(self.handler, "dry_run_state", type("S", (), {
            "save": lambda self, uid, args: saved.setdefault(uid, args),
            "get": lambda self, uid: saved.get(uid),
            "clear": lambda self, uid: saved.pop(uid, None),
        })())
        # fake dry_run returning a clean confirmation
        monkeypatch.setattr(self.handler.bench_handlers, "dry_run_reserve_bench",
                          lambda a: json.dumps({"dry_run": True, "summary": "ok",
                                                "args": a}))
        monkeypatch.setattr(self.handler, "ocl_apply",
            lambda resp, uid, captured=None: OclResult(text=resp, blocked=False, card={"ok": True}))
        out = self.handler._try_reserve_fast_path(
            "预约 TJ001 从明天下午3点到明天下午4点", "ou_x", "x@y.com")
        assert out is not None
        # dry_run_state was saved with the args
        assert "ou_x" in saved
        assert saved["ou_x"]["benchNo"] == "TJ001"

    def test_subsequent_confirm_can_read_dry_run_state(self, monkeypatch):
        """End-to-end: fast path → save → 确认 → confirm path picks it up."""
        from ocl.pipeline import OclResult
        # Fake dry_run_state
        state = {}
        monkeypatch.setattr(self.handler, "dry_run_state", type("S", (), {
            "save": lambda self, uid, args: state.setdefault(uid, args),
            "get": lambda self, uid: state.get(uid),
            "clear": lambda self, uid: state.pop(uid, None),
        })())
        monkeypatch.setattr(self.handler.bench_handlers, "dry_run_reserve_bench",
                          lambda a: json.dumps({"dry_run": True, "summary": "ok",
                                                "args": a}))
        monkeypatch.setattr(self.handler, "ocl_apply",
            lambda resp, uid, captured=None: OclResult(text=resp, blocked=False, card={"ok": True}))
        # Step 1: user requests reservation
        self.handler._try_reserve_fast_path(
            "预约 TJ001 从明天下午3点到明天下午4点", "ou_x", "x@y.com")
        # Step 2: user types "确认" — handler reads dry_run_state
        pending = self.handler.dry_run_state.get("ou_x")
        assert pending is not None
        assert pending["benchNo"] == "TJ001"


class TestBenchExtraction:
    """Bench number regex must work for '预约CT001' (no space between
    Chinese and ASCII) — Python regex \\b doesn't fire on CJK→ASCII
    boundary, so we drop the leading \\b."""

    def setup_method(self):
        import importlib, bot.handler
        importlib.reload(bot.handler)
        self.handler = bot.handler

    def test_bench_extracted_from_cjk_then_ascii(self):
        # Direct bench extraction
        m = self.handler._RESERVE_BENCH_RE.search("预约CT001")
        assert m is not None
        assert m.group(1) == "CT001"

    def test_bench_extracted_with_space(self):
        m = self.handler._RESERVE_BENCH_RE.search("预约 TJ001")
        assert m is not None
        assert m.group(1) == "TJ001"

    def test_bench_extracted_in_middle(self):
        m = self.handler._RESERVE_BENCH_RE.search("帮我预约一下 TB001 明天")
        assert m is not None
        assert m.group(1) == "TB001"

    def test_long_bench_extracted(self):
        m = self.handler._RESERVE_BENCH_RE.search("预约TJ052503")
        assert m is not None
        assert m.group(1) == "TJ052503"


class TestReserveFieldExtraction:
    """Regression: 「任务名称是X」must extract X, not '名称是X'."""

    def setup_method(self):
        import importlib, bot.handler
        importlib.reload(bot.handler)
        self.h = bot.handler

    def test_task_name_prefix_variants(self):
        cases = {
            "任务名称是感知压测，目的是测试": ("感知压测", "测试"),
            "任务是标定，目的是压测": ("标定", "压测"),
            "任务名是A目的是B": ("A", "B"),
        }
        for text, (want_task, want_purpose) in cases.items():
            tm = self.h._RESERVE_TASK_RE.search(text)
            pm = self.h._RESERVE_PURPOSE_RE.search(text)
            assert tm and tm.group(1) == want_task, (text, tm and tm.group(1))
            assert pm and pm.group(1) == want_purpose, (text, pm and pm.group(1))


class TestQueryFastPathRouting:
    """Regression: 「查询我的预约记录 / 审批记录」must hit the deterministic
    fast path (not the LLM, which emits unrenderable markdown tables)."""

    def setup_method(self):
        import importlib, bot.handler
        importlib.reload(bot.handler)
        self.h = bot.handler

    def _route(self, text):
        for pat, tool, _fn in self.h._FAST_PATH_PATTERNS:
            if pat.match(text.strip()):
                return tool
        return None

    def test_reservation_query_variants(self):
        for t in ("查询我的预约记录", "查我的预约", "我的预约", "帮我查我的预约记录"):
            assert self._route(t) == "list_my_reservations", t

    def test_approval_query_variants(self):
        for t in ("查询我的审批记录", "查看我的审批记录", "待审批", "我的待审批列表"):
            assert self._route(t) == "list_my_approvals", t
