"""车辆预约 MCP server（FastMCP stdio）。

把 8 个 booking 工具（来自 dmz-fmp-mcp 的 @Tool）通过 FastMCP 暴露成
stdio MCP server，hermes-agent spawn 它、自动注册到 hermes registry。

**与上游的协议（2026-06-18 修订）：**
dmz-fmp-mcp 是 Spring AI MCP 服务，端点：
- SSE:        /fmpMCP/sse
- Message:    /fmpMCP/message?sessionId=<id>
- 协议：      MCP over SSE + JSON-RPC 2.0

旧版用 httpx POST REST 调上游，路径不存在（fmp-mcp 没有 REST 端点）。
新版用 Python `mcp` 包（`mcp.client.sse.sse_client`）以 SSE+JSON-RPC 协议
直接调 dmz-fmp-mcp 的 8 个 @Tool 方法。

业务身份注入（CLAUDE.md 不变量）：
- LLM-facing 工具 schema **不**含 emailAddress / openId / mobile
- emailAddress 由调用方在 args 里传入（car_tools/handlers.py 从
  CallerIdentity 注入）；本 server 直接转发到 fmp-mcp
- 上游 fmp 端按 emailAddress 鉴权 / 查询
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from typing import Any

from fastmcp import FastMCP
from mcp import ClientSession
from mcp.client.sse import sse_client

log = logging.getLogger("car_booking_mcp")
logging.basicConfig(level=os.getenv("CAR_MCP_LOG_LEVEL", "INFO"), stream=sys.stderr)


# ── 配置：上游 fmp-mcp 的 SSE MCP 端点 ─────────────────────────────────────

CAR_MCP_SSE_URL: str = os.getenv(
    "CAR_MCP_SSE_URL",
    "http://dmz-fmp-mcp:9015/fmpMCP/sse",
)
TIMEOUT: float = float(os.getenv("CAR_MCP_TIMEOUT", "80"))
# 2026-06-18 优化：缩短 SSE connect / read 超时，让失败时快速降级
# （之前 connect 失败时实测 ~4s 才返回 TaskGroup 错误）。
SSE_CONNECT_TIMEOUT: float = float(os.getenv("CAR_MCP_CONNECT_TIMEOUT", "3"))
SSE_READ_TIMEOUT: float = float(os.getenv("CAR_MCP_READ_TIMEOUT", "30"))


# ── MCP 客户端（同步包装） ─────────────────────────────────────────────────
# 2026-06-18 性能优化：模块级 SSE session 池（持久连接 + 线程安全）。
# 之前每次调用都开新 SSE 连接 → 间歇性 TaskGroup 错误耗时 4s+。
# 现在持久化一个 session（独立后台线程 + event loop），
# 跨调用复用；session 失效时自动清掉让下次重建。
import threading as _threading
import queue as _queue

_session_lock = _threading.Lock()
_loop: "asyncio.AbstractEventLoop | None" = None
_loop_thread: "_threading.Thread | None" = None
_session_holder: "queue.Queue" = _queue.Queue(maxsize=1)  # 持久 ClientSession
_init_done = _threading.Event()
_init_error: BaseException | None = None


def _start_loop():
    """后台线程：跑一个独立 event loop，session 活在这里。"""
    global _loop
    _loop = asyncio.new_event_loop()
    asyncio.set_event_loop(_loop)
    try:
        _init_done.set()
        _loop.run_forever()
    finally:
        _loop.close()


def _ensure_loop_started():
    global _loop_thread
    if _loop_thread is not None and _loop_thread.is_alive():
        return
    _loop_thread = _threading.Thread(target=_start_loop, daemon=True, name="mcp-sse-loop")
    _loop_thread.start()
    _init_done.wait(timeout=5)


async def _open_session():
    """在后台 loop 里打开一个新的 SSE session 并放回 holder。"""
    sse_cm = sse_client(CAR_MCP_SSE_URL, timeout=SSE_CONNECT_TIMEOUT,
                        sse_read_timeout=SSE_READ_TIMEOUT)
    read, write = await sse_cm.__aenter__()
    session = ClientSession(read, write)
    await session.__aenter__()
    await session.initialize()
    # 旧的清掉（如果有）
    try:
        old = _session_holder.get_nowait()
        try:
            await old.__aexit__(None, None, None)
        except Exception:
            pass
    except _queue.Empty:
        pass
    _session_holder.put((session, sse_cm))
    return session


async def _close_session():
    try:
        session, sse_cm = _session_holder.get_nowait()
    except _queue.Empty:
        return
    try:
        await session.__aexit__(None, None, None)
    except Exception:
        pass
    try:
        await sse_cm.__aexit__(None, None, None)
    except Exception:
        pass


async def _do_call(tool_name: str, arguments: dict) -> dict:
    """在后台 loop 里执行：取 session → call_tool → 解析 → 失败时清 session。

    2026-06-24：加 1 次自动重试（应对 Spring AI server 间歇性 TaskGroup
    错误 —— 实测首次 call 间歇性 ~3-5s 失败后，下次 call 才能成功）。
    """
    last_exc = None
    for attempt in range(2):  # 最多试 2 次
        # 确保 session 存在
        if _session_holder.empty():
            await _open_session()
        try:
            session, _ = _session_holder.get_nowait()
            _session_holder.put((session, _))
        except _queue.Empty:
            await _open_session()
            session, _ = _session_holder.get_nowait()
            _session_holder.put((session, _))

        try:
            result = await session.call_tool(tool_name, arguments)
            return _parse_tool_result(result, tool_name, arguments)
        except Exception as e:
            last_exc = e
            # 失败时清 session，下次重建
            await _close_session()
            if attempt == 0:
                log.info("fmp_mcp_retry tool=%s attempt=1 err=%s", tool_name, e)
                continue
            raise
    # unreachable, but mypy 友好
    raise last_exc if last_exc else RuntimeError("fmp_mcp call failed")


def _parse_tool_result(result, tool_name: str, arguments: dict) -> dict:
    """从 ClientSession.call_tool() 返回值解析业务 JSON。"""
    texts = [c.text for c in result.content if getattr(c, "type", None) == "text"]
    if not texts:
        log.warning("fmp_mcp_empty_text tool=%s args=%s", tool_name, arguments)
        return {"code": 500, "message": "上游返回空", "data": None}
    raw_text = texts[0]
    # dmz-fmp-mcp (Spring AI MCP) 双层 JSON 序列化
    parsed: Any = raw_text
    if isinstance(parsed, str):
        try:
            envelope = json.loads(parsed)
        except (json.JSONDecodeError, ValueError):
            envelope = None
        if isinstance(envelope, dict) and isinstance(envelope.get("text"), str):
            try:
                parsed = json.loads(envelope["text"])
            except (json.JSONDecodeError, ValueError):
                parsed = envelope["text"]
        elif envelope is not None:
            parsed = envelope
    if not isinstance(parsed, dict):
        return {"raw": parsed}
    return parsed


def _call_fmp_tool(tool_name: str, arguments: dict) -> dict:
    """通过 SSE MCP 调 fmp-mcp 工具（同步包装），返回解析后的 dict。

    2026-06-18 优化：使用持久 SSE session（后台 loop 持有），
    单 call 命中复用 <30ms；失败时清 session 让下次重建。
    """
    _ensure_loop_started()
    assert _loop is not None
    future = asyncio.run_coroutine_threadsafe(
        _do_call(tool_name, arguments), _loop)
    try:
        return future.result(timeout=SSE_CONNECT_TIMEOUT + SSE_READ_TIMEOUT + 5)
    except Exception as e:
        log.warning("fmp_mcp_call_failed tool=%s err=%s", tool_name, e)
        return {"code": 500, "message": f"MCP 调用失败: {type(e).__name__}: {e}", "data": None}


def _call_fmp_tool_args(tool_name: str, args_list: list) -> dict:
    """调 fmp-mcp 工具，args_list 顺序对应 Java @Tool 方法参数顺序。

    Spring AI MCP 1.0.0-SNAPSHOT 不保留 @ToolParam 名称，暴露为 arg0/arg1/...；
    本函数把位置参数映射成 arg0/arg1/... 字典。
    """
    arguments = {f"arg{i}": v for i, v in enumerate(args_list)}
    return _call_fmp_tool(tool_name, arguments)


# ── FastMCP server 实例 ───────────────────────────────────────────────────

mcp = FastMCP("car_booking")


# ── 1. get_user_context ────────────────────────────────────────────────────
# Java @Tool arg0=emailAddress

@mcp.tool()
def get_user_context(emailAddress: str = "") -> dict:
    """通过用户邮箱查询穹驰约车平台的用户信息（角色、部门、手机号等）。

    emailAddress: 飞书用户邮箱（必填，由服务端 CallerIdentity 注入）
    """
    if not emailAddress:
        return {"code": 400, "message": "emailAddress 为必填参数", "data": None}
    return _call_fmp_tool_args("get_user_context", [emailAddress])


# ── 2. get_common_dictionary ───────────────────────────────────────────────
# Java @Tool arg0=typeCode

@mcp.tool()
def get_common_dictionary(typeCode: str = "") -> dict:
    """获取平台枚举字典数据。

    typeCode: VEHICLE_TYPE / VEHICLE_TYPE_DETAIL / VEHICLE_PROJECT / VEHICLE_CHIP
    """
    if not typeCode:
        return {"code": 400, "message": "typeCode 为必填参数", "data": None}
    return _call_fmp_tool_args("get_common_dictionary", [typeCode])


# ── 3. fetch_available_vehicles ────────────────────────────────────────────
# Java @Tool arg 顺序（从 fmp-mcp log 验证）：
# arg0=emailAddress, arg1=vehicleType, arg2=vehicleTypeDetail, arg3=project,
# arg4=platform, arg5=startTime, arg6=endTime

@mcp.tool()
def fetch_available_vehicles(
    emailAddress: str = "",
    vehicleType: str = "",
    vehicleTypeDetail: str = "",
    project: str = "",
    platform: str = "",
    startTime: str = "",
    endTime: str = "",
) -> dict:
    """查询当前用户可预约的车辆列表。

    emailAddress: 预约用户邮箱（必填）
    其他参数：可选筛选条件
    """
    if not emailAddress:
        return {"code": 400, "message": "emailAddress 为必填参数", "data": None}
    return _call_fmp_tool_args("fetch_available_vehicles", [
        emailAddress, vehicleType, vehicleTypeDetail, project,
        platform, startTime, endTime,
    ])


# ── 4. single_vehicle_reservation ──────────────────────────────────────────
# Java @Tool arg 顺序（从 fmp-mcp log 验证）：
# arg0=startTime, arg1=endTime, arg2=vehicleNo, arg3=vin, arg4=emailAddress,
# arg5=taskName, arg6=location, arg7=remark

@mcp.tool()
def single_vehicle_reservation(
    emailAddress: str = "",
    startTime: str = "",
    endTime: str = "",
    taskName: str = "",
    location: str = "",
    vehicleNo: str = "",
    vin: str = "",
    remark: str = "",
    vehicleType: str = "",
    platform: str = "",
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
    # Java @Tool 顺序：startTime, endTime, vehicleNo, vin, emailAddress,
    #                   taskName, location, remark
    return _call_fmp_tool_args("single_vehicle_reservation", [
        startTime, endTime, vehicleNo, vin, emailAddress,
        taskName, location, remark,
    ])


# ── 5. cancel_vehicle_reservation ──────────────────────────────────────────
# Java @Tool arg 顺序（从 fmp-mcp log 验证）：
# arg0=vehicleNo, arg1=vin, arg2=emailAddress, arg3=reservationId

@mcp.tool()
def cancel_vehicle_reservation(
    emailAddress: str = "",
    vehicleNo: str = "",
    vin: str = "",
    reservationId: str = "",
) -> dict:
    """取消状态为"待审批"的预约。"""
    if not emailAddress:
        return {"code": 400, "message": "emailAddress 为必填参数", "data": None}
    if not vehicleNo and not vin:
        return {"code": 400, "message": "vehicleNo 和 vin 至少提供一个", "data": None}
    # Java 顺序：vehicleNo, vin, emailAddress, reservationId
    return _call_fmp_tool_args("cancel_vehicle_reservation", [
        vehicleNo, vin, emailAddress, reservationId,
    ])


# ── 6. approval_vehicle_reservation ────────────────────────────────────────
# Java @Tool arg 顺序（从 fmp-mcp log 验证）：
# arg0=vehicleNo, arg1=vin, arg2=emailAddress, arg3=reviewerStatus (int),
# arg4=reviewerRemark, arg5=reservationId

@mcp.tool()
def approval_vehicle_reservation(
    emailAddress: str = "",
    reviewerStatus: int = 0,
    vehicleNo: str = "",
    vin: str = "",
    reviewerRemark: str = "",
    reservationId: str = "",
    approved: bool = False,
    reviewComment: str = "",
) -> dict:
    """调度员或管理员对待审批预约进行批准/拒绝操作。

    reviewerStatus: 1=批准 / 2=拒绝
    """
    if not emailAddress:
        return {"code": 400, "message": "emailAddress 为必填参数", "data": None}
    effective_status = reviewerStatus
    if effective_status == 0 and approved is not None:
        effective_status = 1 if approved else 2
    effective_vehicle = vehicleNo or reservationId
    if not effective_vehicle and not vin:
        return {"code": 400, "message": "vehicleNo 和 vin 至少提供一个", "data": None}
    if effective_status not in (1, 2):
        return {"code": 400, "message": "reviewerStatus 只能是 1(批准) 或 2(拒绝)", "data": None}
    effective_remark = reviewerRemark or reviewComment
    # Java 顺序：vehicleNo, vin, emailAddress, reviewerStatus, reviewerRemark, reservationId
    return _call_fmp_tool_args("approval_vehicle_reservation", [
        effective_vehicle, vin, emailAddress, effective_status,
        effective_remark, reservationId,
    ])


# ── 7. return_vehicle ──────────────────────────────────────────────────────
# Java @Tool arg 顺序（从 fmp-mcp log 验证）：
# arg0=vehicleNo, arg1=vin, arg2=emailAddress, arg3=returnLocation,
# arg4=keyPosition, arg5=changeModule, arg6=vehicleStatus (int),
# arg7=vehicleStatusDescription

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
    # Java 顺序：vehicleNo, vin, emailAddress, returnLocation, keyPosition,
    #            changeModule, vehicleStatus, vehicleStatusDescription
    return _call_fmp_tool_args("return_vehicle", [
        vehicleNo, vin, emailAddress, returnLocation, keyPosition,
        changeModule, vehicleStatus, vehicleStatusDescription,
    ])


# ── 8. fetch_user_reservation ──────────────────────────────────────────────
# Java @Tool arg 顺序（从 fmp-mcp log 验证）：
# arg0=startTime, arg1=endTime, arg2=vehicleNo, arg3=vin, arg4=emailAddress,
# arg5=taskName, arg6=status (Integer)

@mcp.tool()
def fetch_user_reservation(
    emailAddress: str = "",
    startTime: str = "",
    endTime: str = "",
    vehicleNo: str = "",
    vin: str = "",
    taskName: str = "",
    status: str = "",
) -> dict:
    """查询指定用户的历史预约记录。"""
    if not emailAddress:
        return {"code": 400, "message": "emailAddress 为必填参数", "data": None}
    # Java 顺序：startTime, endTime, vehicleNo, vin, emailAddress, taskName, status
    return _call_fmp_tool_args("fetch_user_reservation", [
        startTime, endTime, vehicleNo, vin, emailAddress, taskName, status,
    ])


# ── 9. fetch_user_approval ─────────────────────────────────────────────────
# Java @Tool arg 顺序（与 fetchUserReservation 一致）：
# arg0=startTime, arg1=endTime, arg2=vehicleNo, arg3=vin, arg4=emailAddress,
# arg5=taskName, arg6=status (Integer)

@mcp.tool()
def fetch_user_approval(
    emailAddress: str = "",
    startTime: str = "",
    endTime: str = "",
    vehicleNo: str = "",
    vin: str = "",
    taskName: str = "",
    status: str = "",
) -> dict:
    """调度员/管理员查询归属自己审批的预约任务列表。"""
    if not emailAddress:
        return {"code": 400, "message": "emailAddress 为必填参数", "data": None}
    # Java 顺序：startTime, endTime, vehicleNo, vin, emailAddress, taskName, status
    return _call_fmp_tool_args("fetch_user_approval", [
        startTime, endTime, vehicleNo, vin, emailAddress, taskName, status,
    ])


# ── 入口 ──────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    log.info("booking_mcp_server starting sse_url=%s timeout=%.0f",
             CAR_MCP_SSE_URL, TIMEOUT)
    mcp.run(transport="stdio")
