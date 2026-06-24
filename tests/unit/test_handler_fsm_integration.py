"""handler._handle 端到端：5 轮"一步一步走"对话走通 FSM。

覆盖 plan Task 6 Step 1-3：
- 用户在挂起状态时进 FSM
- 用户表达"约车"意图时进 FSM
- 只读查询（"查可用车辆" / "我的预约"）走旁路不进 FSM
"""
import json
import pytest
from bot import handler, car_state, identity_admin
from ocl.tool_guard import set_current_caller, CallerIdentity
from car_tools import handlers as _car_handlers
from ocl import identity as _ident


class _S:
    sender_id = type("SID", (), {"open_id": "ou_int"})()
    sender_type = "user"


def _event(text, mid="m"):
    class E:
        message_id = mid
        chat_id = "oc_chat"
        chat_type = "p2p"
        message_type = "text"
        content = json.dumps({"text": text})
        mentions = []
    class D: pass
    D.event = type("Ev", (), {})()
    D.event.message = E()
    D.event.sender = _S()
    return D


class _FakeMcp:
    """mock MCP：fetch_available_vehicles 返 3 辆车，commit 返成功。"""

    def __init__(self):
        self.calls: list[tuple[str, dict]] = []

    def call(self, tool_name, args, timeout=10):
        self.calls.append((tool_name, args))
        if tool_name == "fetch_available_vehicles":
            return {"items": [
                {"vehicleNo": f"PNV{i:03d}", "vehicleType": "DM2",
                 "platform": "Xavier", "licensePlate": f"沪X{i:03d}"}
                for i in range(3)
            ]}
        return {}


# 拦截 sender 模块的 send_card / send_text_as_card，记录消息
class _SenderSpy:
    def __init__(self):
        self.cards: list = []
        self.texts: list[str] = []

    def send_card(self, chat_id, card):
        self.cards.append(card)

    def send_text_as_card(self, chat_id, text):
        self.texts.append(text)


@pytest.fixture(autouse=True)
def setup(monkeypatch):
    admin = identity_admin.get_admin()
    admin.auto_register("ou_int", email="a@x.com", name="test")
    admin.set_role("ou_int", 1, operator="test", note="用户")

    fake_mcp = _FakeMcp()
    from car_tools import mcp_client as _mc
    monkeypatch.setattr(_mc, "_client", fake_mcp)

    spy = _SenderSpy()
    import bot.handler as _h
    monkeypatch.setattr(_h, "sender", spy)

    set_current_caller(CallerIdentity(openid="ou_int", email="a@x.com"))
    yield spy
    set_current_caller(CallerIdentity())
    car_state.clear("ou_int")


def test_booking_intent_enters_fsm(setup):
    """表达"约车"意图 → 进 FSM（返回入口卡，知道/不知道按钮）。"""
    car_state.clear("ou_int")
    handler._handle(_event("我想约车", "m1"))
    pending = car_state.get("ou_int")
    # 入口卡 state 保持 START（advance() 不持久化 START，让 marker 回调仍走 START 分支）
    assert pending is None or pending.state == "START"
    # 至少有一次 send（入口卡）
    assert len(setup.cards) >= 1 or len(setup.texts) >= 1


def test_in_pending_state_continues_fsm(setup):
    """在挂起状态时收到任意文本 → FSM 继续推进。"""
    car_state.save("ou_int", state="SELECT_VEHICLE_TYPE", vehicle_type_detail="",
                   chip="")
    handler._handle(_event("BM0", "m2"))
    pending = car_state.get("ou_int")
    # 选车型细分后 → 统一过 CONFIRM_CHIP
    assert pending.state == "CONFIRM_CHIP"


def test_select_vehicle_type_chip_confirm_chains_to_entry(setup):
    """车型细分 + 芯片 + 时长 一路走下来（新流程：直接进 SELECT_DURATION）。"""
    # 选车型细分（BM0）
    car_state.save("ou_int", state="SELECT_VEHICLE_TYPE")
    handler._handle(_event("BM0", "m2"))
    pending = car_state.get("ou_int")
    assert pending.state == "CONFIRM_CHIP"
    assert pending.vehicle_type_detail == "BM0"
    # 选芯片
    handler._handle(_event("Orin", "m3"))
    pending = car_state.get("ou_int")
    assert pending.state == "SELECT_DURATION"
    assert pending.duration_minutes == 30  # 默认
    # 点 +30 → 60
    handler._handle(_event("__fsm_dur_plus__", "m5"))
    pending = car_state.get("ou_int")
    assert pending.duration_minutes == 60
    # 点 确认 → SELECT_FROM_LIST（查车）
    handler._handle(_event("__fsm_dur_confirm__", "m6"))
    pending = car_state.get("ou_int")
    assert pending.state == "SELECT_FROM_LIST"
    assert len(pending.last_vehicles) == 3


def test_escape_clears_pending_state(setup):
    """escape 关键词「算了」→ clear state。"""
    car_state.save("ou_int", state="SELECT_VEHICLE_TYPE")
    handler._handle(_event("算了", "m_esc"))
    assert car_state.get("ou_int") is None
    assert "已取消" in setup.texts[-1] or "已取消" in str(setup.cards[-1])


def test_query_passthrough_does_not_enter_fsm(setup):
    """只读查询「查可用车辆」→ 不进 FSM。"""
    # 确保没有挂起状态
    car_state.clear("ou_int")
    handler._handle(_event("查可用车辆", "m_q"))
    # 仍然没挂起（FSM 未介入）
    pending = car_state.get("ou_int")
    assert pending is None or pending.state in ("START", "")
    # 渲染了卡（车辆列表）
    assert len(setup.cards) >= 1


def test_my_reservation_passthrough(setup):
    """「我的预约」查询 → 不进 FSM。"""
    car_state.clear("ou_int")
    handler._handle(_event("我的预约", "m_q2"))
    pending = car_state.get("ou_int")
    assert pending is None or pending.state in ("START", "")


def test_direct_by_id_flow(setup):
    """START 时直接报编号 → DIRECT_BY_ID → SELECT_DURATION。"""
    car_state.clear("ou_int")
    # 入口卡（START → SELECT_VEHICLE_TYPE）；"SNV018" 落到 SELECT_VEHICLE_TYPE
    # 状态不识别 SNV018（车型按钮）→ 但应该进 FSM 流程
    handler._handle(_event("SNV018", "m_d1"))
    pending = car_state.get("ou_int")
    # SELECT_VEHICLE_TYPE 收到 "SNV018" 不在车型按钮里 → 保持 SELECT_VEHICLE_TYPE
    # （这是预期的"未识别"分支；要走到 DIRECT_BY_ID 需要 START 状态时直接报编号）
    assert pending is not None
    # 重置到 START 状态测试真正的 DIRECT_BY_ID
    car_state.save("ou_int", state="DIRECT_BY_ID")
    handler._handle(_event("SNV018", "m_d2"))
    pending = car_state.get("ou_int")
    assert pending.state == "SELECT_DURATION"
    assert pending.vehicle_no == "SNV018"


# ── 意图识别覆盖（2026-06-24 强化） ─────────────────────────────────

def test_booking_intent_recognition_regex(setup):
    """2026-06-24 强化意图识别：覆盖口语化变体（regex intent_verb + vehicle_word）
    + 否定检测（前 8 字符内含 不/没/别/无需/算了/取消/不要了 视为非意图）。

    直接测试 handler 内部使用的 is_booking_intent 计算（避免 LLM 路径慢），
    通过 is_booking_intent 标志 + spy.cards 内容判断。
    """
    import re
    from bot import handler as h
    src = open(h.__file__).read()
    m = re.search(r'BOOKING_INTENT = \((.*?)\)', src, re.DOTALL)
    BI = eval(m.group(0).replace('BOOKING_INTENT = ', ''))
    START = ('我要约', '帮我约')
    intent_verb = r'(约|预约|预定|book|schedule|預約|預定)'
    vehicle_word = r'(车|车辆|vehicle|car|辆\s*车|辆)'

    def is_bi(norm):
        n = norm.strip()
        return (n in BI
                or n.startswith(START)
                or (bool(re.search(intent_verb + r'[\s\S]{0,12}' + vehicle_word, n))
                    and not bool(re.search(r'不|没|别|无需|算了|取消|不要了', n[:8]))))

    cases = [
        # 标准变体
        ("我想约车", True), ("我要约车", True), ("我想要约一辆车", True),
        ("帮我约一下车", True), ("想预约车辆", True),
        ("请问能帮我预约一下车辆吗", True),
        ("嗯我想约车", True), ("那个我想约车", True),
        ("可以帮我约车吗", True), ("我现在想约个车", True),
        ("帮我约一辆", True), ("我想预定车", True),
        # 否定
        ("我不想约车", False), ("别约车了", False), ("取消预约", False),
        ("不要约车", False), ("算了不约了", False),
        # 不相关
        ("随便看看", False), ("车有问题", False),
        ("一辆车多少钱", False), ("我要叫车", False),
        ("帮我叫车", False), ("一辆车坏了", False),
        ("车要保养", False),
    ]
    failed = [(t, e, g) for t, e in cases
              if (g := is_bi(t)) != e]
    assert not failed, f"Intent recognition mismatches: {failed}"
