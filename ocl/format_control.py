"""
Format control: strip whitespace, internal turn markers, hallucinated
tool-call JSON, and "建议：" CoT-leak blocks. Returns blocked=True if
the result is empty.

BUGFIX (#9): the previous implementation stitched together 5 independent
passes (internal-markers regex → multi-line JSON brace-counter scanner →
suggestion-block regex → blank-line collapse → re-strip). The step
ordering was an implicit contract and each pass re-parsed the text a
different way. This module now does a single linear scan with one
classifier and an accumulator.
"""
import json
import logging
import re
from dataclasses import dataclass

log = logging.getLogger(__name__)

_MULTI_BLANK = re.compile(r'\n{3,}')

# Tool-call-shaped JSON keys. If a JSON object has BOTH a "tool/name" key
# AND a "parameters/arguments/input" key, the LLM is hallucinating a
# function call (happens when enabled_toolsets is misconfigured).
_TOOL_KEYS = ("tool", "name")
_TOOL_ARG_KEYS = ("parameters", "arguments", "input")


# ── Suggestion block (numbered OR bulleted) ──────────────────────────────
# Strip "**建议：**\n1. ...\n2. ..." or "建议：\n- foo\n- bar" blocks.
# Match the heading line; the follow-up list items are checked separately
# against _SUGGESTION_LINE.
_SUGGESTION_HEADING = re.compile(
    r"\**\s*(?:建议|建议与提示|提示)\s*[：:]\**\s*$"
)
_SUGGESTION_LINE = re.compile(r"\s*(?:\d+\.|[-*•])\s+[^\n]+")


# ── Tool-call hallucination: a single "{...}" JSON block ─────────────────
def _is_tool_call_shape(obj) -> bool:
    return (isinstance(obj, dict)
            and any(k in obj for k in _TOOL_KEYS)
            and any(k in obj for k in _TOOL_ARG_KEYS))


def _try_parse_json(s: str):
    try:
        return json.loads(s)
    except (json.JSONDecodeError, ValueError):
        return None


# ── Single-pass scanner ──────────────────────────────────────────────────

_FALLBACK_SUGGESTION = "如持续失败请联系管理员。"


def _classify_and_clean(text: str) -> str:
    """Walk the text line by line. Accumulate JSON blocks (lines starting
    with '{' until balanced '}'); collect suggestion blocks; drop
    internal turn markers. Re-join everything else.

    Replaces 4 separate passes (internal-markers regex, multi-line JSON
    scanner, suggestion regex, blank-line collapse) with one linear scan.

    Structure: each special case (tool-JSON, suggestion block, turn marker)
    either `continue`s the loop with its own emit, or falls through to the
    single `out.append(line)` at the bottom (hoisted default).
    """
    lines = text.split("\n")
    out: list[str] = []
    i = 0
    n = len(lines)
    while i < n:
        line = lines[i]
        stripped = line.strip()

        # 1) JSON block start — collect until balanced.
        if stripped.startswith("{"):
            depth = stripped.count("{") - stripped.count("}")
            j = i + 1
            while depth > 0 and j < n:
                depth += lines[j].count("{") - lines[j].count("}")
                j += 1
            candidate = "\n".join(lines[i:j]).strip()
            obj = _try_parse_json(candidate)
            if _is_tool_call_shape(obj):
                log.debug("stripped hallucinated tool JSON (%d lines)", j - i)
                i = j
                continue
            # Not a tool-call shape: emit the first line, continue from i+1
            # so the rest is re-evaluated normally.

        # 2) Suggestion heading — consume heading + ≥2 list items.
        # Use search so we can find "建议：" mid-line (e.g. "...失败。建议：\n- ...")
        # in addition to the standalone "**建议：**" form.
        elif (sug := _SUGGESTION_HEADING.search(line)):
            k = i + 1
            while k < n and _SUGGESTION_LINE.match(lines[k]):
                k += 1
            items = k - i - 1
            if items >= 2:
                log.debug("stripped suggestion block (%d items)", items)
                # Preserve any meaningful prefix that precedes the "建议："
                # marker on the same line (e.g. "CT001 当前无可用台架。建议：")
                # — only the suggestion heading + its list items are CoT leak.
                prefix = line[:sug.start()].rstrip(" \t*_~`")
                if prefix.strip():
                    out.append(prefix)
                # Emit a single fallback line in place of the heading+items.
                out.append(_FALLBACK_SUGGESTION)
                i = k
                continue
            # Not enough items: treat as a normal "建议：..." sentence.

        # 3) Internal turn marker — drop the line if it matches the
        # known set. (The previous regex was a separate first pass; now
        # folded into the single scan.)
        elif re.match(r"^(新消息|新会话|System\s*:|Human\s*:|Assistant\s*:|User\s*:|---+\s*$)",
                     stripped):
            i += 1
            continue

        # 4) Default: keep the line.
        out.append(line)
        i += 1

    return "\n".join(out)


@dataclass
class FormatResult:
    text: str
    blocked: bool
    block_reason: str = ""


def apply(response: str) -> FormatResult:
    """Normalise response format. Returns blocked=True if response is empty."""
    text = (response or "").strip()
    if not text:
        return FormatResult(text="", blocked=True, block_reason="empty_response")

    # Single-pass classifier: drop markers, strip tool-JSON hallucinations,
    # replace numbered/bulleted "建议：" blocks — all in one linear scan.
    text = _classify_and_clean(text)

    # Collapse excess blank lines (single trailing pass, no logic here).
    text = _MULTI_BLANK.sub("\n\n", text)

    # Re-check empty after stripping
    text = text.strip()
    if not text:
        return FormatResult(text="", blocked=True, block_reason="empty_after_strip")

    return FormatResult(text=text, blocked=False)
