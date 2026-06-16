"""Convert a model's markdown into Feishu lark_md-friendly text.
lark_md renders **bold**, lists and links, but NOT '#' headers, ``` fences,
or '|'-delimited tables. We map headers to bold lines, strip code fences, and
flatten tables into readable lines (otherwise Feishu shows literal `| a | b |`)."""
import re

_HEADER = re.compile(r'^\s{0,3}#{1,6}\s+(.*?)\s*#*\s*$', re.MULTILINE)
_FENCE = re.compile(r'```[^\n]*\n?')
_MULTI_BLANK = re.compile(r'\n{3,}')
# A markdown table separator row: | --- | :--: | ... (dashes/colons/pipes only)
_TABLE_SEP = re.compile(r'^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)+\|?\s*$')


def _cells(row: str) -> list[str]:
    s = row.strip()
    if s.startswith("|"):
        s = s[1:]
    if s.endswith("|"):
        s = s[:-1]
    return [c.strip() for c in s.split("|")]


def _flatten_tables(text: str) -> str:
    """Turn GitHub-style markdown tables into 「字段：值」lines, since飞书
    lark_md renders raw pipes literally. Header row becomes the field labels;
    each body row becomes a bullet of 'label: value' pairs."""
    lines = text.split("\n")
    out: list[str] = []
    i = 0
    n = len(lines)
    while i < n:
        line = lines[i]
        # A table = a header row followed by a separator row.
        if ("|" in line and i + 1 < n and _TABLE_SEP.match(lines[i + 1])):
            headers = _cells(line)
            i += 2  # skip header + separator
            while i < n and "|" in lines[i] and lines[i].strip():
                vals = _cells(lines[i])
                pairs = []
                for idx, v in enumerate(vals):
                    label = headers[idx] if idx < len(headers) else ""
                    if not v:
                        continue
                    pairs.append(f"{label}：{v}" if label else v)
                if pairs:
                    out.append("• " + "　".join(pairs))
                i += 1
            continue
        out.append(line)
        i += 1
    return "\n".join(out)


def to_lark_md(text: str) -> str:
    text = _flatten_tables(text)
    text = _HEADER.sub(lambda m: f"**{m.group(1).strip()}**", text)
    text = _FENCE.sub("", text)
    text = _MULTI_BLANK.sub("\n\n", text)
    return text.strip()
