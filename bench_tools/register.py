"""注册8 个台架预约工具到 hermes-agent 注册表。

所有 handler 用 ocl.tool_guard.guarded()包裹 → L2兜底权限校验。
L1拦截由 hermes_plugins/feishu_acl钩子负责（参见 ocl/permission.TOOL_MIN_ROLE）。

emailAddress 不出现在任何 schema字段中（结构性防御）——由服务端从 open_id注入。
"""
from tools.registry import registry

from bench_tools import handlers
from ocl.tool_guard import guarded

_TOOLSET = "bench"


def _reg(name, description, properties, required, handler):
 registry.register(
  name=name, toolset=_TOOLSET,
  schema={"type": "function", "function": {
   "name": name, "description": description,
   "parameters": {"type": "object", "properties": properties, "required": required},
  }},
  handler=guarded(name, handler),
 )


_reg("list_architectures", "查询所有可用的台架架构类型（如1.0架构/L3架构）。无需参数。",
 {}, [], handlers.list_architectures)

_reg("list_available_benches", "根据邮箱+架构+是否需要停车测试查询可用台架编号列表。调用前通常先查架构。",
 {"architecture": {"type": "string", "description": "台架架构类型,如1.0架构/L3架构（可选）"},
 "needParkingTest": {"type": "integer", "description": "是否需要停车测试,0=否1=是", "enum": [0,1]}},
 [], handlers.list_available_benches)

_reg("reserve_bench", "预约台架。benchNo 必须来自 list_available_benches 的结果。时间格式严格 yyyy-MM-dd HH:mm:ss，开始晚于当前、早于结束。",
 {"benchNo": {"type": "string", "description": "台架编号,如 TJ002（不是 benchId/id）"},
 "startTime": {"type": "string", "description": "预约开始时间 yyyy-MM-dd HH:mm:ss"},
 "endTime": {"type": "string", "description": "预约结束时间 yyyy-MM-dd HH:mm:ss"},
 "taskName": {"type": "string", "description": "任务名称"},
 "testPurpose": {"type": "string", "description": "测试目的"},
 "remark": {"type": "string", "description": "备注（可选）"}},
 ["benchNo", "startTime", "endTime", "taskName", "testPurpose"], handlers.reserve_bench)

_reg("cancel_reservation", "取消待审批（status=0）的台架预约。可选 startTime/endTime精确定位。",
 {"benchNo": {"type": "string", "description": "台架编号"},
 "startTime": {"type": "string", "description": "预约开始时间，用于精确定位（可选）"},
 "endTime": {"type": "string", "description": "预约结束时间，用于精确定位（可选）"}},
 ["benchNo"], handlers.cancel_reservation)

_reg("approve_reservation", "审批台架预约（仅调度员/管理员）。approvalResult:1=批准2=拒绝。可选 startTime/endTime精确定位。",
 {"benchNo": {"type": "string", "description": "台架编号"},
 "approvalResult": {"type": "integer", "description": "审批结果,1=批准2=拒绝", "enum": [1,2]},
 "approvalRemark": {"type": "string", "description": "审批备注（可选）"},
 "startTime": {"type": "string", "description": "预约开始时间，用于精确定位（可选）"},
 "endTime": {"type": "string", "description": "预约结束时间，用于精确定位（可选）"}},
 ["benchNo", "approvalResult"], handlers.approve_reservation)

_reg("list_my_reservations", "查询我的台架预约记录，可按台架/状态/任务名过滤。status:0待审批1已批准2已拒绝3已取消4已完成。",
 {"benchNo": {"type": "string", "description": "台架编号（可选）"},
 "taskName": {"type": "string", "description": "任务名称模糊搜索（可选）"},
 "status": {"type": "integer", "description": "预约状态0-4（可选）", "enum": [0,1,2,3,4]}},
 [], handlers.list_my_reservations)

_reg("list_my_approvals", "查询我作为审批人的预约记录（仅调度员/管理员）。status:0待审批1已批准…",
 {"status": {"type": "integer", "description": "预约状态0-4（可选）", "enum": [0,1,2,3,4]}},
 [], handlers.list_my_approvals)

_reg("return_bench", "归还台架并结束预约（仅能归还 status=1 的记录 → 自动变为 status=4）。",
 {"benchNo": {"type": "string", "description": "台架编号"},
 "returnLocation": {"type": "string", "description": "还台地点"}},
 ["benchNo", "returnLocation"], handlers.return_bench)

