"""Unit tests for agent_pool session_id integration (Phase 3.5)."""
import threading
from unittest.mock import patch, MagicMock
import pytest

import ocl.session_map as sm


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    """Clean session_map for each test, and provide settings env vars."""
    monkeypatch.setattr(sm, "_map", {})
    monkeypatch.setattr(sm, "_lock", threading.Lock())
    monkeypatch.setenv("FEISHU_APP_ID", "test")
    monkeypatch.setenv("FEISHU_APP_SECRET", "test")
    monkeypatch.setenv("MINIMAX_API_KEY", "test")


def _mock_agent(**kwargs):
    m = MagicMock()
    m.session_id = kwargs.get("session_id", "")
    return m


def test_passes_session_id_to_aiagent():
    from bot.agent_pool import AgentPool

    with patch("bot.agent_pool.AIAgent", side_effect=_mock_agent) as mock_agent_cls:
        pool = AgentPool(max_size=10)
        pool.get_or_create("ou_alice")
        # Verify AIAgent received session_id
        call_kwargs = mock_agent_cls.call_args.kwargs
        assert "session_id" in call_kwargs
        assert call_kwargs["session_id"] == "feishu_ou_alice"


def test_registers_session_map_on_create():
    from bot.agent_pool import AgentPool

    with patch("bot.agent_pool.AIAgent", side_effect=_mock_agent):
        pool = AgentPool(max_size=10)
        pool.get_or_create("ou_alice")
        assert sm.lookup("feishu_ou_alice") == "ou_alice"


def test_evicts_session_map_on_lru():
    from bot.agent_pool import AgentPool

    with patch("bot.agent_pool.AIAgent", side_effect=_mock_agent):
        pool = AgentPool(max_size=2)
        pool.get_or_create("user_A")
        pool.get_or_create("user_B")
        pool.get_or_create("user_C")  # evicts user_A
        # user_A's session_id should be evicted
        assert sm.lookup("feishu_user_A") == ""
        # user_B and user_C still mapped
        assert sm.lookup("feishu_user_B") == "user_B"
        assert sm.lookup("feishu_user_C") == "user_C"


def test_same_user_reuses_session_id():
    from bot.agent_pool import AgentPool

    with patch("bot.agent_pool.AIAgent", side_effect=_mock_agent):
        pool = AgentPool(max_size=10)
        a1 = pool.get_or_create("ou_alice")
        a2 = pool.get_or_create("ou_alice")
        assert a1 is a2  # same instance
        assert a1.session_id == "feishu_ou_alice"


def test_eviction_handles_missing_session_id(monkeypatch):
    """Agent without session_id attribute should not crash eviction."""
    from bot.agent_pool import AgentPool

    with patch("bot.agent_pool.AIAgent", side_effect=_mock_agent):
        pool = AgentPool(max_size=2)
        pool.get_or_create("user_A")
        pool.get_or_create("user_B")
        # Replace user_A's agent with one that has no session_id
        broken_agent = _mock_agent()
        broken_agent.session_id = None
        pool._pool["user_A"] = broken_agent
        # Eviction must not raise
        pool.get_or_create("user_C")


# ── enabled_toolsets alignment (added 2026-06-09 — bug fix) ────────────────

def test_enabled_toolsets_match_registered_toolsets(monkeypatch):
    """Regression: 'testbench' was hardcoded but no toolset was registered with
    that name → LLM had zero tools → hallucinated JSON in text responses.
    Ensure enabled_toolsets overlaps with at least one registered toolset."""
    from bot.agent_pool import AgentPool
    from tools.registry import registry
    import bench_tools.register  # noqa: F401 — side-effect registration
    import vlm_tools.register    # noqa: F401

    with patch("bot.agent_pool.AIAgent", side_effect=_mock_agent) as mock_agent_cls:
        pool = AgentPool(max_size=10)
        pool.get_or_create("ou_alice")
        enabled = mock_agent_cls.call_args.kwargs.get("enabled_toolsets") or []
        registered = set(registry.get_registered_toolset_names())
        overlap = set(enabled) & registered
        assert overlap, (
            f"enabled_toolsets={enabled} has no overlap with "
            f"registered toolsets={sorted(registered)} — LLM will hallucinate tools"
        )
        # Specifically: must include "bench" (the test-bench reservation toolset)
        assert "bench" in enabled, (
            f"bench toolset not enabled; LLM cannot call reservation tools. "
            f"enabled={enabled}"
        )
