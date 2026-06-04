from fastapi import APIRouter
from mock_api import service
from mock_api.models import AvailableBenchesReq, ReturnReq

router = APIRouter(prefix="/fmp/testBenchReservationForAgent", tags=["benches"])


def _envelope(code, message, data):
    return {"code": code, "message": message, "data": data}


@router.get("/architectures")
def architectures():
    code, msg, data = service.architectures()
    return _envelope(code, msg, data)


@router.post("/availableTestBenches")
def available_benches(body: AvailableBenchesReq):
    code, msg, data = service.available(body.emailAddress, body.architecture, body.needParkingTest)
    return _envelope(code, msg, data)


@router.post("/returnTestBench")
def return_bench(body: ReturnReq):
    code, msg, data = service.return_bench(body.emailAddress, body.benchNo, body.returnLocation)
    return _envelope(code, msg, data)
