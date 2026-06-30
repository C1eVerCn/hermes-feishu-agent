"""bot/handler 单路径对话流测试（2026-06-30 Phase 1.3 重构后）。

覆盖：① 输入校验 ② 即时文案优先级 ③ 身份闸 + role==0 阻断
④ 身份/管理命令优先级 ⑤ agent 路径单入口（所有业务请求 → _run_agent）
⑥ contextvars 注入/清理
"""
import json
import pytest

from bot import handler, identity_admin
from ocl.tool_guard import (
    get_current_caller, get_current_session, set_current_caller, set_current_session,
    CallerIdentity,
)


class _S:
    sender_id = type("SID", (), {"open_id": "ou_h"})()
    sender_type = "user"


def _event(text, mid="m", user="ou_h"):
    class E:
        message_id = mid
        chat_id = "oc_chat"
        chat_type = "p2p"
        message_type = "text"
        content = json.dumps({"text": text})
        mentions = []
    class D:
        pass
    D.event = type("Ev", (), {})()
    D.event.message = E()
    D.event.sender = type("Sender", (), {})()
    D.event.sender.sender_id = type("SID", (), {"open_id": user})()
    return D


class _SenderSpy:
    def __init__(self):
        self.cards = []
        self.texts = []

    def send_card(self, chat_id, card):
        self.cards.append(card)

    def send_text_as_card(self, chat_id, text):
        self.texts.append(text)


@pytest.fixture(autouse=True)
def _clean():
    set_current_caller(CallerIdentity())
    set_current_session("")
    yield
    set_current_caller(CallerIdentity())
    set_current_session("")


@pytest.fixture
def setup(monkeypatch):
    admin = identity_admin.get_admin()
    admin.auto_register("ou_h", email="h@x.com", name="h")
    admin.set_role("ou_h", 1, operator="test", note="engineer")
    spy = _SenderSpy()
    monkeypatch.setattr(handler, "sender", spy)
    return spy


# ── 1. 空文本 → _EMPTY_REPLY，不调 agent ─────────────────────────────────

def test_empty_text_replies_static_prompt(setup, monkeypatch):
    called = {"agent": 0}
    monkeypatch.setattr(handler, "_run_agent",
                        lambda *a, **k: called.__setitem__("agent", called["agent"] + 1))
    # 构造一个空文本事件（content={"text": ""}）
    data = _event("", mid="empty")
    data.event.message.content = json.dumps({"text": ""})
    handler._handle(data)
    assert called["agent"] == 0
    assert setup.texts and "请输入文字消息" in setup.texts[-1]


# ── 2. 输入过长 → _INPUT_TOO_LONG_REPLY ──────────────────────────────────

def test_too_long_text_replies_static(setup, monkeypatch):
    called = {"agent": 0}
    monkeypatch.setattr(handler, "_run_agent",
                        lambda *a, **k: called.__setitem__("agent", called["agent"] + 1))
    data = _event("x" * 9000, mid="big")
    handler._handle(data)
    assert called["agent"] == 0
    assert setup.texts and "过长" in setup.texts[-1]


# ── 3. 即时文案优先于身份闸（避免被 18s 飞书 API 阻塞）────────────────────

def test_greeting_returns_instantly_without_resolving_identity(setup, monkeypatch):
    """「你好」「你能做什么」等问候 → 即时文案返回，不调身份闸/agent。"""
    called = {"agent": 0}
    monkeypatch.setattr(handler, "_run_agent",
                        lambda *a, **k: called.__setitem__("agent", called["agent"] + 1))
    handler._handle(_event("你好", mid="g"))
    assert called["agent"] == 0
    assert setup.texts, "应发一条即时文案"


# ── 4. role==0 陌生人阻断在 agent 之前 ──────────────────────────────────

def test_stranger_blocked_before_agent(monkeypatch):
    """role==0 用户 → 友好提示，不进 agent。"""
    spy = _SenderSpy()
    monkeypatch.setattr(handler, "sender", spy)
    # mock _resolve_identity 返回 role=0（陌生人）；auto_register 会自动给 default=1，
    # 但通过 monkeypatch 可以直接覆盖
    monkeypatch.setattr(handler, "_resolve_identity",
                        lambda uid: (0, "", "", None))
    called = {"agent": 0}
    monkeypatch.setattr(handler, "_run_agent",
                        lambda *a, **k: called.__setitem__("agent", called["agent"] + 1))
    handler._handle(_event("约个车", mid="s1", user="ou_stranger"))
    assert called["agent"] == 0
    assert spy.texts and ("无法识别" in spy.texts[-1] or "open_id" in spy.texts[-1])


# ── 5. 业务请求（含查询）→ _run_agent 单入口 ────────────────────────────

def test_query_routes_to_fast_path(setup, monkeypatch):
    """「我的预约」这种业务查询 → 2026-06-30 走 fast_path 短路（直接调
    fetch_user_reservation 工具），不调 LLM。"""
    called = {"agent": 0}
    def _spy(*a, **k):
        called["agent"] += 1
    monkeypatch.setattr(handler, "_run_agent", _spy)
    # 强制 fast_path 成功（不需要真跑工具）
    monkeypatch.setattr(handler, "_try_fast_query", lambda t, u, r: "fake-fast-path-result")
    handler._handle(_event("我的预约", mid="q1"))
    assert called["agent"] == 0  # 走了 fast_path，不进 agent


def test_query_falls_to_agent_if_fast_path_no_match(setup, monkeypatch):
    """fast_path 没匹配的查询 → 落 agent。"""
    called = {"agent": 0}
    def _spy(*a, **k):
        called["agent"] += 1
    monkeypatch.setattr(handler, "_run_agent", _spy)
    # fast_path 返回 None（不匹配）
    monkeypatch.setattr(handler, "_try_fast_query", lambda t, u, r: None)
    handler._handle(_event("约车 PNV332 明天下午 2 点到 4 点 DM2 Xavier 测试 A 区", mid="q2"))
    assert called["agent"] == 1


def test_booking_routes_to_agent(setup, monkeypatch):
    """「预约 PNV332」也走 agent（不再有 FSM 入口）。"""
    called = {"agent": 0}
    monkeypatch.setattr(handler, "_run_agent",
                        lambda *a, **k: called.__setitem__("agent", called["agent"] + 1))
    handler._handle(_event("帮我约 PNV332 明天下午 2 点到 4 点 DM2 Xavier", mid="b1"))
    assert called["agent"] == 1


def test_chitchat_routes_to_agent(setup, monkeypatch):
    """「1+1」业务外问题 → agent（让 LLM 自然应对，不再硬拦）。"""
    called = {"agent": 0}
    monkeypatch.setattr(handler, "_run_agent",
                        lambda *a, **k: called.__setitem__("agent", called["agent"] + 1))
    handler._handle(_event("1+1=?", mid="c1"))
    assert called["agent"] == 1


# ── 6. contextvars 注入/清理：handler._handle 期间有值，结束后清空 ──────────

def test_contextvars_injected_during_agent_call(setup, monkeypatch):
    """_handle 调用 _run_agent 时必须注入 caller + session（让 commit 守卫 / tool_capture 能用）。"""
    captured = {}
    def _spy(*a, **k):
        captured["caller"] = get_current_caller()
        captured["session"] = get_current_session()
    monkeypatch.setattr(handler, "_run_agent", _spy)
    handler._handle(_event("约车 PNV332", mid="cv"))
    assert captured["caller"].openid == "ou_h"
    assert captured["caller"].email == "h@x.com"
    assert captured["session"] == "feishu_ou_h"


# ── 7. _run_agent 异常时清空 contextvars（即使 agent.chat 抛错）────────

def test_contextvars_cleared_on_agent_exception(setup, monkeypatch):
    """_run_agent 内部 try/finally 必须清空 contextvars。"""
    class _BoomAgent:
        def chat(self, text):
            raise RuntimeError("simulated LLM error")
    # handler.py 用 from bot.agent_pool import agent_pool 引入的实例；
    # 替换其 get_or_create 方法（不要改原实例，monkeypatch 会还原）
    monkeypatch.setattr(handler.agent_pool, "get_or_create",
                        lambda uid: _BoomAgent())
    class _SenderSpy2:
        def __init__(self): self.cards = []; self.texts = []
        def send_card(self, *a, **k): self.cards.append(a)
        def send_text_as_card(self, *a, **k): self.texts.append(a)
    monkeypatch.setattr(handler, "sender", _SenderSpy2())
    handler._run_agent("oc_chat", "ou_h", 1, "h", "约车", "m")
    # 异常时仍应清空
    assert get_current_caller().openid == ""
    assert get_current_session() == ""


# ── 8. replies.identity_preamble 注入到 agent_input（identity context）───

def test_identity_preamble_included(setup, monkeypatch):
    """agent 收到的 input = identity_preamble + 用户原话。"""
    captured = {"input": None}
    def _spy(*a, **k):
        captured["input"] = a[4]  # text 参数
    monkeypatch.setattr(handler, "_run_agent", _spy)
    handler._handle(_event("约车", mid="ip"))
    # identity_preamble 包含 role 等元信息
    assert captured["input"].endswith("约车")
    assert "约车" in captured["input"]
