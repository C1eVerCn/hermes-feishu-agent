import time
from unittest.mock import patch
from infra.dedup import Dedup


def test_first_call_not_duplicate():
    d = Dedup()
    assert d.is_duplicate("msg_001") is False


def test_second_call_is_duplicate():
    d = Dedup()
    d.is_duplicate("msg_001")
    assert d.is_duplicate("msg_001") is True


def test_expired_entry_treated_as_new():
    d = Dedup(ttl=1)
    d.is_duplicate("msg_002")
    with patch("time.monotonic", return_value=time.monotonic() + 2):
        assert d.is_duplicate("msg_002") is False


def test_lru_evicts_oldest_beyond_max_size():
    d = Dedup(max_size=3)
    for i in range(4):
        d.is_duplicate(f"msg_{i:03d}")
    # msg_000 should have been evicted — treated as new
    assert d.is_duplicate("msg_000") is False


def test_different_keys_not_duplicates():
    d = Dedup()
    d.is_duplicate("msg_A")
    assert d.is_duplicate("msg_B") is False
