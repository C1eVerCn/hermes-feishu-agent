"""bot/intent — 意图识别的**单一事实源**（Single Source of Truth）。

历史问题（用户反馈"快速路径正则 vs FSM 意图识别两套并存，容易漂移"）：
意图逻辑曾散落 6 处，互相重叠且各自漂移——

- handler: ``_ESCAPE_PHRASES`` / ``BOOKING_INTENT`` / ``_FAST_PATH_PATTERNS`` /
  ``_match_query_intent_during_fsm`` / ``is_vehicle_id`` / ``_TYPE_KEYWORDS``
- car_booking_fsm: ``ESCAPE_PHRASES`` / ``BOOKING_INTENT_PHRASES`` /
  ``_looks_like_vehicle_id`` / ``_VEHICLE_NO_RE``

合并时发现的 3 个真实漂移 BUG（本模块已修正）：

1. 两处 escape 短语集合**不一致**（handler 有"换个/不订了/不要了"，fsm 有
   "退出/不约了/不选了"）→ 这里取**并集**。
2. 两处 booking 短语集合**不一致** → 这里统一为 :data:`BOOKING_PHRASES`。
3. handler 的 ``_has_intent`` 正则写成 ``r"[\\\\s\\\\S]{0,12}"``（双反斜杠，
   只能匹配 {反斜杠,s,S}，几乎等于"verb 紧贴 vehicle word"），而测试却用单反斜杠
   重新实现——**线上代码与测试脱节**。这里用正确的 ``r"[\\s\\S]{0,12}"``。
   同时 handler 的 ``is_vehicle_id`` 缺"必须含数字"约束（fsm 有），这里统一要求含数字。

两档路由（two-tier routing，2026-06-25 redesign）：

- **Tier 1（本模块的确定性函数）**：零歧义/高频的精确模式。规则"又笨又精确"，
  不再追逐自然语言多样性（那是 Tier 2 的活），因此**不再漂移**。
- **Tier 2（:mod:`bot.intent_router`，LLM 结构化分类）**：Tier 1 未命中的
  "措辞多样/表达有问题"的消息交给大模型理解 + 抽槽位。规则层不再为覆盖口语化变体
  无限加正则。

handler 与 car_booking_fsm 都从本模块导入，不再各持一份。
"""
import re

# ── 文本归一化 ───────────────────────────────────────────────────────────
_QUOTE_CHARS = "「」『』[]\"'"


def _norm(text: str) -> str:
    """去首尾空白 + 去包裹引号 + 小写（用于精确短语匹配）。"""
    return (text or "").strip().strip(_QUOTE_CHARS).lower()


# ── escape / confirm（FSM 中途退出 / 文本确认）─────────────────────────────
# 并集：handler._ESCAPE_PHRASES ∪ fsm.ESCAPE_PHRASES（修正漂移 BUG #1）。
ESCAPE_PHRASES: tuple[str, ...] = (
    "算了", "取消", "退出", "不约了", "不订了", "放弃", "不选了", "换个", "不要了",
)
CONFIRM_PHRASES: tuple[str, ...] = ("确认", "确定", "ok", "yes", "yep", "yeah")


def is_escape(text: str) -> bool:
    """精确命中 escape 短语（归一化后整句匹配，避免误伤含"取消"的长句）。"""
    return _norm(text) in ESCAPE_PHRASES


def is_confirm(text: str) -> bool:
    return _norm(text) in CONFIRM_PHRASES


# ── 车型 / 平台关键字（fast-path 与 booking 意图共用白名单）──────────────────
TYPE_KEYWORDS: tuple[str, ...] = (
    # 车辆类型（车型）
    "DM2", "CT1", "BM2", "CM0", "大F车", "小F车", "中F车",
    # 平台（芯片）
    "Xavier", "ADCU", "Orin", "Thor",
    # 英文拼写
    "大Fcar",
)


def is_type_keyword(text: str) -> bool:
    return (text or "").strip() in TYPE_KEYWORDS


# ── 车辆编号识别 ──────────────────────────────────────────────────────────
# 规则（统一自 fsm._looks_like_vehicle_id，stricter 版本）：
#   1) 5-20 字符；2) 以字母或汉字开头；3) **含至少 1 个数字**；
#   4) 全是字母/数字/汉字（无标点空格）。
# 实际编号形态多样：苏EAM0769 / AATI25SNV630 / TTVX25SPV009 / I23SVV021。
VEHICLE_NO_RE = re.compile(r"^[A-Za-z一-鿿][A-Za-z0-9一-鿿]{4,19}$")
# token 切分符（句中嵌入编号的提取）
_TOKEN_SPLIT_RE = re.compile(r"[\s,，.。!！?？;；]+")


def looks_like_vehicle_id(s: str) -> bool:
    """宽松校验单个 token 是否像车辆编号（含数字约束，修正漂移 BUG #3）。"""
    s = (s or "").strip()
    if not s or len(s) < 5 or len(s) > 20:
        return False
    if not VEHICLE_NO_RE.match(s):
        return False
    return any(c.isdigit() for c in s)


def is_pure_vehicle_id(text: str) -> bool:
    """整句就是一个车辆编号（大小写不敏感）。"""
    return looks_like_vehicle_id((text or "").strip().upper())


def extract_embedded_vehicle_id(text: str) -> str:
    """从句中提取首个像车辆编号的 token（"我想约一下TTVX25SPV009"→"TTVX25SPV009"），无则 ""。

    与 FSM START 状态一致：对每个 token 先剥离中文前缀（"约PNV332"→"PNV332"），
    剥离后或整体是合法编号即返回。必须含数字（否则纯中文如"看下我的待审批记录"
    会误匹配 5-20 字符的中文串）。
    """
    for token in _TOKEN_SPLIT_RE.split(text or ""):
        if not token:
            continue
        upper = token.upper()
        stripped = upper
        while stripped and '一' <= stripped[0] <= '鿿':
            stripped = stripped[1:]  # 剥中文前缀
        if stripped and looks_like_vehicle_id(stripped):
            return stripped
        if looks_like_vehicle_id(upper):
            return upper
    return ""


def has_embedded_vehicle_id(text: str) -> bool:
    return bool(extract_embedded_vehicle_id(text))


# ── 约车意图（Tier 1 启发式：精确短语 + verb+vehicle 正则 + 否定守卫）─────────
# 并集化的 canonical 短语（修正漂移 BUG #2）。
BOOKING_PHRASES: tuple[str, ...] = (
    "我想约车", "我要约车", "帮我约车", "约车", "预约车",
    "帮我预约", "我想预约",
    "我现在想约车", "我想约一辆", "我要约一辆",
    "想约车", "想约一辆车", "要约车", "约个车",
    "想预约车", "想预约一辆车",
)
# FSM 全局重置用（用户在流程中途又说"约车"→ 回入口卡）。复用同一份 canonical 集。
BOOKING_RESET_PHRASES = BOOKING_PHRASES

_INTENT_VERB = r"(约|预约|预定|book|schedule|預約|預定)"
_VEHICLE_WORD = r"(车|车辆|vehicle|car|辆\s*车|辆)"
# 修正：单反斜杠 [\s\S]{0,12} = "verb 与 vehicle word 间任意 0-12 字符"。
_HAS_INTENT_RE = re.compile(_INTENT_VERB + r"[\s\S]{0,12}" + _VEHICLE_WORD)
# 否定守卫：前 8 字符内出现否定词 → 视为非约车意图。
_NEGATION_RE = re.compile(r"不|没|别|无需|算了|取消|不要了")

# 动作守卫：句中出现 cancel/return/approve 的动作词时，**不**判为 booking
# （即使句中含车辆编号）。这些属于其它意图域，交 Tier-2 LLM 分类，避免
# "把 PNV332 的预约取消掉" 因含编号被 embedded-id 规则误判成约车。
_ACTION_BLOCK_RE = re.compile(r"取消|退订|退还|归还|还车|审批|批准|驳回|拒绝")

# 还车意图（Tier-1）：含 还车/归还/交车 且无否定、非查询。"归还记录/查询"等归类为查询。
_RETURN_RE = re.compile(r"还车|归还|交车")
_QUERY_HINT_RE = re.compile(r"记录|查询|查看|列表|历史|查一下|看一下|有哪些")


def is_return_intent(text: str) -> bool:
    """Tier-1 还车意图：含 还车/归还/交车，无否定、非查询语句。"""
    norm = (text or "").strip()
    if not norm:
        return False
    if not _RETURN_RE.search(norm):
        return False
    if _NEGATION_RE.search(norm[:8]) or _QUERY_HINT_RE.search(norm):
        return False
    return True


def has_booking_verb(text: str) -> bool:
    """verb+vehicle 模式命中且无前置否定。"""
    norm = (text or "").strip()
    if not norm:
        return False
    return bool(_HAS_INTENT_RE.search(norm)) and not bool(_NEGATION_RE.search(norm[:8]))


def is_booking_intent(text: str) -> bool:
    """Tier 1 约车意图综合判定（单一事实源；handler 与 fsm 共用）。

    命中任一：canonical 短语 / "我要约"|"帮我约"前缀 / verb+vehicle 正则(无否定) /
    句中含车辆编号 / 整句是车型平台关键字 / 整句是车辆编号。
    但若句中含 cancel/return/approve 动作词（取消/归还/审批…），一律不判为 booking。
    """
    norm = (text or "").strip()
    if not norm:
        return False
    if _ACTION_BLOCK_RE.search(norm):
        return False
    return (
        norm in BOOKING_PHRASES
        or norm.startswith(("我要约", "帮我约"))
        or has_booking_verb(norm)
        or has_embedded_vehicle_id(norm)
        or is_type_keyword(norm)
        or is_pure_vehicle_id(norm)
    )


# ── 查询类快速路径（确定性：精确短语 → 工具名 + 参数）──────────────────────
def _empty_args(m: "re.Match") -> dict:
    return {}


def _args_with_type(m: "re.Match") -> dict:
    if m.lastindex and m.group(1):
        return {"vehicleType": m.group(1).strip()}
    return {}


# (pattern, tool_name, args_fn)。单一事实源：handler 的 fast-path 与 in-FSM escape
# 都复用这份 pattern。新增查询工具只需在此处加一行，两处行为自动同步。
QUERY_PATTERNS: list[tuple["re.Pattern", str, "callable"]] = [
    # ── fetch_available_vehicles ──
    (re.compile(r'^(查询|查看|看看|有什么|列出|看|查)(\s*(所有|可用))?\s*(车辆|车)(\s*(列表|号))?[\s!！。.]*$'),
     "fetch_available_vehicles", _empty_args),
    (re.compile(r'^车辆(\s*(列表))?[\s!！。.]*$'),
     "fetch_available_vehicles", _empty_args),
    # 带车型/平台过滤（用白名单匹配，避免误匹配通用短语）
    (re.compile(
        r'^(?:现在|查|看|查询)?\s*(' + "|".join(re.escape(k) for k in TYPE_KEYWORDS) + r')\s*'
        r'(?:有(?:什么|哪些))?\s*车\s*(?:可以)?\s*(?:约|查询|看)?\s*[\s!！。.]*$'),
     "fetch_available_vehicles", _args_with_type),

    # ── fetch_user_reservation ──
    (re.compile(r'^(查询|查看|查一下|查|看看|看下|帮我查|帮我看|查询一下|看一下|看看我的)?\s*(一下\s*)?我的\s*(预约记录|预约|所有预约|预约历史|约车记录)[\s!！。.]*$'),
     "fetch_user_reservation", _empty_args),

    # ── fetch_user_approval ──
    (re.compile(r'^(查询|查看|查一下|查|看看|看下|帮我查|帮我看|查询一下|看一下|看看我的)?\s*(一下\s*)?我的\s*(待审批列表|待审批|待我审批|审批列表|审批记录|待审批记录|审批)[\s!！。.]*$'),
     "fetch_user_approval", _empty_args),
]

# 在 FSM 挂起状态下输入查询语句 → 智能 escape（车辆查询除外，用户可能想进 booking）。
_FSM_ESCAPE_EXEMPT = ("fetch_available_vehicles",)


def match_query(text: str) -> tuple[str, dict, "re.Match"] | None:
    """整句精确匹配查询模式（fast-path 用 .match()）。返回 (tool, args, match) 或 None。"""
    norm = (text or "").strip()
    if not norm:
        return None
    for pattern, tool_name, args_fn in QUERY_PATTERNS:
        m = pattern.match(norm)
        if m:
            return tool_name, args_fn(m), m
    return None


def match_query_intent_during_fsm(text: str) -> bool:
    """用户在 FSM 挂起状态时输入查询类语句（除车辆查询外）→ True，让 handler 清状态走 fast-path。"""
    norm = (text or "").strip()
    if not norm:
        return False
    for pattern, tool_name, _args_fn in QUERY_PATTERNS:
        if tool_name in _FSM_ESCAPE_EXEMPT:
            continue
        if pattern.search(norm):
            return True
    return False
