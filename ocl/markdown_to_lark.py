"""Convert a model's markdown into Feishu lark_md-friendly text.
lark_md renders **bold**, lists and links, but NOT '#' headers or ``` fences.
We map headers to bold lines and strip code fences."""
import re

_HEADER = re.compile(r'^\s{0,3}#{1,6}\s+(.*?)\s*#*\s*$', re.MULTILINE)
_FENCE = re.compile(r'```[^\n]*\n?')
_MULTI_BLANK = re.compile(r'\n{3,}')


def to_lark_md(text: str) -> str:
    text = _HEADER.sub(lambda m: f"**{m.group(1).strip()}**", text)
    text = _FENCE.sub("", text)
    text = _MULTI_BLANK.sub("\n\n", text)
    return text.strip()
