"""车辆预约业务 Pydantic schemas（strict 模式）。

设计原则（与 LangGraph 参考项目一致）：
- 所有 model 都 `extra="forbid"` → MCP 返回字段漂移时 fail-fast（normalizer 抛 NormalizeError）
- Platform 等枚举用 `Literal` 强约束
- 字段名映射：snake_case（Python 内部）+ camelCase（MCP 边界参数）
- 业务字典字段（vehicle_type、status 等）保留字符串原始值，由 get_common_dictionary 在 LLM 侧解析

⚠️ 不变量：
- emailAddress / openId / mobile **不**出现在任何 schema 字段中（结构性防御）
"""
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

# 芯片平台枚举（MCP 边界传字符串；内部 Pydantic 用 Literal 强约束）
Platform = Literal["Xavier", "ADCU", "Orin", "Thor"]


class _Strict(BaseModel):
    """所有 schema 共享 extra=forbid 严格模式。"""
    model_config = ConfigDict(extra="forbid")


class Vehicle(_Strict):
    """可用车辆列表项。"""
    vehicle_no: str
    vin: Optional[str] = None
    license_plate: Optional[str] = None
    vehicle_type: str            # 来自 get_common_dictionary（DM2/CT1/大F车/CM0/BM2/...）
    platform: Platform
    project: Optional[str] = None
    remark: Optional[str] = None


class Dispatcher(_Strict):
    """调度员（审批人）信息。"""
    name: str
    email: str


class Reservation(_Strict):
    """预约记录（fetch_user_reservation / fetch_user_approval 返回）。"""
    vehicle_no: str
    vehicle_type: Optional[str] = None    # record 查询不返回
    platform: Optional[str] = None
    license_plate: Optional[str] = None
    start_time: str     # yyyy-MM-dd HH:mm
    end_time: str
    task_name: Optional[str] = None
    location: Optional[str] = None
    status: str         # 中文 "待审批/已批准/已驳回/已取消/已归还"
    reviewer: Optional[str] = None
    reviewer_remark: Optional[str] = None
    return_time: Optional[str] = None
    return_location: Optional[str] = None


class ReservationResult(_Strict):
    """single_vehicle_reservation 成功返回值。

    2026-06-18 fix：dmz-fmp-mcp 上游响应字段不可靠：
    - platform 字段可能缺失/空（fmp 端 Vehicle 表反查，但 fmp-mcp 没传 platform args）
    - vehicle_no / vehicle_type / start_time / end_time / task_name / location
      在 fmp response 里可能为空字符串
    改为 Optional/默认值让 Pydantic 容错；success: True 表明预约成功。
    """
    success: bool
    vehicle_no: Optional[str] = None
    license_plate: Optional[str] = None
    vehicle_type: Optional[str] = None
    platform: Optional[str] = None
    start_time: Optional[str] = None
    end_time: Optional[str] = None
    task_name: Optional[str] = None
    location: Optional[str] = None
    remark: Optional[str] = None
    dispatchers: list[Dispatcher] = Field(default_factory=list)
    reason: Optional[str] = None
    applicant_name: Optional[str] = None
    applicant_email: Optional[str] = None
    applicant_open_id: Optional[str] = None
    applicant_mobile: Optional[str] = None


class ApprovalResult(_Strict):
    """approval_vehicle_reservation 返回值。"""
    approved: bool
    vehicle_no: str
    start_time: str
    end_time: str
    task_name: str
    reviewer: str
    review_comment: Optional[str] = None
    applicant_name: Optional[str] = None
    applicant_email: Optional[str] = None
    applicant_open_id: Optional[str] = None


class CancelResult(_Strict):
    """cancel_vehicle_reservation 返回值。"""
    vehicle_no: str
    start_time: Optional[str] = None
    end_time: Optional[str] = None
    operator: Optional[str] = None
    cancel_time: Optional[str] = None


class ReturnResult(_Strict):
    """return_vehicle 返回值。"""
    vehicle_no: str
    return_location: str
    key_position: str
    change_module: str
    vehicle_status: str        # 字符串化的 int（如 "1"/"2"）
    vehicle_status_description: Optional[str] = None
    return_time: Optional[str] = None
