"""
OCL pipeline: orchestrates format → content → length checks in sequence.
Single public entry point: apply(response, user_id) → OclResult.
Fail-open on unexpected exceptions (log + pass through).
"""
import logging
from dataclasses import dataclass, field

from ocl import format_control, content_filter, length_limiter, card_builder, intent_filter
from config.settings import settings

log = logging.getLogger(__name__)

_BLOCK_MESSAGE = "抱歉，该内容不在我的服务范围内，请换一个问题。"
_EMPTY_MESSAGE = "抱歉，未能生成有效回复，请重试或换一种问法。"


@dataclass
class OclResult:
    text: str
    blocked: bool
    block_reason: str = ""
    card: dict | None = None


def apply(response: str, user_id: str, captured: list[dict] | None = None) -> OclResult:
    """Run OCL pipeline on the LLM response. Never raises.

    captured: this turn's raw tool results (from ocl.tool_capture) used to build
    a deterministic interactive card. Empty/None → card is summary-only.
    """
    captured = captured or []
    try:
        # 1. Format control
        fmt = format_control.apply(response)
        if fmt.blocked:
            return OclResult(text=_EMPTY_MESSAGE, blocked=True, block_reason=fmt.block_reason, card=None)
        text = fmt.text

        # 2. Content boundary
        content = content_filter.check(text)
        if content.blocked:
            block_msg = getattr(settings, "OCL_CONTENT_BLOCK_MESSAGE", _BLOCK_MESSAGE)
            return OclResult(text=block_msg, blocked=True, block_reason=content.reason, card=None)

        # 2.5 Intent guard — 拒绝与台架/VLM 无关的 LLM 闲聊回复
        # （用户要求：连 1+1 / 天气都不要答，统一引导到正常业务流程）
        intent = intent_filter.check(text)
        if intent.redirected:
            return OclResult(
                text=intent_filter.REDIRECT_MESSAGE,
                blocked=False,
                block_reason="off_topic_chitchat",
                card=None,  # 让引导话术用 text-as-card 渲染，保留视觉一致
            )

        # 3. Length limit
        text = length_limiter.apply(text)

        # 4. Interactive card — ALWAYS built. User-facing requirement 2026-06-10:
        # every LLM reply renders as a card, even when no tool ran. Keeps
        # visual style consistent with intent-path replies (which use
        # sender.send_text_as_card). Empty/short text still produces a valid
        # single-element card.
        card = card_builder.build_card(text, captured)

        return OclResult(text=text, blocked=False, card=card)

    except Exception:
        log.exception("ocl_pipeline_error user_id=%s — failing open", user_id)
        return OclResult(text=response, blocked=False, block_reason="", card=None)
