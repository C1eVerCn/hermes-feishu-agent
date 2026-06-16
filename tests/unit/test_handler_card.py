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

    fake_agent = types.SimpleNamespace(chat=lambda text, stream_callback=None: "您有1条预约")
    monkeypatch.setattr(handler.agent_pool, "get_or_create", lambda uid: fake_agent)
    monkeypatch.setattr(handler.tool_capture, "read", lambda sid: [
        {"tool": "list_my_reservations", "result": {"code": 200, "data": [
            {"benchNo": "TJ001", "startTime": "2099-01-01 09:00:00", "endTime": "2099-01-01 10:00:00",
             "taskName": "t", "status": 0, "statusDesc": "待审批"}]}}
    ])

    handler._handle(_make_event("看看我最近有什么预约需要处理", "ou_x", "oc_chat"))
    assert "card" in sent
    assert sent["chat"] == "oc_chat"
    # the card carries the pending reservation's cancel button
    actions = [e for e in sent["card"]["elements"] if e.get("tag") == "action"]
    assert actions


def test_handle_in_scope_user_without_email_is_platform_user(monkeypatch, tmp_path):
    """权限模型（2026-06-16）：能给机器人发消息 = 在飞书可见范围内 = 默认 role=1，
    即便 Contact API 取不到邮箱也不再拒之门外（线上反馈根因）。"""
    import importlib
    import bot.handler as handler
    import bot.identity_admin as ia_mod
    importlib.reload(ia_mod)
    importlib.reload(handler)
    from bot.identity_admin import IdentityAdmin
    test_admin = IdentityAdmin(str(tmp_path / "im.json"), str(tmp_path / "audit.jsonl"))
    monkeypatch.setattr(ia_mod, "get_admin", lambda: test_admin)
    monkeypatch.setattr(handler, "get_identity_admin", lambda: test_admin)

    sent = {}
    monkeypatch.setattr(handler.sender, "send_card", lambda chat, card: sent.update(card=card))
    monkeypatch.setattr(handler.sender, "send_text_as_card",
                        lambda chat, text: sent.update(card={"_text": text}))
    monkeypatch.setattr(handler.identity, "email_of", lambda uid: "")   # Contact API 无邮箱
    monkeypatch.setattr(handler.identity, "name_of", lambda uid: "")

    called = {}
    fake_agent = types.SimpleNamespace(
        chat=lambda text, stream_callback=None: called.setdefault("text", text) or "好的")
    monkeypatch.setattr(handler.agent_pool, "get_or_create", lambda uid: fake_agent)
    monkeypatch.setattr(handler.tool_capture, "read", lambda sid: [])

    # 自由文本（不命中简单意图 / 快路径）→ 必然走到 agent
    handler._handle(_make_event("随便和你聊聊", "ou_noemail", "oc_chat"))

    # 无邮箱也被提升为 role=1，并真的进入了 agent（没有被身份闸拦下）
    assert test_admin.get_role("ou_noemail") == 1
    assert "text" in called
    rendered = sent.get("card", {})
    text = rendered.get("_text", "") if isinstance(rendered, dict) else ""
    assert "平台用户" not in text and "无法识别" not in text
