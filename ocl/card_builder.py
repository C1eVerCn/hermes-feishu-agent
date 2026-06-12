"""Build a Feishu interactive card (no header) from the model's text plus the
captured raw tool results. Deterministic data blocks come straight from the API
JSON; interactive buttons are added only for parameter-complete actions.

De-duplication: when the model's text already enumerates every item in the
captured data (e.g. it lists all benches), we skip the data block to avoid
showing the same info twice. The text is treated as canonical when it's
already complete; the data block is added only when the text references the
data without enumerating it (e.g. "以下是查询结果" + nothing else).
"""
import json
import re
from ocl.markdown_to_lark import to_lark_md

STATUS_BADGE = {0: "🟡待审批", 1: "🟢已批准", 2: "🔴已拒绝", 3: "⚪已取消", 4: "✅已完成"}

_LIST_RESERVATION_TOOLS = ("list_my_reservations",)
_LIST_APPROVAL_TOOLS = ("list_my_approvals",)
_LIST_BENCH_TOOLS = ("list_available_benches",)
_LIST_ARCH_TOOLS = ("list_architectures",)
# BUGFIX (#10): the LLM-facing tool is now `dry_run_reserve_bench`; the
# real `reserve_bench` is no longer in the LLM's toolset at all.
_CONFIRM_RESERVE_TOOL = "dry_run_reserve_bench"


def _div(content: str) -> dict:
    return {"tag": "div", "text": {"tag": "lark_md", "content": content}}


def _hr() -> dict:
    return {"tag": "hr"}


def _button(text: str, value: dict, btype: str = "default") -> dict:
    return {"tag": "button", "text": {"tag": "plain_text", "content": text},
            "type": btype, "value": value}


def _action(buttons: list[dict]) -> dict:
    return {"tag": "action", "actions": buttons}


def _coerce_result(res):
    """bench_tools handlers return a JSON string (json.dumps); tool_capture
    coerces it back to a dict, but the reservation fast path stores the raw
    string. Accept both shapes — returns a dict, or {} when unparseable."""
    if isinstance(res, dict):
        return res
    if isinstance(res, str):
        try:
            parsed = json.loads(res)
            return parsed if isinstance(parsed, dict) else {}
        except (json.JSONDecodeError, ValueError):
            return {}
    return {}


def _last_structured(captured: list[dict]):
    """Return the last successful structured query result, or None."""
    known = (_LIST_RESERVATION_TOOLS + _LIST_APPROVAL_TOOLS
             + _LIST_BENCH_TOOLS + _LIST_ARCH_TOOLS)
    for entry in reversed(captured):
        res = entry.get("result")
        # bench_tools/handlers._ok() returns a JSON string (json.dumps the
        # fmp response). Some tests / code paths pass a dict directly.
        # Accept both shapes.
        if isinstance(res, str):
            try:
                res = json.loads(res)
            except (json.JSONDecodeError, ValueError):
                continue
        if not isinstance(res, dict) or res.get("code") != 200:
            continue
        if entry["tool"] in known:
            return entry
    return None


def _reservation_field(r: dict) -> str:
    badge = STATUS_BADGE.get(r.get("status"), r.get("statusDesc", ""))
    lines = [f"**{r.get('taskName', '(无任务名)')}**  {badge}",
             f"台架编号：{r.get('benchNo', '')}",
             f"时间：{r.get('startTime', '')} 至 {r.get('endTime', '')}"]
    if r.get("employeeName"):
        lines.append(f"预约人：{r['employeeName']}")
    if r.get("testPurpose"):
        lines.append(f"测试目的：{r['testPurpose']}")
    if r.get("reviewerName"):
        lines.append(f"审批人：{r['reviewerName']}")
    return "\n".join(lines)


def _buttons_for(tool: str, r: dict) -> list[dict]:
    bench_no = r.get("benchNo")
    start, end = r.get("startTime"), r.get("endTime")
    status = r.get("status")
    if tool in _LIST_RESERVATION_TOOLS:
        if status == 0:
            return [_button("取消预约",
                            {"action": "cancel", "benchNo": bench_no,
                             "startTime": start, "endTime": end}, "danger")]
        if status == 1:
            return [_button("归还台架", {"action": "return", "benchNo": bench_no})]
        return []
    if tool in _LIST_APPROVAL_TOOLS:
        if status == 0:
            return [
                _button("批准", {"action": "approve", "benchNo": bench_no,
                                "approvalResult": 1, "startTime": start, "endTime": end}, "primary"),
                _button("拒绝", {"action": "approve", "benchNo": bench_no,
                                "approvalResult": 2, "startTime": start, "endTime": end}, "danger"),
            ]
        return []
    return []


def _format_benches(data: list) -> str:
    """Render list_available_benches data. Tolerates both list[str] (tests)
    and list[dict{benchNo, architecture, status, location, group, dispatcher}]
    (real API at :9013)."""
    if not data:
        return "当前没有可用台架。"
    # Detect shape: first item is dict → inline-tag list; else → inline-tag list.
    if isinstance(data[0], dict):
        parts = []
        for b in data:
            tag = b.get("benchNo", "")
            arch = b.get("architecture", "")
            status = b.get("statusDesc") or b.get("status", "")
            parts.append(f"`{tag}`({arch},{status})" if arch or status else f"`{tag}`")
        return "可用台架：" + " ".join(parts)
    # list[str] fallback
    return "可用台架：" + " ".join(f"`{b}`" for b in data)


def _format_architectures(data: list) -> str:
    """Render list_architectures data. Tolerates list[str] (tests) and
    list[dict{architecture, count}] (real API)."""
    if not data:
        return "当前没有可用架构。"
    if isinstance(data[0], dict):
        return "支持的架构：" + " ".join(
            f"`{a.get('architecture', a.get('name',''))}`"
            f"({a.get('count', '?')}台)"
            for a in data
        )
    return "支持的架构：" + " ".join(f"`{a}`" for a in data)


def build_confirm_reserve_card(entry: dict) -> dict:
    """Text-only confirm card (no buttons). Per the 车辆预约 reference
    flow, the user confirms by replying with the literal word '确认' (or
    '取消' to abort). The handler intercepts that reply and calls
    reserve_bench directly.

    The args are stored server-side in `bot.dry_run_state` (keyed by
    user_id from the conversation), so the confirm text doesn't need to
    carry the bench args through the chat.
    """
    res = _coerce_result(entry.get("result"))
    summary = res.get("summary", "请确认预约信息")

    elements: list[dict] = [_div(summary)]
    elements.append(_hr())
    elements.append(_div(
        "请确认以上信息：\n"
        "• 回复 **\"确认\"** 提交预约\n"
        "• 回复 **\"取消\"** 放弃本次预约\n\n"
        "（10 分钟内未回复本次预约将自动作废）"
    ))
    # No action block — the user replies with plain text.
    return {"config": {"wide_screen_mode": True}, "elements": elements}


def build_missing_fields_card(entry: dict) -> dict:
    """Render a "please supply the missing fields" card. No buttons — the
    user is expected to type a free-text reply, which the LLM will use
    to fill in the missing fields and re-call dry_run_reserve_bench.
    """
    res = _coerce_result(entry.get("result"))
    summary = res.get("summary", "请补充预约信息")
    # No action block — just the prompt. The LLM takes over the
    # back-and-forth until dry_run has all required fields.
    return {
        "config": {"wide_screen_mode": True},
        "elements": [_div(summary)],
    }


def _text_already_enumerates(text: str, items: list[str]) -> bool:
    """Return True if text already mentions all items (any kind).

    Normalises text by stripping markdown formatting / punctuation, then
    checks whether every item appears as a substring. Used to suppress
    the deterministic data block when the LLM has already listed the data.
    """
    if not items:
        return False
    # Strip markdown decoration and common punctuation; keep CJK chars,
    # ASCII letters/digits, and spaces.
    normalised = re.sub(r"[`*_～~#\-—，。：；、（）()\[\]【】!?,.|]", "", text)
    return all(item in normalised for item in items)


def build_card(text: str, captured: list[dict]) -> dict:
    """Build a Feishu interactive card from text + structured tool results.
    Compact layout: text summary and data block on adjacent elements, no
    extra HR separator — saves vertical space.

    De-duplication: when LLM text already enumerates every item in the
    captured data, skip the data block. Otherwise add it as a separate
    element.

    Always returns a valid card (even when captured is empty). Pipeline
    sends this for every LLM reply; intent-path replies use
    sender.send_text_as_card which builds its own single-element card.

    Special case: when the LLM called reserve_bench with dry_run=True to
    get a confirm card, render that confirm card instead of the normal
    text+data layout.
    """
    # Special: dry_run confirm card. Single reversed pass — both the
    # missing-fields and confirm branches are checked inline. The result may
    # be a dict (LLM path, via tool_capture) or a raw JSON string (reservation
    # fast path), so coerce before inspecting.
    for entry in reversed(captured):
        if entry.get("tool") == _CONFIRM_RESERVE_TOOL:
            res = _coerce_result(entry.get("result"))
            if res.get("dry_run"):
                if res.get("missing_fields"):
                    return build_missing_fields_card(entry)
                # Replace the LLM's text with a short "请确认" prompt; the
                # real reservation block lives in the confirm card itself.
                return build_confirm_reserve_card(entry)

    elements: list[dict] = [_div(to_lark_md(text))]
    entry = _last_structured(captured)

    if entry is not None:
        tool = entry["tool"]
        res = entry["result"]
        # Same JSON-string-or-dict tolerance as _last_structured above.
        if isinstance(res, str):
            try:
                res = json.loads(res)
            except (json.JSONDecodeError, ValueError):
                res = {}
        data = res.get("data") or []

        if tool in _LIST_RESERVATION_TOOLS or tool in _LIST_APPROVAL_TOOLS:
            for r in data:
                elements.append(_div(_reservation_field(r)))
                buttons = _buttons_for(tool, r)
                if buttons:
                    elements.append(_action(buttons))
        elif tool in _LIST_BENCH_TOOLS:
            items = [b.get("benchNo", "") for b in data if isinstance(b, dict)] or \
                    [str(b) for b in data]
            if not _text_already_enumerates(text, items):
                elements.append(_div(_format_benches(data)))
        elif tool in _LIST_ARCH_TOOLS:
            items = [a.get("architecture", a.get("name", ""))
                     for a in data if isinstance(a, dict)] or [str(a) for a in data]
            if not _text_already_enumerates(text, items):
                elements.append(_div(_format_architectures(data)))

    return {"config": {"wide_screen_mode": True}, "elements": elements}
