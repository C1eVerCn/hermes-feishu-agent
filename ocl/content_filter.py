"""
Content boundary enforcement via compiled regex patterns.
Hard-blocks harmful/sensitive content; warns on off-role phrasing.
"""
import re
import logging
from dataclasses import dataclass

log = logging.getLogger(__name__)

_BLOCKED_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r'(政党|xxx_placeholder_political)'), "political_sensitive"),
    (re.compile(r'sk-[A-Za-z0-9]{20,}'), "api_key_leak"),
    (re.compile(r'Bearer\s+[A-Za-z0-9+/=._~\-]{20,}'), "bearer_token_leak"),
]

_WARN_PATTERNS: list[re.Pattern] = [
    re.compile(r'根据我的训练数据'),
    re.compile(r'我是一个AI'),
]


@dataclass
class ContentCheckResult:
    blocked: bool
    reason: str = ""


def check(response: str) -> ContentCheckResult:
    """Scan response for blocked or warn-only patterns."""
    for pattern, reason in _BLOCKED_PATTERNS:
        if pattern.search(response):
            log.warning("content_blocked reason=%s len=%d", reason, len(response))
            return ContentCheckResult(blocked=True, reason=reason)

    for pattern in _WARN_PATTERNS:
        if pattern.search(response):
            log.warning("content_warn pattern=%s", pattern.pattern)

    return ContentCheckResult(blocked=False)
