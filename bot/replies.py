"""bot/replies — 无 agent 的确定性文案回复 + 身份/管理命令。

从 handler 拆出（2026-06-25 重构）。三类：
1. 闲聊/问候/帮助等"即时文案"（:func:`match_simple_intent`，正则精确匹配，<1ms）
2. 身份查询（"我的权限"）→ :func:`handle_identity_query`
3. 管理员命令（"设置角色"/"查看用户"）→ :func:`handle_admin_command`

外加 agent 路径用的身份前导词 :func:`identity_preamble`。
意图短语本身收口在 :mod:`bot.intent`；本模块只负责"识别后给什么文案"。
"""
import re

from config.settings import settings
from ocl import identity
from bot.identity_admin import get_admin as get_identity_admin

# ── Layer 0: 即时文案（问候/感谢/帮助/能力介绍）──────────────────────────────
_SIMPLE_REPLIES: list[tuple[re.Pattern, str]] = [
    (re.compile(r'^(你好|hi|hello|hey|嗨|哈[啰咯]|早上好|下午好|晚上好|good\s*morning|good\s*afternoon|good\s*evening)[\s!！。.]*$', re.IGNORECASE),
     "你好！我是约车助手，专注于车辆预约管理（查询 / 预约 / 取消 / 归还 / 审批）。\n输入「帮助」了解我能做什么。"),
    (re.compile(r'^(谢谢|感谢|thanks|thank\s*you|3q|多谢|谢了|辛苦)[\s!！。.]*$', re.IGNORECASE),
     "不客气！有需要随时找我。"),
    (re.compile(r'^(再见|bye|拜拜|88|回见|下次聊)[\s!！。.]*$', re.IGNORECASE),
     "再见！有需要随时找我。"),
    (re.compile(r'^(在吗|在不在|在线吗)[\s!！。.?？]*$'),
     "在的！有什么可以帮您的？"),
    (re.compile(r'^(你是谁|你叫什么|你是做什么的|你能做什么|你能帮我做什么|你能帮我吗|你能干什么|介绍一下你自己)[\s!！。.?？]*$'),
     "我是约车助手，可以帮您：\n• 查询可用车辆\n• 预约 / 取消 / 归还车辆\n• 查询我的预约\n• （调度员/管理员）审批预约、查询待审批\n\n输入「我的权限」查看当前角色。"),
    (re.compile(r'^(帮助|help|怎么用|怎么操作|使用说明|功能)[\s!！。.?？]*$', re.IGNORECASE),
     "📋 我能帮你做的事情：\n\n🔍 查询类\n• 查询可用车辆（指定时间 + 平台 + 类型）\n• 查询我的预约\n• （调度员）查询待审批列表\n\n✏️ 操作类\n• 预约车辆（两步流程：选车 → 确认）\n• 取消待审批的预约\n• 归还已批准的车辆\n\n🛡️ 调度员/管理员\n• 审批预约\n\n💡 输入「我的权限」查看当前角色"),
    (re.compile(r'^(好[的]?|ok|嗯|哦|知道了|明白了|懂了|收到|了解|got\s*it)[\s!！。.]*$', re.IGNORECASE),
     "好的，有问题随时找我。"),
]

_MY_PERMS = re.compile(r'我的权限|查看.*权限|我的角色')
_ADMIN_SET_ROLE = re.compile(r'^设置角色\s+(\S+)\s+([1-5])$')
_ADMIN_SET_MOBILE = re.compile(r'^(?:设置手机|绑定手机)\s+(\S+)\s+([\d\- ]{6,20})$')
_ADMIN_LIST_USERS = re.compile(r'^查看用户(?:\s+(\S+))?$')

# 角色名 / 能力 与 fmp 后端 sys_role 对齐（1 工程师 / 2 调度员 / 3 管理员 / 4 司机 / 5 组管理员）
_ROLE_NAME = {0: "非平台用户", 1: "工程师", 2: "调度员", 3: "管理员", 4: "司机", 5: "组管理员"}
_ROLE_BY_NAME = {
    "待审核": 0, "非平台用户": 0,
    "工程师": 1, "普通用户": 1,   # 普通用户=工程师别名（兼容旧称）
    "调度员": 2, "管理员": 3, "司机": 4, "组管理员": 5,
}
_ROLE_CAPS = {
    1: "可查询可用车辆、预约/取消/归还车辆、查询自己的预约记录。",
    2: "在工程师基础上，可审批本组车辆预约、查询本组待审批列表。",
    3: "拥有全部权限（含跨组审批等系统级操作）。",
    4: "司机角色：约车/审批流程不对司机开放（如需约车请联系管理员调整角色）。",
    5: "组管理员：可约车、审批本组车辆预约（车组/人员管理在 Web 端进行）。",
}


def match_simple_intent(text: str) -> str:
    """命中即时文案则返回该文案，否则返回 ""。"""
    for pattern, reply in _SIMPLE_REPLIES:
        if pattern.search(text):
            return reply
    return ""


# ── 管理员判定 / env 提权 ─────────────────────────────────────────────────
def admin_ids() -> set[str]:
    raw = getattr(settings, "OCL_ADMIN_USER_IDS", "")
    return {uid.strip() for uid in raw.split(",") if uid.strip()}


def is_admin(user_id: str) -> bool:
    return user_id in admin_ids() or identity.role_of(user_id) == 3


def resolve_role_with_env_admin(admin, user_id: str, role: int) -> int:
    """OCL_ADMIN_USER_IDS 中的用户自动提到管理员（role=3）。

    用 ``role != 3`` 而非 ``role < 3``：角色已非线性（司机=4/组管理员=5 数值大于 3 但
    并非管理员），凡在白名单且尚非管理员者一律提到 3。
    """
    if role != 3 and user_id in admin_ids():
        admin.set_role(user_id, 3, operator="ocl_admin_env",
                       note="auto-elevated from OCL_ADMIN_USER_IDS")
        return 3
    return role


# ── agent 路径身份前导词 ──────────────────────────────────────────────────
def identity_preamble(user_id: str, role: int, name: str) -> str:
    role_name = _ROLE_NAME.get(role, "未知")
    caps = _ROLE_CAPS.get(role, "")
    who = f"当前对话用户：{name}（角色：{role_name}，role={role}）。" if name \
        else f"当前对话用户角色：{role_name}（role={role}）。"
    return (
        "［系统已核验的用户身份，以此为准，不要自行推断或质疑］\n"
        f"{who}\n"
        f"权限范围：{caps}\n"
        "回答涉及「你是谁/我的权限/我能做什么」时，必须依据上述角色，"
        "不得默认对方是工程师/普通用户。\n"
        "［领域边界 — 2026-06-25］你是车辆预约管理机器人，**只**处理车辆预约/"
        "审批/归还/记录查询等业务。对股票/天气/纳指/聊天/其他无关问题，"
        "请礼貌拒绝并引导用户：「我仅能帮您处理车辆预约相关需求，请试试约车、"
        "查询预约、查询待审批等功能」。\n"
        "———\n"
        "用户消息："
    )


# ── 身份查询（"我的权限"）──────────────────────────────────────────────────
def handle_identity_query(text: str, user_id: str) -> str:
    """命中身份查询正则则返回回复文案，否则返回 ""（Tier-1 精确路径）。"""
    if not _MY_PERMS.search(text):
        return ""
    return identity_reply(user_id)


def identity_reply(user_id: str) -> str:
    """无条件生成基于角色的"我的权限"回复（供 Tier-1 正则路径与 Tier-2 路由器共用）。"""
    admin = get_identity_admin()
    role = admin.get_role(user_id)
    if role == 0:
        return (f"您当前是【待审核】用户，无平台权限。\n"
                f"请联系管理员开通，并提供您的 open_id：\n"
                f"{user_id}")
    role_name = _ROLE_NAME.get(role, "未知")
    caps = _ROLE_CAPS.get(role, "")
    upgrade_hint = ""
    if role == 1:
        upgrade_hint = ("\n\n💡 如需审批/管理权限：\n"
                        "  • 联系管理员让其执行「设置角色 <你的 open_id> 2」\n"
                        "  • 或在 .env 加 OCL_ADMIN_USER_IDS=<你的 open_id> 自动升级到管理员\n"
                        "  • 注意：MCP 端设置的角色不会自动同步到这里，需要在本系统独立设置")
    elif role == 2:
        upgrade_hint = ("\n\n💡 如需管理员权限：联系现有管理员执行「设置角色 <你的 open_id> 3」")
    elif role == 4:
        upgrade_hint = ("\n\n💡 如需约车权限：联系管理员调整角色（设置角色 <你的 open_id> 1）")
    return f"您是【{role_name}】。\n{caps}{upgrade_hint}"


# ── 管理员命令（"设置角色"/"查看用户"）─────────────────────────────────────
def handle_admin_command(text: str, user_id: str) -> str:
    """命中管理员命令则返回回复文案，否则返回 ""（含非管理员调用）。"""
    if not is_admin(user_id):
        return ""
    m = _ADMIN_SET_ROLE.match(text)
    if m:
        target, role = m.group(1), int(m.group(2))
        admin = get_identity_admin()
        ok, msg = admin.set_role(target, role, operator=user_id, note="via_feishu_admin_command")
        if not ok:
            return f"设置失败：{msg}"
        return f"已设置 {target} 的角色为 {_ROLE_NAME[role]}。"
    m = _ADMIN_SET_MOBILE.match(text)
    if m:
        target = m.group(1)
        mobile = re.sub(r"[\s\-]", "", m.group(2))  # 归一化：去空格/连字符
        admin = get_identity_admin()
        if admin.get(target) is None:
            admin.auto_register(target, mobile=mobile)
        else:
            admin.update_profile(target, mobile=mobile, operator=user_id)
        return f"已设置 {target} 的手机号为 {mobile}。"
    m = _ADMIN_LIST_USERS.match(text)
    if m:
        return _format_user_list(m.group(1))
    return ""


def _format_user_detail(oid: str, rec: dict) -> str:
    return (f"用户 {oid}：\n"
            f"• 角色：{_ROLE_NAME.get(int(rec.get('role', 0)), '未知')}\n"
            f"• 姓名：{rec.get('name', '') or '(未知)'}\n"
            f"• 邮箱：{rec.get('email', '') or '(未知)'}\n"
            f"• 手机：{rec.get('mobile', '') or '(未知)'}\n"
            f"• 建档方式：{rec.get('registered_via', '') or '(未知)'}")


def _format_user_list(filter_arg: str | None) -> str:
    admin = get_identity_admin()
    users = admin.list_all()
    if not users:
        return "当前没有任何用户记录。"
    # 单用户精确查：open_id / 手机号 / 邮箱（手机号、邮箱均为识别符）
    if filter_arg:
        if filter_arg in users:
            return _format_user_detail(filter_arg, users[filter_arg])
        hit = admin.find_by_mobile(filter_arg) or admin.find_by_email(filter_arg)
        if hit:
            return _format_user_detail(hit[0], hit[1])
    role_filter = _ROLE_BY_NAME.get(filter_arg) if filter_arg else None
    if filter_arg and role_filter is None:
        return (f"未找到用户或角色「{filter_arg}」。\n"
                "用法：「查看用户」全部 / 「查看用户 调度员」按角色 / "
                "「查看用户 ou_xxx」按 open_id / 「查看用户 138xxxx」按手机号 / 邮箱。")
    by_role: dict[int, list[str]] = {}
    for oid, rec in users.items():
        r = int(rec.get("role", 0))
        if role_filter is not None and r != role_filter:
            continue
        ident = rec.get("email", "") or rec.get("mobile", "") or "(无邮箱/手机)"
        by_role.setdefault(r, []).append(
            f"  • {rec.get('name', '') or '(无名)'} | {ident} | {oid}")
    total = sum(len(v) for v in by_role.values())
    header = f"📋 用户列表（共 {total} 人）" + (f"，筛选：{filter_arg}" if filter_arg else "")
    lines: list[str] = [header]
    # 展示顺序覆盖全部 6 档（管理员→组管理员→调度员→工程师→司机→待审核）；
    # 未知角色（如未来扩展）兜底追加，避免静默丢弃用户。
    display_order = [3, 5, 2, 1, 4, 0]
    for r in display_order + [r for r in sorted(by_role) if r not in display_order]:
        if by_role.get(r):
            lines.append(f"\n【{_ROLE_NAME.get(r, f'role{r}')}】{len(by_role[r])} 人")
            lines.extend(by_role[r])
    return "\n".join(lines)
