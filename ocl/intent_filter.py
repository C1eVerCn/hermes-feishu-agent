"""OCL intent guard — 拒绝与车辆预约 无关的 LLM 闲聊回复。

设计动机：用户明确要求「连 1+1 都不回复，任何无关闲聊都需要引导到正常流程」。
system prompt 提示不靠谱（最小模型服从性差），所以在 OCL pipeline 里
(LLM 出响应之后) 再做一次硬性脱敏。

判定逻辑（双关键字）：
1. 命中【闲聊标记】（天气、算术、笑话、聊天、诗、代码、菜谱、…）
   AND
2. 不包含【领域关键词】（车辆 / 预约 / 审批 / 调度员 / 归还 / Xavier… 等）

同时满足 → 视为闲聊，替换为「我是车辆预约助手，可帮：…」引导话术。
任一不满足 → 透传（正常业务回答或正常数据回应都不会被误杀）。

短/空文本不在本模块处理（format_control 已处理）。
"""
import re
import logging
from dataclasses import dataclass

log = logging.getLogger(__name__)


# 闲聊标记：命中一个就视作「与本系统无关」的话题领域。
# 注意：用全/半角 + 常见变体；不要写太宽（如「今天」「一下」会误杀业务对话）。
_CHITCHAT_MARKERS: tuple[str, ...] = (
    "天气", "气温", "下雨", "晴天", "阴天", "刮风",
    "摄氏度", "穿什么", "出门",
    # 算术 / 数字题
    "1+1", "1 + 1", "2+2", "算一下", "等于几", "等于多少",
    "几乘以", "几加几", "几减几",
    # 聊天 / 角色扮演
    "讲笑话", "讲个笑话", "说个笑话", "笑话",
    "写一首", "作诗", "写诗", "作一首",
    "写代码", "写一段代码", "写个程序", "写函数",
    "菜谱", "怎么做", "怎么做菜", "做菜", "做饭",
    "你是谁", "你叫什么", "你几岁", "你喜欢什么", "你爱",
    "打招呼", "闲聊", "聊天",
    # 知识 / 百科
    "百科", "历史", "地理", "世界上", "什么是量子", "什么是宇宙",
)


# 领域关键词：业务回复里几乎都会出现。命中一个 = 确实是车辆预约的回复。
# 注意要包含最常见的否定场景下也安全的形式（如「我的预约」「未找到预约」）
_DOMAIN_KEYWORDS: tuple[str, ...] = (
    "车辆", "预约", "审批", "调度员", "归还", "取消预约",
    "vehicle", "platform", "reservation",
    "Xavier", "ADCU", "Orin", "Thor",
    "架构", "状态", "不可用", "待审批", "已批准", "已驳回", "已完成", "已取消",
    "任务", "地点",
    # 车业务领域专有术语
    "PNV", "SVV", "SOV", "DM2", "CT1", "大F车", "CM0", "BM2",
    "vin", "license", "licensePlate",
)


# 预编译：避免在 hot path 重复编译
_MARKER_RE = re.compile("|".join(re.escape(m) for m in _CHITCHAT_MARKERS))
_DOMAIN_RE = re.compile("|".join(re.escape(k) for k in _DOMAIN_KEYWORDS))

# 引导话术：明确告诉用户本系统能做什么，给出可复制的例句提升转化。
_REDIRECT = (
    "我是车辆预约助手，不处理与业务无关的闲聊。\n"
    "可帮你的事情：\n"
    "• 查询可用车辆\n"
    "• 预约 / 取消 / 归还车辆\n"
    "• 查询我的预约、查询待审批（调度员）\n"
    "• 审批预约（调度员/管理员）\n\n"
    "试着这样发：「查明天 9 点到 18 点 DM2 Xavier 车」「预约 PNV332，9 点到 18 点，"
    "任务是测试，地点是测试场 A 区」"
)


@dataclass
class IntentResult:
    """If redirected=True, pipeline should replace the response with _REDIRECT."""
    redirected: bool
    matched_marker: str = ""


def check(response: str) -> IntentResult:
    """检查回复是否属闲聊：命中闲聊标记 + 不含领域关键词 → 拦截。"""
    if not response:
        return IntentResult(redirected=False)

    # 含领域关键词 → 信任为业务回答
    if _DOMAIN_RE.search(response):
        return IntentResult(redirected=False)

    # 不含领域关键词 + 命中闲聊标记 → 拦截
    m = _MARKER_RE.search(response)
    if m:
        log.info("chitchat_redirected marker=%s len=%d", repr(m.group(0)), len(response))
        return IntentResult(redirected=True, matched_marker=m.group(0))

    return IntentResult(redirected=False)


REDIRECT_MESSAGE = _REDIRECT
