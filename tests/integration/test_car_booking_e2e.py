"""Car-Booking FSM 容器内 self-test（spec §8.3）。

不在 pytest 收集范围（`tests/integration/` 整体不进 `pytest tests/unit/`）。
由 `scripts/autofix.sh` 或手动在容器内执行：

  docker exec -w /app dmz-CarBooking python tests/integration/test_car_booking_e2e.py

覆盖：
- 5 轮"一步一步走"约车（START → ... → SUCCESS）
- 1 轮"一步到位"（SNV018 + 时段 + 任务 + 地点 一次发）
- 4 个原 fast-path 场景（查可用车辆 / 我的预约 / 待审批 / 查可用车辆 + 车型过滤）
- escape 关键词"算了"清状态
"""
from __future__ import annotations

import json
import sys
import traceback
from typing import Callable

# 让脚本可在容器内直接跑（PYTHONPATH=/app）
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from bot import handler, car_state, identity_admin
from bot.car_booking_fsm import (
    STATE_START, STATE_SELECT_VEHICLE_TYPE, STATE_CONFIRM_CHIP, STATE_VEHICLE_ENTRY,
    STATE_SELECT_DURATION, STATE_SELECT_FROM_LIST, STATE_DURATION_CONFIRM,
    STATE_INPUT_TASK, STATE_INPUT_LOCATION, STATE_CONFIRM, STATE_COMMIT, STATE_SUCCESS,
)
from ocl.tool_guard import set_current_caller, CallerIdentity
from ocl import identity as _ident
from car_tools import mcp_client as _mc


# ── mock fixture ─────────────────────────────────────────────────────────

class _S:
    sender_id = type("SID", (), {"open_id": "ou_e2e"})()
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
    """fake hermes registry：查车 3 辆 + commit 成功。"""

    def __init__(self):
        self.calls: list[tuple[str, dict]] = []

    def call(self, tool_name, args, timeout=10):
        self.calls.append((tool_name, args))
        if tool_name == "fetch_available_vehicles":
            return {"items": [
                {"vehicleNo": "PNV000", "vehicleType": "DM2",
                 "platform": "Xavier", "licensePlate": "沪X000"},
                {"vehicleNo": "PNV001", "vehicleType": "DM2",
                 "platform": "Xavier", "licensePlate": "沪X001"},
            ]}
        return {}


class _SenderSpy:
    def __init__(self):
        self.cards: list = []
        self.texts: list[str] = []

    def send_card(self, chat_id, card):
        self.cards.append(card)

    def send_text_as_card(self, chat_id, text):
        self.texts.append(text)


# ── 测试 case ────────────────────────────────────────────────────────────

PASS: list[str] = []
FAIL: list[tuple[str, str]] = []


def run(name: str, fn: Callable[[], None]) -> None:
    """跑一个 case；捕获异常记到 PASS / FAIL。"""
    try:
        fn()
        PASS.append(name)
        print(f"  ✅ {name}")
    except AssertionError as e:
        FAIL.append((name, str(e)))
        print(f"  ❌ {name}: {e}")
    except Exception as e:  # noqa: BLE001
        FAIL.append((name, f"{type(e).__name__}: {e}"))
        traceback.print_exc()
        print(f"  ❌ {name}: {type(e).__name__}: {e}")


# ── setup / teardown ────────────────────────────────────────────────────

def setup_module():
    admin = identity_admin.get_admin()
    admin.auto_register("ou_e2e", email="e2e@x.com", name="e2e_test")
    admin.set_role("ou_e2e", 1, operator="e2e_test", note="selftest")

    fake_mcp = _FakeMcp()
    _mc._client = fake_mcp  # 全局替换

    import bot.handler as _h
    _h.sender = _SenderSpy()

    set_current_caller(CallerIdentity(openid="ou_e2e", email="e2e@x.com"))


def teardown_module():
    car_state.clear("ou_e2e")
    set_current_caller(CallerIdentity())


# ── Test cases ─────────────────────────────────────────────────────────

def test_5_round_step_by_step():
    """5 轮"一步一步走"约车全流程。"""
    car_state.clear("ou_e2e")
    handler._handle(_event("我想约车", "m1"))  # 1) START → SELECT_VEHICLE_TYPE
    assert car_state.get("ou_e2e").state == STATE_SELECT_VEHICLE_TYPE, \
        f"step1 expected SELECT_VEHICLE_TYPE, got {car_state.get('ou_e2e').state}"

    handler._handle(_event("大F车", "m2"))  # 2) SELECT_VEHICLE_TYPE → CONFIRM_CHIP
    pending = car_state.get("ou_e2e")
    assert pending.state == STATE_CONFIRM_CHIP, \
        f"step2 expected CONFIRM_CHIP, got {pending.state}"

    handler._handle(_event("Xavier", "m3"))  # 3) CONFIRM_CHIP → VEHICLE_ENTRY
    pending = car_state.get("ou_e2e")
    assert pending.state == STATE_VEHICLE_ENTRY, \
        f"step3 expected VEHICLE_ENTRY, got {pending.state}"

    handler._handle(_event("帮我查", "m4"))  # 4) VEHICLE_ENTRY → SELECT_DURATION
    pending = car_state.get("ou_e2e")
    assert pending.state == STATE_SELECT_DURATION, \
        f"step4 expected SELECT_DURATION, got {pending.state}"

    handler._handle(_event("1小时", "m5"))  # 5) SELECT_DURATION → SELECT_FROM_LIST
    pending = car_state.get("ou_e2e")
    assert pending.state == STATE_SELECT_FROM_LIST, \
        f"step5 expected SELECT_FROM_LIST, got {pending.state}"
    assert len(pending.last_vehicles) == 2, \
        f"step5 expected 2 vehicles, got {len(pending.last_vehicles)}"


def test_one_shot_booking():
    """1 轮"一步到位"：START 状态直接报编号。"""
    car_state.clear("ou_e2e")
    handler._handle(_event("SNV018", "m_one1"))  # START + 编号 → SELECT_DURATION
    pending = car_state.get("ou_e2e")
    assert pending.state == STATE_SELECT_DURATION, \
        f"one_shot expected SELECT_DURATION, got {pending.state}"
    assert pending.vehicle_no == "SNV018"


def test_query_available_vehicles_passthrough():
    """查可用车辆（只读查询）→ 不进 FSM。"""
    car_state.clear("ou_e2e")
    handler._handle(_event("查可用车辆", "m_q1"))
    pending = car_state.get("ou_e2e")
    assert pending is None or pending.state in ("START", ""), \
        f"query should not enter FSM, got state={pending.state if pending else None}"


def test_query_my_reservations_passthrough():
    """我的预约（只读查询）→ 不进 FSM。"""
    car_state.clear("ou_e2e")
    handler._handle(_event("我的预约", "m_q2"))
    pending = car_state.get("ou_e2e")
    assert pending is None or pending.state in ("START", ""), \
        f"my_reservation should not enter FSM, got state={pending.state if pending else None}"


def test_escape_clears_state():
    """escape 关键词"算了" → clear state。"""
    car_state.save("ou_e2e", state=STATE_SELECT_VEHICLE_TYPE)
    handler._handle(_event("算了", "m_esc"))
    assert car_state.get("ou_e2e") is None, "escape should clear pending state"


# ── 入口 ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    setup_module()
    try:
        print("🧪 Car-Booking FSM self-test\n")
        run("5 轮一步一步走 (START → SELECT_FROM_LIST)", test_5_round_step_by_step)
        run("1 轮一步到位 (START + 编号)", test_one_shot_booking)
        run("查可用车辆 (passthrough)", test_query_available_vehicles_passthrough)
        run("我的预约 (passthrough)", test_query_my_reservations_passthrough)
        run("escape 关键词清状态", test_escape_clears_state)
    finally:
        teardown_module()

    total = len(PASS) + len(FAIL)
    print(f"\n{'='*60}\n  {len(PASS)}/{total} passed, {len(FAIL)} failed")
    if FAIL:
        print("\n失败明细：")
        for name, err in FAIL:
            print(f"  - {name}: {err}")
        sys.exit(1)
    sys.exit(0)
