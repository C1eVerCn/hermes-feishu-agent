"""车辆预约业务 handlers（10 个：8 业务 + 2 助手 + 2 内部 dry_run/commit）。

调用路径（对齐参考项目 reservation_agent-test-agent_multigraph）：
  handler(args)
    → car_tools.mcp_client.call(tool_name, args)  # 注入 CallerIdentity
      → hermes registry.get_entry(name).handler(args)  # L1 ACL 已校验
        → scripts/car_mcp_bridge spawn 的 booking_mcp_server.py
          → @mcp.tool() 函数（FastMCP）
            → httpx POST → CAR_API_BASE_URL 上游 fmp 端点
            → 返回 JSON dict / list
          ← model_dump / normalize_*(snake_case) 后返回
    ← stringified JSON
  ← 业务逻辑（normalizer / dry_run 槽位检测）

身份注入（CLAUDE.md 不变量）：
- LLM-facing tool schema 不含 emailAddress / openId / mobile
- _inject_caller 在每次 call 前从 CallerIdentity 注入 openid/email 到 args
- 上游 fmp 端按 emailAddress 鉴权 / 查询
"""
import json
import logging
from typing import Any

from car_tools import mcp_client, normalizers
from ocl.tool_guard import get_current_caller

log = logging.getLogger(__name__)


# ── 身份注入 ──────────────────────────────────────────────────────────────

def _inject_caller(args: dict) -> dict:
    """从 contextvars 读 CallerIdentity，注入 emailAddress（camelCase 给上游 MCP）。

    2026-06-18 fix：移除 openId 注入 —— dmz-fmp-mcp 9 个 @Tool 函数签名都不接受
    openId（只认 emailAddress / 业务参数），注入 openId 会导致
    `TypeError: got an unexpected keyword argument 'openId'`，所有走 _call_mcp
    路径的 fast-path / handler 工具调用都失败。
    openid 仍可通过 CallerIdentity 在 OCL L1/L2 鉴权层读取，不必传给上游 fmp。

    2026-06-25：mobile 同理**不**注入上游 —— 上游每个 @Tool 都以 emailAddress 为识别符、
    签名里没有 mobile 参数（注入会 TypeError）。手机号是本系统的**第二识别符**（存
    identity_map、随 CallerIdentity 透传、可按手机号查人），上游接入 mobile 参数前不下传。
    """
    caller = get_current_caller()
    injected: dict[str, Any] = {}
    if caller.email:
        injected["emailAddress"] = caller.email
    return {**injected, **args}


def _call_mcp(tool_name: str, args: dict) -> str:
    """调 MCP 工具，返回 stringified JSON（dict / list 均可被下游 json.loads）。

    ``McpToolNotFound`` **不**被吞 —— 调用方（如 ``get_common_dictionary``）需要
    知道工具没注册以便走 fallback。其他 ``McpError``（连接失败 / 工具抛异常）
    包装为 ``{"error": ...}`` 返回。
    """
    try:
        full_args = _inject_caller(args)
    except Exception as e:
        log.warning("caller_inject_failed tool=%s err=%s", tool_name, e)
        return json.dumps({"error": f"身份注入失败: {e}"}, ensure_ascii=False)
    try:
        raw = mcp_client.get_mcp_client().call(tool_name, full_args)
    except mcp_client.McpToolNotFound:
        raise  # 让调用方处理（fallback / 升级错误）
    except mcp_client.McpError as e:
        return json.dumps({"error": f"MCP 调用失败: {e}"}, ensure_ascii=False)
    except Exception as e:
        log.exception("mcp_unexpected tool=%s", tool_name)
        return json.dumps({"error": f"MCP 调用异常: {type(e).__name__}: {e}"},
                          ensure_ascii=False)
    return raw if isinstance(raw, str) else json.dumps(raw, ensure_ascii=False)


# ── 1. fetch_available_vehicles ────────────────────────────────────────────

def fetch_available_vehicles(args: dict, **_) -> str:
    """查询可用车辆。返回 list[dict]（车辆字段，camelCase）。"""
    payload = {
        "vehicleType": args.get("vehicleType") or args.get("vehicle_type"),
        "platform":    args.get("platform"),
        "startTime":   args.get("startTime") or args.get("start_time"),
        "endTime":     args.get("endTime") or args.get("end_time"),
    }
    payload = {k: v for k, v in payload.items() if v is not None and v != ""}
    raw = _call_mcp("fetch_available_vehicles", payload)
    try:
        raw_obj = json.loads(raw) if isinstance(raw, str) else raw
    except (json.JSONDecodeError, ValueError):
        return raw
    if isinstance(raw_obj, dict) and "error" in raw_obj:
        return json.dumps(raw_obj, ensure_ascii=False)
    try:
        vehicles = normalizers.normalize_vehicles(raw_obj)
        return json.dumps([v.model_dump() for v in vehicles], ensure_ascii=False)
    except normalizers.NormalizeError as e:
        return json.dumps({"error": f"上游返回格式异常: {e.reason}"}, ensure_ascii=False)


# ── 2. single_vehicle_reservation（真实下单 — LLM 不可见） ──────────────────

def _commit_single_vehicle_reservation(args: dict, **_) -> str:
    """实际下单。仅供 card_action_handler.confirm 流程调用，不暴露给 LLM。

    返回 snake_case 序列化的 ReservationResult（normalizers.normalize → model_dump），
    让 build_success_card / notify_dispatchers / reservation_store 一律按 snake_case 读取。
    """
    payload = {
        "vehicleNo":    args.get("vehicleNo") or args.get("vehicle_no"),
        "startTime":    args.get("startTime") or args.get("start_time"),
        "endTime":      args.get("endTime") or args.get("end_time"),
        "taskName":     args.get("taskName") or args.get("task_name"),
        "location":     args.get("location"),
        "remark":       args.get("remark"),
        "vin":          args.get("vin"),
    }
    payload = {k: v for k, v in payload.items() if v is not None and v != ""}
    raw = _call_mcp("single_vehicle_reservation", payload)
    try:
        raw_obj = json.loads(raw) if isinstance(raw, str) else raw
    except (json.JSONDecodeError, ValueError):
        return raw
    if isinstance(raw_obj, dict) and "error" in raw_obj:
        return json.dumps(raw_obj, ensure_ascii=False)
    try:
        caller = get_current_caller()
        applicant = caller.as_dict() if caller.is_authenticated else None
        result = normalizers.normalize_reservation_result(raw_obj, applicant=applicant)
        return json.dumps(result.model_dump(), ensure_ascii=False)
    except normalizers.NormalizeError as e:
        return json.dumps({"error": f"上游返回格式异常: {e.reason}"}, ensure_ascii=False)


# ── 3. _dry_run_reservation（LLM 可见；collect 槽位 + 渲染确认卡） ─────────

_BOOKING_REQUIRED = ("vehicle_type", "platform", "start_time", "end_time",
                     "task_name", "location")

_FIELD_LABELS_CN = {
    "vehicle_type": "车辆类型",
    "platform":     "芯片平台",
    "start_time":   "开始时间",
    "end_time":     "结束时间",
    "task_name":    "任务名称",
    "location":     "地点",
}

_FIELD_EXAMPLES = {
    "vehicle_type": "<车辆类型，如 DM2 / CT1 / 大F车>",
    "platform":     "<芯片平台，如 Xavier / ADCU / Orin / Thor>",
    "start_time":   "<开始时间，如 2026-06-16 09:00>",
    "end_time":     "<结束时间，如 2026-06-16 18:00>",
    "task_name":    "高速测试",
    "location":     "测试场 A 区",
}


def _dry_run_reservation(args: dict, **_) -> str:
    """Dry-run: collect slots, return summary or missing-fields prompt.

    与 LangGraph 参考项目一致：先校验必填字段，全部齐全 → 返回 summary 让
    bot.card_action_handler 渲染 [确认] [取消] 卡片；缺字段 → 返回 missing_fields
    + summary 让 bot 渲染「请补充 X」文本卡片，LLM 据此追问用户。
    """
    normalized = {
        "vehicle_no":   args.get("vehicleNo") or args.get("vehicle_no"),
        "vehicle_type": args.get("vehicleType") or args.get("vehicle_type"),
        "platform":     args.get("platform"),
        "license_plate": args.get("licensePlate") or args.get("license_plate"),
        "start_time":   args.get("startTime") or args.get("start_time"),
        "end_time":     args.get("endTime") or args.get("end_time"),
        "task_name":    args.get("taskName") or args.get("task_name"),
        "location":     args.get("location"),
        "remark":       args.get("remark"),
        "vin":          args.get("vin"),
    }

    missing = [k for k in _BOOKING_REQUIRED
               if not str(normalized.get(k) or "").strip()]
    if missing:
        missing_cn = [_FIELD_LABELS_CN[k] for k in missing]
        already = {k: v for k, v in normalized.items() if v and k not in missing}
        ex_pairs = []
        for k in _BOOKING_REQUIRED:
            ex_pairs.append(str(already.get(k) or _FIELD_EXAMPLES[k]))
        ex_vehicle = normalized.get("vehicle_no") or "<车辆编号，如 PNV332>"
        summary = (
            f"我还缺少以下信息：{', '.join(missing_cn)}\n"
            f"请补充后重新发送，例如：\"预约{ex_vehicle}，从{ex_pairs[2]}到{ex_pairs[3]}，"
            f"任务是{ex_pairs[4]}，地点是{ex_pairs[5]}\""
        )
        return json.dumps({
            "dry_run": True,
            "missing_fields": missing,
            "summary": summary,
            "args": normalized,
            "already_filled": already,
        }, ensure_ascii=False)

    parts = [
        f"车辆编号：{normalized['vehicle_no']}",
        f"平台：{normalized['platform']}",
        f"类型：{normalized['vehicle_type']}",
        f"开始：{normalized['start_time']}",
        f"结束：{normalized['end_time']}",
        f"任务：{normalized['task_name']}",
        f"地点：{normalized['location']}",
    ]
    if normalized.get("remark"):
        parts.append(f"备注：{normalized['remark']}")
    return json.dumps({
        "dry_run": True,
        "summary": "\n".join(parts),
        "args": normalized,
    }, ensure_ascii=False)


# ── 4. cancel_vehicle_reservation ───────────────────────────────────────────

def cancel_vehicle_reservation(args: dict, **_) -> str:
    payload = {
        "vehicleNo":     args.get("vehicleNo") or args.get("vehicle_no"),
        "reservationId": args.get("reservationId") or args.get("reservation_id"),
    }
    payload = {k: v for k, v in payload.items() if v is not None and v != ""}
    return _call_mcp("cancel_vehicle_reservation", payload)


# ── 5. approval_vehicle_reservation ────────────────────────────────────────

def approval_vehicle_reservation(args: dict, **_) -> str:
    """MCP 契约：reviewerStatus int 1=批准 / 2=拒绝。LLM 侧用 bool approved。"""
    approved = args.get("approved")
    if approved is None:
        reviewer_status: int | None = None
    elif isinstance(approved, bool):
        reviewer_status = 1 if approved else 2
    elif isinstance(approved, (int, float)):
        reviewer_status = 1 if int(approved) == 1 else 2
    elif isinstance(approved, str):
        s = approved.strip()
        if s in ("1", "true", "True", "批准", "同意", "yes"):
            reviewer_status = 1
        elif s in ("0", "2", "false", "False", "拒绝", "不同意", "no"):
            reviewer_status = 2
        else:
            reviewer_status = None
    else:
        reviewer_status = None

    payload = {
        "vehicleNo":     args.get("vehicleNo") or args.get("vehicle_no"),
        "reviewerStatus": reviewer_status,
        "reviewerRemark": args.get("reviewComment") or args.get("review_comment") or args.get("reviewer_remark"),
        "reservationId": args.get("reservationId") or args.get("reservation_id"),
    }
    payload = {k: v for k, v in payload.items() if v is not None and v != ""}
    raw = _call_mcp("approval_vehicle_reservation", payload)
    try:
        raw_obj = json.loads(raw) if isinstance(raw, str) else raw
    except (json.JSONDecodeError, ValueError):
        return raw
    try:
        result = normalizers.normalize_approval_result(raw_obj)
    except normalizers.NormalizeError as e:
        return json.dumps({"error": f"上游返回格式异常: {e.reason}"}, ensure_ascii=False)

    # 2026-06-18 通知申请人：审批结果 DM（之前只更新 MCP，不通知 applicant）。
    try:
        from bot import reservation_store
        rid = payload.get("reservationId") or ""
        rec = reservation_store.get(rid) if rid else None
        if rec is None and payload.get("vehicleNo"):
            start_time = (getattr(result, "start_time", "") or "") or ""
            rec = reservation_store.find_by_vehicle_and_time(
                payload.get("vehicleNo", ""), start_time)
        if rec and rec.get("applicant_open_id"):
            from feishu import notify
            decision = "✅ 已批准" if reviewer_status == 1 else "❌ 已驳回" if reviewer_status == 2 else "处理中"
            vno = rec.get("vehicle_no", "")
            st = rec.get("start_time", "")
            et = rec.get("end_time", "")
            task = rec.get("task_name", "")
            remark = payload.get("reviewerRemark") or ""
            text = (
                f"📬 **您的车辆预约审批结果**\n\n"
                f"🚗 车辆编号：**{vno}**\n"
                f"⏱️ 时段：{st} ~ {et}\n"
                f"📝 任务：{task}\n"
                f"📋 审批结果：**{decision}**\n"
                + (f"💬 审批意见：{remark}\n" if remark else "")
                + ("\n💡 可以说「我的预约」查看完整列表" if reviewer_status == 1
                   else "\n💡 可以说「我的预约」查看完整列表，或重新发起申请")
            )
            notify.submit_text_to_user(rec["applicant_open_id"], text)
            log.info("approval_notified applicant=%s vehicle=%s decision=%s",
                     rec["applicant_open_id"], vno, decision)
    except Exception as e:
        log.warning("approval_notification_failed: %s", e)

    return json.dumps(result.model_dump(), ensure_ascii=False)


# ── 6. return_vehicle ─────────────────────────────────────────────────────

def return_vehicle(args: dict, **_) -> str:
    payload = {
        "vehicleNo":                args.get("vehicleNo") or args.get("vehicle_no"),
        "returnLocation":           args.get("returnLocation") or args.get("return_location"),
        "keyPosition":              args.get("keyPosition") or args.get("key_position"),
        "changeModule":             args.get("changeModule") or args.get("change_module"),
        "vehicleStatus":            args.get("vehicleStatus") or args.get("vehicle_status"),
        "vehicleStatusDescription": args.get("vehicleStatusDescription")
                                          or args.get("vehicle_status_description"),
        "vin":                      args.get("vin"),
    }
    payload = {k: v for k, v in payload.items() if v is not None and v != ""}
    raw = _call_mcp("return_vehicle", payload)
    try:
        raw_obj = json.loads(raw) if isinstance(raw, str) else raw
    except (json.JSONDecodeError, ValueError):
        return raw
    try:
        result = normalizers.normalize_return_result(raw_obj)
        return json.dumps(result.model_dump(), ensure_ascii=False)
    except normalizers.NormalizeError as e:
        return json.dumps({"error": f"上游返回格式异常: {e.reason}"}, ensure_ascii=False)


# ── 7. fetch_user_reservation ──────────────────────────────────────────────

def fetch_user_reservation(args: dict, **_) -> str:
    payload = {
        "startTime":  args.get("startTime") or args.get("start_time"),
        "endTime":    args.get("endTime") or args.get("end_time"),
        "vehicleNo":  args.get("vehicleNo") or args.get("vehicle_no"),
        "taskName":   args.get("taskName") or args.get("task_name"),
        "status":     args.get("status"),
    }
    payload = {k: v for k, v in payload.items() if v is not None and v != ""}
    raw = _call_mcp("fetch_user_reservation", payload)
    try:
        raw_obj = json.loads(raw) if isinstance(raw, str) else raw
    except (json.JSONDecodeError, ValueError):
        return raw
    try:
        records = normalizers.normalize_records(raw_obj, source="fetch_user_reservation")
        return json.dumps([r.model_dump() for r in records], ensure_ascii=False)
    except normalizers.NormalizeError as e:
        return json.dumps({"error": f"上游返回格式异常: {e.reason}"}, ensure_ascii=False)


# ── 8. fetch_user_approval ─────────────────────────────────────────────────

def fetch_user_approval(args: dict, **_) -> str:
    payload = {
        "startTime":  args.get("startTime") or args.get("start_time"),
        "endTime":    args.get("endTime") or args.get("end_time"),
        "vehicleNo":  args.get("vehicleNo") or args.get("vehicle_no"),
        "taskName":   args.get("taskName") or args.get("task_name"),
        "status":     args.get("status"),
    }
    payload = {k: v for k, v in payload.items() if v is not None and v != ""}
    raw = _call_mcp("fetch_user_approval", payload)
    try:
        raw_obj = json.loads(raw) if isinstance(raw, str) else raw
    except (json.JSONDecodeError, ValueError):
        return raw
    try:
        records = normalizers.normalize_records(raw_obj, source="fetch_user_approval")
        return json.dumps([r.model_dump() for r in records], ensure_ascii=False)
    except normalizers.NormalizeError as e:
        return json.dumps({"error": f"上游返回格式异常: {e.reason}"}, ensure_ascii=False)


# ── 9. get_user_context ────────────────────────────────────────────────────

def get_user_context(args: dict, **_) -> str:
    """查询当前用户的全局上下文（部门 / 项目 / 默认车辆组等）。

    不接受 emailAddress 等身份参数——openid/email 由 CallerIdentity 自动注入。
    """
    return _call_mcp("get_user_context", {})


# ── 10. get_common_dictionary ──────────────────────────────────────────────

_BUILTIN_DICTIONARY: dict[str, list[dict]] = {
    "VEHICLE_TYPE": [
        {"code": "DM2",  "name": "DM2 车型"},
        {"code": "CT1",  "name": "CT1 车型"},
        {"code": "大F车", "name": "大F车型"},
        {"code": "CM0",  "name": "CM0 车型"},
        {"code": "BM2",  "name": "BM2 车型"},
    ],
    "VEHICLE_CHIP": [
        {"code": "Xavier", "name": "Xavier 芯片"},
        {"code": "ADCU",   "name": "ADCU 芯片"},
        {"code": "Orin",   "name": "Orin 芯片"},
        {"code": "Thor",   "name": "Thor 芯片"},
    ],
    "RESERVATION_STATUS": [
        {"code": 0, "name": "待审批"},
        {"code": 1, "name": "已批准"},
        {"code": 2, "name": "已拒绝"},
        {"code": 3, "name": "已取消"},
        {"code": 4, "name": "已完成"},
    ],
    "VEHICLE_STATUS": [
        {"code": 1, "name": "可用"},
        {"code": 2, "name": "故障"},
        {"code": 3, "name": "维保"},
        {"code": 4, "name": "报废"},
    ],
}


def get_common_dictionary(args: dict, **_) -> str:
    """查询通用字典（vehicleType / platform / status 等枚举的中文含义）。

    优先级：MCP tool > 内置 fallback。
    """
    payload = {
        "typeCode": args.get("typeCode") or args.get("type_code"),
    }
    payload = {k: v for k, v in payload.items() if v is not None and v != ""}
    try:
        raw = _call_mcp("get_common_dictionary", payload)
    except mcp_client.McpToolNotFound:
        # MCP server 当前未暴露该工具 → 返回内置字典
        type_code = payload.get("typeCode", "")
        items = _BUILTIN_DICTIONARY.get(type_code, [])
        if items:
            return json.dumps({"items": items}, ensure_ascii=False)
        return json.dumps({
            "error": f"字典类型 {type_code!r} 未在内置 fallback 中定义，"
                     f"已知类型：{sorted(_BUILTIN_DICTIONARY.keys())}",
        }, ensure_ascii=False)
    return raw
