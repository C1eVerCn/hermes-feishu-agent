"""car_tools/notify_dispatchers.py 单元测试：mock feishu/notify。"""
import pytest

from car_tools import notify_dispatchers as nd


@pytest.fixture
def fake_notify(monkeypatch):
    """mock feishu.notify.submit_dispatchers_by_email"""
    import feishu.notify as n
    calls = []

    def fake_submit(emails, subject, body):
        calls.append({"emails": list(emails), "subject": subject, "body": body})
        from concurrent.futures import Future
        fut = Future()
        fut.set_result(len(emails))
        return fut

    monkeypatch.setattr(n, "submit_dispatchers_by_email", fake_submit)
    return calls


def test_no_dispatchers_returns_zero(fake_notify):
    """dispatchers 列表为空 → 不调 feishu.notify。"""
    from concurrent.futures import Future
    fut = nd.submit_reservation_dispatchers({"dispatchers": []})
    assert fut.result() == 0
    assert fake_notify == []


def test_no_emails_returns_zero(fake_notify):
    """dispatchers 列表无 email 字段 → 不调 feishu.notify。"""
    fut = nd.submit_reservation_dispatchers({"dispatchers": [
        {"name": "Alice"}, {"name": "Bob"},
    ]})
    assert fut.result() == 0
    assert fake_notify == []


def test_submit_calls_notify_with_emails(fake_notify):
    fut = nd.submit_reservation_dispatchers({"dispatchers": [
        {"name": "Alice", "email": "alice@x.com"},
        {"name": "Bob", "email": "bob@x.com"},
    ]})
    assert fut.result() == 2
    assert len(fake_notify) == 1
    call = fake_notify[0]
    assert set(call["emails"]) == {"alice@x.com", "bob@x.com"}


def test_subject_body_contains_vehicle_info(fake_notify):
    nd.submit_reservation_dispatchers({
        "dispatchers": [{"name": "Alice", "email": "alice@x.com"}],
        "vehicle_no": "PNV332",
        "start_time": "2026-06-16 09:00",
        "end_time": "2026-06-16 18:00",
        "task_name": "高速测试",
        "location": "测试场A区",
        "applicant_name": "张三",
    })
    body = fake_notify[0]["body"]
    assert "PNV332" in body
    assert "2026-06-16 09:00" in body
    assert "高速测试" in body
    assert "测试场A区" in body
    assert "张三" in body


def test_subject_is_chinese(fake_notify):
    nd.submit_reservation_dispatchers({"dispatchers": [
        {"name": "Alice", "email": "a@x.com"},
    ]})
    assert fake_notify[0]["subject"] == "📋 新预约待审批"


def test_falls_back_to_email_when_no_name(fake_notify):
    """applicant_name 缺失时回退到 applicant_email。"""
    nd.submit_reservation_dispatchers({
        "dispatchers": [{"name": "Alice", "email": "a@x.com"}],
        "vehicle_no": "PNV332", "start_time": "x", "end_time": "y",
        "task_name": "t", "location": "l",
        "applicant_email": "zs@x.com",
    })
    assert "zs@x.com" in fake_notify[0]["body"]
