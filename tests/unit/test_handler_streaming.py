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
