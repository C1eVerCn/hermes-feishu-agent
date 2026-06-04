import pytest
from pydantic import ValidationError
from mock_api.models import (
    AvailableBenchesReq, ReserveReq, CancelReq, ApproveReq,
    MyReservationsReq, MyApprovalsReq, ReturnReq,
)


def test_reserve_requires_all_mandatory_fields():
    with pytest.raises(ValidationError):
        ReserveReq(emailAddress="a@b.com", benchNo="TJ001")  # missing times/task/purpose


def test_reserve_accepts_full_payload():
    r = ReserveReq(emailAddress="a@b.com", benchNo="TJ001",
                   startTime="2099-01-01 09:00:00", endTime="2099-01-01 10:00:00",
                   taskName="t", testPurpose="p")
    assert r.remark == ""


def test_approve_result_must_be_1_or_2():
    with pytest.raises(ValidationError):
        ApproveReq(emailAddress="a@b.com", benchNo="TJ001", approvalResult=3)


def test_available_need_parking_test_must_be_0_or_1():
    with pytest.raises(ValidationError):
        AvailableBenchesReq(emailAddress="a@b.com", needParkingTest=2)
