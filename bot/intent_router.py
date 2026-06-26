"""bot/intent_router — Tier-2 LLM 意图路由器（结构化分类）。

两档路由的第二档：Tier-1（:mod:`bot.intent` 的确定性精确模式）未命中的"措辞多样/
表达有问题"的消息，交给大模型做**一次轻量结构化分类**，输出枚举内的 intent + 槽位。

设计要点（满足"自由度但不漂移"）：
- **结构化输出**：模型只能返回枚举内的 intent（物理上无法漂成自由闲聊）。
- **抽槽位**：book 意图顺带抽 vehicle_no/车型/芯片/时段/任务/地点，用于给 FSM 播种，
  让"说全了的人"跳过前面的按钮步骤。
- **轻量**：单次 chat.completions（非 30 轮 agent loop），temperature=0，max_tokens 小。
- **fail-open**：无 API key / 超时 / 解析失败 → intent="unknown"，handler 落到完整 agent。

`_complete` 是唯一的网络出口，单测 monkeypatch 它即可（无网络）。
"""
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from config.settings import settings

log = logging.getLogger(__name__)

# 允许的 intent 枚举（模型输出必须落在此集合，否则归一化为 unknown）
ALLOWED_INTENTS = frozenset({
    "book", "query_vehicles", "query_reservations", "query_approvals",
    "cancel", "return", "approve", "identity", "chitchat", "unknown",
})
# 各 intent 允许抽取的槽位键（其余键丢弃，防止模型乱塞）
_BOOK_SLOT_KEYS = frozenset({
    "vehicle_no", "vehicle_type_detail", "platform", "duration_minutes",
    "start_time", "end_time", "task_name", "location",
})
# cancel/return/approve 是 mutation：需要识别符（vehicle_no / reservation_id）等
_MUTATION_SLOT_KEYS = frozenset({
    "vehicle_no", "reservation_id", "approved", "review_comment",
})
_SLOT_KEYS_BY_INTENT = {
    "book": _BOOK_SLOT_KEYS,
    "cancel": _MUTATION_SLOT_KEYS,
    "return": _MUTATION_SLOT_KEYS,
    "approve": _MUTATION_SLOT_KEYS,
}
_VALID_PLATFORMS = {"Xavier", "ADCU", "Orin", "Thor"}

# approved 文本 → bool 归一化
_APPROVED_TRUE = {"true", "1", "批准", "同意", "通过", "yes", "approve", "approved", "ok"}
_APPROVED_FALSE = {"false", "0", "2", "拒绝", "驳回", "不同意", "否", "no", "reject", "rejected"}

# 单次分类调用的硬超时（秒）。路由器在 consumer 线程同步执行，超时要短，
# 避免某条消息长时间占住串行消费管线（Minimax 健康时实测 ~1-2s）。
_TIMEOUT_SECONDS = 6


@dataclass
class RouteResult:
    intent: str = "unknown"
    slots: dict = field(default_factory=dict)
    confidence: float = 0.0
    reason: str = ""

    @property
    def is_confident(self) -> bool:
        return self.intent != "unknown" and self.confidence >= 0.6


def _now_cn() -> str:
    now = datetime.now()  # 容器 TZ=Asia/Shanghai → 已是北京时间
    weekdays = ["一", "二", "三", "四", "五", "六", "日"]
    return now.strftime("%Y-%m-%d %H:%M:%S") + f" (周{weekdays[now.weekday()]})"


def _system_prompt() -> str:
    return (
        "你是车辆预约助手的意图分类器。只输出一个 JSON 对象，不要任何解释、不要代码块。\n"
        f"当前时间：{_now_cn()}（据此把「今天/明天/后天」换算为 yyyy-MM-dd）。\n\n"
        "判断用户消息的 intent（必须是下列之一）：\n"
        '- "book"：想预约车辆（约车/预约，或报了车辆编号/车型/芯片/时段/任务/地点）\n'
        '- "query_vehicles"：查询有哪些可用车辆\n'
        '- "query_reservations"：查询自己的预约记录\n'
        '- "query_approvals"：查询待审批列表\n'
        '- "cancel"：取消预约\n'
        '- "return"：归还车辆\n'
        '- "approve"：审批某个预约\n'
        '- "identity"：询问自己的角色/权限/能做什么\n'
        '- "chitchat"：与车辆预约无关（天气/算术/闲聊/写代码等）\n'
        '- "unknown"：无法判断\n\n'
        "仅当 intent=book 时，尽量抽取已明确提到的 slots（用户没说的**不要**填、不要编造）：\n"
        "  vehicle_no(车辆编号 如 PNV332/苏EAM0769), vehicle_type_detail(车型 如 DM2/CT1/BM0),\n"
        "  platform(芯片，仅 Xavier/ADCU/Orin/Thor), duration_minutes(整数分钟),\n"
        "  start_time/end_time(yyyy-MM-dd HH:mm), task_name(任务), location(地点)\n"
        "intent=cancel/return 时抽：vehicle_no(车辆编号) 或 reservation_id(预约号)。\n"
        "intent=approve 时抽：vehicle_no 或 reservation_id，外加 approved(布尔：批准=true / 拒绝=false)、"
        "review_comment(审批意见，可选)。\n\n"
        'confidence 是 0~1 的浮点，表示对 intent 判断的把握。\n'
        '输出示例：{"intent":"book","slots":{"vehicle_no":"PNV332","duration_minutes":120},"confidence":0.9}'
    )


def _complete(messages: list) -> str:
    """唯一网络出口：调 Minimax（OpenAI 兼容）chat.completions，返回 content 文本。

    单测 monkeypatch 本函数即可完全离线。任何异常都向上抛，由 classify 兜底。
    """
    from openai import OpenAI
    client = OpenAI(api_key=settings.MINIMAX_API_KEY, base_url=settings.MINIMAX_BASE_URL)
    resp = client.chat.completions.create(
        model=settings.MINIMAX_MODEL,
        messages=messages,
        temperature=0.0,
        max_tokens=300,
        timeout=_TIMEOUT_SECONDS,
    )
    return resp.choices[0].message.content or ""


def _extract_json(raw: str) -> dict:
    """从模型输出中抠出第一个 JSON 对象（容忍 ```json 代码块 / 前后噪声）。"""
    if not raw:
        return {}
    s = raw.strip()
    # 去代码块围栏
    s = re.sub(r"^```(?:json)?\s*", "", s)
    s = re.sub(r"\s*```$", "", s)
    try:
        return json.loads(s)
    except (json.JSONDecodeError, ValueError):
        pass
    # 退而求其次：抓第一个 {...}
    m = re.search(r"\{.*\}", s, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(0))
        except (json.JSONDecodeError, ValueError):
            return {}
    return {}


def _normalize(obj: dict) -> RouteResult:
    """把模型 JSON 归一化为受控的 RouteResult（防漂移的最后一道闸）。"""
    if not isinstance(obj, dict):
        return RouteResult()
    raw_intent = str(obj.get("intent", "")).strip()
    intent = raw_intent if raw_intent in ALLOWED_INTENTS else "unknown"

    try:
        confidence = float(obj.get("confidence", 0.0))
    except (TypeError, ValueError):
        confidence = 0.0
    confidence = max(0.0, min(1.0, confidence))

    slots: dict = {}
    raw_slots = obj.get("slots")
    allowed_keys = _SLOT_KEYS_BY_INTENT.get(intent)
    if allowed_keys and isinstance(raw_slots, dict):
        for k, v in raw_slots.items():
            if k not in allowed_keys or v in (None, "", []):
                continue
            if k == "duration_minutes":
                try:
                    slots[k] = int(v)
                except (TypeError, ValueError):
                    continue
            elif k == "platform":
                if str(v) in _VALID_PLATFORMS:
                    slots[k] = str(v)
            elif k == "approved":
                b = _coerce_approved(v)
                if b is not None:
                    slots[k] = b
            else:
                slots[k] = str(v).strip()
    return RouteResult(intent=intent, slots=slots, confidence=confidence,
                       reason=str(obj.get("reason", ""))[:120])


def _coerce_approved(v) -> "bool | None":
    """approved 槽位 → bool（批准/同意/yes→True，拒绝/驳回/no→False，无法判定→None）。"""
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return int(v) == 1
    s = str(v).strip().lower()
    if s in _APPROVED_TRUE:
        return True
    if s in _APPROVED_FALSE:
        return False
    return None


def classify(text: str) -> RouteResult:
    """Tier-2 分类入口。fail-open：任何异常 → unknown（handler 落到完整 agent）。"""
    text = (text or "").strip()
    if not text:
        return RouteResult()
    messages = [
        {"role": "system", "content": _system_prompt()},
        {"role": "user", "content": text},
    ]
    try:
        raw = _complete(messages)
    except Exception:
        log.warning("intent_router LLM call failed — failing open to unknown", exc_info=True)
        return RouteResult(intent="unknown", reason="llm_error")
    result = _normalize(_extract_json(raw))
    # 不记录用户消息原文（CLAUDE.md 不变量）：只记 intent / 置信度 / 槽位"键名"。
    log.info("intent_router → intent=%s conf=%.2f slot_keys=%s",
             result.intent, result.confidence, list(result.slots))
    return result
