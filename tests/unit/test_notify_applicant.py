"""car_tools/notify_applicant.py 单元测试：mock feishu/notify + reservation_store。"""
import pytest

from car_tools import notify_applicant as na


@pytest.fixture
def fake_notify(monkeypatch):
    import feishu.notify as n
    calls = []

    def fake_submit(open_id, text):
        calls.append({"open_id": open_id, "text": text})
        from concurrent.futures import Future
        fut = Future()
        fut.set_result(True)
        return fut

    monkeypatch.setattr(n, "submit_text_to_user", fake_submit)
    return calls


@pytest.fixture
def fake_store(monkeypatch):
    """mock bot.reservation_store"""
    from bot import reservation_store
    data = {}

    def fake_get(rid):
        return data.get(rid)

    def fake_save(rid, oid, email, vehicle_no, start_time, end_time="", task_name=""):
        data[rid] = {
            "applicant_open_id": oid, "applicant_email": email,
            "vehicle_no": vehicle_no, "start_time": start_time,
            "end_time": end_time, "task_name": task_name,
        }

    def fake_find_by_vehicle_and_time(vehicle_no, start_time):
        for v in data.values():
            if v["vehicle_no"] == vehicle_no and v["start_time"] == start_time:
                return v
        return None

    monkeypatch.setattr(reservation_store, "get", fake_get)
    monkeypatch.setattr(reservation_store, "save", fake_save)
    monkeypatch.setattr(reservation_store, "find_by_vehicle_and_time", fake_find_by_vehicle_and_time)
    return data


def test_approved_subject_in_text(fake_notify, fake_store):
    fake_store["RID1"] = {
        "applicant_open_id": "ou_alice", "vehicle_no": "PNV332",
        "start_time": "2026-06-16 09:00",
    }
    na.submit_approval_to_applicant(
        {"approved": True, "vehicle_no": "PNV332",
         "start_time": "2026-06-16 09:00", "end_time": "2026-06-16 18:00",
         "task_name": "test", "reviewer": "Bob", "review_comment": "OK"},
        reservation_id="RID1",
    )
    assert len(fake_notify) == 1
    text = fake_notify[0]["text"]
    assert "✅" in text
    assert "Bob" in text


def test_rejected_subject_in_text(fake_notify, fake_store):
    fake_store["RID1"] = {
        "applicant_open_id": "ou_alice", "vehicle_no": "PNV332",
        "start_time": "2026-06-16 09:00",
    }
    na.submit_approval_to_applicant(
        {"approved": False, "vehicle_no": "PNV332",
         "start_time": "2026-06-16 09:00", "end_time": "2026-06-16 18:00",
         "task_name": "test", "reviewer": "Bob", "review_comment": "时间冲突"},
        reservation_id="RID1",
    )
    text = fake_notify[0]["text"]
    assert "❌" in text
    assert "时间冲突" in text


def test_no_record_returns_none(fake_notify):
    """reservation_store 查不到 → 不通知。"""
    result = na.submit_approval_to_applicant(
        {"approved": True, "vehicle_no": "PNV332"},
        reservation_id="RID_UNKNOWN",
    )
    assert result is None
    assert fake_notify == []


def test_no_open_id_returns_none(fake_notify, fake_store):
    fake_store["RID1"] = {"applicant_open_id": "", "vehicle_no": "PNV332"}
    result = na.submit_approval_to_applicant(
        {"approved": True, "vehicle_no": "PNV332"},
        reservation_id="RID1",
    )
    assert result is None


def test_lookup_via_vehicle_and_time(fake_notify, fake_store):
    """没传 reservation_id 时用 (vehicleNo, startTime) 反查。"""
    fake_store["RID_OTHER"] = {
        "applicant_open_id": "ou_alice", "vehicle_no": "PNV332",
        "start_time": "2026-06-16 09:00",
    }
    na.submit_approval_to_applicant(
        {"approved": True, "vehicle_no": "PNV332",
         "start_time": "2026-06-16 09:00", "end_time": "2026-06-16 18:00",
         "task_name": "test", "reviewer": "Bob"},
        vehicle_no="PNV332", start_time="2026-06-16 09:00",
    )
    assert len(fake_notify) == 1


def test_reviewer_in_text(fake_notify, fake_store):
    fake_store["RID1"] = {"applicant_open_id": "ou_alice", "vehicle_no": "PNV332",
                          "start_time": "2026-06-16 09:00"}
    na.submit_approval_to_applicant(
        {"approved": True, "vehicle_no": "PNV332",
         "start_time": "2026-06-16 09:00", "end_time": "2026-06-16 18:00",
         "task_name": "test", "reviewer": "审批人姓名"},
        reservation_id="RID1",
    )
    assert "审批人姓名" in fake_notify[0]["text"]


def test_missing_review_comment_shows_placeholder(fake_notify, fake_store):
    fake_store["RID1"] = {"applicant_open_id": "ou_alice", "vehicle_no": "PNV332",
                          "start_time": "2026-06-16 09:00"}
    na.submit_approval_to_applicant(
        {"approved": True, "vehicle_no": "PNV332",
         "start_time": "2026-06-16 09:00", "end_time": "2026-06-16 18:00",
         "task_name": "test", "reviewer": "Bob"},
        reservation_id="RID1",
    )
    assert "（无）" in fake_notify[0]["text"]
