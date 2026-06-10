"""Tests that handler._handle captures tool results and dispatches an interactive card."""
import json
import types
import pytest


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    monkeypatch.setenv("FEISHU_APP_ID", "test")
    monkeypatch.setenv("FEISHU_APP_SECRET", "test")
    monkeypatch.setenv("MINIMAX_API_KEY", "test")


def _make_event(text, open_id, chat_id):
    return types.SimpleNamespace(
        event=types.SimpleNamespace(
            message=types.SimpleNamespace(
                message_type="text",
                content=json.dumps({"text": text}),
                message_id="m1",
                chat_id=chat_id,
            ),
            sender=types.SimpleNamespace(
                sender_id=types.SimpleNamespace(open_id=open_id),
            ),
        )
    )


def test_handle_dispatches_card_for_structured_result(monkeypatch, tmp_path):
    import importlib
    import bot.handler as handler
    import bot.identity_admin as ia_mod
    importlib.reload(ia_mod)
    importlib.reload(handler)
    # 预置 ou_x 为 role=1 (平台用户)
    from bot.identity_admin import IdentityAdmin
    test_admin = IdentityAdmin(str(tmp_path / "im.json"), str(tmp_path / "audit.jsonl"))
    test_admin.set_role("ou_x", 1, operator="root")
    monkeypatch.setattr(ia_mod, "get_admin", lambda: test_admin)
    monkeypatch.setattr(handler, "get_identity_admin", lambda: test_admin)

    sent = {}
    monkeypatch.setattr(handler.sender, "send_card", lambda chat, card: sent.update(card=card, chat=chat))
    monkeypatch.setattr(handler.sender, "send", lambda chat, text: sent.update(text=text))
    monkeypatch.setattr(handler.identity, "email_of", lambda uid: "zhangsan@example.com")

    fake_agent = types.SimpleNamespace(chat=lambda text: "您有1条预约")
    monkeypatch.setattr(handler.agent_pool, "get_or_create", lambda uid: fake_agent)
    monkeypatch.setattr(handler.tool_capture, "read", lambda sid: [
        {"tool": "list_my_reservations", "result": {"code": 200, "data": [
            {"benchNo": "TJ001", "startTime": "2099-01-01 09:00:00", "endTime": "2099-01-01 10:00:00",
             "taskName": "t", "status": 0, "statusDesc": "待审批"}]}}
    ])

    handler._handle(_make_event("帮我看我的预约", "ou_x", "oc_chat"))
    assert "card" in sent
    assert sent["chat"] == "oc_chat"
    # the card carries the pending reservation's cancel button
    actions = [e for e in sent["card"]["elements"] if e.get("tag") == "action"]
    assert actions


def test_handle_non_platform_user_gets_text_not_card(monkeypatch):
    import importlib
    import bot.handler as handler
    importlib.reload(handler)

    sent = {}
    monkeypatch.setattr(handler.sender, "send_card", lambda chat, card: sent.update(card=card))
    monkeypatch.setattr(handler.sender, "send", lambda chat, text: sent.update(text=text))
    monkeypatch.setattr(handler.identity, "email_of", lambda uid: "")  # not a platform user

    handler._handle(_make_event("帮我看我的预约", "ou_ghost", "oc_chat"))
    assert "card" not in sent
    assert "平台用户" in sent["text"]
