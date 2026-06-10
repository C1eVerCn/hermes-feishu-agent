"""
Format control: strip whitespace, collapse excess blank lines, detect empty responses,
and remove internal turn markers that the LLM occasionally leaks (e.g. "新消息",
"System:", "Human:"). These must never reach the user.
"""
import re
import json
import logging
from dataclasses import dataclass

log = logging.getLogger(__name__)

_MULTI_BLANK = re.compile(r'\n{3,}')

# Internal turn markers that LLMs (esp. minimax-M3) sometimes emit when a model
# turn boundary or system prompt is reflected back. Stripped defensively at the
# earliest OCL stage so downstream renderers (card_builder, sender) never see them.
_INTERNAL_MARKERS = re.compile(
    r"^(新消息|新会话|System\s*:|Human\s*:|Assistant\s*:|User\s*:|---+\s*$)",
    re.MULTILINE,
)

# Tool-call-shaped JSON keys. If a single-line JSON object has BOTH a "tool/name"
# key AND a "parameters/arguments/input" key, the LLM is hallucinating a function
# call (happens when enabled_toolsets is misconfigured and the LLM has no real
# tool schema to call). We strip such lines — never let raw tool JSON reach users.
_TOOL_KEYS = ("tool", "name")
_TOOL_ARG_KEYS = ("parameters", "arguments", "input")


def _strip_hallucinated_tool_json(text: str) -> str:
    """Drop any standalone line OR multi-line block that parses as a tool-call
    JSON object. Robust against nested `{}` (e.g. `"parameters": {}`) and
    pretty-printed (multi-line) JSON.

    Strategy: scan for the first '{' on a line; accumulate subsequent lines
    until a balanced '}' count is reached (naive brace counter — good enough
    for top-level tool-call JSON which is one flat object). Then json.loads
    and check tool-call shape.
    """
    lines = text.split("\n")
    out: list[str] = []
    i = 0
    n = len(lines)
    while i < n:
        line = lines[i]
        stripped = line.strip()
        if stripped.startswith("{"):
            # Try to collect a balanced JSON object starting from this line.
            buf = [line]
            depth = stripped.count("{") - stripped.count("}")
            j = i + 1
            while depth > 0 and j < n:
                buf.append(lines[j])
                depth += lines[j].count("{") - lines[j].count("}")
                j += 1
            candidate = "\n".join(buf).strip()
            try:
                obj = json.loads(candidate)
                if isinstance(obj, dict) and (
                    any(k in obj for k in _TOOL_KEYS)
                    and any(k in obj for k in _TOOL_ARG_KEYS)
                ):
                    log.debug("stripped hallucinated tool JSON block (%d lines): %s",
                              j - i, candidate[:120])
                    i = j  # skip past the JSON block
                    continue
            except (json.JSONDecodeError, ValueError):
                pass
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
    text = response.strip()
    if not text:
        return FormatResult(text="", blocked=True, block_reason="empty_response")

    # 1. Strip internal turn markers (line by line)
    text = _INTERNAL_MARKERS.sub("", text)

    # 2. Strip hallucinated tool-call JSON lines
    text = _strip_hallucinated_tool_json(text)

    # 3. Collapse excess blank lines created by stripping
    text = _MULTI_BLANK.sub("\n\n", text)

    # 4. Re-check empty after stripping
    text = text.strip()
    if not text:
        return FormatResult(text="", blocked=True, block_reason="empty_after_strip")

    return FormatResult(text=text, blocked=False)
