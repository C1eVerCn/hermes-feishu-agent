"""车辆预约 MCP server（FastMCP + stdio）。

对齐参考项目 reservation_agent-test-agent_multigraph/src/utils/booking_mcp_server.py
的架构模式：用 FastMCP 把 9 个 booking 工具暴露成 stdio MCP server，
hermes-agent 通过 ~/.hermes/config.yaml::mcp_servers::car_booking 把它 spawn 为
子进程、自动发现 + 注册到 hermes 的 tool registry。

业务身份注入（CLAUDE.md 不变量）：
- LLM-facing 工具 schema **不**含 emailAddress / openId / mobile
- 这 9 个 @mcp.tool() 的实现是从 CallerIdentity 读 email/openid 并注入到上游 body
- 上游 fmp 端按 emailAddress 鉴权 / 查询

上游后端：CAR_API_BASE_URL + CAR_API_PREFIX（settings.py）—— 仿 bench_tools 模式，
每个 tool 拼 BASE + PREFIX + PATH 后 POST。9 个端点路径与 dmz-fmp-mcp-dev-260409
的 @Value URL 占位符一一对应。

为什么不用 Spring AI MCP server？
- Spring AI 1.0.0-SNAPSHOT 用旧 SSE+message 协议，hermes 不直接兼容
- 上游 9 个 @Value URL 才是真业务端点；本 server 直接复用同一组 path
- 后续若 Spring AI server 走 StreamableHTTP，可让本 server 改成纯代理
"""
from __future__ import annotations

import json
import logging
import os
import sys
from typing import Any, Optional

import httpx

# FastMCP 在包未装时给清晰报错
try:
    from fastmcp import FastMCP
except ImportError as e:  # pragma: no cover
    sys.stderr.write(
        "ERROR: fastmcp not installed.  pip install fastmcp>=2.0\n"
    )
    raise

# ── 路径 & 配置 ────────────────────────────────────────────────────────────

CAR_API_BASE_URL: str = os.getenv("CAR_API_BASE_URL", "http://localhost:9015")
CAR_API_PREFIX: str = os.getenv("CAR_API_PREFIX", "/fmp/vehicleReservation")
TIMEOUT: float = float(os.getenv("CAR_API_TIMEOUT", "10"))

PATH_GET_USER_CONTEXT          = "/getUserContext"
PATH_GET_COMMON_DICTIONARY     = "/getCommonDictionary"
PATH_FETCH_AVAILABLE_VEHICLES  = "/fetchAvailableVehicles"
PATH_SINGLE_VEHICLE_RESERVATION = "/singleVehicleReservation"
PATH_CANCEL_VEHICLE_RESERVATION = "/cancelVehicleReservation"
PATH_APPROVAL_VEHICLE_RESERVATION = "/approvalVehicleReservation"
PATH_RETURN_VEHICLE            = "/returnVehicle"
PATH_FETCH_USER_RESERVATION    = "/fetchUserReservation"
PATH_FETCH_USER_APPROVAL       = "/fetchUserApproval"

log = logging.getLogger("car_booking_mcp")
logging.basicConfig(level=os.getenv("CAR_MCP_LOG_LEVEL", "WARNING"), stream=sys.stderr)


# ── HTTP 辅助 ──────────────────────────────────────────────────────────────

def _post(path: str, body: dict) -> dict:
    """POST 到上游 fmp 端点；上游返回 JSON dict / list → 统一包装为 dict。

    FastMCP 3.x 的 structured_content 要求 dict；上游 list 返回会被它强行包成
    list-of-dict 失败。这里在 MCP 边界处把 list 包成 ``{"items": [...]}``，
    handlers.py / normalizers.py 已经按这个结构解析。
    """
    url = f"{CAR_API_BASE_URL}{CAR_API_PREFIX}{path}"
    try:
        r = httpx.post(url, json=body, timeout=TIMEOUT)
    except httpx.HTTPError as e:
        log.warning("car_api_unreachable path=%s err=%s", path, e)
        return {"error": f"car backend unavailable: {type(e).__name__}: {e}"}
    if r.status_code >= 400:
        return {"error": f"HTTP {r.status_code}: {r.text[:300]}"}
    try:
        data = r.json()
    except (json.JSONDecodeError, ValueError):
        return {"raw": r.text}
    if isinstance(data, list):
        return {"items": data}
    return data


# ── FastMCP server 实例 ───────────────────────────────────────────────────

mcp = FastMCP("car_booking")


# ── 1. get_user_context ────────────────────────────────────────────────────

@mcp.tool()
def get_user_context(emailAddress: str) -> dict:
    """通过用户邮箱查询穹驰约车平台的用户信息（角色、部门、手机号等）。

    所有约车操作的前置步骤。
    """
    return _post(PATH_GET_USER_CONTEXT, {"emailAddress": emailAddress})


# ── 2. get_common_dictionary ───────────────────────────────────────────────

@mcp.tool()
def get_common_dictionary(typeCode: str) -> dict:
    """获取平台枚举字典数据，用于将用户描述映射到合法参数值。

    typeCode 可选值：
    - VEHICLE_TYPE       车型大类
    - VEHICLE_TYPE_DETAIL 车型细分
    - VEHICLE_PROJECT    项目
    - VEHICLE_CHIP       芯片/平台
    """
    return _post(PATH_GET_COMMON_DICTIONARY, {"typeCode": typeCode})


# ── 3. fetch_available_vehicles ────────────────────────────────────────────

@mcp.tool()
def fetch_available_vehicles(
    emailAddress: str = "",
    vehicleType: str | None = None,
    vehicleTypeDetail: str | None = None,
    project: str | None = None,
    platform: str | None = None,
    startTime: str | None = None,
    endTime: str | None = None,
) -> dict:
    """查询当前用户可预约的车辆列表。

    emailAddress: 预约用户邮箱（必填，由服务端 CallerIdentity 注入）
    """
    if not emailAddress:
        return {"code": 400, "message": "emailAddress 为必填参数", "data": None}
    body: dict[str, Any] = {"emailAddress": emailAddress}
    for k, v in (("vehicleType", vehicleType), ("vehicleTypeDetail", vehicleTypeDetail),
                 ("project", project), ("platform", platform),
                 ("startTime", startTime), ("endTime", endTime)):
        if v is not None and v != "":
            body[k] = v
    return _post(PATH_FETCH_AVAILABLE_VEHICLES, body)


# ── 4. single_vehicle_reservation ──────────────────────────────────────────

@mcp.tool()
def single_vehicle_reservation(
    emailAddress: str = "",
    startTime: str = "",
    endTime: str = "",
    taskName: str = "",
    location: str = "",
    vehicleNo: str = "",
    vin: str = "",
    remark: str | None = None,
    vehicleType: str | None = None,
    platform: str | None = None,
) -> dict:
    """提交单辆车预约申请。

    emailAddress: 申请人邮箱（必填）
    startTime / endTime: 预约时间（yyyy-MM-dd HH:mm）
    taskName / location: 任务名称 / 任务地点
    vehicleNo / vin: 至少提供一个
    """
    if not emailAddress:
        return {"code": 400, "message": "emailAddress 为必填参数", "data": None}
    if not vehicleNo and not vin:
        return {"code": 400, "message": "vehicleNo 和 vin 至少提供一个", "data": None}
    body: dict[str, Any] = {
        "emailAddress": emailAddress,
        "startTime": startTime,
        "endTime": endTime,
        "taskName": taskName,
        "location": location,
        "vehicleNo": vehicleNo,
        "vin": vin,
    }
    for k, v in (("remark", remark), ("vehicleType", vehicleType), ("platform", platform)):
        if v is not None and v != "":
            body[k] = v
    return _post(PATH_SINGLE_VEHICLE_RESERVATION, body)


# ── 5. cancel_vehicle_reservation ──────────────────────────────────────────

@mcp.tool()
def cancel_vehicle_reservation(
    emailAddress: str = "",
    vehicleNo: str = "",
    vin: str = "",
    reservationId: str | None = None,
) -> dict:
    """取消状态为"待审批"的预约。"""
    if not emailAddress:
        return {"code": 400, "message": "emailAddress 为必填参数", "data": None}
    if not vehicleNo and not vin:
        return {"code": 400, "message": "vehicleNo 和 vin 至少提供一个", "data": None}
    body: dict[str, Any] = {
        "emailAddress": emailAddress,
        "vehicleNo": vehicleNo,
        "vin": vin,
    }
    if reservationId:
        body["reservationId"] = reservationId
    return _post(PATH_CANCEL_VEHICLE_RESERVATION, body)


# ── 6. approval_vehicle_reservation ────────────────────────────────────────

@mcp.tool()
def approval_vehicle_reservation(
    emailAddress: str = "",
    reviewerStatus: int = 0,
    vehicleNo: str = "",
    vin: str = "",
    reviewerRemark: str | None = None,
    reservationId: str | None = None,
    approved: bool | None = None,
    reviewComment: str | None = None,
) -> dict:
    """调度员或管理员对待审批预约进行批准/拒绝操作。

    reviewerStatus: 1=批准 / 2=拒绝
    """
    if not emailAddress:
        return {"code": 400, "message": "emailAddress 为必填参数", "data": None}
    effective_vehicle = vehicleNo or (reservationId or "")
    effective_status = reviewerStatus
    if effective_status == 0 and approved is not None:
        effective_status = 1 if approved else 2
    if not effective_vehicle and not vin:
        return {"code": 400, "message": "vehicleNo 和 vin 至少提供一个", "data": None}
    if effective_status not in (1, 2):
        return {"code": 400, "message": "reviewerStatus 只能是 1(批准) 或 2(拒绝)", "data": None}
    body: dict[str, Any] = {
        "emailAddress": emailAddress,
        "reviewerStatus": effective_status,
        "vehicleNo": effective_vehicle,
        "vin": vin,
    }
    effective_remark = reviewerRemark or reviewComment
    if effective_remark:
        body["reviewerRemark"] = effective_remark
    if reservationId:
        body["reservationId"] = reservationId
    return _post(PATH_APPROVAL_VEHICLE_RESERVATION, body)


# ── 7. return_vehicle ──────────────────────────────────────────────────────

@mcp.tool()
def return_vehicle(
    emailAddress: str = "",
    returnLocation: str = "",
    keyPosition: str = "",
    changeModule: str = "",
    vehicleStatus: int = 0,
    vehicleStatusDescription: str = "",
    vehicleNo: str = "",
    vin: str = "",
) -> dict:
    """归还已批准预约的车辆，更新车辆状态和位置。

    vehicleStatus: 1-可用 / 2-故障 / 3-维保 / 4-报废
    """
    if not emailAddress:
        return {"code": 400, "message": "emailAddress 为必填参数", "data": None}
    if not vehicleNo and not vin:
        return {"code": 400, "message": "vehicleNo 和 vin 至少提供一个", "data": None}
    body: dict[str, Any] = {
        "emailAddress": emailAddress,
        "returnLocation": returnLocation,
        "keyPosition": keyPosition,
        "changeModule": changeModule,
        "vehicleStatus": vehicleStatus,
        "vehicleStatusDescription": vehicleStatusDescription,
        "vehicleNo": vehicleNo,
        "vin": vin,
    }
    return _post(PATH_RETURN_VEHICLE, body)


# ── 8. fetch_user_reservation ──────────────────────────────────────────────

@mcp.tool()
def fetch_user_reservation(
    emailAddress: str = "",
    startTime: str | None = None,
    endTime: str | None = None,
    vehicleNo: str | None = None,
    vin: str | None = None,
    taskName: str | None = None,
    status: str | None = None,
) -> dict:
    """查询指定用户的历史预约记录。"""
    if not emailAddress:
        return {"code": 400, "message": "emailAddress 为必填参数", "data": None}
    body: dict[str, Any] = {"emailAddress": emailAddress}
    for k, v in (("startTime", startTime), ("endTime", endTime), ("vehicleNo", vehicleNo),
                 ("vin", vin), ("taskName", taskName), ("status", status)):
        if v is not None and v != "":
            body[k] = v
    return _post(PATH_FETCH_USER_RESERVATION, body)


# ── 9. fetch_user_approval ─────────────────────────────────────────────────

@mcp.tool()
def fetch_user_approval(
    emailAddress: str = "",
    startTime: str | None = None,
    endTime: str | None = None,
    vehicleNo: str | None = None,
    vin: str | None = None,
    taskName: str | None = None,
    status: str | None = None,
) -> dict:
    """调度员/管理员查询归属自己审批的预约任务列表。"""
    if not emailAddress:
        return {"code": 400, "message": "emailAddress 为必填参数", "data": None}
    body: dict[str, Any] = {"emailAddress": emailAddress}
    for k, v in (("startTime", startTime), ("endTime", endTime), ("vehicleNo", vehicleNo),
                 ("vin", vin), ("taskName", taskName), ("status", status)):
        if v is not None and v != "":
            body[k] = v
    return _post(PATH_FETCH_USER_APPROVAL, body)


# ── 入口 ──────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    # 与参考项目 booking_mcp_server.py 一致：stdio transport
    mcp.run(transport="stdio")
