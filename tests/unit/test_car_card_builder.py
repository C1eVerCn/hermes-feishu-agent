"""car_tools/card_builder.py 单元测试（2026-06-30 Phase 1.6 改造后）。

所有卡片都是只读展示（div / hr / markdown 文本），不点不动。
测试断言：① 含正确标题 ② 含正确数据 ③ 没有 button / select / form 元素。
"""
import pytest

from car_tools import card_builder as cb


def _has_any_interactive(card) -> bool:
    """递归检查 card 是否有任何可交互元素（button/select/form/column_set 容器）。"""
    def walk(el):
        if not isinstance(el, dict):
            return False
        if el.get("tag") in ("button", "select_static", "form"):
            return True
        for k in ("elements", "columns", "actions"):
            v = el.get(k)
            if isinstance(v, list):
                for x in v:
                    if walk(x):
                        return True
            elif isinstance(v, dict):
                if walk(v):
                    return True
        return False
    return walk(card)


def _all_text(card) -> str:
    """收集卡片所有 lark_md 文本（递归遍历 card 整棵树）。"""
    out = []
    def walk(node):
        if isinstance(node, list):
            for x in node:
                walk(x)
            return
        if not isinstance(node, dict):
            return
        t = node.get("text")
        if isinstance(t, dict) and "content" in t:
            out.append(t["content"])
        for v in node.values():
            if isinstance(v, (list, dict)):
                walk(v)
    walk(card)
    return "\n".join(out)


# ── 1. vehicles_card ──────────────────────────────────────────────────────

def test_vehicles_card_empty():
    card = cb.build_vehicles_card([])
    assert "没有可用车辆" in _all_text(card)
    assert not _has_any_interactive(card)


def test_vehicles_card_with_vehicles():
    vehicles = [
        {"vehicle_no": "PNV332", "vehicle_type": "DM2", "platform": "Xavier",
         "license_plate": "沪A1"},
        {"vehicle_no": "SVV027", "vehicle_type": "CT1", "platform": "Orin",
         "license_plate": ""},
    ]
    card = cb.build_vehicles_card(vehicles)
    text = _all_text(card)
    # 表格包含两辆车后六位
    assert "PNV332"[-6:] in text
    assert "SVV027"[-6:] in text
    # 引导打字
    assert "回复" in text
    # 不应有任何可交互元素
    assert not _has_any_interactive(card)


def test_vehicles_card_per_platform_cap():
    """展示限制：每芯片 3 辆 + 总共 10 辆。"""
    vehicles = []
    for i in range(3):
        vehicles.append({"vehicle_no": f"X{i:03d}", "vehicle_type": "DM2", "platform": "Xavier"})
    for i in range(3):
        vehicles.append({"vehicle_no": f"A{i:03d}", "vehicle_type": "CT1", "platform": "ADCU"})
    for i in range(2):
        vehicles.append({"vehicle_no": f"O{i:03d}", "vehicle_type": "BM2", "platform": "Orin"})
    for i in range(2):
        vehicles.append({"vehicle_no": f"T{i:03d}", "vehicle_type": "CM0", "platform": "Thor"})
    assert len(vehicles) == 10
    card = cb.build_vehicles_card(vehicles)
    text = _all_text(card)
    # 共 10 辆标记
    assert "10 辆" in text
    assert not _has_any_interactive(card)


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
    text = _all_text(card)
    assert "预约提交成功" in text
    assert "Alice" in text
    assert not _has_any_interactive(card)


def test_success_card_no_dispatchers():
    result = {
        "vehicle_no": "PNV332", "vehicle_type": "DM2", "platform": "Xavier",
        "start_time": "2026-06-16 09:00", "end_time": "2026-06-16 18:00",
        "task_name": "test", "location": "loc",
        "dispatchers": [],
    }
    card = cb.build_success_card(result)
    text = _all_text(card)
    assert "（无）" in text
    assert not _has_any_interactive(card)


# ── 3. fail_card ──────────────────────────────────────────────────────────

def test_fail_card_renders_error():
    card = cb.build_fail_card("MCP 调用失败", context="提交预约")
    text = _all_text(card)
    assert "❌" in text
    assert "提交预约" in text


def test_fail_card_no_context():
    card = cb.build_fail_card("some error")
    assert "❌" in _all_text(card)


# ── 4. cancel_confirm_card ────────────────────────────────────────────────

def test_cancel_confirm_card_guides_text_reply():
    card = cb.build_cancel_confirm_card("PNV332", start_time="2026-06-30 14:00")
    text = _all_text(card)
    assert "确认取消" in text
    assert "PNV332" in text
    assert "14:00" in text
    assert "确认" in text and "算了" in text
    assert not _has_any_interactive(card)


# ── 5. records_card ───────────────────────────────────────────────────────

def test_records_card_empty():
    card = cb.build_records_card([], title="我的预约")
    text = _all_text(card)
    assert "我的预约" in text
    assert "暂无记录" in text
    assert not _has_any_interactive(card)


def test_records_card_renders_items():
    records = [
        {"vehicle_no": "PNV332", "platform": "Xavier",
         "start_time": "2026-06-30 09:00", "end_time": "2026-06-30 10:00",
         "task_name": "测试", "location": "A区", "status": "待审批"},
        {"vehicle_no": "SVV027", "platform": "Orin",
         "start_time": "2026-06-29 14:00", "end_time": "2026-06-29 16:00",
         "task_name": "调试", "location": "B区", "status": "已批准"},
    ]
    card = cb.build_records_card(records, title="我的预约", show_cancel=True)
    text = _all_text(card)
    assert "PNV332" in text  # 待审批条显示完整编号（方便打字取消）
    assert "测试" in text
    # 不应有任何可交互元素
    assert not _has_any_interactive(card)
    # 应有打字取消引导
    assert "取消" in text and "PNV332" in text


def test_records_card_no_interactive_even_when_show_cancel():
    """2026-06-30 Phase 1.6：show_cancel=True 也不再产按钮，全打字交互。"""
    records = [{"vehicle_no": "PNV332", "platform": "Xavier",
                "start_time": "2026-06-30 09:00", "end_time": "2026-06-30 10:00",
                "task_name": "t", "location": "L", "status": "待审批"}]
    card = cb.build_records_card(records, title="我的预约", show_cancel=True)
    assert not _has_any_interactive(card)
