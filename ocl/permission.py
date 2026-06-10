"""DMZ智能体的工具 ACL（访问控制列表）。

按用户角色（1=普通用户 /2=调度员 /3=管理员）控制哪些 LLM工具可达。

设计要点：
- L1（hermes插件 pre_tool_call钩子）=硬拦截
- L2（ocl.tool_guard.guarded()包裹）=兜底
-真实业务权限仍由后端按 emailAddress /角色校验（mock_api模拟、台架预约走真实9013、VLM走真实9014）
- 本地只做"工具能否被 LLM调起"的粗粒度门控；细粒度由后端负责

角色映射（与各业务域接口文档一致）：
- role=1 普通用户：查询 + 自己预约
- role=2调度员：+审批、查看本组 + 数据导出
- role=3管理员：+跨组审批 + 系统级操作（同步、触发等）
"""
import logging

from ocl import identity

log = logging.getLogger(__name__)


# ──工具 →所需最低角色（1=普通 /2=调度员 /3=管理员） ────────────────────
TOOL_MIN_ROLE: dict[str, int] = {
 # ── 台架预约（mock_api模拟） ──
 "list_architectures":1,
 "list_available_benches":1,
 "reserve_bench":1,
 "cancel_reservation":1,
 "return_bench":1,
 "list_my_reservations":1,
 "approve_reservation":2, #调度员/管理员审批
 "list_my_approvals":2, #调度员/管理员查看待审批

 # ── VLM精标（真实 dmz-ess-vlm API） ──
 #纯查询类：普通用户可用
 "list_event_names":1,
 "list_camera_types":1,
 "list_bags":1,
 "get_bag":1,
 "list_frames":1,
 "get_frame":1,
 "playback_bag":1,
 # 数据导出（可能下载大量数据）：调度员起
 "download_bag_metadata":2,
 "frame_image_url":2,
 #同步/系统控制：仅管理员
 "sync_execute":3,
 "trigger_sync_async":3,
 "sync_status":3,
}


def is_tool_permitted(open_id: str, tool_name: str) -> bool:
    """判断当前用户是否能调用此工具。
    
    判定流程：
    1.工具未注册 → 直接拒绝（防 LLM 编造工具名）
    2. 用户无身份（open_id 为空）→ 通过（内部 / 系统调用，跳过门控）
    3. 用户在 identity_map 中无角色 →视为 role=0（陌生人），拒绝
    4. 用户角色 ≥ TOOL_MIN_ROLE[tool_name] → 通过
    5. 否则 →拒绝
    
    注意：本函数抛异常时调用方（feishu_acl插件 / guarded()）应 fail-open兜底。
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

