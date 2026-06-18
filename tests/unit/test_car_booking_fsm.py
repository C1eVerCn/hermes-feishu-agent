"""bot/car_booking_fsm.py 单元测试：14 状态机。"""
import json
import pytest
from bot.car_booking_fsm import (
    CarBookingFSM,
    STATE_START,
    STATE_DIRECT_BY_ID,
    STATE_SELECT_VEHICLE_TYPE,
    STATE_SELECT_FROM_LIST,
    STATE_DURATION_CONFIRM,
    STATE_SELECT_TIME,
    advance,
    _resolve_vehicle_from_text,
    _match_slots,
)
from bot import car_state
from car_tools import mcp_client as _mc


def test_states_defined():
    """13 个状态名必须存在（spec §3.2，2026-06-18 删 VEHICLE_ENTRY）。"""
    expected = {
        "STATE_START", "STATE_DIRECT_BY_ID", "STATE_SELECT_VEHICLE_TYPE",
        "STATE_CONFIRM_CHIP", "STATE_SELECT_DURATION",
        "STATE_SELECT_FROM_LIST", "STATE_DURATION_CONFIRM", "STATE_SELECT_TIME",
        "STATE_INPUT_TASK", "STATE_INPUT_LOCATION", "STATE_CONFIRM",
        "STATE_COMMIT", "STATE_SUCCESS",
    }
    from bot import car_booking_fsm as fsm
    actual = {n for n in dir(fsm) if n.startswith("STATE_")}
    assert expected.issubset(actual), f"missing states: {expected - actual}"


def test_fsm_class_instantiable():
    """CarBookingFSM() 不需要参数。"""
    fsm = CarBookingFSM()
    assert fsm is not None


def test_advance_start_returns_entry_card():
    """START → 入口卡（知道/不知道 按钮）。状态保持 START 等用户点按钮。"""
    car_state.clear("ou_t1")
    new_state, resp = advance("ou_t1", "")
    assert new_state == "START"
    assert "card" in resp or "text" in resp  # 任一渲染形式


def test_advance_select_vehicle_type_button():
    """SELECT_VEHICLE_TYPE 收车型细分按钮 → 统一进 CONFIRM_CHIP（用户偏好：选完车型再选芯片）。"""
    car_state.save("ou_t2", state="SELECT_VEHICLE_TYPE")
    new_state, resp = advance("ou_t2", "BM0")
    assert new_state == "CONFIRM_CHIP"


# ── _resolve_vehicle_from_text helper ─────────────────────────────────────────

def test_resolve_by_index():
    """数字 → 列表第 N 个（1-based）。"""
    candidates = [
        {"vehicle_no": "PNV001"}, {"vehicle_no": "PNV002"}, {"vehicle_no": "PNV003"},
    ]
    assert _resolve_vehicle_from_text("1", candidates)["vehicle_no"] == "PNV001"
    assert _resolve_vehicle_from_text("3", candidates)["vehicle_no"] == "PNV003"


def test_resolve_by_full_id():
    """完整 vehicle_no（大小写不敏感）。"""
    candidates = [{"vehicle_no": "SNV018"}, {"vehicle_no": "PNV003"}]
    assert _resolve_vehicle_from_text("snv018", candidates)["vehicle_no"] == "SNV018"
    assert _resolve_vehicle_from_text("PNV003", candidates)["vehicle_no"] == "PNV003"


def test_resolve_by_suffix():
    """后缀匹配（≥3 字符）。"""
    candidates = [{"vehicle_no": "SNV018"}, {"vehicle_no": "PNV003"}]
    assert _resolve_vehicle_from_text("SNV018", candidates)["vehicle_no"] == "SNV018"
    # ≥3 才匹配
    assert _resolve_vehicle_from_text("003", candidates)["vehicle_no"] == "PNV003"
    # 短串不匹配
    assert _resolve_vehicle_from_text("03", candidates) is None


def test_resolve_returns_none():
    """空 / 越界 / 无匹配 → None。"""
    assert _resolve_vehicle_from_text("", []) is None
    assert _resolve_vehicle_from_text("99", [{"vehicle_no": "PNV001"}]) is None
    assert _resolve_vehicle_from_text("garbage", [{"vehicle_no": "PNV001"}]) is None


# ── _match_slots helper ──────────────────────────────────────────────────────

def test_match_slots_returns_three_candidates():
    """≤6 个候选时段，duration_minutes=120 走完应返非空（2026-06-18 review 3→6）。"""
    slots = _match_slots("PNV001", 120)
    assert len(slots) == 6
    assert all("start" in s and "end" in s and "label" in s for s in slots)


def test_match_slots_8h_cap():
    """>8h（480 分钟）→ 空列表。"""
    assert _match_slots("PNV001", 500) == []


# ── SELECT_FROM_LIST state ───────────────────────────────────────────────────

class _FakeMcpWithCars:
    """mock hermes registry：fetch_available_vehicles 返 5 辆车。"""

    def call(self, tool_name, args, timeout=10):
        if tool_name == "fetch_available_vehicles":
            return {"items": [
                {"vehicleNo": f"PNV{i:03d}", "vehicleType": "DM2",
                 "platform": "Xavier", "licensePlate": f"沪X{i:03d}"}
                for i in range(5)
            ]}
        return {}


def test_advance_select_from_list_returns_table(monkeypatch):
    """SELECT_FROM_LIST 查车 → 表格卡 + 缓存 last_vehicles 到 car_state。
    新流程：SELECT_DURATION 收到「确认」且 vehicle_no 空 → 查车 → SELECT_FROM_LIST。"""
    monkeypatch.setattr(_mc, "_client", _FakeMcpWithCars())
    car_state.save("ou_t3", state="SELECT_DURATION", vehicle_type_detail="DM0", chip="Xavier",
                   duration_minutes=60)
    new_state, resp = advance("ou_t3", "__fsm_dur_confirm__")
    assert new_state == "SELECT_FROM_LIST"
    assert "card" in resp
    pending = car_state.get("ou_t3")
    assert len(pending.last_vehicles) == 5
    assert pending.last_vehicles[0]["vehicle_no"] == "PNV000"
    car_state.clear("ou_t3")


def test_advance_select_from_list_no_cars(monkeypatch):
    """查车无结果 → 空表卡，仍留在 SELECT_FROM_LIST。"""

    class _Empty:
        def call(self, tool_name, args, timeout=10):
            return {"items": []}

    monkeypatch.setattr(_mc, "_client", _Empty())
    car_state.save("ou_t3b", state="SELECT_DURATION", vehicle_type_detail="DM0", chip="Xavier",
                   duration_minutes=60)
    new_state, resp = advance("ou_t3b", "__fsm_dur_confirm__")
    assert new_state == "SELECT_FROM_LIST"
    pending = car_state.get("ou_t3b")
    assert pending.last_vehicles == []
    car_state.clear("ou_t3b")


def test_advance_select_from_list_fetch_error(monkeypatch):
    """查车抛异常 → 空列表 + 留在 SELECT_FROM_LIST。"""

    class _Fail:
        def call(self, tool_name, args, timeout=10):
            from car_tools.mcp_client import McpError
            raise McpError("upstream timeout")

    monkeypatch.setattr(_mc, "_client", _Fail())
    car_state.save("ou_t3c", state="SELECT_DURATION", vehicle_type_detail="DM0", chip="Xavier",
                   duration_minutes=60)
    new_state, resp = advance("ou_t3c", "__fsm_dur_confirm__")
    assert new_state == "SELECT_FROM_LIST"
    pending = car_state.get("ou_t3c")
    assert pending.last_vehicles == []
    car_state.clear("ou_t3c")


# ── DURATION_CONFIRM state ──────────────────────────────────────────────────

def test_advance_duration_confirm_by_index(monkeypatch):
    """DURATION_CONFIRM 收「选 1」（vehicle_no 未定）→ 写 vehicle + 渲染时段按钮。"""
    candidates = [
        {"vehicle_no": "PNV000", "vehicle_type": "DM2", "platform": "Xavier",
         "license_plate": "沪X000"},
        {"vehicle_no": "PNV001", "vehicle_type": "DM2", "platform": "Xavier",
         "license_plate": "沪X001"},
    ]
    car_state.save("ou_t4", state="DURATION_CONFIRM", vehicle_type="DM2", chip="Xavier",
                   duration_minutes=120, last_vehicles=candidates)
    new_state, resp = advance("ou_t4", "选 1")
    # 新设计：先解析 vehicle → 留在 DURATION_CONFIRM 展示时段按钮
    assert new_state == "DURATION_CONFIRM"
    assert "buttons" in resp  # 候选时段按钮
    pending = car_state.get("ou_t4")
    assert pending.vehicle_no == "PNV000"
    assert len(pending.last_slots) == 6  # 候选时段已缓存（2026-06-18 review 3→6）
    car_state.clear("ou_t4")


def test_advance_duration_confirm_by_full_id(monkeypatch):
    """DURATION_CONFIRM 收「PNV001」（vehicle_no 未定）→ 解析为第二个候选。"""
    candidates = [
        {"vehicle_no": "PNV000", "vehicle_type": "DM2", "platform": "Xavier"},
        {"vehicle_no": "PNV001", "vehicle_type": "DM2", "platform": "Xavier"},
    ]
    car_state.save("ou_t5", state="DURATION_CONFIRM", vehicle_type="DM2", chip="Xavier",
                   duration_minutes=60, last_vehicles=candidates)
    new_state, resp = advance("ou_t5", "PNV001")
    assert new_state == "DURATION_CONFIRM"
    pending = car_state.get("ou_t5")
    assert pending.vehicle_no == "PNV001"
    car_state.clear("ou_t5")


def test_advance_duration_confirm_unrecognized(monkeypatch):
    """DURATION_CONFIRM 收未识别文本（vehicle_no 未定）→ 保持 + 提示。"""
    candidates = [{"vehicle_no": "PNV000", "vehicle_type": "DM2"}]
    car_state.save("ou_t6", state="DURATION_CONFIRM", vehicle_type="DM2", chip="Xavier",
                   duration_minutes=60, last_vehicles=candidates)
    new_state, resp = advance("ou_t6", "garbage")
    assert new_state == "DURATION_CONFIRM"
    assert "未识别车辆" in resp["text"]
    car_state.clear("ou_t6")


def test_advance_duration_confirm_no_slots(monkeypatch):
    """_match_slots 返空（duration>8h）→ SELECT_TIME 兜底。"""
    candidates = [{"vehicle_no": "PNV000", "vehicle_type": "DM2"}]
    car_state.save("ou_t7", state="DURATION_CONFIRM", vehicle_type="DM2", chip="Xavier",
                   duration_minutes=600, last_vehicles=candidates)  # > 8h cap
    new_state, resp = advance("ou_t7", "选 1")
    assert new_state == "SELECT_TIME"
    assert "buttons" in resp
    car_state.clear("ou_t7")


# ── SELECT_TIME state ───────────────────────────────────────────────────────

def test_advance_select_time_to_input_task():
    """SELECT_TIME 收任何文本 → INPUT_TASK。"""
    car_state.save("ou_t8", state="SELECT_TIME", vehicle_no="PNV000",
                   vehicle_type="DM2", chip="Xavier", duration_minutes=120)
    new_state, resp = advance("ou_t8", "1小时后")
    assert new_state == "INPUT_TASK"
    car_state.clear("ou_t8")


# ── DIRECT_BY_ID state ────────────────────────────────────────────────────

def test_advance_direct_by_id_valid_format():
    """DIRECT_BY_ID 收有效编号 → SELECT_DURATION（带 duration_card）。"""
    car_state.save("ou_t9", state="DIRECT_BY_ID")
    new_state, resp = advance("ou_t9", "SNV018")
    assert new_state == "SELECT_DURATION"
    # 新版渲染 duration_card（不是 text+buttons）
    assert "card" in resp
    pending = car_state.get("ou_t9")
    assert pending.vehicle_no == "SNV018"
    car_state.clear("ou_t9")


def test_advance_direct_by_id_lowercase_normalized():
    """DIRECT_BY_ID 小写编号也接受。"""
    car_state.save("ou_t9b", state="DIRECT_BY_ID")
    new_state, resp = advance("ou_t9b", "snv018")
    assert new_state == "SELECT_DURATION"
    assert car_state.get("ou_t9b").vehicle_no == "SNV018"
    car_state.clear("ou_t9b")


def test_advance_direct_by_id_invalid_format():
    """DIRECT_BY_ID 格式不符 → 保持 + 提示。"""
    car_state.save("ou_t9c", state="DIRECT_BY_ID")
    new_state, resp = advance("ou_t9c", "garbage")
    assert new_state == "DIRECT_BY_ID"
    assert "编号格式不符" in resp["text"]
    car_state.clear("ou_t9c")


# ── DURATION_CONFIRM 时段子流程 ─────────────────────────────────────────────

def test_advance_duration_confirm_pick_slot_by_index():
    """DURATION_CONFIRM vehicle_no 已定 → 选时段 1 → INPUT_TASK。"""
    slots = [
        {"start": "2026-06-17 14:00", "end": "2026-06-17 16:00",
         "label": "06-17 14:00 ~ 16:00"},
        {"start": "2026-06-17 18:00", "end": "2026-06-17 20:00",
         "label": "06-17 18:00 ~ 20:00"},
    ]
    car_state.save("ou_t10", state="DURATION_CONFIRM", vehicle_no="PNV000",
                   vehicle_type="DM2", chip="Xavier", duration_minutes=120,
                   last_slots=slots)
    new_state, resp = advance("ou_t10", "1")
    assert new_state == "INPUT_TASK"
    pending = car_state.get("ou_t10")
    assert pending.start_time == "2026-06-17 14:00"
    assert pending.time_range_start == "2026-06-17 14:00"
    car_state.clear("ou_t10")


def test_advance_duration_confirm_pick_slot_by_full_time():
    """DURATION_CONFIRM 用完整时间反查时段。"""
    slots = [
        {"start": "2026-06-17 14:00", "end": "2026-06-17 16:00", "label": "x"},
        {"start": "2026-06-17 18:00", "end": "2026-06-17 20:00", "label": "y"},
    ]
    car_state.save("ou_t10b", state="DURATION_CONFIRM", vehicle_no="PNV000",
                   vehicle_type="DM2", chip="Xavier", duration_minutes=120,
                   last_slots=slots)
    new_state, resp = advance("ou_t10b", "2026-06-17 18:00")
    assert new_state == "INPUT_TASK"
    pending = car_state.get("ou_t10b")
    assert pending.start_time == "2026-06-17 18:00"
    car_state.clear("ou_t10b")


def test_advance_duration_confirm_pick_slot_unrecognized():
    """DURATION_CONFIRM 时段未识别 → 保持。"""
    slots = [{"start": "2026-06-17 14:00", "end": "2026-06-17 16:00", "label": "x"}]
    car_state.save("ou_t10c", state="DURATION_CONFIRM", vehicle_no="PNV000",
                   vehicle_type="DM2", chip="Xavier", duration_minutes=120,
                   last_slots=slots)
    new_state, resp = advance("ou_t10c", "99")
    assert new_state == "DURATION_CONFIRM"
    assert "未识别时段" in resp["text"]
    car_state.clear("ou_t10c")


# ── INPUT_TASK state ──────────────────────────────────────────────────────

def test_advance_input_task_saves_text():
    """INPUT_TASK 收文本 → 写 task_name → INPUT_LOCATION。"""
    car_state.save("ou_t11", state="INPUT_TASK", vehicle_no="PNV000",
                   time_range_start="2026-06-17 14:00",
                   time_range_end="2026-06-17 16:00")
    new_state, resp = advance("ou_t11", "MFF调试")
    assert new_state == "INPUT_LOCATION"
    pending = car_state.get("ou_t11")
    assert pending.task_name == "MFF调试"
    car_state.clear("ou_t11")


def test_advance_input_task_empty_keeps_state():
    """INPUT_TASK 收空文本 → 保持 + 提示。"""
    car_state.save("ou_t11b", state="INPUT_TASK", vehicle_no="PNV000")
    new_state, resp = advance("ou_t11b", "")
    assert new_state == "INPUT_TASK"
    assert "任务名称不能为空" in resp["text"]
    car_state.clear("ou_t11b")


# ── INPUT_LOCATION state ──────────────────────────────────────────────────

def test_advance_input_location_saves_and_goes_to_confirm():
    """INPUT_LOCATION 收文本 → 写 location → CONFIRM 卡。"""
    car_state.save("ou_t12", state="INPUT_LOCATION", vehicle_no="PNV000",
                   vehicle_type="DM2", chip="Xavier", duration_minutes=120,
                   time_range_start="2026-06-17 14:00",
                   time_range_end="2026-06-17 16:00", task_name="MFF调试")
    new_state, resp = advance("ou_t12", "上海")
    assert new_state == "CONFIRM"
    assert "card" in resp
    pending = car_state.get("ou_t12")
    assert pending.location == "上海"
    car_state.clear("ou_t12")


# ── CONFIRM state ─────────────────────────────────────────────────────────

def test_advance_confirm_to_commit(monkeypatch):
    """CONFIRM 收「确认」→ 同一次 advance 内完成 commit → SUCCESS（commit 3b504db
    设计：避免切到 STATE_COMMIT 等下一次 advance 时用户没发消息卡住）。
    """
    from car_tools import handlers as _h
    from ocl.tool_guard import set_current_caller, CallerIdentity

    class _FakeHandlers:
        @staticmethod
        def _commit_single_vehicle_reservation(args, **_):
            return json.dumps({"vehicle_no": "PNV000", "vehicle_type": "DM2",
                               "vehicle_type_detail": "DM0",
                               "platform": "Xavier", "license_plate": "沪X000",
                               "start_time": "2026-06-17 14:00",
                               "end_time": "2026-06-17 16:00",
                               "task_name": "MFF调试", "location": "上海"})

    monkeypatch.setattr(_h, "_commit_single_vehicle_reservation",
                        _FakeHandlers._commit_single_vehicle_reservation)
    # _do_commit 会 set_current_caller + 调 identity.email_of；注入假身份免触发网络
    set_current_caller(CallerIdentity(openid="ou_t13", email="t13@x.com"))
    monkeypatch.setattr("ocl.identity.email_of", lambda uid: "t13@x.com")

    car_state.save("ou_t13", state="CONFIRM", vehicle_no="PNV000",
                   vehicle_type="DM2", vehicle_type_detail="DM0",
                   chip="Xavier", duration_minutes=120,
                   start_time="2026-06-17 14:00", end_time="2026-06-17 16:00",
                   time_range_start="2026-06-17 14:00",
                   time_range_end="2026-06-17 16:00",
                   task_name="MFF调试", location="上海")
    new_state, resp = advance("ou_t13", "确认")
    assert new_state == "SUCCESS"
    assert "card" in resp
    car_state.clear("ou_t13")


def test_advance_confirm_cancel_returns_to_start():
    """CONFIRM 收「取消」→ clear state → START。"""
    car_state.save("ou_t13b", state="CONFIRM", vehicle_no="PNV000",
                   task_name="MFF调试", location="上海")
    new_state, resp = advance("ou_t13b", "取消")
    assert new_state == "START"
    assert car_state.get("ou_t13b") is None  # 已清


def test_advance_confirm_modify_returns_to_task():
    """CONFIRM 收「修改」→ INPUT_TASK。"""
    car_state.save("ou_t13c", state="CONFIRM", vehicle_no="PNV000",
                   task_name="MFF调试", location="上海")
    new_state, resp = advance("ou_t13c", "修改")
    assert new_state == "INPUT_TASK"
    car_state.clear("ou_t13c")


# ── COMMIT state ──────────────────────────────────────────────────────────

def test_advance_commit_to_success(monkeypatch):
    """COMMIT 调 _commit_single_vehicle_reservation → SUCCESS 卡。"""
    from car_tools import handlers as _h

    class _FakeHandlers:
        @staticmethod
        def _commit_single_vehicle_reservation(args, **_):
            return json.dumps({"vehicle_no": "PNV000", "vehicle_type": "DM2",
                               "vehicle_type_detail": "DM0",
                               "platform": "Xavier", "license_plate": "沪X000",
                               "start_time": "2026-06-17 14:00",
                               "end_time": "2026-06-17 16:00",
                               "task_name": "MFF调试", "location": "上海"})

    monkeypatch.setattr(_h, "_commit_single_vehicle_reservation",
                        _FakeHandlers._commit_single_vehicle_reservation)
    car_state.save("ou_t14", state="COMMIT", vehicle_no="PNV000",
                   vehicle_type="DM2", vehicle_type_detail="DM0",
                   chip="Xavier", duration_minutes=120,
                   start_time="2026-06-17 14:00", end_time="2026-06-17 16:00",
                   time_range_start="2026-06-17 14:00",
                   time_range_end="2026-06-17 16:00",
                   task_name="MFF调试", location="上海",
                   last_vehicles=[{"vehicle_no": "PNV000", "vehicle_type": "DM2",
                                    "vehicle_type_detail": "DM0", "platform": "Xavier"}])
    new_state, resp = advance("ou_t14", "")
    assert new_state == "SUCCESS"
    assert "card" in resp
    car_state.clear("ou_t14")


def test_advance_commit_error_returns_to_start(monkeypatch):
    """COMMIT 调 commit 失败 → START + 错误提示。"""
    from car_tools import handlers as _h

    class _Fail:
        @staticmethod
        def _commit_single_vehicle_reservation(args, **_):
            return json.dumps({"error": "车辆已被占用"})

    monkeypatch.setattr(_h, "_commit_single_vehicle_reservation",
                        _Fail._commit_single_vehicle_reservation)
    car_state.save("ou_t14b", state="COMMIT", vehicle_no="PNV000",
                   vehicle_type="DM2", vehicle_type_detail="DM0",
                   chip="Xavier", duration_minutes=120,
                   start_time="2026-06-17 14:00", end_time="2026-06-17 16:00",
                   time_range_start="2026-06-17 14:00",
                   time_range_end="2026-06-17 16:00",
                   task_name="MFF调试", location="上海",
                   last_vehicles=[{"vehicle_no": "PNV000", "vehicle_type": "DM2",
                                    "vehicle_type_detail": "DM0", "platform": "Xavier"}])
    new_state, resp = advance("ou_t14b", "")
    assert new_state == "START"
    assert "提交失败" in resp["text"]
    car_state.clear("ou_t14b")


# ── SUCCESS state ─────────────────────────────────────────────────────────

def test_advance_success_clears_state():
    """SUCCESS 收任何文本 → clear + 回 START。"""
    car_state.save("ou_t15", state="SUCCESS", vehicle_no="PNV000",
                   task_name="MFF调试")
    new_state, resp = advance("ou_t15", "")
    assert new_state == "START"
    assert car_state.get("ou_t15") is None  # 清空


# ── 端到端多轮 ─────────────────────────────────────────────────────────────

def test_e2e_pick_slot_through_input_task():
    """DURATION_CONFIRM 选时段 → INPUT_TASK。"""
    slots = [
        {"start": "2026-06-17 14:00", "end": "2026-06-17 16:00", "label": "x"},
    ]
    car_state.save("ou_e2e", state="DURATION_CONFIRM", vehicle_no="PNV000",
                   vehicle_type="DM2", chip="Xavier", duration_minutes=120,
                   last_slots=slots)
    # 选时段
    new_state, _ = advance("ou_e2e", "1")
    assert new_state == "INPUT_TASK"
    # 输入任务
    new_state, _ = advance("ou_e2e", "MFF调试")
    assert new_state == "INPUT_LOCATION"
    # 输入地点
    new_state, _ = advance("ou_e2e", "上海")
    assert new_state == "CONFIRM"
    car_state.clear("ou_e2e")
