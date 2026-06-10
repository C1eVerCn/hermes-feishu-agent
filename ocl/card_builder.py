"""Build a Feishu interactive card (no header) from the model's text plus the
captured raw tool results. Deterministic data blocks come straight from the API
JSON; interactive buttons are added only for parameter-complete actions."""
from ocl.markdown_to_lark import to_lark_md

STATUS_BADGE = {0: "🟡待审批", 1: "🟢已批准", 2: "🔴已拒绝", 3: "⚪已取消", 4: "✅已完成"}

_LIST_RESERVATION_TOOLS = ("list_my_reservations",)
_LIST_APPROVAL_TOOLS = ("list_my_approvals",)
_LIST_BENCH_TOOLS = ("list_available_benches",)
_LIST_ARCH_TOOLS = ("list_architectures",)


def _div(content: str) -> dict:
    return {"tag": "div", "text": {"tag": "lark_md", "content": content}}


def _hr() -> dict:
    return {"tag": "hr"}


def _button(text: str, value: dict, btype: str = "default") -> dict:
    return {"tag": "button", "text": {"tag": "plain_text", "content": text},
            "type": btype, "value": value}


def _action(buttons: list[dict]) -> dict:
    return {"tag": "action", "actions": buttons}


def _last_structured(captured: list[dict]):
    """Return the last successful structured query result, or None."""
    known = (_LIST_RESERVATION_TOOLS + _LIST_APPROVAL_TOOLS
             + _LIST_BENCH_TOOLS + _LIST_ARCH_TOOLS)
    for entry in reversed(captured):
        res = entry.get("result")
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
    # Detect shape: first item is dict → table; else → bullet list.
    if isinstance(data[0], dict):
        rows = ["| 台架编号 | 架构 | 状态 | 位置 | 调度员 |",
                "|---------|------|------|------|--------|"]
        for b in data:
            status = b.get("statusDesc") or b.get("status", "")
            rows.append(
                f"| {b.get('benchNo','')} | {b.get('architecture','')} | "
                f"{status} | {b.get('location','')} | {b.get('dispatcher','')} |"
            )
        return "可用台架：\n" + "\n".join(rows)
    # list[str] fallback
    return "可用台架：\n" + "\n".join(f"- {b}" for b in data)


def _format_architectures(data: list) -> str:
    """Render list_architectures data. Tolerates list[str] (tests) and
    list[dict{architecture, count}] (real API)."""
    if not data:
        return "当前没有可用架构。"
    if isinstance(data[0], dict):
        return "支持的架构：\n" + "\n".join(
            f"- {a.get('architecture', a.get('name',''))} "
            f"({a.get('count', '?')} 台)" for a in data
        )
    return "支持的架构：\n" + "\n".join(f"- {a}" for a in data)


def build_card(text: str, captured: list[dict]) -> dict:
    elements: list[dict] = [_div(to_lark_md(text))]
    entry = _last_structured(captured)

    if entry is not None:
        tool = entry["tool"]
        data = entry["result"].get("data") or []
        elements.append(_hr())

        if tool in _LIST_RESERVATION_TOOLS or tool in _LIST_APPROVAL_TOOLS:
            for r in data:
                elements.append(_div(_reservation_field(r)))
                buttons = _buttons_for(tool, r)
                if buttons:
                    elements.append(_action(buttons))
        elif tool in _LIST_BENCH_TOOLS:
            elements.append(_div(_format_benches(data)))
        elif tool in _LIST_ARCH_TOOLS:
            elements.append(_div(_format_architectures(data)))

    return {"config": {"wide_screen_mode": True}, "elements": elements}
