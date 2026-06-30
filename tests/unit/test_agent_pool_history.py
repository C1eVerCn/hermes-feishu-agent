"""bot/agent_pool 的 per-user 历史 deque 单测（窗口/TTL/截断/驱逐）。"""
from unittest.mock import patch, MagicMock

import pytest

from bot.agent_pool import AgentPool, _HISTORY_MAXLEN, _HISTORY_CONTENT_CAP, _HISTORY_TTL_SECONDS


def _u(i):  return {"role": "user", "content": f"u{i}"}
def _a(i):  return {"role": "assistant", "content": f"a{i}"}


def _mock_agent(**kwargs):  # 复用 test_agent_pool.py 的 mock 模式
    return MagicMock()


def test_append_then_get_roundtrip():
    p = AgentPool(max_size=10)
    p.append_turn("ou_a", _u(1), _a(1))
    hist = p.get_history("ou_a")
    assert hist == [{"role": "user", "content": "u1"},
                    {"role": "assistant", "content": "a1"}]


def test_get_empty_for_unknown_user():
    p = AgentPool(max_size=10)
    assert p.get_history("nobody") == []


def test_window_caps_at_maxlen():
    p = AgentPool(max_size=10)
    for i in range(7):                       # 7 轮 = 14 条 > maxlen(12)
        p.append_turn("ou_a", _u(i), _a(i))
    hist = p.get_history("ou_a")
    assert len(hist) == _HISTORY_MAXLEN      # 12
    assert hist[0] == {"role": "user", "content": "u1"}   # 第 0 轮被挤掉


def test_content_capped():
    p = AgentPool(max_size=10)
    long = "x" * (_HISTORY_CONTENT_CAP + 500)
    p.append_turn("ou_a", {"role": "user", "content": long}, _a(1))
    assert len(p.get_history("ou_a")[0]["content"]) == _HISTORY_CONTENT_CAP


def test_ttl_clears_stale_history(monkeypatch):
    import bot.agent_pool as ap
    clock = {"t": 1000.0}
    monkeypatch.setattr(ap.time, "monotonic", lambda: clock["t"])
    p = AgentPool(max_size=10)
    p.append_turn("ou_a", _u(1), _a(1))
    clock["t"] = 1000.0 + _HISTORY_TTL_SECONDS + 1   # 超过 TTL
    assert p.get_history("ou_a") == []


def test_empty_user_id_is_noop():
    p = AgentPool(max_size=10)
    p.append_turn("", _u(1), _a(1))
    assert p.get_history("") == []


def test_clear_history():
    p = AgentPool(max_size=10)
    p.append_turn("ou_a", _u(1), _a(1))
    p.clear_history("ou_a")
    assert p.get_history("ou_a") == []


def test_none_content_becomes_empty_string():
    p = AgentPool(max_size=10)
    p.append_turn("ou_a", {"role": "user", "content": None}, _a(1))
    assert p.get_history("ou_a")[0] == {"role": "user", "content": ""}


def test_get_or_create_eviction_drops_history():
    """驱动真实 get_or_create 触发 LRU 驱逐，确认历史被同步清理。"""
    with patch("bot.agent_pool.AIAgent", side_effect=_mock_agent):
        p = AgentPool(max_size=1)
        p.get_or_create("ou_a")
        p.append_turn("ou_a", _u(1), _a(1))
        p.get_or_create("ou_b")          # 超出 max_size=1 → 驱逐 ou_a
        assert p.get_history("ou_a") == []
        assert "ou_a" not in p._history
