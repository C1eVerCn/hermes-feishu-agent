from ocl.card_builder import build_card


def _div_texts(card):
    out = []
    for el in card["elements"]:
        if el.get("tag") == "div" and "text" in el:
            out.append(el["text"]["content"])
    return "\n".join(out)


def test_no_header():
    card = build_card("你好", [])
    assert "header" not in card


def test_summary_block_present():
    card = build_card("# 结果\n**好的**", [])
    assert "**结果**" in _div_texts(card)
    assert "**好的**" in _div_texts(card)


def test_reservations_data_block_renders_fields():
    captured = [{"tool": "list_my_reservations", "result": {"code": 200, "data": [
        {"benchNo": "TJ001", "startTime": "2099-01-01 09:00:00", "endTime": "2099-01-01 10:00:00",
         "taskName": "性能测试", "status": 0, "statusDesc": "待审批"}]}}]
    card = build_card("您有1条预约", captured)
    text = _div_texts(card)
    assert "TJ001" in text
    assert "性能测试" in text
    assert "待审批" in text


def test_pending_reservation_gets_cancel_button():
    captured = [{"tool": "list_my_reservations", "result": {"code": 200, "data": [
        {"benchNo": "TJ001", "startTime": "2099-01-01 09:00:00", "endTime": "2099-01-01 10:00:00",
         "taskName": "t", "status": 0, "statusDesc": "待审批"}]}}]
    card = build_card("x", captured)
    actions = [e for e in card["elements"] if e.get("tag") == "action"]
    assert actions, "expected an action block with a cancel button"
    btn = actions[0]["actions"][0]
    assert btn["value"]["action"] == "cancel"
    assert btn["value"]["benchNo"] == "TJ001"


def test_approved_reservation_gets_return_button():
    captured = [{"tool": "list_my_reservations", "result": {"code": 200, "data": [
        {"benchNo": "TJ006", "startTime": "2099-03-01 09:00:00", "endTime": "2099-03-01 12:00:00",
         "taskName": "t", "status": 1, "statusDesc": "已批准"}]}}]
    card = build_card("x", captured)
    values = [a["value"]["action"] for e in card["elements"] if e.get("tag") == "action" for a in e["actions"]]
    assert "return" in values


def test_approvals_pending_gets_approve_reject_buttons():
    captured = [{"tool": "list_my_approvals", "result": {"code": 200, "data": [
        {"benchNo": "TJ002", "startTime": "2099-01-01 09:00:00", "endTime": "2099-01-01 10:00:00",
         "taskName": "t", "employeeName": "张三", "status": 0, "statusDesc": "待审批"}]}}]
    card = build_card("x", captured)
    values = [a["value"] for e in card["elements"] if e.get("tag") == "action" for a in e["actions"]]
    results = {v.get("approvalResult") for v in values if v["action"] == "approve"}
    assert results == {1, 2}


def test_available_benches_no_reserve_button():
    captured = [{"tool": "list_available_benches", "result": {"code": 200, "data": ["TJ002", "TJ004"]}}]
    card = build_card("可用台架有 TJ002、TJ004", captured)
    assert "TJ002" in _div_texts(card)
    actions = [e for e in card["elements"] if e.get("tag") == "action"]
    assert actions == []


def test_architectures_rendered_as_list():
    captured = [{"tool": "list_architectures", "result": {"code": 200, "data": ["1.0架构", "L3架构"]}}]
    card = build_card("支持以下架构", captured)
    text = _div_texts(card)
    assert "1.0架构" in text and "L3架构" in text


def test_error_result_no_data_block_no_buttons():
    captured = [{"tool": "reserve_bench", "result": {"error": "HTTP 400: 台架不存在"}}]
    card = build_card("预约失败：台架不存在", captured)
    actions = [e for e in card["elements"] if e.get("tag") == "action"]
    assert actions == []
    assert "预约失败" in _div_texts(card)


# ── Dict-shaped data from real API (added 2026-06-09 — bug fix) ─────────────

def test_available_benches_dict_shape_renders_compact():
    """Real API returns list[dict{benchNo, architecture, status, ...}];
    card renders compact inline-tag list to minimise vertical space."""
    captured = [{"tool": "list_available_benches", "result": {"code": 200, "data": [
        {"benchNo": "B-001", "architecture": "1.0架构", "status": 0,
         "statusDesc": "可用", "location": "A区", "dispatcher": "张工"},
        {"benchNo": "B-002", "architecture": "1.0架构", "status": 1,
         "statusDesc": "占用", "location": "A区", "dispatcher": "张工"},
    ]}}]
    card = build_card("当前可用台架", captured)
    text = _div_texts(card)
    assert "B-001" in text
    assert "1.0架构" in text
    assert "可用" in text
    assert "占用" in text
    # Compact format: bench tag inline, not a markdown table
    assert "`B-001`" in text
    assert "`B-002`" in text


def test_available_benches_dict_empty():
    captured = [{"tool": "list_available_benches", "result": {"code": 200, "data": []}}]
    card = build_card("查询完成", captured)
    text = _div_texts(card)
    assert "没有可用台架" in text


def test_architectures_dict_shape_renders_compact():
    """Real API returns list[dict{architecture, count}]; render compact inline."""
    captured = [{"tool": "list_architectures", "result": {"code": 200, "data": [
        {"architecture": "1.0架构", "count": 5},
        {"architecture": "L3架构", "count": 3},
    ]}}]
    card = build_card("支持的架构", captured)
    text = _div_texts(card)
    assert "1.0架构" in text
    assert "L3架构" in text
    assert "5" in text
    assert "3" in text
    # Compact: architecture in backticks, count in parens — single line, no
    # per-item bullets that blow up vertical space.
    assert "`1.0架构`(5台)" in text
    assert "`L3架构`(3台)" in text


def test_architectures_list_str_fallback_still_works():
    """Backward compat: list[str] shape (used by old tests + possible LLM mock)"""
    captured = [{"tool": "list_architectures", "result": {"code": 200, "data": ["1.0架构", "L3架构"]}}]
    card = build_card("支持以下架构", captured)
    text = _div_texts(card)
    assert "1.0架构" in text
    assert "L3架构" in text


# ── Always builds a card (added 2026-06-10 — user-facing requirement) ───────

def test_builds_card_even_with_empty_captured():
    """Every LLM reply renders as a card, even when no tool ran."""
    card = build_card("你好", [])
    assert "elements" in card
    assert len(card["elements"]) >= 1
    # First element is the text div
    assert card["elements"][0]["tag"] == "div"


# ── De-duplication (added 2026-06-10 — kill the "twice" problem) ─────────────

def test_skips_bench_data_block_when_text_enumerates_all_benches():
    """When LLM text already lists every bench, skip the data block —
    otherwise the user sees the same list twice."""
    captured = [{"tool": "list_available_benches", "result": {"code": 200, "data": [
        {"benchNo": "CT001"}, {"benchNo": "TJ001"}, {"benchNo": "TJ002"},
    ]}}]
    text = "1.0 架构现有 3 个可用台架：CT001、TJ001、TJ002。如需预约直接告知台架编号。"
    card = build_card(text, captured)
    elements_text = "\n".join(
        e.get("text", {}).get("content", "") for e in card["elements"]
        if e.get("tag") == "div"
    )
    # Bench appears exactly once (in the text div), not twice (would mean
    # the data block was appended as well).
    assert elements_text.count("CT001") == 1
    assert elements_text.count("TJ001") == 1
    # Card should have only ONE div — the text. No second data-block div.
    divs = [e for e in card["elements"] if e.get("tag") == "div"]
    assert len(divs) == 1


# ── dry_run confirm card (added 2026-06-10 — product flow) ──────────────────

def test_dry_run_reserve_renders_text_only_confirm_card():
    """Per the 车辆预约 reference flow, the dry-run confirm card has NO
    action buttons. The user confirms by replying with the literal word
    '确认' (intercepted by handler.py → dry_run_state).
    """
    captured = [{"tool": "dry_run_reserve_bench", "result": {
        "dry_run": True,
        "summary": "台架编号：CT001\n开始：2026-06-11 17:00:00\n结束：2026-06-11 20:00:00\n任务：测试\n目的：测试",
        "args": {"benchNo": "CT001", "startTime": "2026-06-11 17:00:00",
                 "endTime": "2026-06-11 20:00:00",
                 "taskName": "测试", "testPurpose": "测试"},
    }}]
    card = build_card("已生成确认卡片，请用户确认。", captured)

    # No action block — the user replies with plain text.
    actions = [e for e in card["elements"] if e.get("tag") == "action"]
    assert actions == []
    # The card body tells the user to reply "确认" or "取消".
    divs = [e for e in card["elements"] if e.get("tag") == "div"]
    body = "\n".join(d.get("text", {}).get("content", "") for d in divs)
    assert "确认" in body
    assert "取消" in body


def test_dry_run_takes_precedence_over_other_captured_data():
    """If both a dry_run reserve AND a list call are captured, the confirm
    card wins (it's the user's last action target)."""
    captured = [
        {"tool": "list_available_benches", "result": {"code": 200, "data": ["TJ001"]}},
        {"tool": "dry_run_reserve_bench", "result": {
            "dry_run": True, "summary": "test", "args": {"benchNo": "TJ001"},
        }},
    ]
    card = build_card("请确认。", captured)
    divs = [e for e in card["elements"] if e.get("tag") == "div"]
    body = "\n".join(d.get("text", {}).get("content", "") for d in divs)
    # Confirm card body, not the list data block
    assert "确认" in body
    # No action buttons in the new flow
    actions = [e for e in card["elements"] if e.get("tag") == "action"]
    assert actions == []


def test_includes_bench_data_block_when_text_omits_items():
    """When text says '以下是查询结果' but doesn't enumerate, the data block
    is the user's only way to see the actual list."""
    captured = [{"tool": "list_available_benches", "result": {"code": 200, "data": [
        {"benchNo": "CT001"}, {"benchNo": "TJ001"},
    ]}}]
    text = "以下是查询结果。"
    card = build_card(text, captured)
    elements_text = "\n".join(
        e.get("text", {}).get("content", "") for e in card["elements"]
        if e.get("tag") == "div"
    )
    # Data block should appear (text didn't enumerate)
    assert "可用台架：" in elements_text
    assert "CT001" in elements_text
    assert "TJ001" in elements_text


# ── Fast-path raw-JSON-string result (added 2026-06-12 — review fix #1) ──────

def test_dry_run_confirm_card_from_raw_json_string_result():
    """The reservation fast path (handler.py) stores the dry_run result as a
    RAW JSON string, not a dict. build_card must still detect the confirm
    card and render the summary + 确认/取消 prompt (regression: previously the
    isinstance(res, dict) guard skipped the string, rendering a bare card)."""
    import json
    raw = json.dumps({
        "dry_run": True,
        "summary": "台架编号：CT001\n开始：2026-06-11 17:00:00\n结束：2026-06-11 20:00:00",
        "args": {"benchNo": "CT001"},
    }, ensure_ascii=False)
    card = build_card("请确认预约信息", [{"tool": "dry_run_reserve_bench", "result": raw}])
    body = _div_texts(card)
    assert "CT001" in body
    assert "确认" in body
    assert "取消" in body
    # No action buttons in the text-reply confirm flow
    assert all(e.get("tag") != "action" for e in card["elements"])


def test_missing_fields_card_from_raw_json_string_result():
    """missing_fields branch must also work when result is a raw JSON string."""
    import json
    raw = json.dumps({
        "dry_run": True, "missing_fields": ["testPurpose"],
        "summary": "我还缺少以下信息：测试目的",
    }, ensure_ascii=False)
    card = build_card("x", [{"tool": "dry_run_reserve_bench", "result": raw}])
    body = _div_texts(card)
    assert "测试目的" in body
    assert all(e.get("tag") != "action" for e in card["elements"])
