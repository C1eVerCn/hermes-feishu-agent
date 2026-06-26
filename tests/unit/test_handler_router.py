"""tests for bot/handler Tier-2 routing — LLM 路由器分发（离线，monkeypatch classify）。

验证：Tier-1 未命中的消息 → intent_router.classify → 按 intent 分发到
FSM 播种 / fast_path / 身份回复 / 闲聊引导 / agent 兜底。
"""
import json
import pytest

from bot import handler, car_state, identity_admin, intent_router, fast_path
from bot.intent_router import RouteResult
from ocl.tool_guard import set_current_caller, CallerIdentity


class _S:
    sender_id = type("SID", (), {"open_id": "ou_r2"})()
    sender_type = "user"


def _event(text, mid="m"):
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
    D.event.sender = _S()
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
def setup(monkeypatch):
    admin = identity_admin.get_admin()
    admin.auto_register("ou_r2", email="r2@x.com", name="r2")
    admin.set_role("ou_r2", 1, operator="test", note="user")
    spy = _SenderSpy()
    monkeypatch.setattr(handler, "sender", spy)
    set_current_caller(CallerIdentity(openid="ou_r2", email="r2@x.com"))
    yield spy
    set_current_caller(CallerIdentity())
    car_state.clear("ou_r2")


def test_router_book_seeds_fsm(setup, monkeypatch):
    """非 Tier-1 的约车句 → classify=book(slots) → FSM 播种到 CONFIRM_CHIP。"""
    car_state.clear("ou_r2")
    monkeypatch.setattr(intent_router, "classify",
                        lambda t: RouteResult(intent="book",
                                              slots={"vehicle_type_detail": "DM2"},
                                              confidence=0.9))
    handler._handle(_event("帮我整辆车明天用一下", "m1"))
    p = car_state.get("ou_r2")
    assert p is not None and p.state == "CONFIRM_CHIP"
    assert p.vehicle_type_detail == "DM2"
    assert len(setup.cards) >= 1


def test_router_query_runs_fast_path(setup, monkeypatch):
    """classify=query_reservations → fast_path.run_tool → 卡片，不进 agent。"""
    car_state.clear("ou_r2")
    monkeypatch.setattr(intent_router, "classify",
                        lambda t: RouteResult(intent="query_reservations", confidence=0.95))
    monkeypatch.setattr(fast_path, "run_tool",
                        lambda tool, uid, role, args=None: {"card": {"x": 1}, "blocked": False})
    handler._handle(_event("我最近都约了点啥", "m2"))
    assert setup.cards and setup.cards[-1] == {"x": 1}


def test_router_chitchat_redirects(setup, monkeypatch):
    """classify=chitchat → 引导话术（不进 agent）。"""
    car_state.clear("ou_r2")
    monkeypatch.setattr(intent_router, "classify",
                        lambda t: RouteResult(intent="chitchat", confidence=0.99))
    handler._handle(_event("给我讲个冷笑话呗", "m3"))
    assert setup.texts and "车辆预约" in setup.texts[-1]


def test_router_identity_reply(setup, monkeypatch):
    car_state.clear("ou_r2")
    monkeypatch.setattr(intent_router, "classify",
                        lambda t: RouteResult(intent="identity", confidence=0.9))
    handler._handle(_event("我在这能干点啥", "m4"))
    assert setup.texts and "工程师" in setup.texts[-1]


def test_router_unknown_falls_to_agent(setup, monkeypatch):
    """低置信/unknown → 落到 agent 路径（这里 mock 掉 _run_agent 验证被调用）。"""
    car_state.clear("ou_r2")
    monkeypatch.setattr(intent_router, "classify",
                        lambda t: RouteResult(intent="unknown", confidence=0.0))
    called = {"agent": False}
    monkeypatch.setattr(handler, "_run_agent",
                        lambda *a, **k: called.__setitem__("agent", True))
    handler._handle(_event("帮我把这个事情安排一下吧", "m5"))
    assert called["agent"] is True


def test_router_low_confidence_falls_to_agent(setup, monkeypatch):
    car_state.clear("ou_r2")
    monkeypatch.setattr(intent_router, "classify",
                        lambda t: RouteResult(intent="book", slots={}, confidence=0.3))
    called = {"agent": False}
    monkeypatch.setattr(handler, "_run_agent",
                        lambda *a, **k: called.__setitem__("agent", True))
    handler._handle(_event("呃这个不太确定", "m6"))
    assert called["agent"] is True
    assert car_state.get("ou_r2") is None  # 没误进 FSM
