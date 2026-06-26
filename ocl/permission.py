"""DMZ智能体的工具 ACL（访问控制列表）。

按用户角色控制哪些 LLM 工具可达。角色与 fmp 后端 RBAC（sys_role）保持一致：
0=待审核（非平台用户） / 1=工程师 / 2=调度员 / 3=管理员 / 4=司机 / 5=组管理员。

设计要点：
- L1（hermes 插件 pre_tool_call 钩子）= 硬拦截
- L2（ocl.tool_guard.guarded() 包裹）= 兜底
- 真实业务权限仍由后端（飞书→agent→fmp-mcp→fmp）按 emailAddress 校验；openid 仅在
  OCL L1/L2 本地鉴权层使用。本地只做"工具能否被 LLM 调起"的粗粒度门控。
- fmp 的 5 个角色**非线性**（司机权限比工程师还少、组管理员≈调度员而非管理员），故
  OCL 不用 role>=min_role 线性比较，改为按角色显式列允许工具集（ROLE_TOOLS），精确镜像
  fmp 的 sys_role_menu 矩阵。

角色 → 约车能力（与 fmp sys_role_menu 对齐）：
- 1 工程师：查可用车辆、预约/取消/归还、查我的预约（不能审批）
- 2 调度员：+ 审批本组预约、查待我审批
- 3 管理员：全部
- 4 司机：fmp 仅"出车状态监控"，bot 无对应业务工具 → 不开放约车/审批（仅助手工具）
- 5 组管理员：约车 + 审批本组（车组/人员管理在 Web 端，bot 不暴露）≈ 调度员
细粒度（本组审批、预约状态、车组归属）仍由后端按 emailAddress 兜——两道闸独立。
"""
import logging

from ocl import identity

log = logging.getLogger(__name__)


# ── 工具分组（键 == car_tools/register.py 注册给 LLM 的工具名，共 10 个）──────────
# 车辆预约域（car_tools/）；上一代台架预约和 VLM 工具已于 2026-06-16 业务域合并时删除。
_QUERY_BOOK_TOOLS = frozenset({
    "fetch_available_vehicles",
    "_dry_run_vehicle_reservation",   # LLM 只见 dry_run
    "_commit_vehicle_reservation",    # commit 对 LLM 不可见，仅 card/FSM 触发
    "cancel_vehicle_reservation",
    "return_vehicle",
    "fetch_user_reservation",
})
_APPROVAL_TOOLS = frozenset({
    "approval_vehicle_reservation",
    "fetch_user_approval",
})
_HELPER_TOOLS = frozenset({
    "get_user_context",
    "get_common_dictionary",
})
ALL_TOOLS: frozenset[str] = _QUERY_BOOK_TOOLS | _APPROVAL_TOOLS | _HELPER_TOOLS


# ── 角色 → 可调工具集（键 == fmp sys_role.id；自定义组角色在 identity_map 归并到 5）──
# 显式镜像 fmp sys_role_menu，**非线性**——不要改回 role>=min_role 比较，否则司机(4)>=2 会
# 误授审批、组管理员(5)>=3 会误授管理员。
ROLE_TOOLS: dict[int, frozenset[str]] = {
    0: frozenset(),                                          # 非平台用户：无
    1: _QUERY_BOOK_TOOLS | _HELPER_TOOLS,                    # 工程师
    2: _QUERY_BOOK_TOOLS | _HELPER_TOOLS | _APPROVAL_TOOLS,  # 调度员
    3: ALL_TOOLS,                                            # 管理员
    4: _HELPER_TOOLS,                                        # 司机（fmp 无约车菜单）
    5: _QUERY_BOOK_TOOLS | _HELPER_TOOLS | _APPROVAL_TOOLS,  # 组管理员 ≈ 调度员
}


def role_allows(role: int, tool_name: str) -> bool:
    """纯角色判定：该角色能否调用此工具（调用方已解析出 role 时用，如 fast_path）。

    未注册工具 → 拒绝（防 LLM 编造工具名）；role 不在表中（异常值）→ 拒绝。
    """
    if tool_name not in ALL_TOOLS:
        return False
    return tool_name in ROLE_TOOLS.get(role, frozenset())


def is_tool_permitted(open_id: str, tool_name: str) -> bool:
    """判断当前用户是否能调用此工具（L1 feishu_acl 插件 / L2 guarded 用）。

    判定流程：
    1. 工具未注册 → 拒绝（防 LLM 编造工具名）
    2. 无身份（open_id 为空）→ 通过（内部 / 系统调用，跳过门控）
    3. 解析角色（identity_map；陌生人 = role 0 → 空工具集）→ 查 ROLE_TOOLS

    注意：本函数抛异常时调用方（feishu_acl 插件 / guarded()）应 fail-open 兜底。
    """
    if tool_name not in ALL_TOOLS:
        return False
    if not open_id:
        return True
    try:
        role = identity.role_of(open_id)
    except Exception as e:
        log.warning("permission_check_failed tool=%s user=%s err=%s", tool_name, open_id, e)
        return False
    return role_allows(role, tool_name)
