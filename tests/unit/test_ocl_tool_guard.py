"""
Unit tests for ocl/tool_guard.py.
Tests thread-local user/email context and guarded handler permission enforcement
against the role-based permission model.
"""
import json
import threading
import pytest

import ocl.identity as identity
import ocl.tool_guard as guard


@pytest.fixture(autouse=True)
def _fresh(tmp_path, monkeypatch):
    f = tmp_path / "identity_map.json"
    f.write_text(json.dumps({
        # role 1 → can use reserve_bench / list_my_reservations, not approve
        "ou_alice": {"email": "zhangsan@example.com", "name": "张三", "role": 1},
    }, ensure_ascii=False))
    monkeypatch.setattr(identity, "_MAP_FILE", str(f))
    identity._invalidate_cache()
    guard.set_current_user("")   # reset between tests
    guard.set_current_email("")
    yield


def test_set_and_get_current_user():
    guard.set_current_user("ou_alice")
    assert guard.get_current_user() == "ou_alice"
    guard.set_current_user("")
    assert guard.get_current_user() == ""


def test_email_context_set_and_get():
    guard.set_current_email("a@b.com")
    assert guard.get_current_email() == "a@b.com"
    guard.set_current_email("")
    assert guard.get_current_email() == ""


def test_email_defaults_empty_without_set():
    guard._current.__dict__.pop("email", None)
    assert guard.get_current_email() == ""


def test_guarded_handler_passes_when_permitted():
    guard.set_current_user("ou_alice")
    calls = []
    inner = lambda args: (calls.append(args), "ok")[1]
    wrapped = guard.guarded("list_my_reservations", inner)  # role 1 → permitted
    result = wrapped({"benchNo": "TJ001"})
    assert result == "ok"
    assert len(calls) == 1


def test_guarded_handler_blocks_when_no_permission():
    guard.set_current_user("ou_alice")
    inner = lambda args: "should not run"
    wrapped = guard.guarded("approve_reservation", inner)  # role 1 cannot approve
    result = wrapped({})
    data = json.loads(result)
    assert "error" in data
    assert result != "should not run"


def test_guarded_tolerates_extra_kwargs():
    """registry.dispatch injects task_id/user_task — guarded must accept them."""
    guard.set_current_user("ou_alice")
    inner = lambda args: "ok"
    wrapped = guard.guarded("list_my_reservations", inner)
    assert wrapped({"benchNo": "TJ001"}, task_id="abc", user_task="x") == "ok"


def test_thread_isolation():
    results = {}

    def worker(user_id, sleep_before=False):
        guard.set_current_user(user_id)
        if sleep_before:
            import time; time.sleep(0.01)
        results[user_id] = guard.get_current_user()

    t1 = threading.Thread(target=worker, args=("ou_user1", False))
    t2 = threading.Thread(target=worker, args=("ou_user2", True))
    t1.start(); t2.start()
    t1.join(); t2.join()

    assert results["ou_user1"] == "ou_user1"
    assert results["ou_user2"] == "ou_user2"


def test_guarded_passes_when_no_user_set():
    """If current_user is empty string, guard is skipped (internal/system calls)."""
    guard.set_current_user("")
    called = []
    inner = lambda args: (called.append(True), "ok")[1]
    wrapped = guard.guarded("approve_reservation", inner)
    result = wrapped({})
    assert result == "ok"
    assert called
