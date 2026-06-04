"""Role-based tool ACL. role 0=unknown,1=普通,2=调度员,3=管理员."""
import json
import pytest
import ocl.permission as perm
import ocl.identity as identity


@pytest.fixture(autouse=True)
def _fresh(tmp_path, monkeypatch):
    f = tmp_path / "identity_map.json"
    f.write_text(json.dumps({
        "ou_user":  {"email": "zhangsan@example.com", "name": "张三", "role": 1},
        "ou_sched": {"email": "scheduler1@example.com", "name": "调度员1", "role": 2},
        "ou_admin": {"email": "admin@example.com", "name": "王五", "role": 3},
    }, ensure_ascii=False))
    monkeypatch.setattr(identity, "_MAP_FILE", str(f))
    identity._invalidate_cache()
    yield


def test_unknown_user_permitted_nothing():
    assert not perm.is_tool_permitted("ou_ghost", "list_architectures")


def test_normal_user_can_query_and_reserve():
    for tool in ("list_architectures", "list_available_benches", "reserve_bench",
                 "cancel_reservation", "return_bench", "list_my_reservations"):
        assert perm.is_tool_permitted("ou_user", tool), tool


def test_normal_user_cannot_approve():
    assert not perm.is_tool_permitted("ou_user", "approve_reservation")
    assert not perm.is_tool_permitted("ou_user", "list_my_approvals")


def test_scheduler_can_approve():
    assert perm.is_tool_permitted("ou_sched", "approve_reservation")
    assert perm.is_tool_permitted("ou_sched", "list_my_approvals")


def test_admin_can_do_everything():
    for tool in ("reserve_bench", "approve_reservation", "list_my_approvals"):
        assert perm.is_tool_permitted("ou_admin", tool)


def test_unknown_tool_denied():
    assert not perm.is_tool_permitted("ou_admin", "drop_database")
