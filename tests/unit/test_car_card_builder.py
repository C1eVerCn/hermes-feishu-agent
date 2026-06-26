"""car_tools/card_builder.py 单元测试：5+1 套卡片元素 / 按钮。"""
import pytest

from car_tools import card_builder as cb


def _find_actions(card):
    """Card 1.0/2.0 兼容：从 card 找 actions 容器（Card 1.0）或 buttons 列表
    （Card 2.0：可能在 body.elements 顶层，也可能在 column_set.columns[].elements 里）。

    2026-06-18 横排化后：build_vehicles_card 把 select_buttons 包成 _button_row
    (column_set)，cancel_btn 单独一个 _button_row。所以 buttons 散在两个
    column_set 里，本 helper 递归收集所有 button 元素（按顺序）。
    """
    def _collect(elements: list[dict]) -> list[dict]:
        out: list[dict] = []
        for e in elements:
            if e.get("tag") == "button":
                out.append(e)
            elif e.get("tag") == "column_set" and isinstance(e.get("columns"), list):
                for col in e["columns"]:
                    if isinstance(col.get("elements"), list):
                        out.extend(_collect(col["elements"]))
        return out
    return _collect(card.get("body", {}).get("elements", card.get("elements", [])))


def _find_actions_inline(elements):
    """与 _find_actions 类似，但接受直接的 elements 列表（不包 card 字典）。"""
    def _collect(els: list[dict]) -> list[dict]:
        out: list[dict] = []
        for e in els:
            if e.get("tag") == "button":
                out.append(e)
            elif e.get("tag") == "column_set" and isinstance(e.get("columns"), list):
                for col in e["columns"]:
                    if isinstance(col.get("elements"), list):
                        out.extend(_collect(col["elements"]))
        return out
    return _collect(elements)


# ── 1. vehicles_card ──────────────────────────────────────────────────────

def test_vehicles_card_empty():
    card = cb.build_vehicles_card([])
    elements = card["body"]["elements"]
    # 应该只有一条 div，提示无车辆
    assert any("没有可用车辆" in e["text"]["content"] for e in elements)
    # 不应有按钮
    assert not any(e["tag"] == "action" for e in elements)


def test_vehicles_card_with_vehicles():
    vehicles = [
        {"vehicle_no": "PNV332", "vehicle_type": "DM2", "platform": "Xavier",
         "license_plate": "沪A1"},
        {"vehicle_no": "SVV027", "vehicle_type": "CT1", "platform": "Orin",
         "license_plate": ""},
    ]
    card = cb.build_vehicles_card(vehicles)
    elements = card["body"]["elements"]
    # Card 2.0 横排化：1 div 标题 + 1 div 表格 + 2 column_set（选车 + 取消分两组）
    assert len(elements) == 4
    actions = _find_actions_inline(elements)
    # 2 个选车按钮 + 1 个取消按钮 = 3 个
    assert len(actions) == 3


def test_vehicles_card_max_5_buttons():
    """展示限制：每芯片 3 辆 + 总共 10 辆。

    10 辆车分散到 4 个平台（每平台 2-3 辆）→ 全 10 辆应全展示，
    即 10 个选车按钮 + 1 个取消 = 11 个。
    """
    vehicles = []
    # Xavier 3 辆
    for i in range(3):
        vehicles.append({"vehicle_no": f"X{i:03d}", "vehicle_type": "DM2", "platform": "Xavier"})
    # ADCU 3 辆
    for i in range(3):
        vehicles.append({"vehicle_no": f"A{i:03d}", "vehicle_type": "CT1", "platform": "ADCU"})
    # Orin 2 辆
    for i in range(2):
        vehicles.append({"vehicle_no": f"O{i:03d}", "vehicle_type": "BM2", "platform": "Orin"})
    # Thor 2 辆
    for i in range(2):
        vehicles.append({"vehicle_no": f"T{i:03d}", "vehicle_type": "CM0", "platform": "Thor"})
    assert len(vehicles) == 10
    card = cb.build_vehicles_card(vehicles)
    actions = _find_actions(card)
    # 10 个选车按钮 + 1 个取消 = 11 个
    assert len(actions) == 11


def test_vehicles_button_payload_contains_vehicle_fields():
    vehicles = [{"vehicle_no": "PNV332", "vehicle_type": "DM2", "platform": "Xavier",
                 "license_plate": "沪A1"}]
    card = cb.build_vehicles_card(vehicles)
    actions = _find_actions(card)
    btn0 = actions[0]
    assert btn0["value"]["action"] == "select_vehicle"
    assert btn0["value"]["vehicle_no"] == "PNV332"
    assert btn0["value"]["vehicle_type"] == "DM2"
    assert btn0["value"]["platform"] == "Xavier"


def test_vehicles_cancel_button():
    vehicles = [{"vehicle_no": "PNV332", "vehicle_type": "DM2", "platform": "Xavier"}]
    card = cb.build_vehicles_card(vehicles)
    actions = _find_actions(card)
    cancel = actions[-1]
    assert cancel["text"]["content"] == "取消"
    assert cancel["value"]["action"] == "cancel_flow"
    assert cancel["type"] == "danger"


# ── 2. success_card ───────────────────────────────────────────────────────

def test_success_card_renders_details():
    result = {
        "vehicle_no": "PNV332", "vehicle_type": "DM2", "platform": "Xavier",
        "license_plate": "沪A1",
        "start_time": "2026-06-16 09:00", "end_time": "2026-06-16 18:00",
        "task_name": "高速测试", "location": "A区",
        "dispatchers": [{"name": "Alice", "email": "a@x.com"}],
    }
    card = cb.build_success_card(result)
    text_contents = [e["text"]["content"] for e in card["body"]["elements"] if "text" in e]
    # 应该含「预约提交成功」
    assert any("预约提交成功" in c for c in text_contents)
    # 应该含 dispatchers 名字
    assert any("Alice" in c for c in text_contents)


def test_success_card_no_dispatchers():
    result = {
        "vehicle_no": "PNV332", "vehicle_type": "DM2", "platform": "Xavier",
        "start_time": "2026-06-16 09:00", "end_time": "2026-06-16 18:00",
        "task_name": "test", "location": "loc",
        "dispatchers": [],
    }
    card = cb.build_success_card(result)
    text_contents = [e["text"]["content"] for e in card["body"]["elements"] if "text" in e]
    assert any("（无）" in c for c in text_contents)


def test_success_card_no_buttons():
    result = {"vehicle_no": "PNV332", "vehicle_type": "DM2", "platform": "Xavier",
              "start_time": "2026-06-16 09:00", "end_time": "2026-06-16 18:00",
              "task_name": "t", "location": "l"}
    card = cb.build_success_card(result)
    assert not any(e["tag"] == "action" for e in card["body"]["elements"])


# ── 5. fail_card ──────────────────────────────────────────────────────────

def test_fail_card_renders_error():
    card = cb.build_fail_card("MCP 调用失败", context="提交预约")
    elements = card["body"]["elements"]
    assert any("❌" in e["text"]["content"] for e in elements)
    assert any("提交预约" in e["text"]["content"] for e in elements)


def test_fail_card_no_context():
    card = cb.build_fail_card("some error")
    elements = card["body"]["elements"]
    assert any("❌" in e["text"]["content"] for e in elements)


def _all_buttons(card):
    """递归收集卡里所有 button（_button_row 会把按钮包进 column_set）。"""
    out = []
    def walk(el):
        if isinstance(el, dict):
            if el.get("tag") == "button":
                out.append(el)
            for v in el.values():
                walk(v)
        elif isinstance(el, list):
            for x in el:
                walk(x)
    walk(card)
    return out


def test_records_card_cancel_button_on_pending():
    """show_cancel=True 时，待审批记录挂 [取消该预约] 按钮（action=cancel_record）。"""
    recs = [
        {"vehicle_no": "AATI25SNV639", "platform": "Thor", "status": "待审批",
         "start_time": "2026-06-26 14:00", "end_time": "2026-06-26 15:00", "task_name": "冒烟"},
        {"vehicle_no": "AATI25SNV639", "platform": "Thor", "status": "已取消",
         "start_time": "2026-06-25 10:00", "end_time": "2026-06-25 11:00", "task_name": "x"},
    ]
    card = cb.build_records_card(recs, title="我的预约", show_cancel=True)
    cancel_btns = [b for b in _all_buttons(card)
                   if b.get("value", {}).get("action") == "cancel_record"]
    assert len(cancel_btns) == 1, "只有待审批那条该有取消按钮"
    assert cancel_btns[0]["value"]["vehicle_no"] == "AATI25SNV639"
    assert cancel_btns[0]["value"]["start_time"] == "2026-06-26 14:00"


def test_records_card_no_cancel_when_disabled():
    recs = [{"vehicle_no": "PNV1", "status": "待审批", "start_time": "x", "end_time": "y"}]
    card = cb.build_records_card(recs, title="我的待审批")  # show_cancel 默认 False
    assert not _all_buttons(card)


def test_cancel_confirm_card_shows_time():
    card = cb.build_cancel_confirm_card("PNV332", start_time="2026-06-26 14:00")
    body = " ".join(e.get("text", {}).get("content", "") for e in card["body"]["elements"]
                    if e.get("tag") == "div")
    assert "PNV332" in body and "2026-06-26 14:00" in body
