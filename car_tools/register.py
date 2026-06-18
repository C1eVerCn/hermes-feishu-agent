"""注册车辆预约业务工具（7 业务 + 2 助手 + 1 内部 dry_run + 1 内部 commit）到 hermes registry。

所有 handler 用 ocl.tool_guard.guarded() 包裹 → L2 兜底权限校验。
L1 拦截由 hermes_plugins/feishu_acl 钩子负责（参见 ocl.permission.TOOL_MIN_ROLE）。

⚠️ emailAddress / openId / mobile **不**出现在任何 schema 字段中（结构性防御）
—— 由服务端从 contextvars 注入，LLM 永远看不到。
"""
from tools.registry import registry

from car_tools import handlers
from car_tools import __init__ as _car_init  # noqa: F401  # 确保 CAR_TOOLSET 可读
from ocl.tool_guard import guarded

CAR_TOOLSET = "car"


def _reg(name, description, properties, required, handler):
    registry.register(
        name=name, toolset=CAR_TOOLSET,
        schema={"type": "function", "function": {
            "name": name, "description": description,
            "parameters": {"type": "object", "properties": properties, "required": required},
        }},
        handler=guarded(name, handler),
    )


# ── 1. 业务：查询可用车辆 ─────────────────────────────────────────────────
_reg(
    "fetch_available_vehicles",
    "查询指定时间段内可用的车辆。必填：vehicleType（车辆类型如DM2/CT1/大F车/CM0/BM2）、"
    "platform（芯片平台Xavier/ADCU/Orin/Thor）、startTime（yyyy-MM-dd HH:mm）、endTime。"
    "返回 list[Vehicle]。",
    {
        "vehicleType": {"type": "string", "description": "车辆类型（如 DM2/CT1/大F车/CM0/BM2）"},
        "platform":    {"type": "string", "description": "芯片平台（Xavier/ADCU/Orin/Thor）",
                        "enum": ["Xavier", "ADCU", "Orin", "Thor"]},
        "startTime":   {"type": "string", "description": "开始时间 yyyy-MM-dd HH:mm"},
        "endTime":     {"type": "string", "description": "结束时间 yyyy-MM-dd HH:mm"},
    },
    ["vehicleType", "platform", "startTime", "endTime"],
    handlers.fetch_available_vehicles,
)


# ── 2. dry_run（LLM 唯一可调的预约入口；collects 槽位 + 渲染确认卡） ────────
_reg(
    "_dry_run_vehicle_reservation",
    "**dry-run** 预约（永远 dry，不真调 API）。vehicleNo 来自 fetch_available_vehicles 的结果"
    "（如 PNV332 / SVV027 / SOV646）。时间格式严格 yyyy-MM-dd HH:mm，开始晚于当前、早于结束。\n\n"
    "**必填字段**：vehicleNo、vehicleType、platform、startTime、endTime、taskName、location。\n\n"
    "**这是预约两步流程中的第一步（必走）**：\n"
    "1. 调本工具 → 若返回 `missing_fields`，向用户追问并重调（保留已填字段 + 补缺失字段）\n"
    "2. 用户点 [确认] 后，**系统自动**调用 _commit_vehicle_reservation 真正下单"
    "（这个工具 LLM 看不到、也调不到）\n\n"
    "**LLM 不要尝试直接调 _commit_vehicle_reservation** —— 它对 LLM 不可见。",
    {
        "vehicleNo":    {"type": "string", "description": "JSON key: vehicleNo。车辆编号（如 PNV332 / SVV027 / SOV646）"},
        "vehicleType":  {"type": "string", "description": "车辆类型（如 DM2/CT1/大F车）"},
        "platform":     {"type": "string", "description": "芯片平台（Xavier/ADCU/Orin/Thor）"},
        "licensePlate": {"type": "string", "description": "车牌号（可选）"},
        "startTime":    {"type": "string", "description": "JSON key: startTime。预约开始时间 yyyy-MM-dd HH:mm"},
        "endTime":      {"type": "string", "description": "JSON key: endTime。预约结束时间 yyyy-MM-dd HH:mm"},
        "taskName":     {"type": "string", "description": "任务名称（如「高速测试」「感知压测」）"},
        "location":     {"type": "string", "description": "预约地点"},
        "remark":       {"type": "string", "description": "备注（可选）"},
        "vin":          {"type": "string", "description": "VIN 码（可选）"},
    },
    ["vehicleNo", "vehicleType", "platform", "startTime", "endTime", "taskName", "location"],
    handlers._dry_run_reservation,
)


# ── 3. _commit（LLM 不可见；仅 card_action_handler.confirm 流程调用） ─────
_reg(
    "_commit_vehicle_reservation",
    "**真正下单**（不暴露给 LLM）。仅供 bot.card_action_handler.confirm 流程调用，"
    "用户点 [确认] 卡片后由系统触发。",
    {
        "vehicleNo":    {"type": "string"},
        "vehicleType":  {"type": "string"},
        "platform":     {"type": "string"},
        "licensePlate": {"type": "string"},
        "startTime":    {"type": "string"},
        "endTime":      {"type": "string"},
        "taskName":     {"type": "string"},
        "location":     {"type": "string"},
        "remark":       {"type": "string"},
        "vin":          {"type": "string"},
    },
    ["vehicleNo", "startTime", "endTime", "taskName", "location"],
    handlers._commit_single_vehicle_reservation,
)


# ── 4. 取消预约 ──────────────────────────────────────────────────────────
_reg(
    "cancel_vehicle_reservation",
    "取消待审批（status=待审批）的车辆预约。可选 reservationId 精确定位。",
    {
        "vehicleNo":     {"type": "string", "description": "车辆编号"},
        "reservationId": {"type": "string", "description": "预约 ID（可选，用于精确定位）"},
    },
    ["vehicleNo"],
    handlers.cancel_vehicle_reservation,
)


# ── 5. 审批预约（仅调度员/管理员） ───────────────────────────────────────
_reg(
    "approval_vehicle_reservation",
    "审批车辆预约（仅调度员/管理员）。approved: true=批准 / false=拒绝。"
    "可选 reservationId 精确定位；reviewComment 是审批意见。",
    {
        "vehicleNo":     {"type": "string", "description": "车辆编号"},
        "approved":      {"type": "boolean", "description": "是否批准（true=批准，false=拒绝）"},
        "reviewComment": {"type": "string", "description": "审批意见（可选）"},
        "reservationId": {"type": "string", "description": "预约 ID（可选）"},
    },
    ["vehicleNo", "approved"],
    handlers.approval_vehicle_reservation,
)


# ── 6. 归还车辆 ──────────────────────────────────────────────────────────
_reg(
    "return_vehicle",
    "归还车辆并结束预约。必填：vehicleNo、returnLocation（还车地点）、keyPosition（钥匙位置）、"
    "changeModule（模块更换情况）、vehicleStatus（车辆状态，整数转字符串）。",
    {
        "vehicleNo":                {"type": "string", "description": "车辆编号"},
        "returnLocation":           {"type": "string", "description": "还车地点"},
        "keyPosition":              {"type": "string", "description": "钥匙位置"},
        "changeModule":             {"type": "string", "description": "模块更换情况"},
        "vehicleStatus":            {"type": "string", "description": "车辆状态（整数转字符串）"},
        "vehicleStatusDescription": {"type": "string", "description": "状态描述（可选）"},
        "vin":                      {"type": "string", "description": "VIN 码（可选）"},
    },
    ["vehicleNo", "returnLocation", "keyPosition", "changeModule", "vehicleStatus"],
    handlers.return_vehicle,
)


# ── 7. 我的预约记录 ─────────────────────────────────────────────────────
_reg(
    "fetch_user_reservation",
    "查询我作为申请人的预约记录。可按 vehicleNo / taskName / status / 时间段过滤。",
    {
        "startTime": {"type": "string", "description": "开始时间过滤（可选）"},
        "endTime":   {"type": "string", "description": "结束时间过滤（可选）"},
        "vehicleNo": {"type": "string", "description": "车辆编号过滤（可选）"},
        "taskName":  {"type": "string", "description": "任务名模糊搜索（可选）"},
        "status":    {"type": "string", "description": "状态过滤（可选，如 待审批/已批准/已驳回/已取消/已归还）"},
    },
    [],
    handlers.fetch_user_reservation,
)


# ── 8. 我的待审批列表（仅调度员/管理员） ──────────────────────────────────
_reg(
    "fetch_user_approval",
    "查询我作为审批人的预约记录（仅调度员/管理员）。可按 vehicleNo / taskName / status / 时间段过滤。",
    {
        "startTime": {"type": "string", "description": "开始时间过滤（可选）"},
        "endTime":   {"type": "string", "description": "结束时间过滤（可选）"},
        "vehicleNo": {"type": "string", "description": "车辆编号过滤（可选）"},
        "taskName":  {"type": "string", "description": "任务名模糊搜索（可选）"},
        "status":    {"type": "string", "description": "状态过滤（可选，默认仅看待审批）"},
    },
    [],
    handlers.fetch_user_approval,
)


# ── 9. 助手：查询当前用户上下文 ─────────────────────────────────────────
_reg(
    "get_user_context",
    "查询当前用户的全局上下文（部门 / 项目 / 默认车辆组 / 角色），用于业务侧权限校验。"
    "调用方身份（openid / email）由 CallerIdentity 自动注入，不需要 LLM 传参。",
    {},
    [],
    handlers.get_user_context,
)


# ── 10. 助手：查询通用字典 ───────────────────────────────────────────────
_reg(
    "get_common_dictionary",
    "查询通用字典（vehicleType / platform / status 等枚举的中文含义）。调用前可先调此工具"
    "了解可用值。typeCode 示例：「vehicleType」「platform」「reservationStatus」。",
    {
        "typeCode": {"type": "string", "description": "字典类型编码"},
    },
    ["typeCode"],
    handlers.get_common_dictionary,
)
