"""bot/agent_pool._FEISHU_SYSTEM_PROMPT_BASE + skill/car-booking/SKILL.md 单元测试。

2026-06-30 改造：system prompt 瘦身到只剩身份 + 工具列表（~150 token）；
字段枚举 / 流程 / 闲聊应对 / 查不到车的处理 → 全部在 skill 文件里。
本测试同时检查 system prompt（轻量、不变信息）+ skill 文件（操作知识）。
"""
from bot import agent_pool
from bot.skills import load_skill


def _prompt():
    return agent_pool._FEISHU_SYSTEM_PROMPT_BASE


def _skill():
    return load_skill("car-booking") or ""


# ── system prompt：只放身份 + 工具 + 不变量 ───────────────────────────

def test_prompt_mentions_tool_list():
    """系统提示必须列出可用工具（OCL 门控过的）。"""
    p = _prompt()
    for tool in ("fetch_available_vehicles", "_dry_run_vehicle_reservation",
                 "_commit_vehicle_reservation", "cancel_vehicle_reservation",
                 "return_vehicle", "fetch_user_reservation",
                 "approval_vehicle_reservation", "fetch_user_approval",
                 "get_user_context", "get_common_dictionary"):
        assert tool in p, f"system prompt 缺失工具: {tool}"


def test_prompt_documents_invariants():
    """系统提示必须强调服务端注入 / 不编造 / 单轮 ≤2 工具调用。"""
    p = _prompt()
    for kw in ("emailAddress", "openId", "mobile"):
        assert kw in p, f"不变量缺失: {kw}"
    assert "编造" in p  # v3 改用"绝不编造"
    assert "2 次" in p or "单轮" in p


def test_prompt_does_not_hide_commit():
    """commit 工具对 LLM 可见（2026-06-30 Phase 0.3 + 1.4 改造）。"""
    p = _prompt()
    assert "看不到" not in p
    assert "对 LLM 不可见" not in p


def test_prompt_keeps_reply_style_brief():
    """回复风格简短（≤200 字）+ 卡片渲染交给系统。"""
    s = _skill()
    p = _prompt()
    # 200 字回复限制搬到 skill（操作知识）；prompt 提到卡片
    assert "200" in s, "200 字限制在 skill 里"
    assert "卡片" in p or "卡片" in s


# ── skill：操作知识（字段 / 流程 / 应对） ─────────────────────────────

def test_skill_lists_booking_required_fields():
    s = _skill()
    for kw in ("vehicleNo", "vehicleType", "platform",
               "startTime", "endTime", "taskName", "location"):
        assert kw in s, f"skill 缺失约车必填字段: {kw}"


def test_skill_lists_return_required_fields():
    s = _skill()
    for kw in ("returnLocation", "keyPosition", "changeModule", "vehicleStatus"):
        assert kw in s, f"skill 缺失还车必填字段: {kw}"


def test_skill_includes_platform_enum():
    s = _skill()
    for plat in ("Xavier", "ADCU", "Orin", "Thor"):
        assert plat in s, f"skill 缺失平台枚举: {plat}"


def test_skill_includes_time_format():
    s = _skill()
    assert "yyyy-MM-dd HH:mm" in s


def test_skill_documents_dry_run_commit_contract():
    s = _skill()
    assert "_dry_run_vehicle_reservation" in s
    assert "_commit_vehicle_reservation" in s
    # v2 skill 触发词精简为 5 个核心词
    for kw in ("确认", "好", "可以", "ok", "对"):
        assert kw in s, f"skill 缺失确认触发词: {kw}"


def test_skill_handles_chitchat():
    s = _skill()
    assert "闲聊" in s
    assert "1+1" in s or "天气" in s


def test_skill_handles_zero_vehicles():
    s = _skill()
    # v2 skill 用"0 辆"或"没有"等更短描述
    assert "0 辆" in s or "没有" in s or "返回" in s, "skill 应说明 0 辆场景"
    # 引导用户提供车辆编号的兜底
    assert "车辆编号" in s


def test_skill_mentions_vehicle_no_format():
    s = _skill()
    assert "PNV" in s or "SVV" in s


def test_skill_mentions_status_code_coercion():
    s = _skill()
    # return_vehicle 的 vehicleStatus 中文/数字互转
    assert "可用" in s and "故障" in s
    assert "维保" in s and "报废" in s


# ── YAML frontmatter（hermes skill 协议） ──────────────────────────────

def test_skill_has_yaml_frontmatter():
    """hermes skill 协议要求文件以 --- 开头的 YAML frontmatter 开头。"""
    s = _skill()
    assert s.startswith("---"), "skill 必须以 --- 开头的 YAML frontmatter 开头"
    # 必须含 name / description（hermes 必需字段）
    assert "name: car-booking" in s
    assert "description:" in s


def test_prompt_no_longer_bakes_static_time():
    """时间已移到每轮 preamble，常驻 system prompt 不再含时间占位/写死时间。"""
    p = _prompt()
    assert "__NOW_CN__" not in p
    assert "当前时间" not in p


def test_prompt_permits_one_bounded_clarification():
    """放松后的规则 #1：意图可推断直接查；仅发散/缺关键信息时最多问一个、不连问。"""
    p = _prompt()
    assert "可推断" in p
    assert "最多问一个" in p or "最多一个" in p
    assert "不得连问" in p or "第二次" in p


def test_skill_core_principle_permits_clarification():
    s = _skill()
    assert "最多问一个" in s or "最多一个" in s
