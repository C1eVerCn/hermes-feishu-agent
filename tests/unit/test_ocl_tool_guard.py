"""ocl/tool_guard.py单元测试：thread-local上下文 + guarded() Layer2兜底权限校验。"""
import json
import threading
import pytest

import ocl.identity as identity
import ocl.tool_guard as guard


@pytest.fixture(autouse=True)
def _fresh(tmp_path, monkeypatch):
 f = tmp_path / "identity_map.json"
 f.write_text(json.dumps({"ou_alice":1}, ensure_ascii=False))
 monkeypatch.setattr(identity, "_MAP_FILE", str(f))
 identity._invalidate_role_overrides()
 monkeypatch.setattr(identity, "_resolve_open_id", lambda oid: ("", ""))
 guard.set_current_user("")
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
    guard.set_current_email("")
    assert guard.get_current_email() == ""


def test_context_propagates_across_thread_pool_executor():
    """Regression: bot/handler.py runs agent.chat() in a worker thread spawned
    by ThreadPoolExecutor. contextvars do NOT auto-propagate across threads
    (only asyncio tasks do). The handler must copy_context() before submit().
    This test mirrors the handler's pattern to lock the contract."""
    import concurrent.futures
    import contextvars

    guard.set_current_user("ou_alice")
    guard.set_current_email("alice@immotors.com")
    ctx = contextvars.copy_context()  # capture consumer thread context

    def worker():
        # Worker runs in captured context — sees the same values.
        return (
            guard.get_current_user(),
            guard.get_current_email(),
        )

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
        uid, email = ex.submit(ctx.run, worker).result(timeout=2)

    assert uid == "ou_alice", f"context not propagated: got {uid!r}"
    assert email == "alice@immotors.com", f"context not propagated: got {email!r}"


def test_guarded_handler_passes_when_permitted():
# role=1 用户调 level=1工具 → 通过
 guard.set_current_user("ou_alice")
 calls = []
 inner = lambda args: (calls.append(args), "ok")[1]
 wrapped = guard.guarded("list_my_reservations", inner)
 result = wrapped({"benchNo": "TJ001"})
 assert result == "ok"
 assert len(calls) ==1


def test_guarded_handler_blocks_unknown_tool():
# 工具名不在 TOOL_MIN_ROLE → 直接拦截
 guard.set_current_user("ou_alice")
 inner = lambda args: "should not run"
 wrapped = guard.guarded("drop_database", inner)
 result = wrapped({})
 data = json.loads(result)
 assert "error" in data
 assert result != "should not run"


def test_guarded_blocks_role1_from_level2_tool():
# role=1 用户调 approve_reservation（level=2）→ Layer2拦截
 guard.set_current_user("ou_alice")
 inner = lambda args: "should not run"
 wrapped = guard.guarded("approve_reservation", inner)
 result = wrapped({})
 data = json.loads(result)
 assert "error" in data
 assert "权限不足" in data["error"]


def test_guarded_blocks_role1_from_level3_vlm_tool():
# role=1 用户调 sync_execute（level=3）→ Layer2拦截
 guard.set_current_user("ou_alice")
 inner = lambda args: "should not run"
 wrapped = guard.guarded("sync_execute", inner)
 result = wrapped({})
 data = json.loads(result)
 assert "权限不足" in data["error"]


def test_guarded_tolerates_extra_kwargs():
# registry.dispatch注入 task_id/user_task — guarded 必须容忍。
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
# 无 open_id 时跳过门控（hermes内部/系统调用）。
 guard.set_current_user("")
 called = []
 inner = lambda args: (called.append(True), "ok")[1]
 wrapped = guard.guarded("approve_reservation", inner)
 result = wrapped({})
 assert result == "ok"
 assert called

