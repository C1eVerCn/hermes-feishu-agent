"""
Format control: strip whitespace, collapse excess blank lines, detect empty responses.
"""
import re
import logging
from dataclasses import dataclass

log = logging.getLogger(__name__)

_MULTI_BLANK = re.compile(r'\n{3,}')


@dataclass
class FormatResult:
    text: str
    blocked: bool
    block_reason: str = ""


def apply(response: str) -> FormatResult:
    """Normalise response format. Returns blocked=True if response is empty."""
    text = response.strip()
    if not text:
        return FormatResult(text="", blocked=True, block_reason="empty_response")
    text = _MULTI_BLANK.sub("\n\n", text)
    return FormatResult(text=text, blocked=False)
