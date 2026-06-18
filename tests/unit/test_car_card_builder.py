"""car_tools/card_builder.py 单元测试：5+1 套卡片元素 / 按钮。"""
import pytest

from car_tools import card_builder as cb


# ── 1. vehicles_card ──────────────────────────────────────────────────────

def test_vehicles_card_empty():
    card = cb.build_vehicles_card([])
    elements = card["elements"]
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
    elements = card["elements"]
    # 1 个 div（标题）+ 1 个 div（表格）+ 1 个 action（按钮组）
    assert len(elements) == 3
    actions = next(e for e in elements if e["tag"] == "action")
    # 2 个选车按钮 + 1 个取消按钮 = 3 个
    assert len(actions["actions"]) == 3


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
    actions = next(e for e in card["elements"] if e["tag"] == "action")
    # 10 个选车按钮 + 1 个取消 = 11 个
    assert len(actions["actions"]) == 11


def test_vehicles_button_payload_contains_vehicle_fields():
    vehicles = [{"vehicle_no": "PNV332", "vehicle_type": "DM2", "platform": "Xavier",
                 "license_plate": "沪A1"}]
    card = cb.build_vehicles_card(vehicles)
    actions = next(e for e in card["elements"] if e["tag"] == "action")
    btn0 = actions["actions"][0]
    assert btn0["value"]["action"] == "select_vehicle"
    assert btn0["value"]["vehicle_no"] == "PNV332"
    assert btn0["value"]["vehicle_type"] == "DM2"
    assert btn0["value"]["platform"] == "Xavier"


def test_vehicles_cancel_button():
    vehicles = [{"vehicle_no": "PNV332", "vehicle_type": "DM2", "platform": "Xavier"}]
    card = cb.build_vehicles_card(vehicles)
    actions = next(e for e in card["elements"] if e["tag"] == "action")
    cancel = actions["actions"][-1]
    assert cancel["text"]["content"] == "取消"
    assert cancel["value"]["action"] == "cancel_flow"
    assert cancel["type"] == "danger"


# ── 2. missing_fields_card ────────────────────────────────────────────────

def test_missing_fields_card_renders_summary():
    card = cb.build_missing_fields_card("缺少：任务名称、地点")
    elements = card["elements"]
    assert len(elements) == 1
    assert "缺少" in elements[0]["text"]["content"]


def test_missing_fields_card_no_buttons():
    card = cb.build_missing_fields_card("X")
    assert not any(e["tag"] == "action" for e in card["elements"])


# ── 3. confirm_card ──────────────────────────────────────────────────────

def test_confirm_card_has_confirm_and_cancel_buttons():
    args = {
        "vehicle_no": "PNV332", "vehicle_type": "DM2", "platform": "Xavier",
        "start_time": "2026-06-16 09:00", "end_time": "2026-06-16 18:00",
        "task_name": "高速测试", "location": "A区",
    }
    card = cb.build_confirm_card("summary text", args)
    actions = next(e for e in card["elements"] if e["tag"] == "action")
    assert len(actions["actions"]) == 2
    confirm_btn = actions["actions"][0]
    cancel_btn = actions["actions"][1]
    assert confirm_btn["value"]["action"] == "confirm_booking"
    assert cancel_btn["value"]["action"] == "cancel_flow"


def test_confirm_card_button_payload_includes_args():
    args = {
        "vehicle_no": "PNV332", "vehicle_type": "DM2", "platform": "Xavier",
        "start_time": "2026-06-16 09:00", "end_time": "2026-06-16 18:00",
        "task_name": "高速测试", "location": "A区", "remark": "VIP",
    }
    card = cb.build_confirm_card("summary", args)
    actions = next(e for e in card["elements"] if e["tag"] == "action")
    confirm_value = actions["actions"][0]["value"]
    assert confirm_value["vehicleNo"] == "PNV332"
    assert confirm_value["taskName"] == "高速测试"
    assert confirm_value["remark"] == "VIP"


# ── 4. success_card ───────────────────────────────────────────────────────

def test_success_card_renders_details():
    result = {
        "vehicle_no": "PNV332", "vehicle_type": "DM2", "platform": "Xavier",
        "license_plate": "沪A1",
        "start_time": "2026-06-16 09:00", "end_time": "2026-06-16 18:00",
        "task_name": "高速测试", "location": "A区",
        "dispatchers": [{"name": "Alice", "email": "a@x.com"}],
    }
    card = cb.build_success_card(result)
    text_contents = [e["text"]["content"] for e in card["elements"] if "text" in e]
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
    text_contents = [e["text"]["content"] for e in card["elements"] if "text" in e]
    assert any("（无）" in c for c in text_contents)


def test_success_card_no_buttons():
    result = {"vehicle_no": "PNV332", "vehicle_type": "DM2", "platform": "Xavier",
              "start_time": "2026-06-16 09:00", "end_time": "2026-06-16 18:00",
              "task_name": "t", "location": "l"}
    card = cb.build_success_card(result)
    assert not any(e["tag"] == "action" for e in card["elements"])


# ── 5. fail_card ──────────────────────────────────────────────────────────

def test_fail_card_renders_error():
    card = cb.build_fail_card("MCP 调用失败", context="提交预约")
    elements = card["elements"]
    assert any("❌" in e["text"]["content"] for e in elements)
    assert any("提交预约" in e["text"]["content"] for e in elements)


def test_fail_card_no_context():
    card = cb.build_fail_card("some error")
    elements = card["elements"]
    assert any("❌" in e["text"]["content"] for e in elements)
