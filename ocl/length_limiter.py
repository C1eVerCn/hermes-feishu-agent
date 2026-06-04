"""
Output length limiter: truncates responses exceeding _MAX_CHARS at the nearest
sentence boundary, then appends a continuation note.
"""
import logging

log = logging.getLogger(__name__)

_MAX_CHARS = 4000
_WARN_CHARS = 2000
_SUFFIX = "\n\n[...内容已截断，如需更多详情请分步查询]"
_LOOKBACK = 200
_BOUNDARIES = frozenset("。.！!？?\n")


def apply(text: str) -> str:
    """Return text, truncated if it exceeds _MAX_CHARS."""
    if len(text) <= _MAX_CHARS:
        if len(text) > _WARN_CHARS:
            log.warning("ocl_length_warn chars=%d", len(text))
        return text

    log.warning("ocl_length_truncated chars=%d limit=%d", len(text), _MAX_CHARS)
    cut = _MAX_CHARS
    # look back up to _LOOKBACK chars for a sentence boundary
    search_start = max(0, _MAX_CHARS - _LOOKBACK)
    for i in range(_MAX_CHARS, search_start, -1):
        if text[i - 1] in _BOUNDARIES:
            cut = i
            break

    return text[:cut] + _SUFFIX
