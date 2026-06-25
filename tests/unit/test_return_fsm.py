"""tests for bot/return_fsm — 还车表单 FSM（5 必填字段 + 二次确认）。"""
import json
import pytest

from bot import return_fsm, car_state, intent
from bot.return_fsm import (
    start, advance, RET_VEHICLE, RET_LOCATION, RET_KEY, RET_MODULE,
    RET_STATUS, RET_DESC, RET_CONFIRM, RET_DONE,
)


@pytest.fixture(autouse=True)
def _clean():
    car_state.clear("ou_ret")
    yield
    car_state.clear("ou_ret")


# ── 入口 ─────────────────────────────────────────────────────────────────
def test_start_with_vehicle_skips_to_location():
    state, resp = start("ou_ret", {"vehicle_no": "PNV332"})
    assert state == RET_LOCATION
    assert car_state.get("ou_ret").vehicle_no == "PNV332"
    assert car_state.get("ou_ret").intent == "return"


def test_start_without_vehicle_asks_vehicle():
    state, resp = start("ou_ret", {})
    assert state == RET_VEHICLE
    assert "车辆编号" in resp["text"]


def test_vehicle_input_validation():
    start("ou_ret", {})
    state, resp = advance("ou_ret", "不是编号")  # 无数字
    assert state == RET_VEHICLE and "格式不符" in resp["text"]
    state, resp = advance("ou_ret", "PNV332")
    assert state == RET_LOCATION


# ── 完整流程 ──────────────────────────────────────────────────────────────
def test_full_return_flow(monkeypatch):
    captured = {}
    from car_tools import handlers as ch
    monkeypatch.setattr(ch, "return_vehicle",
                        lambda args: captured.update(args) or json.dumps({"returned": True}))
    monkeypatch.setattr("ocl.identity.email_of", lambda oid: "r@x.com")
    monkeypatch.setattr("ocl.identity.mobile_of", lambda oid: "")

    start("ou_ret", {"vehicle_no": "PNV332"})           # → RET_LOCATION
    assert advance("ou_ret", "张江")[0] == RET_KEY
    assert advance("ou_ret", "前台保安")[0] == RET_MODULE
    assert advance("ou_ret", "无变更")[0] == RET_STATUS
    assert advance("ou_ret", "可用")[0] == RET_DESC
    assert advance("ou_ret", "车况正常")[0] == RET_CONFIRM
    state, resp = advance("ou_ret", "确认")             # → 执行
    assert state == RET_DONE
    assert "已归还" in resp["text"]
    # 上游入参齐全且 vehicleStatus 映射成 int 1（可用）
    assert captured["vehicleNo"] == "PNV332"
    assert captured["returnLocation"] == "张江"
    assert captured["keyPosition"] == "前台保安"
    assert captured["changeModule"] == "无变更"
    assert captured["vehicleStatus"] == 1
    assert captured["vehicleStatusDescription"] == "车况正常"
    assert car_state.get("ou_ret") is None  # 清状态


def test_status_invalid_reprompts():
    start("ou_ret", {"vehicle_no": "PNV332"})
    advance("ou_ret", "张江"); advance("ou_ret", "车内"); advance("ou_ret", "无")
    state, resp = advance("ou_ret", "随便")  # 非 可用/故障/维保/报废
    assert state == RET_STATUS and "未识别状态" in resp["text"]


def test_status_maps_to_code(monkeypatch):
    captured = {}
    from car_tools import handlers as ch
    monkeypatch.setattr(ch, "return_vehicle",
                        lambda args: captured.update(args) or json.dumps({"returned": True}))
    monkeypatch.setattr("ocl.identity.email_of", lambda oid: "r@x.com")
    monkeypatch.setattr("ocl.identity.mobile_of", lambda oid: "")
    start("ou_ret", {"vehicle_no": "PNV1"})
    advance("ou_ret", "上海"); advance("ou_ret", "钥匙柜"); advance("ou_ret", "传感器")
    advance("ou_ret", "故障"); advance("ou_ret", "刹车异响")
    advance("ou_ret", "确认")
    assert captured["vehicleStatus"] == 2  # 故障


def test_confirm_cancel_clears():
    start("ou_ret", {"vehicle_no": "PNV332"})
    advance("ou_ret", "张江"); advance("ou_ret", "车内"); advance("ou_ret", "无")
    advance("ou_ret", "可用"); advance("ou_ret", "车况正常")  # → RET_CONFIRM
    state, resp = advance("ou_ret", "取消")
    assert state == RET_DONE and "已取消" in resp["text"]
    assert car_state.get("ou_ret") is None


def test_escape_clears_mid_flow():
    start("ou_ret", {"vehicle_no": "PNV332"})  # RET_LOCATION
    state, resp = advance("ou_ret", "算了")
    assert car_state.get("ou_ret") is None
    assert "已取消" in resp["text"]


def test_return_failure_surfaces(monkeypatch):
    from car_tools import handlers as ch
    monkeypatch.setattr(ch, "return_vehicle", lambda args: json.dumps({"error": "车辆未借出"}))
    monkeypatch.setattr("ocl.identity.email_of", lambda oid: "r@x.com")
    monkeypatch.setattr("ocl.identity.mobile_of", lambda oid: "")
    start("ou_ret", {"vehicle_no": "PNV332"})
    advance("ou_ret", "张江"); advance("ou_ret", "车内"); advance("ou_ret", "无")
    advance("ou_ret", "可用"); advance("ou_ret", "车况正常")
    state, resp = advance("ou_ret", "确认")
    assert "归还失败" in resp["text"] and "车辆未借出" in resp["text"]


# ── handler / card_action 集成 ─────────────────────────────────────────────
def test_handler_return_intent_enters_fsm(monkeypatch):
    from bot import handler, identity_admin
    admin = identity_admin.get_admin()
    admin.auto_register("ou_rh", email="rh@x.com", name="rh")
    admin.set_role("ou_rh", 1, operator="test")

    class _Spy:
        def __init__(self):
            self.cards = []; self.texts = []
        def send_card(self, c, card): self.cards.append(card)
        def send_text_as_card(self, c, t): self.texts.append(t)
    spy = _Spy()
    monkeypatch.setattr(handler, "sender", spy)

    class _S:
        sender_id = type("SID", (), {"open_id": "ou_rh"})()
        sender_type = "user"

    def _event(text):
        class E:
            message_id = "m"; chat_id = "oc"; chat_type = "p2p"
            message_type = "text"; content = json.dumps({"text": text}); mentions = []
        class D: pass
        D.event = type("Ev", (), {})(); D.event.message = E(); D.event.sender = _S()
        return D

    car_state.clear("ou_rh")
    handler._handle(_event("归还PNV332"))  # Tier-1 还车意图 → return FSM（已带编号 → 问地点）
    p = car_state.get("ou_rh")
    assert p is not None and p.intent == "return" and p.state == RET_LOCATION
    assert p.vehicle_no == "PNV332"
    car_state.clear("ou_rh")


def test_card_action_ret_button(monkeypatch):
    from bot import card_action_handler as cah
    monkeypatch.setattr("ocl.identity.email_of", lambda oid: "rb@x.com")
    monkeypatch.setattr("ocl.identity.mobile_of", lambda oid: "")
    car_state.clear("ou_rb")
    start("ou_rb", {"vehicle_no": "PNV332"})  # RET_LOCATION
    # 模拟点 [张江] 按钮 → ret_loc
    toast, card = cah.handle("ou_rb", {"action": "ret_loc", "value": "张江"})
    p = car_state.get("ou_rb")
    assert p.return_location == "张江" and p.state == RET_KEY
    car_state.clear("ou_rb")

