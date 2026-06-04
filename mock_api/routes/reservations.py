from fastapi import APIRouter
from mock_api import service
from mock_api.models import ReserveReq, CancelReq, ApproveReq, MyReservationsReq, MyApprovalsReq

router = APIRouter(prefix="/fmp/testBenchReservationForAgent", tags=["reservations"])


def _envelope(code, message, data):
    return {"code": code, "message": message, "data": data}


@router.post("/reserveTestBench")
def reserve(body: ReserveReq):
    code, msg, data = service.reserve(body.emailAddress, body.benchNo, body.startTime,
                                      body.endTime, body.taskName, body.testPurpose, body.remark)
    return _envelope(code, msg, data)


@router.post("/cancel")
def cancel(body: CancelReq):
    code, msg, data = service.cancel(body.emailAddress, body.benchNo, body.startTime, body.endTime)
    return _envelope(code, msg, data)


@router.post("/approve")
def approve(body: ApproveReq):
    code, msg, data = service.approve(body.emailAddress, body.benchNo, body.approvalResult,
                                      body.approvalRemark, body.startTime, body.endTime)
    return _envelope(code, msg, data)


@router.post("/myReservations")
def my_reservations(body: MyReservationsReq):
    code, msg, data = service.my_reservations(body.emailAddress, body.benchNo, body.startTime,
                                              body.endTime, body.taskName, body.status)
    return _envelope(code, msg, data)


@router.post("/myApprovals")
def my_approvals(body: MyApprovalsReq):
    code, msg, data = service.my_approvals(body.emailAddress, body.status)
    return _envelope(code, msg, data)
