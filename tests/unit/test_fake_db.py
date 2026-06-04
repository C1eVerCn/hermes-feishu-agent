"""Unit tests for mock_api/fake_db.py seed data + data access."""
import pytest
from mock_api import fake_db


@pytest.fixture(autouse=True)
def _reset():
    fake_db.reset()
    yield


def test_five_architectures():
    assert fake_db.list_architectures() == ["1.0架构", "1.5架构", "3.0架构", "L3架构", "L4架构"]


def test_thirty_benches_seeded():
    assert len(fake_db.benches) == 30


def test_users_cover_all_three_roles():
    roles = {u["role"] for u in fake_db.users.values()}
    assert roles == {1, 2, 3}


def test_get_user_by_email_known_and_unknown():
    any_email = next(iter(fake_db.users))
    assert fake_db.get_user(any_email) is not None
    assert fake_db.get_user("nobody@example.com") is None


def test_available_benches_filter_by_architecture_and_status():
    # pick a normal user
    user = next(u for u in fake_db.users.values() if u["role"] == 1)
    arch = fake_db.benches[next(iter(fake_db.benches))]["architecture"]
    result = fake_db.available_benches(user, architecture=arch, need_parking_test=None)
    assert all(isinstance(b, str) for b in result)
    # all returned benches are status=1 and same group as the user
    for bench_no in result:
        b = fake_db.benches[bench_no]
        assert b["status"] == 1
        assert b["architecture"] == arch
        assert b["group_id"] == user["group_id"]


def test_create_reservation_starts_pending():
    user = next(u for u in fake_db.users.values() if u["role"] == 1)
    bench_no = next(bn for bn, b in fake_db.benches.items()
                    if b["status"] == 1 and b["group_id"] == user["group_id"])
    r = fake_db.create_reservation(
        user=user, bench_no=bench_no,
        start_time="2099-01-01 09:00:00", end_time="2099-01-01 10:00:00",
        task_name="t", test_purpose="p", remark="")
    assert r["status"] == 0
    assert r["benchNo"] == bench_no
    assert r["employeeName"] == user["name"]


def test_transition_reservation_rejects_invalid():
    user = next(u for u in fake_db.users.values() if u["role"] == 1)
    bench_no = next(bn for bn, b in fake_db.benches.items()
                    if b["status"] == 1 and b["group_id"] == user["group_id"])
    r = fake_db.create_reservation(
        user=user, bench_no=bench_no,
        start_time="2099-01-01 09:00:00", end_time="2099-01-01 10:00:00",
        task_name="t", test_purpose="p", remark="")
    with pytest.raises(ValueError):
        fake_db.transition(r, 4)  # 0 → 4 invalid
