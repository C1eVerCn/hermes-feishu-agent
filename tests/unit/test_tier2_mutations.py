"""tests for Tier-2 mutation 确定性分发（cancel/return/approve）。

覆盖 fast_path.run_mutation 的入参构造、缺识别符/权限守卫、成功/失败渲染，
以及 handler._route_with_llm 对 mutation intent 的分发。离线（mock handler 调用）。
"""
import json
import pytest

from bot import fast_path, handler, car_state, identity_admin, intent_router
from bot.intent_router import RouteResult
from ocl.tool_guard import set_current_caller, CallerIdentity


# ── fast_path.run_mutation：入参构造 + 守卫 ───────────────────────────────
def test_build_mutation_args_cancel():
    assert fast_path.build_mutation_args("cancel", {"vehicle_no": "PNV332"}) == {"vehicleNo": "PNV332"}
    # 新版上游去掉 reservationId → 仅 reservation_id 不够，需 vehicle_no
    assert fast_path.build_mutation_args("cancel", {"reservation_id": "R1"}) is None
    assert fast_path.build_mutation_args("cancel", {}) is None  # 缺识别符


def test_build_mutation_args_return():
    assert fast_path.build_mutation_args("return", {"vehicle_no": "PNV332"}) == {"vehicleNo": "PNV332"}
    assert fast_path.build_mutation_args("return", {}) is None


def test_build_mutation_args_approve():
    a = fast_path.build_mutation_args("approve", {"vehicle_no": "PNV1", "approved": True})
    assert a["vehicleNo"] == "PNV1" and a["approved"] is True
    assert "reservationId" not in a  # 新版上游去掉
    # approved=False 必须保留（不能被空值过滤掉）
    a2 = fast_path.build_mutation_args("approve", {"vehicle_no": "PNV1", "approved": False})
    assert a2["approved"] is False
    # 缺 approved → None
    assert fast_path.build_mutation_args("approve", {"vehicle_no": "PNV1"}) is None
    # 缺 vehicle_no → None
    assert fast_path.build_mutation_args("approve", {"approved": True}) is None


def test_run_mutation_missing_identifier_returns_none(monkeypatch):
    assert fast_path.run_mutation("cancel", {}, "ou_m", 1) is None  # → agent


def test_run_mutation_role_guard(monkeypatch):
    # approve 需要 role>=2；role=1 → None（落 agent）
    assert fast_path.run_mutation("approve", {"vehicle_no": "PNV1", "approved": True}, "ou_m", 1) is None


def test_run_mutation_cancel_success(monkeypatch):
    from car_tools import handlers as ch
    monkeypatch.setattr(ch, "cancel_vehicle_reservation",
                        lambda args: json.dumps({"cancelled": True, "vehicle_no": "PNV332"}))
    monkeypatch.setattr("ocl.identity.email_of", lambda oid: "m@x.com")
    monkeypatch.setattr("ocl.identity.mobile_of", lambda oid: "")
    res = fast_path.run_mutation("cancel", {"vehicle_no": "PNV332"}, "ou_m", 1)
    assert res and "已取消预约" in res["text"] and "PNV332" in res["text"]


def test_run_mutation_error_renders_fail(monkeypatch):
    from car_tools import handlers as ch
    monkeypatch.setattr(ch, "return_vehicle",
                        lambda args: json.dumps({"error": "车辆未借出"}))
    monkeypatch.setattr("ocl.identity.email_of", lambda oid: "m@x.com")
    monkeypatch.setattr("ocl.identity.mobile_of", lambda oid: "")
    res = fast_path.run_mutation("return", {"vehicle_no": "PNV332"}, "ou_m", 1)
    assert res and "操作失败" in res["text"] and "车辆未借出" in res["text"]


def test_run_mutation_approve_success(monkeypatch):
    from car_tools import handlers as ch
    monkeypatch.setattr(ch, "approval_vehicle_reservation",
                        lambda args: json.dumps({"approved": True, "vehicle_no": "PNV1"}))
    monkeypatch.setattr("ocl.identity.email_of", lambda oid: "d@x.com")
    monkeypatch.setattr("ocl.identity.mobile_of", lambda oid: "")
    res = fast_path.run_mutation("approve", {"vehicle_no": "PNV1", "approved": True}, "ou_d", 2)
    assert res and "已批准" in res["text"]
    res2 = fast_path.run_mutation("approve", {"vehicle_no": "PNV1", "approved": False}, "ou_d", 2)
    assert res2 and "已驳回" in res2["text"]


# ── handler._route_with_llm：mutation intent 分发 ──────────────────────────
class _S:
    sender_id = type("SID", (), {"open_id": "ou_mh"})()
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


@pytest.fixture
def setup(monkeypatch):
    admin = identity_admin.get_admin()
    admin.auto_register("ou_mh", email="mh@x.com", name="mh")
    admin.set_role("ou_mh", 1, operator="test")
    spy = _SenderSpy()
    monkeypatch.setattr(handler, "sender", spy)
    set_current_caller(CallerIdentity(openid="ou_mh", email="mh@x.com"))
    yield spy
    set_current_caller(CallerIdentity())
    car_state.clear("ou_mh")


def test_handler_cancel_shows_confirm_card(setup, monkeypatch):
    """取消 → 二次确认卡（不直接执行）。"""
    car_state.clear("ou_mh")
    monkeypatch.setattr(intent_router, "classify",
                        lambda t: RouteResult(intent="cancel",
                                              slots={"vehicle_no": "PNV332"}, confidence=0.9))
    handler._handle(_event("把我那个PNV332的预约取消掉", "m1"))
    assert setup.cards, "应发二次确认卡"
    blob = json.dumps(setup.cards[-1], ensure_ascii=False)
    assert "确认取消" in blob and "PNV332" in blob


def test_handler_cancel_missing_id_falls_to_agent(setup, monkeypatch):
    car_state.clear("ou_mh")
    monkeypatch.setattr(intent_router, "classify",
                        lambda t: RouteResult(intent="cancel", slots={}, confidence=0.9))
    called = {"agent": False}
    monkeypatch.setattr(handler, "_run_agent",
                        lambda *a, **k: called.__setitem__("agent", True))
    handler._handle(_event("帮我取消一个预约", "m2"))
    assert called["agent"] is True


def test_confirm_mutation_callback_executes(monkeypatch):
    """点 [确认取消] → card_action_handler 执行 cancel。"""
    from bot import card_action_handler as cah
    from car_tools import handlers as ch
    admin = identity_admin.get_admin()
    admin.auto_register("ou_cm", email="cm@x.com", name="cm")
    admin.set_role("ou_cm", 1, operator="test")
    monkeypatch.setattr(ch, "cancel_vehicle_reservation",
                        lambda args: json.dumps({"cancelled": True, "vehicle_no": "PNV332"}))
    monkeypatch.setattr("ocl.identity.email_of", lambda oid: "cm@x.com")
    monkeypatch.setattr("ocl.identity.mobile_of", lambda oid: "")
    toast, card = cah.handle("ou_cm", {"action": "confirm_mutation",
                                       "mutation": "cancel", "vehicle_no": "PNV332"})
    blob = (toast or "") + json.dumps(card, ensure_ascii=False)
    assert "已取消" in blob and "PNV332" in blob
