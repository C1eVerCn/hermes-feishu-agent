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
