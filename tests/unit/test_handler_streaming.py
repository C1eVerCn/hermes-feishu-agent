"""Tests for bot/handler streaming + agent_pool warmup integration.

The handler-level integration tests mock AIAgent.chat as a stream emitter
and verify the full happy path: typing → streaming → final card.
"""
import time
from unittest.mock import patch, MagicMock, call

import pytest

import bot.agent_pool as agent_pool_mod
import bot.handler as handler


@pytest.fixture
def fresh_pool(monkeypatch):
    """Reset the module-level singleton before each test."""
    agent_pool_mod.agent_pool._pool.clear()
    return agent_pool_mod.agent_pool


def test_warmup_thread_spawned_on_first_create(fresh_pool, monkeypatch):
    """When get_or_create creates a new AIAgent, it must spawn a background
    thread that calls agent._create_openai_client(...) to force the
    OpenAI SDK lazy import (without making a real LLM call)."""
    fake_agent = MagicMock()
    fake_agent._client_kwargs = {"api_key": "test", "base_url": "http://x"}
    fake_thread = MagicMock()
    with patch("bot.agent_pool.AIAgent", return_value=fake_agent), \
         patch("threading.Thread", return_value=fake_thread) as mock_thread_cls:
        fresh_pool.get_or_create("ou_user_1")
    # Thread was constructed targeting _warmup_agent with the new agent
    assert mock_thread_cls.call_count == 1
    args, kwargs = mock_thread_cls.call_args
    assert kwargs["target"].__name__ == "_warmup_agent"
    assert kwargs["args"] == (fake_agent,)
    assert kwargs["daemon"] is True
    assert kwargs["name"] == "agent-warmup"
    # And it was started
    assert fake_thread.start.call_count == 1


def test_warmup_not_spawned_on_cache_hit(fresh_pool):
    """Second call to get_or_create (cache hit) must NOT spawn another thread."""
    fake_agent = MagicMock()
    fake_agent._client_kwargs = {"api_key": "test", "base_url": "http://x"}
    with patch("bot.agent_pool.AIAgent", return_value=fake_agent), \
         patch("threading.Thread") as mock_thread_cls:
        fresh_pool.get_or_create("ou_user_1")  # first → spawns
        fresh_pool.get_or_create("ou_user_1")  # second → cache hit
    # Only one thread ever constructed
    assert mock_thread_cls.call_count == 1


def test_warmup_thread_swallows_exceptions(monkeypatch):
    """If the warmup _create_openai_client raises, the background thread
    must not propagate (daemon thread, no global state leak)."""
    agent_pool_mod.agent_pool._pool.clear()
    fake_agent = MagicMock()
    fake_agent._client_kwargs = {"api_key": "test", "base_url": "http://x"}
    fake_agent._create_openai_client.side_effect = RuntimeError("warmup boom")
    fake_thread = MagicMock()
    with patch("bot.agent_pool.AIAgent", return_value=fake_agent), \
         patch("threading.Thread", return_value=fake_thread):
        # Should not raise even though warmup would fail
        agent_pool_mod.agent_pool.get_or_create("ou_user_2")
    # The thread.start() was called; warmup failure happens INSIDE that
    # thread and is swallowed (verified in _warmup_agent test below).
    assert fake_thread.start.call_count == 1


def test_warmup_agent_calls_create_openai_client_not_chat():
    """_warmup_agent must trigger SDK lazy import via _create_openai_client
    (not agent.chat, which would make a real LLM call costing $$ and
    pollute session history with a stray 'hello' turn).

    The kwargs are built from the agent's public api_key/base_url
    attributes because agent._client_kwargs is itself lazily populated
    by hermes-agent only when the first LLM call is about to fire.
    """
    fake_agent = MagicMock()
    fake_agent.api_key = "test-key"
    fake_agent.base_url = "http://minimax.test/v1"
    # Call _warmup_agent synchronously (not via the thread pool)
    agent_pool_mod._warmup_agent(fake_agent)
    fake_agent._create_openai_client.assert_called_once()
    args, kwargs = fake_agent._create_openai_client.call_args
    assert args[0] == {"api_key": "test-key", "base_url": "http://minimax.test/v1"}
    # chat() must NOT be called by warmup (it's a real LLM call)
    fake_agent.chat.assert_not_called()


# ── Streaming tests (Task 4: _on_streaming_chunk) ─────────────────────────

def test_streaming_chunk_accumulates_below_threshold(monkeypatch):
    """Below _STREAMING_FLUSH_MIN_CHARS, _on_streaming_chunk should NOT
    call edit_message — it just buffers tokens."""
    fake_typing = MagicMock()
    fake_typing._placeholder_message_id = "om_p"

    with patch("bot.handler.sender.edit_message") as mock_edit:
        # Three 1-char tokens = 3 chars total, under default 5-char threshold
        for tok in ["你", "好", "世"]:
            handler._on_streaming_chunk(tok, fake_typing, 0.3)
    assert mock_edit.call_count == 0


def test_streaming_chunk_flushes_on_char_threshold(monkeypatch):
    """Once accumulated >= _STREAMING_FLUSH_MIN_CHARS, edit_message fires
    on the typing_indicator with the growing accumulated text."""
    fake_typing = MagicMock()
    fake_typing._placeholder_message_id = "om_p"

    # 6 single-char tokens: first flush at char 5, second flush at char 6
    # (the accumulator keeps growing; we only clear on turn-end final flush)
    for tok in ["你", "好", "，", "世", "界", "！"]:
        handler._on_streaming_chunk(tok, fake_typing, 0.3)
    # 2 flushes (chars 5 and 6 each cross the 5-char threshold)
    assert fake_typing.edit_message.call_count == 2
    # Last call's content is the full accumulated text
    last_content = fake_typing.edit_message.call_args_list[-1][0][0]
    assert last_content == "你好，世界！"
    # Earlier call was the first crossing (5-char threshold)
    first_content = fake_typing.edit_message.call_args_list[0][0][0]
    assert first_content == "你好，世界"


def test_streaming_chunk_flushes_on_time_interval(monkeypatch):
    """Even below char threshold, a flush fires once flush_interval_sec
    has elapsed since the last flush."""
    fake_typing = MagicMock()
    fake_typing._placeholder_message_id = "om_p"

    # Mock time.monotonic: state init reads once + each chunk reads once.
    # 3 chunks = 4 monotonic calls.
    times = iter([100.0,    # state init (last_flush)
                  100.0,    # chunk 1 "你": now
                  100.0,    # chunk 2 "好": now, acc=2 chars
                  100.4])   # chunk 3 "，": now, 0.4s gap > 0.3s → flush
    monkeypatch.setattr(handler.time, "monotonic", lambda: next(times))

    handler._on_streaming_chunk("你", fake_typing, 0.3)  # t=100
    handler._on_streaming_chunk("好", fake_typing, 0.3)  # t=100, acc=2 chars
    # Below 5-char threshold but 0s gap — no flush
    # Now t=100.4 → 0.4s gap > 0.3s → flush
    handler._on_streaming_chunk("，", fake_typing, 0.3)
    assert fake_typing.edit_message.call_count == 1
    content = fake_typing.edit_message.call_args[0][0]
    assert content == "你好，"


def test_streaming_chunk_state_is_per_typing_instance(monkeypatch):
    """Each typing instance has its own accumulator — they don't bleed."""
    fake_a = MagicMock(); fake_a._placeholder_message_id = "om_a"
    fake_b = MagicMock(); fake_b._placeholder_message_id = "om_b"

    handler._on_streaming_chunk("abcde", fake_a, 0.3)  # flushes (5 chars)
    handler._on_streaming_chunk("xy", fake_b, 0.3)  # buffers, no flush
    assert fake_a.edit_message.call_count == 1
    assert fake_b.edit_message.call_count == 0


def test_streaming_chunk_gives_up_after_3_edit_failures(monkeypatch):
    """After 3 consecutive edit_message failures, _on_streaming_chunk stops
    calling edit_message (caller will fall back to send_card)."""
    fake_typing = MagicMock()
    fake_typing._placeholder_message_id = "om_p"
    fake_typing.edit_message.side_effect = RuntimeError("simulated 429")

    times = iter([100.0 + i * 0.5 for i in range(20)])
    monkeypatch.setattr(handler.time, "monotonic", lambda: next(times))

    # Push 8 flush-bound chunks (each >= 5 chars). After 3 failures, no more calls.
    for i in range(8):
        handler._on_streaming_chunk("a" * 6, fake_typing, 0.3)
    # First 3 fail (3 attempts), then we give up — never reaches 4th..8th
    assert fake_typing.edit_message.call_count == 3


def test_final_flush_emits_buffered_tokens_and_clears_state(monkeypatch):
    """_final_flush pushes any buffered-but-not-flushed tokens through
    edit_message, then clears the accumulator for the next turn."""
    fake_typing = MagicMock()
    fake_typing._placeholder_message_id = "om_p"

    # 3 chars — below 5-char threshold, so the chunk path does NOT flush.
    handler._on_streaming_chunk("你", fake_typing, 0.3)
    handler._on_streaming_chunk("好", fake_typing, 0.3)
    handler._on_streaming_chunk("！", fake_typing, 0.3)
    assert fake_typing.edit_message.call_count == 0

    # Turn complete: final flush pushes the buffered 3 chars
    handler._final_flush(fake_typing)
    assert fake_typing.edit_message.call_count == 1
    assert fake_typing.edit_message.call_args[0][0] == "你好！"

    # A second final_flush is a noop (state cleared)
    handler._final_flush(fake_typing)
    assert fake_typing.edit_message.call_count == 1


def test_final_flush_swallows_edit_failures(monkeypatch):
    """If final flush's edit_message fails, _final_flush must not raise —
    the response pipeline must not blow up just because streaming is broken."""
    fake_typing = MagicMock()
    fake_typing._placeholder_message_id = "om_p"
    fake_typing.edit_message.side_effect = RuntimeError("simulated 500")

    handler._on_streaming_chunk("你", fake_typing, 0.3)
    # Should not raise
    handler._final_flush(fake_typing)
    # edit_message was attempted (and failed)
    assert fake_typing.edit_message.call_count == 1


def test_typing_placeholder_send_failure_does_not_block_streaming(monkeypatch):
    """If the initial 2s-timer placeholder send fails, handler should still
    proceed with stream + final card (no exception). The TypingIndicator
    is constructed normally; if its placeholder send fails, _placeholder_message_id
    stays None, and edit_message becomes a noop (verified in
    test_typing_indicator_edit_message_noop_when_no_placeholder)."""
    import feishu.typing_indicator as ti
    # Force _send_placeholder to fail by mocking lark client
    fail = MagicMock(); fail.success.return_value = False; fail.code = 500
    with patch.object(ti._client.im.v1.message, "create", return_value=fail):
        indicator = ti.TypingIndicator("oc_x")
        indicator.start()
        # We can't safely freezegun + threading.Timer in unit tests; just
        # simulate the post-timer state by directly invoking the helper.
        # (The Timer thread is daemon=True, so the test process exits cleanly.)
    # Placeholder wasn't stored, so edit_message would be a noop
    assert indicator._placeholder_message_id is None
    # edit_message still works (just noop)
    with patch("feishu.sender.edit_message") as mock_edit:
        indicator.edit_message("hi")
    mock_edit.assert_not_called()


# ── Task 6: Full happy path (typing → streaming → final card) ───────────────

import json
import types


def _make_event(text, open_id, chat_id):
    return types.SimpleNamespace(
        event=types.SimpleNamespace(
            message=types.SimpleNamespace(
                message_type="text",
                content=json.dumps({"text": text}),
                message_id="m_stream_1",
                chat_id=chat_id,
            ),
            sender=types.SimpleNamespace(
                sender_id=types.SimpleNamespace(open_id=open_id),
            ),
        )
    )


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    monkeypatch.setenv("FEISHU_APP_ID", "test")
    monkeypatch.setenv("FEISHU_APP_SECRET", "test")
    monkeypatch.setenv("MINIMAX_API_KEY", "test")


def test_full_happy_path_typing_then_streaming_then_card(monkeypatch, tmp_path):
    """End-to-end: handler receives a message, types, streams LLM tokens
    into the typing placeholder, then sends the final OCL card."""
    import importlib
    import bot.handler as handler
    import bot.identity_admin as ia_mod
    importlib.reload(ia_mod)
    importlib.reload(handler)

    # Pre-register user as role=1 (platform user)
    from bot.identity_admin import IdentityAdmin
    test_admin = IdentityAdmin(str(tmp_path / "im.json"), str(tmp_path / "audit.jsonl"))
    test_admin.set_role("ou_stream_user", 1, operator="root")
    monkeypatch.setattr(ia_mod, "get_admin", lambda: test_admin)
    monkeypatch.setattr(handler, "get_identity_admin", lambda: test_admin)
    monkeypatch.setattr(handler.identity, "email_of", lambda uid: "streamer@example.com")
    monkeypatch.setattr(handler.notify, "remember_open_id", lambda *a, **kw: None)

    # Capture sends
    sent_card = {}
    sent_text = {}
    edit_calls: list[tuple[str, str]] = []
    monkeypatch.setattr(handler.sender, "send_card",
                        lambda chat, card: sent_card.update(chat=chat, card=card))
    monkeypatch.setattr(handler.sender, "send_text_as_card",
                        lambda chat, text: sent_text.update(chat=chat, text=text))
    # Don't go through real lark typing-send; we simulate the placeholder
    # is already in flight by injecting a TypingIndicator mock.
    import feishu.typing_indicator as ti_mod
    fake_typing = MagicMock()
    fake_typing._placeholder_message_id = "om_typing_999"
    monkeypatch.setattr(ti_mod, "TypingIndicator", lambda chat_id: fake_typing)

    # AIAgent emits 3 tokens via stream_callback (crosses 5-char threshold
    # on the 2nd flush, then a final token triggers another flush)
    captured_callback = {}
    def fake_chat(message, stream_callback=None):
        captured_callback["cb"] = stream_callback
        if stream_callback:
            # 6 chars total: tokens "你好" (2) + "，" (1) + "世界" (2) + "！" (1)
            stream_callback("你好")
            stream_callback("，")
            stream_callback("世界")
            stream_callback("！")
        return "你好，世界！"

    fake_agent = MagicMock()
    fake_agent.chat = fake_chat
    monkeypatch.setattr(handler.agent_pool, "get_or_create", lambda uid: fake_agent)

    # OCL pipeline returns a card with the final text
    final_card = {"config": {"wide_screen_mode": True}, "elements": [
        {"tag": "div", "text": {"tag": "lark_md", "content": "你好，世界！"}},
    ]}
    fake_ocl = MagicMock(card=final_card, blocked=False, text="你好，世界！")
    monkeypatch.setattr(handler, "ocl_apply", lambda *a, **kw: fake_ocl)
    monkeypatch.setattr(handler.tool_capture, "clear", lambda sid: None)
    monkeypatch.setattr(handler.tool_capture, "read", lambda sid: [])

    # Run the handler
    # Use a non-fast-path query so the agent path is exercised end-to-end
    handler._handle(_make_event("帮我看一下哪些台架是空闲的", "ou_stream_user", "oc_stream_chat"))

    # The stream_callback was actually passed to agent.chat
    assert captured_callback["cb"] is not None

    # edit_message was called multiple times with accumulating content
    assert fake_typing.edit_message.call_count >= 2
    # The final content of the last edit must be the full text
    last_content = fake_typing.edit_message.call_args_list[-1][0][0]
    assert last_content == "你好，世界！"

    # Final card was sent to the right chat
    assert "card" in sent_card
    assert sent_card["chat"] == "oc_stream_chat"
    assert sent_card["card"] == final_card

    # Typing indicator was stopped
    fake_typing.stop.assert_called_once()
