"""MCP 返回值 → Pydantic schema（strict mode）。

所有 normalize_* 函数遵循：
- 接受 `Any` 输入（LLM 通过 MCP 拿到的 dict / list / 嵌套结构）
- 失败时抛 NormalizeError（fail-fast），由 handler 转成工具结果 `{"error": ...}`
- 返回 Pydantic BaseModel 或 list[BaseModel]，**不**返回 raw dict

字段命名：MCP 用 camelCase（vehicleNo / startTime / emailAddress），Pydantic 用
snake_case（vehicle_no / start_time / email）。本模块做映射；如果 MCP 字段漂移
（新增字段未在 schema 声明）→ Pydantic extra=forbid 抛 ValidationError →
我们捕获并包装为 NormalizeError。

业务状态码（来自上游车辆预约平台）：

| int | 含义 (reservation status) | 含义 (vehicle status) |
|-----|---------------------------|------------------------|
|  0  | 待审批                     |  —                    |
|  1  | 已批准                     | 可用                   |
|  2  | 已拒绝                     | 故障                   |
|  3  | 已取消                     | 维保                   |
|  4  | 已完成                     | 报废                   |
"""
import logging
from typing import Any, List, Optional

from car_tools.schemas import (
    Vehicle, Reservation, ReservationResult, ApprovalResult,
    CancelResult, ReturnResult, Dispatcher,
)

log = logging.getLogger(__name__)


# ── 业务状态码映射（MCP 上游 int ↔ 中文） ─────────────────────────────────

_RESERVATION_STATUS_INT_TO_CN = {
    0: "待审批",
    1: "已批准",
    2: "已拒绝",
    3: "已取消",
    4: "已完成",
}
_VEHICLE_STATUS_INT_TO_CN = {
    1: "可用",
    2: "故障",
    3: "维保",
    4: "报废",
}


def _normalize_reservation_status(raw: Any) -> str:
    """MCP 上游 reservation.status 是 int 0-4；本项目 schema 用中文。"""
    if isinstance(raw, int):
        return _RESERVATION_STATUS_INT_TO_CN.get(raw, str(raw))
    if isinstance(raw, str):
        s = raw.strip()
        # 已经是中文 → 透传
        if s in {"待审批", "已批准", "已拒绝", "已取消", "已完成", "已归还"}:
            return s
        # 数字字符串
        try:
            return _RESERVATION_STATUS_INT_TO_CN.get(int(s), s)
        except (TypeError, ValueError):
            return s
    return str(raw) if raw is not None else ""


def _normalize_vehicle_status(raw: Any) -> str:
    """MCP 上游 vehicleStatus 是 int 1-4；本项目 schema 用「字符串化的 int」。

    ReturnResult.vehicle_status 字段约定是 ``"1"/"2"/"3"/"4"`` 字符串（schema L108 注释），
    因此这里只做 int→str；中文映射由 card_builder 层在渲染时附加。
    """
    if isinstance(raw, int):
        return str(raw)
    if isinstance(raw, str):
        return raw.strip()
    return str(raw) if raw is not None else ""


def vehicle_status_to_cn(raw: Any) -> str:
    """MCP 上游 int 1-4 → 中文「可用/故障/维保/报废」。

    用在 card_builder 渲染阶段（让用户看到「1（可用）」而不是裸 "1"）。
    """
    if isinstance(raw, int):
        cn = _VEHICLE_STATUS_INT_TO_CN.get(raw)
        return f"{raw}（{cn}）" if cn else str(raw)
    if isinstance(raw, str):
        s = raw.strip()
        try:
            n = int(s)
            cn = _VEHICLE_STATUS_INT_TO_CN.get(n)
            return f"{n}（{cn}）" if cn else s
        except (TypeError, ValueError):
            return s
    return str(raw) if raw is not None else ""


def _normalize_approved(raw: Any) -> bool:
    """MCP 上游 approval 用 reviewerStatus int 1/2；本项目 schema 用 bool。"""
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, (int, float)):
        return int(raw) == 1
    if isinstance(raw, str):
        s = raw.strip()
        if s in ("1", "true", "True", "批准", "同意"):
            return True
        if s in ("0", "2", "false", "False", "拒绝", "不同意"):
            return False
    return bool(raw) if raw is not None else False


class NormalizeError(ValueError):
    """MCP 返回不符合 Pydantic schema 时抛出。"""

    def __init__(self, source: str, reason: str, *, raw: Any = None):
        super().__init__(f"{source} 规范化失败: {reason}")
        self.source = source
        self.reason = reason
        self.raw = raw


# ── 字段映射（camelCase MCP → snake_case Pydantic） ─────────────────────────
# 仅列出 schema 已声明的字段；新出现的 MCP 字段会被 Pydantic extra=forbid 拒绝。

def _vehicle_from_raw(d: dict) -> Vehicle:
    """单条 vehicle dict → Vehicle。"""
    return Vehicle(
        vehicle_no=d.get("vehicleNo") or d.get("vehicle_no") or "",
        vin=d.get("vin"),
        license_plate=d.get("licensePlate") or d.get("license_plate"),
        vehicle_type=d.get("vehicleType") or d.get("vehicle_type") or "",
        platform=d.get("platform") or "",
        project=d.get("project"),
        remark=d.get("remark"),
    )


def _reservation_from_raw(d: dict) -> Reservation:
    return Reservation(
        vehicle_no=d.get("vehicleNo") or d.get("vehicle_no") or "",
        vehicle_type=d.get("vehicleType") or d.get("vehicle_type"),
        platform=d.get("platform"),
        license_plate=d.get("licensePlate") or d.get("license_plate"),
        start_time=d.get("startTime") or d.get("start_time") or "",
        end_time=d.get("endTime") or d.get("end_time") or "",
        task_name=d.get("taskName") or d.get("task_name"),
        location=d.get("location"),
        status=_normalize_reservation_status(d.get("status")),
        reviewer=d.get("reviewer") or d.get("reviewerName"),
        reviewer_remark=d.get("reviewerRemark") or d.get("reviewer_remark"),
        return_time=d.get("returnTime") or d.get("return_time"),
        return_location=d.get("returnLocation") or d.get("return_location"),
    )


# ── 公开 API ───────────────────────────────────────────────────────────────

def normalize_vehicles(raw: Any) -> List[Vehicle]:
    """fetch_available_vehicles 返回值规范化。

    接受：
    - list[dict] 直接列表
    - dict {"data": list[dict]} 包装（兼容旧 fmp 风格）
    - dict {"vehicles": list[dict]} 包装
    - dict {"items": list[dict]} 包装（FastMCP 3.x 边界格式，booking_mcp_server 统一包装）
    """
    items: Optional[list] = None
    if isinstance(raw, list):
        items = raw
    elif isinstance(raw, dict):
        items = (raw.get("data") or raw.get("vehicles")
                 or raw.get("items") or raw.get("list") or [])
    if items is None or not isinstance(items, list):
        raise NormalizeError("fetch_available_vehicles",
                              f"期望 list 或 dict[vehicles]，实得 {type(raw).__name__}",
                              raw=raw)
    out: List[Vehicle] = []
    for i, it in enumerate(items):
        if not isinstance(it, dict):
            raise NormalizeError("fetch_available_vehicles",
                                  f"第 {i} 项不是 dict: {type(it).__name__}",
                                  raw=it)
        try:
            out.append(_vehicle_from_raw(it))
        except Exception as e:
            raise NormalizeError("fetch_available_vehicles",
                                  f"第 {i} 项字段错误: {e}", raw=it) from e
    return out


def normalize_records(raw: Any, *, source: str = "fetch_user_reservation") -> List[Reservation]:
    """fetch_user_reservation / fetch_user_approval 返回值规范化。

    同 normalize_vehicles：接受 list、{"data":...}、{"reservations":...}、
    {"items":...}（FastMCP 3.x 边界）几种包装。
    """
    items: Optional[list] = None
    if isinstance(raw, list):
        items = raw
    elif isinstance(raw, dict):
        items = (raw.get("data") or raw.get("reservations")
                 or raw.get("items") or raw.get("list") or [])
    if items is None or not isinstance(items, list):
        raise NormalizeError(source,
                              f"期望 list 或 dict[data]，实得 {type(raw).__name__}",
                              raw=raw)
    out: List[Reservation] = []
    for i, it in enumerate(items):
        if not isinstance(it, dict):
            raise NormalizeError(source, f"第 {i} 项不是 dict", raw=it)
        try:
            out.append(_reservation_from_raw(it))
        except Exception as e:
            raise NormalizeError(source, f"第 {i} 项字段错误: {e}", raw=it) from e
    return out


def normalize_reservation_result(raw: Any, *, applicant: Optional[dict] = None) -> ReservationResult:
    """single_vehicle_reservation 返回值规范化。

    applicant: 来自 CallerIdentity（as_dict 格式），用于补 applicant_* 字段。
    """
    if not isinstance(raw, dict):
        raise NormalizeError("single_vehicle_reservation",
                              f"期望 dict，实得 {type(raw).__name__}", raw=raw)
    d = raw.get("data") if "data" in raw else raw
    if not isinstance(d, dict):
        raise NormalizeError("single_vehicle_reservation",
                              f"data 字段不是 dict: {type(d).__name__}", raw=raw)
    try:
        dispatchers_raw = d.get("dispatchers") or []
        dispatchers = [Dispatcher(
            name=dp.get("name", ""),
            email=dp.get("email", ""),
        ) for dp in dispatchers_raw if isinstance(dp, dict)]
        applicant = applicant or {}
        return ReservationResult(
            success=bool(d.get("success", True)),
            vehicle_no=d.get("vehicleNo") or d.get("vehicle_no") or "",
            license_plate=d.get("licensePlate") or d.get("license_plate"),
            vehicle_type=d.get("vehicleType") or d.get("vehicle_type") or "",
            platform=d.get("platform") or "",
            start_time=d.get("startTime") or d.get("start_time") or "",
            end_time=d.get("endTime") or d.get("end_time") or "",
            task_name=d.get("taskName") or d.get("task_name") or "",
            location=d.get("location") or "",
            remark=d.get("remark"),
            dispatchers=dispatchers,
            reason=d.get("reason"),
            applicant_name=d.get("applicantName") or applicant.get("name"),
            applicant_email=d.get("applicantEmail") or applicant.get("emailAddress"),
            applicant_open_id=d.get("applicantOpenId") or applicant.get("openId"),
            applicant_mobile=d.get("applicantMobile") or applicant.get("mobile"),
        )
    except Exception as e:
        raise NormalizeError("single_vehicle_reservation",
                              f"字段错误: {e}", raw=raw) from e


def normalize_approval_result(raw: Any) -> ApprovalResult:
    if not isinstance(raw, dict):
        raise NormalizeError("approval_vehicle_reservation",
                              f"期望 dict，实得 {type(raw).__name__}", raw=raw)
    d = raw.get("data") if "data" in raw else raw
    if not isinstance(d, dict):
        raise NormalizeError("approval_vehicle_reservation",
                              "data 字段不是 dict", raw=raw)
    try:
        # MCP 契约优先级：reviewerStatus (int 1/2) > approvalResult > approved (bool)
        if "reviewerStatus" in d or "approvalResult" in d:
            reviewer_status = d.get("reviewerStatus", d.get("approvalResult"))
        else:
            reviewer_status = d.get("approved")
        return ApprovalResult(
            approved=_normalize_approved(reviewer_status),
            vehicle_no=d.get("vehicleNo") or d.get("vehicle_no") or "",
            start_time=d.get("startTime") or d.get("start_time") or "",
            end_time=d.get("endTime") or d.get("end_time") or "",
            task_name=d.get("taskName") or d.get("task_name") or "",
            reviewer=d.get("reviewer") or d.get("reviewerName") or "",
            review_comment=d.get("reviewComment") or d.get("review_comment") or d.get("reviewerRemark"),
            applicant_name=d.get("applicantName") or d.get("employeeName"),
            applicant_email=d.get("applicantEmail"),
            applicant_open_id=d.get("applicantOpenId") or d.get("employeeNo"),
        )
    except Exception as e:
        raise NormalizeError("approval_vehicle_reservation",
                              f"字段错误: {e}", raw=raw) from e


def normalize_cancel_result(raw: Any) -> CancelResult:
    if not isinstance(raw, dict):
        raise NormalizeError("cancel_vehicle_reservation",
                              f"期望 dict，实得 {type(raw).__name__}", raw=raw)
    d = raw.get("data") if "data" in raw else raw
    if not isinstance(d, dict):
        raise NormalizeError("cancel_vehicle_reservation",
                              "data 字段不是 dict", raw=raw)
    try:
        return CancelResult(
            vehicle_no=d.get("vehicleNo") or d.get("vehicle_no") or "",
            start_time=d.get("startTime") or d.get("start_time"),
            end_time=d.get("endTime") or d.get("end_time"),
            operator=d.get("operator"),
            cancel_time=d.get("cancelTime") or d.get("cancel_time"),
        )
    except Exception as e:
        raise NormalizeError("cancel_vehicle_reservation",
                              f"字段错误: {e}", raw=raw) from e


def normalize_return_result(raw: Any) -> ReturnResult:
    if not isinstance(raw, dict):
        raise NormalizeError("return_vehicle",
                              f"期望 dict，实得 {type(raw).__name__}", raw=raw)
    d = raw.get("data") if "data" in raw else raw
    if not isinstance(d, dict):
        raise NormalizeError("return_vehicle", "data 字段不是 dict", raw=raw)
    try:
        return ReturnResult(
            vehicle_no=d.get("vehicleNo") or d.get("vehicle_no") or "",
            return_location=d.get("returnLocation") or d.get("return_location") or "",
            key_position=d.get("keyPosition") or d.get("key_position") or "",
            change_module=d.get("changeModule") or d.get("change_module") or "",
            vehicle_status=_normalize_vehicle_status(d.get("vehicleStatus") or d.get("vehicle_status")),
            vehicle_status_description=d.get("vehicleStatusDescription")
                                       or d.get("vehicle_status_description"),
            return_time=d.get("returnTime") or d.get("return_time"),
        )
    except Exception as e:
        raise NormalizeError("return_vehicle", f"字段错误: {e}", raw=raw) from e
