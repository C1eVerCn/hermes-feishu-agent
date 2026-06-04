"""Unit tests for ocl/session_map.py."""
import threading
import pytest
import ocl.session_map as sm


@pytest.fixture(autouse=True)
def _clear_map(monkeypatch):
    """Each test starts with a fresh empty map."""
    monkeypatch.setattr(sm, "_map", {})
    monkeypatch.setattr(sm, "_lock", threading.Lock())


def test_register_and_lookup():
    sm.register("sid_1", "ou_alice")
    assert sm.lookup("sid_1") == "ou_alice"


def test_lookup_unknown_returns_empty():
    assert sm.lookup("nonexistent") == ""


def test_evict_removes_mapping():
    sm.register("sid_2", "ou_bob")
    assert sm.lookup("sid_2") == "ou_bob"
    sm.evict("sid_2")
    assert sm.lookup("sid_2") == ""


def test_thread_safety_concurrent():
    errors = []

    def worker(prefix, n):
        for i in range(n):
            sid = f"{prefix}_sid_{i}"
            uid = f"ou_{i}"
            sm.register(sid, uid)
            found = sm.lookup(sid)
            if found != uid:
                errors.append(f"mismatch: {sid} expected {uid} got {found}")
            sm.evict(sid)

    threads = [threading.Thread(target=worker, args=(f"t{t}", 20)) for t in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors


def test_size_reflects_map():
    sm.register("s_a", "u_a")
    sm.register("s_b", "u_b")
    assert sm.size() == 2
    sm.evict("s_a")
    assert sm.size() == 1
    sm.evict("s_b")
    assert sm.size() == 0
