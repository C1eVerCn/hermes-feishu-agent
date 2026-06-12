"""Tests for the ws_client card-action callback thin wrapper (injection-based)."""
import types
import pytest


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("FEISHU_APP_ID", "test")
    monkeypatch.setenv("FEISHU_APP_SECRET", "test")
    monkeypatch.setenv("MINIMAX_API_KEY", "test")


def _make_card_action(open_id, value):
    return types.SimpleNamespace(
        event=types.SimpleNamespace(
            operator=types.SimpleNamespace(open_id=open_id),
            action=types.SimpleNamespace(value=value),
        )
    )


def test_on_card_action_delegates_and_returns_toast():
    import feishu.ws_client as ws
    captured = {}

    def fake_handle(open_id, value, chat_id=""):
        captured["open_id"] = open_id
        captured["value"] = value
        captured["chat_id"] = chat_id
        return "取消预约成功", None

    ws.set_card_action_handler(fake_handle)
    resp = ws._on_card_action(_make_card_action("ou_user", {"action": "cancel", "benchNo": "TJ001"}))
    assert captured["open_id"] == "ou_user"
    assert captured["value"]["action"] == "cancel"
    assert "取消预约成功" in ws._toast_text(resp)


def test_on_card_action_handles_exception_gracefully():
    import feishu.ws_client as ws

    def boom(open_id, value):
        raise RuntimeError("kaboom")

    ws.set_card_action_handler(boom)
    resp = ws._on_card_action(_make_card_action("ou_user", {"action": "cancel"}))
    assert "失败" in ws._toast_text(resp)


def test_on_card_action_no_handler_set():
    import feishu.ws_client as ws
    ws.set_card_action_handler(None)
    resp = ws._on_card_action(_make_card_action("ou_user", {"action": "cancel"}))
    assert ws._toast_text(resp)  # non-empty fallback toast
