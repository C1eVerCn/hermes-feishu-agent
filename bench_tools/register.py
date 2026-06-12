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

_reg("dry_run_reserve_bench", "**确认预约**（永远是 dry-run，不真调 API）。benchNo 必须来自 list_available_benches 的结果（如 TJ001/CT001）。时间格式严格 yyyy-MM-dd HH:mm:ss，开始晚于当前、早于结束。\n\n**任务/目的字段填写规则**（避免空字段）：\n- 用户说「任务是X」「任务名X」「task X」→ taskName=X\n- 用户说「目的是Y」「purpose Y」「用来Y」→ testPurpose=Y\n- 用户只说了一个（如「任务是测试」）→ 另一个**填同样的值**（系统允许），不要留空\n- 用户都没说 → 用 taskName=用户开放问句 + testPurpose=taskName（兜底）\n- 都是自由文本（不是架构名）。**「AEB标定」「感知压测」「测试A」都是合法值**\n\n**JSON 字段名必须严格使用下方的英文 key**（大小写一致）：benchNo / startTime / endTime / taskName / testPurpose / remark。**不要用 task / start_time / end_time / test_purpose 等变体**。\n\n**这是预约的两步流程中的第一步（必走）**：\n1. 调本工具（dry_run_reserve_bench）生成**纯文本**确认卡片（无按钮——末尾写明「回复\'确认\'提交；\'取消\'放弃」）\n2. 用户点「确认」后，**系统自动**调用 reserve_bench 真正预约（这个工具 LLM 看不到、也调不到）\n\n**必填字段缺失处理（强约束）**：\n- 如果工具返回的 result 含 `\"missing_fields\": [...]` 字段，说明必填字段没填全\n- **必须**直接告诉用户「请补充 X」并列出缺失字段，**不要**自己瞎填默认值\n- 等用户回复后，**用户的下一条消息就是缺失字段的答案**（不是新意图）——**重新调本工具**，把之前已填的字段**全部保留** + 新字段填入\n- 例：上轮缺 testPurpose，用户回感知压测 → 重新调本工具传 `{benchNo:CT001, startTime, endTime, taskName:测试, testPurpose:感知压测}`\n- **禁止**用猜测或默认值绕过（如不要把 testPurpose 自动填 taskName）\n- **禁止**把用户回复当作新意图去查其他工具（如 list_available_benches / list_event_names）\n\nLLM **不要**尝试直接调 reserve_bench——它不暴露给你。你的工作就是收集用户意图、调本工具、把卡片交给用户。",
 {"benchNo": {"type": "string", "description": "JSON key: benchNo。台架编号,如 TJ001/CT001（不是 benchId/id），从 list_available_benches 结果拿"},
 "startTime": {"type": "string", "description": "JSON key: startTime。预约开始时间 yyyy-MM-dd HH:mm:ss（例：2026-06-11 17:00:00）"},
 "endTime": {"type": "string", "description": "JSON key: endTime。预约结束时间 yyyy-MM-dd HH:mm:ss（例：2026-06-11 20:00:00）"},
 "taskName": {"type": "string", "description": "JSON key: taskName。任务名称（如「AEB标定」「感知压测」「测试A」），**JSON key 必须是 taskName**，**不要用 task / task_name 变体**"},
 "testPurpose": {"type": "string", "description": "JSON key: testPurpose。测试目的（如「AEB标定」「感知压测」「测试A」），**JSON key 必须是 testPurpose**，**不要用 purpose / test_purpose 变体**。如果用户没单独说目的，填和 taskName 一样的值。"},
 "remark": {"type": "string", "description": "JSON key: remark。备注（可选）"}},
 ["benchNo", "startTime", "endTime", "taskName", "testPurpose"], handlers.dry_run_reserve_bench)

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

_reg("list_my_reservations", "查询我的台架预约记录，可按台架/状态/任务名过滤。\n\n**默认过滤（强约束）**：\n- **不要**显示状态 2（已拒绝）或 3（已取消）的记录——用户只看活跃预约\n- 默认查询时**必须**传 `status=[0, 1, 4]`：0 待审批 + 1 已批准 + 4 已完成（活跃状态全集）\n- 只有当用户**明确说**「包括已取消的」「包括被驳回的」时，才不传 status（或传 status=[0,1,2,3,4]）\n- 状态值：0待审批 1已批准 2已拒绝 3已取消 4已完成",
 {"benchNo": {"type": "string", "description": "台架编号（可选）"},
 "taskName": {"type": "string", "description": "任务名称模糊搜索（可选）"},
 "status": {"type": "array", "items": {"type": "integer", "enum": [0,1,2,3,4]}, "description": "状态过滤数组。**默认必传 [0,1,4]** 排除已取消/已拒绝"}},
 [], handlers.list_my_reservations)

_reg("list_my_approvals", "查询我作为审批人的预约记录（仅调度员/管理员）。\n\n**默认过滤（强约束）**：\n- **只**显示状态 0（待审批）——审批人列表只关心待处理的\n- 默认**必须**传 `status=0`\n- 只有用户**明确说**「包括已审批的」「包括历史」时，才不传 status\n- 状态值：0待审批 1已批准 2已拒绝 3已取消 4已完成",
 {"status": {"type": "integer", "description": "状态过滤。**默认必传 0**（只看待审批）", "enum": [0,1,2,3,4]}},
 [], handlers.list_my_approvals)

_reg("return_bench", "归还台架并结束预约（仅能归还 status=1 的记录 → 自动变为 status=4）。",
 {"benchNo": {"type": "string", "description": "台架编号"},
 "returnLocation": {"type": "string", "description": "还台地点"}},
 ["benchNo", "returnLocation"], handlers.return_bench)

