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
# 2026-06-30：return_vehicle LLM 友好层 —— 接受中文/英文/数字字符串，统一转 int（1-4）。
# 原 bot/return_fsm._STATUS_CODE 字典的等价映射搬到这里，给 handlers.return_vehicle 调。
_VEHICLE_STATUS_NAME_TO_INT = {
    "可用": 1, "正常": 1, "good": 1, "available": 1,
    "故障": 2, "坏": 2, "broken": 2, "fault": 2,
    "维保": 3, "保养": 3, "maintenance": 3,
    "报废": 4, "scrapped": 4, "scrap": 4,
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


def coerce_vehicle_status_to_int(raw: Any) -> int | None:
    """2026-06-30：return_vehicle 入参层把 LLM 给的 vehicleStatus 归一为 int 1-4。

    接受：int / 数字字符串 / 中文名（可用/故障/维保/报废）/ 英文（good/available/...）。
    无法识别 → None（让 handler 直接透传，避免静默改值）。
    """
    if raw is None or raw == "":
        return None
    if isinstance(raw, bool):
        return None  # 防御 bool → int
    if isinstance(raw, int):
        return raw if raw in (1, 2, 3, 4) else None
    if isinstance(raw, str):
        s = raw.strip()
        if s in _VEHICLE_STATUS_NAME_TO_INT:
            return _VEHICLE_STATUS_NAME_TO_INT[s]
        try:
            n = int(s)
            return n if n in (1, 2, 3, 4) else None
        except (TypeError, ValueError):
            return None
    return None


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


# ── 字段映射辅助：三级 fallback helper ─────────────────────────────────────
# 2026-06-18：dmz-fmp-mcp 返回中文字段（车型细分/车型/车辆编号/芯片/车牌号/VIN码/
# 项目/审批人/归还时间/归还地点 等），同时仍可能返 camelCase / snake_case 英文。
# 用 _get_first 统一按优先级取第一个非 None 值；替代到处复制的 d.get('X') or d.get('Y') 链。

def _get_first(d: dict, *keys: str, default: Any = "") -> Any:
    """按 keys 顺序取 d 中第一个非 None 值；都缺则返回 default（默认 ''）。"""
    for k in keys:
        v = d.get(k)
        if v is not None:
            return v
    return default


# ── 字段映射（camelCase MCP → snake_case Pydantic） ─────────────────────────
# 仅列出 schema 已声明的字段；新出现的 MCP 字段会被 Pydantic extra=forbid 拒绝。

def _vehicle_from_raw(d: dict) -> Vehicle:
    """单条 vehicle dict → Vehicle。

    2026-06-18 fix：兼容 dmz-fmp-mcp 实际返回的**中文字段**（车辆编号/车型/车型细分/
    项目/芯片/车牌号/VIN码），同时保留对 camelCase / snake_case 英文键名的兼容。
    """
    return Vehicle(
        vehicle_no=_get_first(d, "车辆编号", "vehicleNo", "vehicle_no"),
        vin=_get_first(d, "VIN码", "vin") or None,
        license_plate=_get_first(d, "车牌号", "licensePlate", "license_plate") or None,
        vehicle_type=_get_first(d, "车型", "vehicleType", "vehicle_type"),
        platform=_get_first(d, "芯片", "platform"),
        project=_get_first(d, "项目", "project") or None,
        remark=d.get("remark"),
    )


def _vehicle_to_card_dict(d: dict) -> dict:
    """raw vehicle dict → 卡片用 snake_case dict（不构造 Pydantic Vehicle）。

    2026-06-18 新增：供 bot/car_booking_fsm.py::_normalize_vehicle_keys 复用，
    避免 fsm.py 与 normalizers.py 维护两份相同的中英字段映射表。返回 dict 字段：
    vehicle_no / vin / license_plate / vehicle_type / vehicle_type_detail / platform / project。
    缺值时 '' 兜底（与 _vehicle_from_raw 一致）。
    """
    return {
        "vehicle_no":          _get_first(d, "车辆编号", "vehicleNo", "vehicle_no"),
        "vin":                 _get_first(d, "VIN码", "vin"),
        "license_plate":       _get_first(d, "车牌号", "licensePlate", "license_plate"),
        "vehicle_type":        _get_first(d, "车型", "vehicleType", "vehicle_type"),
        "vehicle_type_detail": _get_first(d, "车型细分", "vehicleTypeDetail", "vehicle_type_detail"),
        "platform":            _get_first(d, "芯片", "platform"),
        "project":             _get_first(d, "项目", "project"),
    }


def _reservation_from_raw(d: dict) -> Reservation:
    # 2026-06-18 fix：兼容 dmz-fmp-mcp 中文字段（任务名称/地点/状态/审批人/审批意见 等）
    return Reservation(
        vehicle_no=_get_first(d, "车辆编号", "vehicleNo", "vehicle_no"),
        vehicle_type=_get_first(d, "车型", "vehicleType", "vehicle_type"),
        platform=_get_first(d, "芯片", "platform"),
        license_plate=_get_first(d, "车牌号", "licensePlate", "license_plate"),
        start_time=_get_first(d, "开始时间", "startTime", "start_time"),
        end_time=_get_first(d, "结束时间", "endTime", "end_time"),
        task_name=_get_first(d, "任务名称", "taskName", "task_name"),
        location=_get_first(d, "地点", "location"),
        status=_normalize_reservation_status(_get_first(d, "状态", "status", default=None)),
        reviewer=_get_first(d, "审批人", "reviewer", "reviewerName", default=None),
        reviewer_remark=_get_first(d, "审批意见", "reviewerRemark", "reviewer_remark", default=None),
        return_time=_get_first(d, "归还时间", "returnTime", "return_time", default=None),
        return_location=_get_first(d, "归还地点", "returnLocation", "return_location", default=None),
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
