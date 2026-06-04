import threading
from unittest.mock import patch, MagicMock
from bot.agent_pool import AgentPool


def _mock_agent(**kwargs):
    return MagicMock()


def test_same_user_returns_same_instance():
    with patch("bot.agent_pool.AIAgent", side_effect=_mock_agent):
        pool = AgentPool(max_size=10)
        a1 = pool.get_or_create("user_A")
        a2 = pool.get_or_create("user_A")
        assert a1 is a2


def test_different_users_get_different_instances():
    with patch("bot.agent_pool.AIAgent", side_effect=_mock_agent):
        pool = AgentPool(max_size=10)
        a1 = pool.get_or_create("user_A")
        a2 = pool.get_or_create("user_B")
        assert a1 is not a2


def test_lru_evicts_at_max_size():
    with patch("bot.agent_pool.AIAgent", side_effect=_mock_agent):
        pool = AgentPool(max_size=2)
        a = pool.get_or_create("user_A")
        pool.get_or_create("user_B")
        pool.get_or_create("user_C")  # evicts user_A
        a_new = pool.get_or_create("user_A")
        assert a is not a_new


def test_thread_safe_concurrent_access():
    results = {}
    with patch("bot.agent_pool.AIAgent", side_effect=_mock_agent):
        pool = AgentPool(max_size=100)

        def get(user_id):
            results[user_id] = pool.get_or_create(user_id)

        threads = [threading.Thread(target=get, args=(f"user_{i}",)) for i in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

    assert len(results) == 20
