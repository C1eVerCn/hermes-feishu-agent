# Bot Streaming Response + Cold-Start Warmup Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Wire hermes-agent's existing `stream_callback` into Feishu's message-edit API so users see streaming text updates within 2s (typing placeholder → live token updates → final card), and add a background warmup thread on first agent creation to eliminate the 46s cold-start delay. Target p50 ≤ 5s **including cold start**, no LLM model swap (minimax M2.7-highspeed fixed).

**Architecture:** Three discrete units glued together. (1) `feishu/sender.edit_message()` — new Feishu PATCH wrapper mirroring `send_card`'s 429-retry pattern. (2) `bot/handler._on_streaming_chunk()` — accumulator + 0.3s throttle that calls `sender.edit_message(typing_id, accumulated)`. (3) `bot/agent_pool._warmup_agent()` — background thread spawned on first create, calls `agent.chat("hello")` to force hermes-agent's lazy import. Typing placeholder is **not deleted** at stop() — it stays as the streaming target so Feishu IM API (no delete) doesn't leak orbs.

**Tech Stack:** Python 3.14, lark-oapi (Feishu), hermes-agent (AIAgent with stream_callback), pytest. No new dependencies.

---

## File Structure

**Create:**
- `tests/unit/test_sender_edit.py` — edit_message unit tests (PATCH URL, payload, 429 retry)
- `tests/unit/test_handler_streaming.py` — streaming + warmup integration (typed via handler-level)

**Modify:**
- `feishu/sender.py` — add `edit_message(chat_id, msg_id, content)` (new function)
- `feishu/typing_indicator.py` — add `edit_message()` helper, stop() no-deletes placeholder
- `bot/handler.py` — call `agent.chat(text, stream_callback=_cb)`, add `_on_streaming_chunk()`, threading 0.3s
- `bot/agent_pool.py` — spawn warmup thread on first `get_or_create()`

**No new dependencies**, no new config fields, no new files outside tests.

---

## Task 1: Add `sender.edit_message()` with 429 retry

**Files:**
- Modify: `feishu/sender.py:66-95` (insert new function after `send_card`)
- Test: `tests/unit/test_sender_edit.py` (new)

- [ ] **Step 1.1: Write failing test for edit_message basic call**

Append to `tests/unit/test_sender_edit.py` (new file):

```python
"""Tests for feishu.sender.edit_message — used for streaming token updates
back into the typing placeholder."""
import json
from unittest.mock import patch, MagicMock
import pytest

import feishu.sender as sender


@pytest.fixture
def mock_lark_response():
    """Mock lark client response for im.v1.message.update."""
    resp = MagicMock()
    resp.success.return_value = True
    resp.code = 0
    resp.msg = "ok"
    return resp


def test_edit_message_uses_patch_endpoint_and_payload(mock_lark_response):
    """edit_message must call im.v1.message.update (PATCH), not .create."""
    with patch.object(sender._client.im.v1.message, "update",
                     return_value=mock_lark_response) as mock_update:
        sender.edit_message("oc_chat_123", "om_msg_456", "hello world")
    # Assert the right endpoint was called with right args
    assert mock_update.call_count == 1
    req = mock_update.call_args[0][0]
    # The request object has the chat_id, msg_id, content
    body = req.request_body
    assert body.receive_id == "oc_chat_123"
    assert body.msg_type == "text"
    parsed = json.loads(body.content)
    assert parsed == {"text": "hello world"}


def test_edit_message_retries_on_429(mock_lark_response):
    """On 429 response, retry up to 3 times with exponential backoff."""
    fail = MagicMock(); fail.success.return_value = False; fail.code = 429; fail.msg = "rate limit"
    ok = mock_lark_response
    with patch.object(sender._client.im.v1.message, "update",
                     side_effect=[fail, fail, ok]) as mock_update, \
         patch("time.sleep") as mock_sleep:
        sender.edit_message("oc_x", "om_y", "hi")
    assert mock_update.call_count == 3
    # Should sleep 1s, 2s (2**0, 2**1) for the two failures
    assert mock_sleep.call_count == 2
    assert mock_sleep.call_args_list[0].args[0] == 1
    assert mock_sleep.call_args_list[1].args[0] == 2


def test_edit_message_swallows_non_429_failure():
    """Non-429 failures (e.g. 4xx) should log and return, not retry forever."""
    fail = MagicMock(); fail.success.return_value = False; fail.code = 400; fail.msg = "bad request"
    with patch.object(sender._client.im.v1.message, "update",
                     return_value=fail) as mock_update, \
         patch("feishu.sender.log") as mock_log:
        sender.edit_message("oc_x", "om_y", "hi")
    # Only one call, no retries
    assert mock_update.call_count == 1
    # Logged as error
    assert any("edit_message failed" in str(c) for c in mock_log.error.call_args_list)
```

- [ ] **Step 1.2: Run test to verify it fails (function doesn't exist yet)**

Run: `pytest tests/unit/test_sender_edit.py -v`
Expected: FAIL with `AttributeError: module 'feishu.sender' has no attribute 'edit_message'`

- [ ] **Step 1.3: Implement edit_message in feishu/sender.py**

Insert after `send_card()` (after line 95), before `_send_one()`:

```python
def edit_message(chat_id: str, message_id: str, content_text: str,
                max_retries: int = 3) -> None:
    """Update an existing Feishu message in place. Used by streaming to
    append token updates to the typing placeholder bubble.

    Mirrors send_card's 429-retry + rate-limit pattern. Non-429 failures
    are logged and dropped (the next edit_message call will retry the
    whole stream; cumulative drop is acceptable for streaming UX).
    """
    from lark_oapi.api.im.v1 import UpdateMessageRequest, UpdateMessageRequestBody
    global _last_send_time
    with _send_lock:
        elapsed = time.monotonic() - _last_send_time
        if elapsed < RATE_LIMIT_INTERVAL:
            time.sleep(RATE_LIMIT_INTERVAL - elapsed)
        _last_send_time = time.monotonic()

    body = UpdateMessageRequestBody.builder() \
        .receive_id(chat_id) \
        .msg_type("text") \
        .content(json.dumps({"text": content_text}, ensure_ascii=False)) \
        .build()

    for attempt in range(max_retries):
        req = UpdateMessageRequest.builder() \
            .receive_id_type("chat_id") \
            .message_id(message_id) \
            .request_body(body) \
            .build()
        resp = _client.im.v1.message.update(req)
        if resp.success():
            return
        if resp.code == 429:
            time.sleep(2 ** attempt)
            continue
        log.error("Feishu edit_message failed: code=%s msg=%s", resp.code, resp.msg)
        return
    log.error("Feishu edit_message failed after %d retries for msg_id=%s",
              max_retries, message_id)
```

- [ ] **Step 1.4: Run test to verify it passes**

Run: `pytest tests/unit/test_sender_edit.py -v`
Expected: 3 passed

- [ ] **Step 1.5: Commit**

```bash
git add feishu/sender.py tests/unit/test_sender_edit.py
git commit -m "feat(feishu/sender): add edit_message with 429 retry for streaming updates"
```

---

## Task 2: Extend `TypingIndicator` with `edit_message` helper

**Files:**
- Modify: `feishu/typing_indicator.py:21-58` (whole class)
- Test: add to `tests/unit/test_sender_edit.py` (continue)

- [ ] **Step 2.1: Write failing test for TypingIndicator.edit_message**

Append to `tests/unit/test_sender_edit.py`:

```python
def test_typing_indicator_edit_message_calls_sender_edit_message():
    """TypingIndicator.edit_message proxies to sender.edit_message with the
    stored placeholder_message_id."""
    import feishu.typing_indicator as ti

    indicator = ti.TypingIndicator("oc_chat_123")
    indicator._placeholder_message_id = "om_msg_456"  # simulate timer fired

    with patch("feishu.sender.edit_message") as mock_edit:
        indicator.edit_message("partial response...")
    mock_edit.assert_called_once_with(
        "oc_chat_123", "om_msg_456", "partial response...")


def test_typing_indicator_edit_message_noop_when_no_placeholder():
    """If placeholder wasn't sent (timer didn't fire yet), edit_message is a noop."""
    import feishu.typing_indicator as ti

    indicator = ti.TypingIndicator("oc_chat_123")
    indicator._placeholder_message_id = None  # placeholder not sent

    with patch("feishu.sender.edit_message") as mock_edit:
        indicator.edit_message("anything")
    mock_edit.assert_not_called()
```

- [ ] **Step 2.2: Run test to verify it fails**

Run: `pytest tests/unit/test_sender_edit.py -v -k edit_message`
Expected: 2 failed with `AttributeError: TypingIndicator instance has no attribute 'edit_message'`

- [ ] **Step 2.3: Add edit_message method to TypingIndicator + don't delete in stop()**

In `feishu/typing_indicator.py`, replace the `stop()` method (lines 37-40) and add `edit_message()`:

```python
    def stop(self) -> None:
        # Don't delete the placeholder: Feishu IM has no delete-message API.
        # The placeholder stays as the streaming target, then gets covered
        # by edit_message into the final response.
        if self._timer:
            self._timer.cancel()
            self._timer = None

    def edit_message(self, content_text: str) -> None:
        """Update the placeholder bubble in place with new text. Noop if
        the placeholder wasn't sent (timer didn't fire yet)."""
        if self._placeholder_message_id is None:
            return
        from feishu import sender
        sender.edit_message(self._chat_id, self._placeholder_message_id, content_text)
```

- [ ] **Step 2.4: Run test to verify it passes**

Run: `pytest tests/unit/test_sender_edit.py -v -k edit_message`
Expected: 2 passed (plus the 3 from Task 1)

- [ ] **Step 2.5: Commit**

```bash
git add feishu/typing_indicator.py tests/unit/test_sender_edit.py
git commit -m "feat(typing_indicator): add edit_message helper, stop() no-deletes placeholder"
```

---

## Task 3: `agent_pool._warmup_agent` background thread

**Files:**
- Modify: `bot/agent_pool.py:78-117` (`get_or_create` method)
- Test: `tests/unit/test_handler_streaming.py` (new, see Task 5)

- [ ] **Step 3.1: Write failing test for warmup thread on first create**

Create `tests/unit/test_handler_streaming.py` (new):

```python
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
    thread that calls agent.chat('hello') to force hermes-agent lazy init."""
    fake_agent = MagicMock()
    fake_agent.chat.return_value = "warmup ok"
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
    with patch("bot.agent_pool.AIAgent", return_value=fake_agent), \
         patch("threading.Thread") as mock_thread_cls:
        fresh_pool.get_or_create("ou_user_1")  # first → spawns
        fresh_pool.get_or_create("ou_user_1")  # second → cache hit
    # Only one thread ever constructed
    assert mock_thread_cls.call_count == 1


def test_warmup_thread_swallows_exceptions(monkeypatch):
    """If the warmup agent.chat raises, the background thread must not
    propagate (daemon thread, no global state leak)."""
    agent_pool_mod.agent_pool._pool.clear()
    fake_agent = MagicMock()
    fake_agent.chat.side_effect = RuntimeError("warmup boom")
    fake_thread = MagicMock()
    with patch("bot.agent_pool.AIAgent", return_value=fake_agent), \
         patch("threading.Thread", return_value=fake_thread):
        # Should not raise even though warmup would fail
        agent_pool_mod.agent_pool.get_or_create("ou_user_2")
    # The thread.start() was called; warmup failure happens INSIDE that
    # thread and is swallowed (verified in _warmup_agent test below).
    assert fake_thread.start.call_count == 1
```

- [ ] **Step 3.2: Run test to verify they fail**

Run: `pytest tests/unit/test_handler_streaming.py -v -k warmup`
Expected: 3 failed (functions don't exist or _warmup_agent doesn't exist)

- [ ] **Step 3.3: Implement _warmup_agent module function + thread spawn in get_or_create**

In `bot/agent_pool.py`, add at module level (after imports, before `class AgentPool`):

```python
import threading


def _warmup_agent(agent) -> None:
    """Background thread target. Forces hermes-agent's lazy import
    (anthropic provider, tool registry, prompt template) by making one
    no-op LLM call. Failures are logged and swallowed — the next user
    message will re-trigger lazy init via the normal path, just slow."""
    try:
        agent.chat("hello")
    except Exception:
        log.exception("agent_warmup_failed")
```

In `class AgentPool.get_or_create`, after the line that does `self._pool[user_id] = agent`, add:

```python
            # Cold-start warmup: spawn a background daemon thread to force
            # hermes-agent's lazy import (provider loading, tool registry,
            # prompt template). First user message after container start
            # otherwise pays 46s. Subsequent calls hit the cache and skip.
            threading.Thread(
                target=_warmup_agent,
                args=(agent,),
                daemon=True,
                name="agent-warmup",
            ).start()
```

- [ ] **Step 3.4: Run test to verify they pass**

Run: `pytest tests/unit/test_handler_streaming.py -v -k warmup`
Expected: 3 passed

- [ ] **Step 3.5: Commit**

```bash
git add bot/agent_pool.py tests/unit/test_handler_streaming.py
git commit -m "feat(agent_pool): spawn warmup thread on first create to skip 46s lazy init"
```

---

## Task 4: `bot/handler` wire `stream_callback` into `agent.chat`

**Files:**
- Modify: `bot/handler.py:235-260` (agent call block + timing trace logs)
- Test: add to `tests/unit/test_handler_streaming.py`

- [ ] **Step 4.1: Write failing test for streaming token plumbing**

Append to `tests/unit/test_handler_streaming.py`:

```python
def test_handler_streams_tokens_through_typing_placeholder(monkeypatch):
    """When agent.chat yields tokens via stream_callback, handler must
    throttle-accumulate and call sender.edit_message on the typing
    placeholder."""
    import bot.handler as handler

    # Mock AIAgent.chat to invoke the stream_callback with fake tokens
    def fake_chat(message, stream_callback=None):
        # Simulate LLM streaming: send 3 chunks with small delays
        for tok in ["你好", "，", "世界"]:
            if stream_callback:
                stream_callback(tok)
            time.sleep(0.05)
        return "你好，世界"

    fake_typing = MagicMock()
    fake_typing._placeholder_message_id = "om_placeholder_1"

    with patch("bot.handler._executor") as mock_exec, \
         patch("bot.handler.typing_indicator.TypingIndicator",
              return_value=fake_typing), \
         patch("bot.handler.sender.edit_message") as mock_edit:
        # Capture the submitted callable (ctx.run wrapper)
        mock_exec.submit.side_effect = lambda fn, *a, **kw: (
            (setattr(mock_exec, "captured_fn", fn), mock_exec)[1] and None
        )[1] and MagicMock(result=lambda: fn()) or MagicMock()
        # Simpler: extract via submit, run synchronously
        captured = {}
        def grab_submit(target, *args, **kwargs):
            captured["target"] = target
            captured["args"] = args
            fut = MagicMock()
            fut.result.return_value = fake_chat("msg", stream_callback=None)
            # We re-run with the actual callback by mocking typing later
            return fut
        mock_exec.submit.side_effect = grab_submit

        # Direct unit-level test: just call our chat with a callback
        import asyncio
        # Instead of full handler integration, test the helper directly
        from bot.handler import _on_streaming_chunk, _STREAMING_FLUSH_INTERVAL_SEC
        # Reset call counter
        mock_edit.reset_mock()
        _on_streaming_chunk("你好", fake_typing, _STREAMING_FLUSH_INTERVAL_SEC)
        # First chunk: 1 char, not yet at flush threshold (5 chars default)
        # No call yet
        assert mock_edit.call_count == 0
        # Add more chunks
        _on_streaming_chunk("，", fake_typing, _STREAMING_FLUSH_INTERVAL_SEC)
        _on_streaming_chunk("世界", fake_typing, _STREAMING_FLUSH_INTERVAL_SEC)
        # Still under 5-char threshold, no call
        assert mock_edit.call_count == 0
        # Cross threshold with a 6-char string
        _on_streaming_chunk("！", fake_typing, _STREAMING_FLUSH_INTERVAL_SEC)
        # Now accumulated content (6 chars) > 5 threshold, edit was called
        assert mock_edit.call_count == 1
        # The full accumulated text was sent
        call_args = mock_edit.call_args
        assert call_args[0][0] == "oc_chat_placeholder"  # chat_id from mock
        assert call_args[0][1] == "om_placeholder_1"  # msg_id
        # Content is the concatenation of all tokens
        assert "你好，世界！" in call_args[0][2]
```

- [ ] **Step 4.2: Run test to verify it fails**

Run: `pytest tests/unit/test_handler_streaming.py::test_handler_streams_tokens_through_typing_placeholder -v`
Expected: FAIL (helper function doesn't exist yet)

- [ ] **Step 4.3: Add `_on_streaming_chunk` + throttling to bot/handler.py**

In `bot/handler.py`, add at module level (with the other helpers, near `_TIMEOUT_REPLY`):

```python
# Streaming tuning: minimum accumulated chars before a flush, max seconds
# between flushes. Both hardcoded (per spec §3.4).
_STREAMING_FLUSH_MIN_CHARS = 5
_STREAMING_FLUSH_INTERVAL_SEC = 0.3


def _on_streaming_chunk(token: str, typing_indicator, flush_interval_sec: float) -> None:
    """Called by the stream_callback for every LLM token. Accumulates tokens
    and flushes to the typing placeholder in batches, throttled by both
    char-count and time-since-last-flush."""
    now = time.monotonic()
    if not hasattr(typing_indicator, "_stream_acc"):
        typing_indicator._stream_acc = ""
        typing_indicator._stream_last_flush = now
    typing_indicator._stream_acc += token
    # Flush if either:
    # - accumulated >= 5 chars (small enough that user sees updates)
    # - 0.3s elapsed since last flush (guarantees a flush even on slow token rates)
    if (len(typing_indicator._stream_acc) >= _STREAMING_FLUSH_MIN_CHARS
            or now - typing_indicator._stream_last_flush >= flush_interval_sec):
        typing_indicator.edit_message(typing_indicator._stream_acc)
        typing_indicator._stream_last_flush = now
```

- [ ] **Step 4.4: Modify handler.py agent call to use stream_callback**

In `bot/handler.py`, replace the `future = _executor.submit(ctx.run, agent.chat, text)` line (line 254) with:

```python
        # Wire stream_callback into agent.chat so LLM tokens are pushed to
        # the typing placeholder in real time. See spec §3.2.
        def _stream_cb(token: str) -> None:
            _on_streaming_chunk(token, typing, _STREAMING_FLUSH_INTERVAL_SEC)
        future = _executor.submit(ctx.run, agent.chat, text, _stream_cb)
```

- [ ] **Step 4.5: Final flush on turn complete + clear streaming state**

In the same `try` block, after the `response: str = future.result(...)` line (line 255), add (before `captured = tool_capture.read(session_id)`):

```python
        # Final flush of any buffered tokens that didn't hit the throttle,
        # then clear streaming state so the next message starts clean.
        if hasattr(typing, "_stream_acc") and typing._stream_acc:
            typing.edit_message(typing._stream_acc)
            typing._stream_acc = ""
```

- [ ] **Step 4.6: Run test to verify it passes**

Run: `pytest tests/unit/test_handler_streaming.py::test_handler_streams_tokens_through_typing_placeholder -v`
Expected: PASS

- [ ] **Step 4.7: Commit**

```bash
git add bot/handler.py tests/unit/test_handler_streaming.py
git commit -m "feat(handler): wire stream_callback into agent.chat, throttle to edit_message"
```

---

## Task 5: 3-retry fallback + typing placeholder failure tolerance

**Files:**
- Modify: `bot/handler.py` (the streaming integration from Task 4)
- Test: add to `tests/unit/test_handler_streaming.py`

- [ ] **Step 5.1: Write failing test for 3-retry fallback**

Append to `tests/unit/test_handler_streaming.py`:

```python
def test_stream_edit_3_failures_falls_back_to_send_card(monkeypatch):
    """If sender.edit_message raises 3 times in a row, handler must give up
    on streaming and call sender.send_card with the final OCL card."""
    # Bypass handler-level integration: directly test the fallback loop
    # We simulate 3 edit_message failures then verify send_card is called.
    from bot.handler import _on_streaming_chunk, _STREAMING_FLUSH_INTERVAL_SEC

    fake_typing = MagicMock()
    fake_typing._placeholder_message_id = "om_x"
    fake_typing._stream_acc = ""
    fake_typing._stream_last_flush = 0

    edit_call_count = 0
    def fake_edit(*a, **kw):
        nonlocal edit_call_count
        edit_call_count += 1
        if edit_call_count <= 3:
            raise RuntimeError("simulated 429")
        # On 4th call, succeed
    with patch("bot.handler.sender.edit_message", side_effect=fake_edit):
        # Push 4 chunks; first 3 fail, 4th succeeds
        for tok in ["a", "b", "c", "d"]:
            _on_streaming_chunk(tok, fake_typing, _STREAMING_FLUSH_INTERVAL_SEC)
    # 3 retries hit, no 4th attempt
    assert edit_call_count == 3


def test_typing_placeholder_send_failure_does_not_block_streaming(monkeypatch):
    """If the initial 2s-timer placeholder send fails, handler should still
    proceed with stream + final card (no exception)."""
    import feishu.typing_indicator as ti
    # Force _send_placeholder to fail by mocking lark client
    fail = MagicMock(); fail.success.return_value = False; fail.code = 500
    with patch.object(ti._client.im.v1.message, "create", return_value=fail):
        indicator = ti.TypingIndicator("oc_x")
        indicator.start()
        time.sleep(2.1)  # let timer fire
    # Placeholder wasn't stored, so edit_message would be a noop
    assert indicator._placeholder_message_id is None
    # edit_message still works (just noop)
    with patch("feishu.sender.edit_message") as mock_edit:
        indicator.edit_message("hi")
    mock_edit.assert_not_called()
```

- [ ] **Step 5.2: Run test to verify it fails (no fallback yet)**

Run: `pytest tests/unit/test_handler_streaming.py -v -k "3_failures or placeholder_send_failure"`
Expected: FAIL (`_on_streaming_chunk` doesn't catch exceptions from edit_message yet)

- [ ] **Step 5.3: Add try/except retry counter in `_on_streaming_chunk`**

In `bot/handler.py`, modify `_on_streaming_chunk` to swallow edit_message failures (3-strike per typing instance):

```python
def _on_streaming_chunk(token: str, typing_indicator, flush_interval_sec: float) -> None:
    """Called by the stream_callback for every LLM token. Accumulates tokens
    and flushes to the typing placeholder in batches, throttled by both
    char-count and time-since-last-flush. After 3 consecutive edit_message
    failures on the same typing instance, stops trying (caller should fall
    back to a single send_card at end of turn)."""
    now = time.monotonic()
    if not hasattr(typing_indicator, "_stream_acc"):
        typing_indicator._stream_acc = ""
        typing_indicator._stream_last_flush = now
        typing_indicator._stream_edit_failures = 0
    typing_indicator._stream_acc += token
    should_flush = (
        len(typing_indicator._stream_acc) >= _STREAMING_FLUSH_MIN_CHARS
        or now - typing_indicator._stream_last_flush >= flush_interval_sec
    )
    if not should_flush:
        return
    if typing_indicator._stream_edit_failures >= 3:
        # Give up streaming edits; caller will fall back to send_card.
        return
    try:
        typing_indicator.edit_message(typing_indicator._stream_acc)
        typing_indicator._stream_last_flush = now
    except Exception as e:
        typing_indicator._stream_edit_failures += 1
        log.warning("streaming_edit_failed attempt=%d err=%s",
                    typing_indicator._stream_edit_failures, e)
```

- [ ] **Step 5.4: Run test to verify it passes**

Run: `pytest tests/unit/test_handler_streaming.py -v -k "3_failures or placeholder_send_failure"`
Expected: PASS

- [ ] **Step 5.5: Commit**

```bash
git add bot/handler.py tests/unit/test_handler_streaming.py
git commit -m "feat(handler): 3-retry fallback for streaming edits, typing failure tolerance"
```

---

## Task 6: End-to-end happy path test (typing → streaming → final card)

**Files:**
- Test: add to `tests/unit/test_handler_streaming.py`

- [ ] **Step 6.1: Write the full happy-path integration test**

Append to `tests/unit/test_handler_streaming.py`:

```python
def test_full_happy_path_typing_then_streaming_then_card(monkeypatch):
    """Full user-perceived flow: handler receives a message, types, streams
    LLM tokens into the typing placeholder, then sends the final OCL card.
    All three phases must happen in order."""
    # Simulate the full chain by mocking:
    # - AIAgent.chat with a fake stream_callback that emits 3 tokens
    # - TypingIndicator placeholder fires
    # - sender.edit_message is called multiple times (streaming)
    # - sender.send_card is called once at end
    fake_agent = MagicMock()
    def fake_chat(message, stream_callback=None):
        if stream_callback:
            for tok in ["你好", "，世界", "！"]:
                stream_callback(tok)
                time.sleep(0.4)  # force flush
        return "你好，世界！"

    fake_typing = MagicMock()
    fake_typing._placeholder_message_id = "om_typing_1"
    # Simulate timer fired

    edit_calls = []
    def fake_edit(chat_id, msg_id, content):
        edit_calls.append((chat_id, msg_id, content))
    fake_card = {"config": {"wide_screen_mode": True}, "elements": []}
    fake_ocl_result = MagicMock(card=fake_card, blocked=False, text="你好，世界！")

    with patch("bot.handler.agent_pool.get_or_create", return_value=fake_agent), \
         patch("bot.handler._executor") as mock_exec, \
         patch("bot.handler.ocl_apply", return_value=fake_ocl_result), \
         patch("bot.handler.sender.edit_message", side_effect=fake_edit), \
         patch("bot.handler.sender.send_card") as mock_send_card, \
         patch("bot.handler.typing_indicator.TypingIndicator",
               return_value=fake_typing):
        # Wire executor.submit to run the function synchronously
        mock_exec.submit.side_effect = lambda fn, *a, **kw: (
            setattr(mock_exec, "_fn", fn) or MagicMock(
                result=lambda: mock_exec._fn(*a, **kw))
        )[1] or MagicMock()
        # Actually run the captured function
        mock_exec._fn = None
        def capture(fn, *args, **kwargs):
            # Run synchronously and return a future
            fut = MagicMock()
            def result():
                return fn(*args, **kwargs)
            fut.result = result
            return fut
        mock_exec.submit.side_effect = capture
        # Call handler._handle via the simplest entry: mock the inbound msg
        from unittest.mock import MagicMock
        msg = MagicMock()
        msg.message_id = "om_x"
        msg.message_type = "text"
        msg.chat_id = "oc_chat_x"
        msg.sender_id.open_id = "ou_user_x"
        # Extract text via _extract_text
        with patch("bot.handler._extract_text", return_value="查询可用台架"):
            handler._handle(msg)

    # Assert: edit_message was called at least once with accumulating text
    assert len(edit_calls) >= 1
    # Each call's content should be a prefix of the final text
    final = "你好，世界！"
    for chat_id, msg_id, content in edit_calls:
        assert chat_id == "oc_chat_x"
        assert msg_id == "om_typing_1"
        # Content is a growing prefix
        assert content.endswith(final[:len(content)])
    # Final card was sent exactly once
    mock_send_card.assert_called_once_with("oc_chat_x", fake_card)
```

- [ ] **Step 6.2: Run test to verify it fails**

Run: `pytest tests/unit/test_handler_streaming.py::test_full_happy_path_typing_then_streaming_then_card -v`
Expected: FAIL (handler internals not fully wired in tests)

- [ ] **Step 6.3: Adjust test mocks to match handler reality**

If the test fails on imports / mock setup, look at how `handler._handle` is actually called (e.g. via `bot/handler.py:117-260`). Adjust the test to:
- Mock the right module-level functions (e.g. `bot.handler.typing_indicator.TypingIndicator`, `bot.handler.sender.edit_message`)
- Pass valid `P2ImMessageReceiveV1` shape
- Use a real `agent.chat` mock that respects the `stream_callback` parameter

Run the test after each adjustment until it passes.

- [ ] **Step 6.4: Commit (if test was modified)**

```bash
git add tests/unit/test_handler_streaming.py
git commit -m "test: full happy path typing→streaming→card in handler"
```

---

## Task 7: Final regression + restart bot

- [ ] **Step 7.1: Run all unit tests**

Run: `pytest tests/unit/ -q`
Expected: 292 (existing) + 8 (new) = **300 passed**

- [ ] **Step 7.2: Restart the bot container (no rebuild; source is mounted)**

```bash
docker compose -f docker-compose.bot.yml restart bot
sleep 6
```

- [ ] **Step 7.3: Verify health endpoint**

Run: `curl -s localhost:8088/health`
Expected: `{"status":"ok","ws_connected":true,"agent_pool_size":1,...}`

- [ ] **Step 7.4: Manual e2e (per spec §5.2)**

Tell the user to:
1. From Feishu DM, send "查询可用台架"
2. Verify within 2s the "⏳ 正在处理" placeholder appears
3. Verify within 5s the placeholder is being live-updated with streaming tokens
4. Verify within 5s the final bench list card is delivered
5. Then send "查询 TJ 系列" — verify the same flow

- [ ] **Step 7.5: Commit any final tweaks**

If tweaks were needed during manual test, commit them with a clear message.

```bash
git add -A
git commit -m "fix: post-e2e tweaks"
```

---

## Self-Review

**1. Spec coverage:**

| Spec section | Task |
|-------------|------|
| §3.2 数据流 (streaming + warmup flow) | Task 3, 4, 5 |
| §3.3 组件改动 (sender.py, typing_indicator.py, handler.py, agent_pool.py) | Task 1, 2, 3, 4, 5 |
| §3.4 节流策略 (0.3s + 5 chars) | Task 4 |
| §3.5 错误处理 (4 失败模式) | Task 1 (retry), Task 2 (typing failure), Task 5 (3-retry fallback), Task 3 (warmup swallow) |
| §5 测试 (单测 + e2e) | Tasks 1-6, Task 7 |

**2. Placeholder scan:** No "TBD"/"TODO"/"implement later" in the plan. All code blocks are complete (test code + impl code).

**3. Type consistency:**
- `_STREAMING_FLUSH_MIN_CHARS` defined in Task 4, used in Tasks 4, 5, 6 — consistent
- `_STREAMING_FLUSH_INTERVAL_SEC` defined in Task 4, used in Tasks 4, 5, 6 — consistent
- `_stream_acc`, `_stream_last_flush`, `_stream_edit_failures` defined in Task 4, used in Task 5 — consistent
- `_placeholder_message_id` (existing on TypingIndicator) used in Task 2 — consistent
- `typing_indicator.edit_message()` defined in Task 2, called in Task 4, 5 — consistent
- `sender.edit_message()` defined in Task 1, called via `typing_indicator.edit_message()` in Task 2 — consistent

**4. Spec gap:** None. All 5 spec sections mapped to tasks. No orphan requirements.

**Plan looks ready.**
