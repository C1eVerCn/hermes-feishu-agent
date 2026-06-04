import pytest
from mock_api import fake_db, service


@pytest.fixture(autouse=True)
def _reset():
    fake_db.reset()
    yield


def _normal():
    return "zhangsan@example.com"   # role=1, group G1


def _scheduler():
    return "scheduler1@example.com"  # role=2, group G1


def test_reserve_unknown_user():
    code, msg, _ = service.reserve("ghost@example.com", "TJ001",
                                   "2099-01-01 09:00:00", "2099-01-01 10:00:00", "t", "p", "")
    assert code != 200
    assert "平台用户" in msg


def test_reserve_start_after_end():
    code, msg, _ = service.reserve(_normal(), "TJ001",
                                   "2099-01-01 11:00:00", "2099-01-01 10:00:00", "t", "p", "")
    assert code != 200
    assert "晚于结束时间" in msg


def test_reserve_start_in_past():
    code, msg, _ = service.reserve(_normal(), "TJ001",
                                   "2000-01-01 09:00:00", "2000-01-01 10:00:00", "t", "p", "")
    assert code != 200
    assert "早于当前时间" in msg


def test_reserve_bench_not_found():
    code, msg, _ = service.reserve(_normal(), "TJ999",
                                   "2099-01-01 09:00:00", "2099-01-01 10:00:00", "t", "p", "")
    assert code != 200
    assert "台架不存在" in msg


def test_reserve_other_group_denied_for_normal_user():
    # zhangsan is G1; find a status=1 bench in another group
    bench = next(bn for bn, b in fake_db.benches.items()
                 if b["status"] == 1 and b["group_id"] not in (None, "G1"))
    code, msg, _ = service.reserve(_normal(), bench,
                                   "2099-01-01 09:00:00", "2099-01-01 10:00:00", "t", "p", "")
    assert code != 200
    assert "权限" in msg


def test_reserve_happy_path():
    bench = next(bn for bn, b in fake_db.benches.items()
                 if b["status"] == 1 and b["group_id"] == "G1")
    code, msg, _ = service.reserve(_normal(), bench,
                                   "2099-01-01 09:00:00", "2099-01-01 10:00:00", "t", "p", "")
    assert code == 200
    assert "预约成功" in msg
    # 调度员信息出现在 message
    assert "调度员1" in msg


def test_approve_requires_scheduler_role():
    code, msg, _ = service.approve(_normal(), "TJ001", 1, "", None, None)
    assert code != 200
    assert "权限" in msg


def test_approve_pending_by_scheduler():
    # seeded pending reservation on TJ001 (group G2)... but scheduler1 owns G1.
    # TJ001 group is G2 per seed (n=1 → G2); approve by scheduler2 (G2 owner).
    code, msg, _ = service.approve("scheduler2@example.com", "TJ001", 1, "同意", None, None)
    assert code == 200
    assert "审批成功" in msg


def test_cancel_only_pending():
    # approve TJ001 first → status 1 → cancel should fail
    service.approve("scheduler2@example.com", "TJ001", 1, "", None, None)
    code, msg, _ = service.cancel(_normal(), "TJ001", None, None)
    assert code != 200


def test_return_only_approved():
    # TJ006 is seeded as approved(1) for zhangsan
    code, msg, _ = service.return_bench(_normal(), "TJ006", "A区3号位")
    assert code == 200
    assert "归还" in msg
    # now it's completed(4) → second return fails
    code2, _, _ = service.return_bench(_normal(), "TJ006", "A区3号位")
    assert code2 != 200


def test_my_reservations_filters():
    code, msg, data = service.my_reservations(_normal(), None, None, None, None, None)
    assert code == 200
    assert all(r["employeeId"] == _normal() for r in data)


def test_my_approvals_requires_scheduler():
    code, msg, _ = service.my_approvals(_normal(), None)
    assert code != 200
