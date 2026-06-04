"""
OCL pipeline: orchestrates format → content → length checks in sequence.
Single public entry point: apply(response, user_id) → OclResult.
Fail-open on unexpected exceptions (log + pass through).
"""
import logging
from dataclasses import dataclass, field

from ocl import format_control, content_filter, length_limiter
from config.settings import settings

log = logging.getLogger(__name__)

_BLOCK_MESSAGE = "抱歉，该内容不在我的服务范围内，请换一个问题。"
_EMPTY_MESSAGE = "抱歉，未能生成有效回复，请重试或换一种问法。"


@dataclass
class OclResult:
    text: str
    blocked: bool
    block_reason: str = ""


def apply(response: str, user_id: str) -> OclResult:
    """Run OCL pipeline on the LLM response. Never raises."""
    try:
        # 1. Format control
        fmt = format_control.apply(response)
        if fmt.blocked:
            return OclResult(text=_EMPTY_MESSAGE, blocked=True, block_reason=fmt.block_reason)
        text = fmt.text

        # 2. Content boundary
        content = content_filter.check(text)
        if content.blocked:
            block_msg = getattr(settings, "OCL_CONTENT_BLOCK_MESSAGE", _BLOCK_MESSAGE)
            return OclResult(text=block_msg, blocked=True, block_reason=content.reason)

        # 3. Length limit
        text = length_limiter.apply(text)

        return OclResult(text=text, blocked=False)

    except Exception:
        log.exception("ocl_pipeline_error user_id=%s — failing open", user_id)
        return OclResult(text=response, blocked=False, block_reason="")
