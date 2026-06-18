"""DMZ智能体的工具 ACL（访问控制列表）。

按用户角色（1=普通用户 /2=调度员 /3=管理员）控制哪些 LLM工具可达。

设计要点：
- L1（hermes插件 pre_tool_call钩子）=硬拦截
- L2（ocl.tool_guard.guarded()包裹）=兜底
- 真实业务权限仍由后端（MCP server）按 openid/email 校验；本地只做
  工具能否被 LLM调起的粗粒度门控。

角色映射（车辆预约域）：
- role=1 普通用户：查询可用车辆、预约 / 取消 / 归还、查我的预约
- role=2调度员：+审批预约、查询待我审批
- role=3管理员：+跨组审批、系统级操作（未来）
"""
import logging

from ocl import identity

log = logging.getLogger(__name__)


# ── 工具 → 所需最低角色（1=普通 /2=调度员 /3=管理员） ──────────────────────
# 车辆预约域（car_tools/）；上一代台架预约（bench_tools/）和 VLM 精标（vlm_tools/）
# 已于 2026-06-16 业务域合并时彻底删除。
TOOL_MIN_ROLE: dict[str, int] = {
    # ── 业务工具（7 个） ──
    "fetch_available_vehicles":       1,
    "single_vehicle_reservation":     1,
    "cancel_vehicle_reservation":     1,
    "approval_vehicle_reservation":   2,   # 仅调度员/管理员
    "return_vehicle":                 1,
    "fetch_user_reservation":         1,
    "fetch_user_approval":            2,   # 仅调度员/管理员

    # ── 助手（2 个） ──
    "get_user_context":               1,
    "get_common_dictionary":          1,

    # ── 内部回调工具（LLM 不可见，仅 card_action_handler 调） ──
    "_dry_run_vehicle_reservation":   1,
    "_commit_vehicle_reservation":    1,
}


def is_tool_permitted(open_id: str, tool_name: str) -> bool:
    """判断当前用户是否能调用此工具。

    判定流程：
    1. 工具未注册 → 直接拒绝（防 LLM 编造工具名）
    2. 用户无身份（open_id 为空）→ 通过（内部 / 系统调用，跳过门控）
    3. 用户在 identity_map 中无角色 → 视为 role=0（陌生人），拒绝
    4. 用户角色 ≥ TOOL_MIN_ROLE[tool_name] → 通过
    5. 否则 → 拒绝

    注意：本函数抛异常时调用方（feishu_acl插件 / guarded()）应 fail-open 兜底。
    """
    if tool_name not in TOOL_MIN_ROLE:
        return False
    if not open_id:
        return True
    try:
        user_role = identity.role_of(open_id)
    except Exception as e:
        log.warning("permission_check_failed tool=%s user=%s err=%s", tool_name, open_id, e)
        return False
    return user_role >= TOOL_MIN_ROLE[tool_name]
