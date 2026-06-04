"""Request bodies for the test-bench reservation API.
emailAddress is included here because the API contract requires it; the LLM-facing
tools (mock_tools) deliberately omit it and inject it server-side.
"""
from typing import Optional
from pydantic import BaseModel, Field


class AvailableBenchesReq(BaseModel):
    emailAddress: str
    architecture: Optional[str] = None
    needParkingTest: Optional[int] = Field(default=None, ge=0, le=1)


class ReserveReq(BaseModel):
    emailAddress: str
    benchNo: str
    startTime: str
    endTime: str
    taskName: str
    testPurpose: str
    remark: str = ""


class CancelReq(BaseModel):
    emailAddress: str
    benchNo: str
    startTime: Optional[str] = None
    endTime: Optional[str] = None


class ApproveReq(BaseModel):
    emailAddress: str
    benchNo: str
    approvalResult: int = Field(ge=1, le=2)
    approvalRemark: str = ""
    startTime: Optional[str] = None
    endTime: Optional[str] = None


class MyReservationsReq(BaseModel):
    emailAddress: str
    benchNo: Optional[str] = None
    startTime: Optional[str] = None
    endTime: Optional[str] = None
    taskName: Optional[str] = None
    status: Optional[int] = Field(default=None, ge=0, le=4)


class MyApprovalsReq(BaseModel):
    emailAddress: str
    status: Optional[int] = Field(default=None, ge=0, le=4)


class ReturnReq(BaseModel):
    emailAddress: str
    benchNo: str
    returnLocation: str
