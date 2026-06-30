# 多轮引导式约车 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让约车 agent 在一次对话内记得上文、能跨轮收集槽位并应对用户改主意，同时保住 dry_run→commit 安全不变量。

**Architecture:** 在 `bot/agent_pool` 维护 per-user 内存 deque（最近 6 轮，30min TTL），handler 用 `agent.run_conversation(..., conversation_history=hist)` 取代不带历史的 `agent.chat()`；新增 `bot/dry_run_state.py` 让 dry_run 快照跨轮存活给 commit 守卫，并收紧 fail-open 漏洞；放松 prompt 规则 #1 实现"最多问一个澄清问题"；把当前时间从常驻 system prompt 移到每轮 preamble。结构性护栏（L1/L2/email 注入/schema/ROLE_TOOLS）一行不改。

**Tech Stack:** Python 3.14, hermes-agent (`run_agent.AIAgent`), pytest, contextvars, threading。

**Spec:** `docs/superpowers/specs/2026-06-30-multiturn-guided-booking-design.md`

---

## File Structure

| 文件 | 责任 | 动作 |
|------|------|------|
| `bot/dry_run_state.py` | 跨轮 dry_run 快照（内存 / 600s TTL / 脱敏） | **Create** |
| `tests/unit/test_dry_run_state.py` | dry_run_state 单测 | **Create** |
| `car_tools/handlers.py` | commit 守卫双源 + 收紧 fail-open | **Modify** `_check_dry_run_guard`（41-76）+ 新增 2 个 helper |
| `tests/unit/test_commit_guard.py` | 更新 fail-open 测试 + 新增多轮/双源测试 | **Modify** |
| `bot/agent_pool.py` | per-user 历史 deque + 去掉常驻时间 + 放松规则#1 | **Modify** `AgentPool`、`_FEISHU_SYSTEM_PROMPT_BASE`、`get_or_create` |
| `tests/unit/test_agent_pool_history.py` | 历史 deque 单测 | **Create** |
| `bot/handler.py` | run_conversation + append_turn + dry_run_state.save + 时间 preamble | **Modify** `_run_agent`（195-262）、`_handle` fast_path 段、新增 helper |
| `tests/unit/test_handler_multiturn.py` | handler 多轮接线单测 | **Create** |
| `bot/skills/car-booking/SKILL.md` | 核心原则 #1 放松 | **Modify** 第 15 行 |
| `tests/unit/test_system_prompt.py` | 新增澄清策略断言 | **Modify** |
| `CLAUDE.md` / `bot/CLAUDE.md` | 修正失实描述（state.db / agent.history） | **Modify** |

---

## Task 1: `bot/dry_run_state.py` — 跨轮 dry_run 快照

**Files:**
- Create: `bot/dry_run_state.py`
- Test: `tests/unit/test_dry_run_state.py`

- [ ] **Step 1: 写失败测试**

```python
# tests/unit/test_dry_run_state.py
"""bot/dry_run_state 单测：跨轮 dry_run 快照（内存 / 600s TTL / 脱敏）。"""
import pytest

from bot import dry_run_state


@pytest.fixture(autouse=True)
def _fresh():
    with dry_run_state._lock:
        dry_run_state._store.clear()
    dry_run_state.set_clock(lambda: 1000.0)  # 固定时钟
    yield
    with dry_run_state._lock:
        dry_run_state._store.clear()
    dry_run_state.set_clock(__import__("time").monotonic)


_ARGS = {
    "vehicle_type": "DM2", "platform": "Xavier",
    "start_time": "2026-06-30 09:00", "end_time": "2026-06-30 10:00",
    "task_name": "MFF调试", "location": "上海",
}


def test_save_then_get_roundtrip():
    dry_run_state.save("ou_a", dict(_ARGS))
    snap = dry_run_state.get("ou_a")
    assert snap is not None
    assert snap["args"]["platform"] == "Xavier"
    assert snap["ts"] == 1000.0


def test_get_returns_none_when_absent():
    assert dry_run_state.get("ou_missing") is None


def test_save_redacts_identity_and_extra_fields():
    """只保留 6 个业务字段；email/openid/mobile/任意多余键必须被剔除。"""
    polluted = dict(_ARGS)
    polluted.update({"emailAddress": "a@x.com", "openId": "ou_a",
                     "mobile": "13800000000", "secret": "sk-xxx"})
    dry_run_state.save("ou_a", polluted)
    stored = dry_run_state.get("ou_a")["args"]
    assert set(stored.keys()) <= set(dry_run_state._ALLOWED_FIELDS)
    for leaked in ("emailAddress", "openId", "mobile", "secret"):
        assert leaked not in stored


def test_get_evicts_after_ttl():
    dry_run_state.save("ou_a", dict(_ARGS), ts=1000.0)
    dry_run_state.set_clock(lambda: 1000.0 + 601)  # 超过 600s
    assert dry_run_state.get("ou_a") is None
    with dry_run_state._lock:
        assert "ou_a" not in dry_run_state._store  # 顺手清除


def test_get_passes_within_ttl():
    dry_run_state.save("ou_a", dict(_ARGS), ts=1000.0)
    dry_run_state.set_clock(lambda: 1000.0 + 599)
    assert dry_run_state.get("ou_a") is not None


def test_save_overwrites_previous():
    dry_run_state.save("ou_a", dict(_ARGS))
    dry_run_state.save("ou_a", {**_ARGS, "platform": "Orin"})
    assert dry_run_state.get("ou_a")["args"]["platform"] == "Orin"


def test_save_ignores_empty_openid():
    dry_run_state.save("", dict(_ARGS))
    with dry_run_state._lock:
        assert dry_run_state._store == {}


def test_clear_removes_entry():
    dry_run_state.save("ou_a", dict(_ARGS))
    dry_run_state.clear("ou_a")
    assert dry_run_state.get("ou_a") is None
```

- [ ] **Step 2: 跑测试确认失败**

Run: `pytest tests/unit/test_dry_run_state.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'bot.dry_run_state'`

- [ ] **Step 3: 写实现**

```python
# bot/dry_run_state.py
"""bot/dry_run_state — 跨轮存活的 dry_run 快照（多轮约车 commit 守卫用）。

背景：开启多轮对话后，dry_run（第 N 轮）与 commit（第 N+1 轮"确认"）落在不同的
``agent.run_conversation()`` 调用里，而 ``ocl.tool_capture`` 每轮被 handler 清空，
``car_tools.handlers._check_dry_run_guard`` 的 600s 回溯找不到上一轮的 dry_run →
合法预约被拒。本模块在内存里按 openid 存最近一次**完整** dry_run 的 6 个必填字段
+ 时间戳；commit 守卫在 tool_capture 查不到时回落到这里。

安全铁律（与 DMZ 记忆一致）：
- 只存 6 个必填业务字段 + 时间戳；**绝不**存 email / openId / mobile / 密钥 / 原文
- 纯内存（dict + Lock + 可注入时钟）；不落盘；进程重启即焚
- 600s TTL：与 commit 守卫 _DRY_RUN_LOOKBACK_SECONDS 对齐
"""
import threading
import time as _time
from typing import Callable, Optional

_TTL_SECONDS = 600
# 结构性脱敏：只允许存这 6 个 snake_case 业务字段
_ALLOWED_FIELDS = ("vehicle_type", "platform", "start_time", "end_time",
                   "task_name", "location")

_lock = threading.Lock()
_store: dict[str, dict] = {}                 # openid -> {"args": {...}, "ts": float}
_clock: Callable[[], float] = _time.monotonic


def set_clock(fn: Callable[[], float]) -> None:
    """注入时钟（单测 monkeypatch 用）。"""
    global _clock
    _clock = fn


def _redact(args: dict) -> dict:
    """只保留 6 个必填字段，剔除任何身份/敏感键。"""
    return {k: args.get(k) for k in _ALLOWED_FIELDS
            if args.get(k) not in (None, "")}


def save(openid: str, args: dict, ts: Optional[float] = None) -> None:
    """存最近一次完整 dry_run 的字段快照（覆盖旧的）。openid 为空则忽略。"""
    if not openid:
        return
    snap = {"args": _redact(args), "ts": ts if ts is not None else _clock()}
    with _lock:
        _store[openid] = snap


def get(openid: str) -> Optional[dict]:
    """返回 {"args":..., "ts":...}；不存在或已过期则 None（过期顺手清除）。"""
    if not openid:
        return None
    with _lock:
        snap = _store.get(openid)
        if not snap:
            return None
        if _clock() - snap["ts"] > _TTL_SECONDS:
            _store.pop(openid, None)
            return None
        return dict(snap)


def clear(openid: str) -> None:
    with _lock:
        _store.pop(openid, None)


def evict_expired() -> None:
    now = _clock()
    with _lock:
        for k in [k for k, v in _store.items() if now - v["ts"] > _TTL_SECONDS]:
            _store.pop(k, None)
```

- [ ] **Step 4: 跑测试确认通过**

Run: `pytest tests/unit/test_dry_run_state.py -v`
Expected: PASS（8 passed）

- [ ] **Step 5: 提交**

```bash
git add bot/dry_run_state.py tests/unit/test_dry_run_state.py
git commit -m "feat(bot): 新增 dry_run_state 跨轮 dry_run 快照（内存/600s TTL/脱敏）"
```

---

## Task 2: commit 守卫双源 + 收紧 fail-open

**Files:**
- Modify: `car_tools/handlers.py:41-76`（`_check_dry_run_guard`）+ 新增 `_scan_capture_for_dry_run` / `_match_dry_run_fields`
- Modify: `tests/unit/test_commit_guard.py`

- [ ] **Step 1: 改测试（先改期望，TDD）**

把 `tests/unit/test_commit_guard.py` 的 fixture 与第 1 个测试改成新行为，并新增 4 个多轮/双源测试。

替换 fixture（17-26 行）为：
```python
@pytest.fixture(autouse=True)
def _fresh_context():
    from bot import dry_run_state
    set_current_caller(CallerIdentity())
    set_current_session("")
    tool_capture.clear("test_session")
    with dry_run_state._lock:
        dry_run_state._store.clear()
    yield
    set_current_caller(CallerIdentity())
    set_current_session("")
    tool_capture.clear("test_session")
    with dry_run_state._lock:
        dry_run_state._store.clear()
```

替换旧的 `test_guard_failopen_when_no_session`（44-51 行）为新行为 + 新增测试：
```python
# ── 1. 收紧 fail-open：无 session 且无 dry_run_state → 拒绝 ───────────────

def test_guard_rejects_when_no_session_and_no_state():
    """删除 FSM/卡片路径后：openid 在但既无 session 又无 dry_run_state → 拒绝（堵漏）。"""
    set_current_caller(CallerIdentity(openid="ou_a", email="a@x.com"))
    # 不调 set_current_session；dry_run_state 为空
    err = handlers._check_dry_run_guard({"vehicleNo": "PNV1"})
    assert err is not None
    assert "_dry_run" in err


def test_guard_rejects_when_both_anchors_empty():
    """session 与 openid 都为空（匿名/误配）→ 拒绝下单。"""
    set_current_caller(CallerIdentity())  # openid=""
    err = handlers._check_dry_run_guard({"vehicleNo": "PNV1"})
    assert err is not None
    assert "缺失" in err


# ── 多轮：tool_capture 已清，回落 dry_run_state（按 openid）──────────────

def _seed_state(openid, *, args, ts=None):
    from bot import dry_run_state
    dry_run_state.save(openid, args, ts=ts)


def test_guard_passes_via_dry_run_state_across_turns():
    """模拟多轮：本轮 tool_capture 空（已清），但 dry_run_state 有上一轮完整快照 → 通过。"""
    from bot import dry_run_state
    dry_run_state.set_clock(lambda: 5000.0)
    set_current_caller(CallerIdentity(openid="ou_a", email="a@x.com"))
    set_current_session("test_session")  # session 在，但 capture 是空的
    _seed_state("ou_a", args={
        "vehicle_type": "DM2", "platform": "Xavier",
        "start_time": "2026-06-30 09:00", "end_time": "2026-06-30 10:00",
        "task_name": "MFF调试", "location": "上海",
    }, ts=5000.0)
    err = handlers._check_dry_run_guard({
        "vehicleType": "DM2", "platform": "Xavier",
        "startTime": "2026-06-30 09:00", "endTime": "2026-06-30 10:00",
        "taskName": "MFF调试", "location": "上海",
    })
    assert err is None
    dry_run_state.set_clock(__import__("time").monotonic)


def test_guard_state_rejects_on_args_mismatch():
    from bot import dry_run_state
    set_current_caller(CallerIdentity(openid="ou_a", email="a@x.com"))
    set_current_session("test_session")
    _seed_state("ou_a", args={
        "vehicle_type": "DM2", "platform": "Xavier",
        "start_time": "2026-06-30 09:00", "end_time": "2026-06-30 10:00",
        "task_name": "MFF调试", "location": "上海",
    })
    err = handlers._check_dry_run_guard({
        "vehicleType": "CT1",  # ← 与快照不一致
        "platform": "Xavier",
        "startTime": "2026-06-30 09:00", "endTime": "2026-06-30 10:00",
        "taskName": "MFF调试", "location": "上海",
    })
    assert err is not None
    assert "不一致" in err
```

> 其余测试（`test_guard_rejects_when_no_dry_run`、missing/mismatch/stale/pass、commit 集成）**不改**——它们都设了 `session="test_session"` 并灌了 capture，走 capture 源，行为不变。

- [ ] **Step 2: 跑测试确认失败**

Run: `pytest tests/unit/test_commit_guard.py -v`
Expected: FAIL — `test_guard_rejects_when_no_session_and_no_state` / `test_guard_rejects_when_both_anchors_empty` / `test_guard_passes_via_dry_run_state_across_turns` / `test_guard_state_rejects_on_args_mismatch`（旧 fail-open 逻辑还放行 / 还没读 dry_run_state）

- [ ] **Step 3: 改实现**

把 `car_tools/handlers.py` 的 `_check_dry_run_guard`（41-76 行）整体替换为下面三段（保留 `_commit_arg_key` 79-89 不动）：

```python
def _check_dry_run_guard(args: dict) -> str | None:
    """Return None if guard passes, else an error message explaining why.

    双源校验（commit 必须有一份近期、完整、字段一致的 dry_run）：
    - 源 1 tool_capture：单轮——dry_run 与 commit 在同一个 chat()/run_conversation 调用内
    - 源 2 dry_run_state：多轮——dry_run 在上一轮，tool_capture 已被 handler 清空（按 openid 回落）

    收紧后的 fail-open：删除旧 card_action_handler / FSM commit 路径后，
    "两个身份锚（session_id + openid）都为空"不再放行——视为误配/匿名调用，拒绝下单。
    """
    from ocl import tool_capture
    from bot import dry_run_state
    import time
    session_id = get_current_session()
    caller = get_current_caller()
    openid = caller.openid if caller else ""
    now = time.time()

    # 源 1：tool_capture（单轮）
    if session_id:
        found, verdict = _scan_capture_for_dry_run(tool_capture.read(session_id), args, now)
        if found:
            return verdict  # None=通过 / str=具体拒绝原因

    # 源 2：dry_run_state（多轮，过期由 get() 内部清除）
    if openid:
        snap = dry_run_state.get(openid)
        if snap is not None:
            return _match_dry_run_fields(snap["args"], args)

    # 两源都无有效 dry_run
    if not session_id and not openid:
        return "无法校验下单前置（会话与身份均缺失），请重新发起预约并由用户确认"
    return "本会话内未找到有效的 _dry_run，请先调用 _dry_run_vehicle_reservation 完成槽位收集"


def _scan_capture_for_dry_run(history: list, args: dict, now: float):
    """扫 tool_capture，返回 (found, verdict)。
    found=是否存在 dry_run 记录；verdict=None 通过 / str 拒绝原因。无记录 → (False, None)。"""
    for entry in reversed(history):
        if entry.get("tool") != "_dry_run_vehicle_reservation":
            continue
        result = entry.get("result")
        if not isinstance(result, dict) or not result.get("dry_run"):
            continue
        if result.get("missing_fields"):
            return True, "最近一次 dry_run 仍有缺字段，请先补齐后再下单"
        mismatch = _match_dry_run_fields(result.get("args") or {}, args)
        if mismatch:
            return True, mismatch
        ts = entry.get("timestamp") or 0
        if ts and (now - ts) > _DRY_RUN_LOOKBACK_SECONDS:
            return True, f"dry_run 已超过 {_DRY_RUN_LOOKBACK_SECONDS // 60} 分钟，请重新确认"
        return True, None
    return False, None


def _match_dry_run_fields(dry_args: dict, commit_args: dict) -> str | None:
    """6 个必填字段逐一相等校验。返回 None 通过 / str 不一致原因。"""
    for k in _COMMIT_REQUIRED_FIELDS:
        if str(dry_args.get(k) or "").strip() != str(commit_args.get(_commit_arg_key(k)) or "").strip():
            return (f"字段 {k} 与最近一次 dry_run 不一致（{dry_args.get(k)!r} vs "
                    f"{commit_args.get(_commit_arg_key(k))!r}），请重走 dry_run")
    return None
```

- [ ] **Step 4: 跑测试确认通过**

Run: `pytest tests/unit/test_commit_guard.py -v`
Expected: PASS（原 9 + 新 4 = 13 项全过）

- [ ] **Step 5: 提交**

```bash
git add car_tools/handlers.py tests/unit/test_commit_guard.py
git commit -m "fix(car): commit 守卫双源（tool_capture+dry_run_state）+ 收紧两锚皆空 fail-open 漏洞"
```

---

## Task 3: `agent_pool` per-user 历史 deque

**Files:**
- Modify: `bot/agent_pool.py`（imports、模块常量、`AgentPool.__init__`、`get_or_create` 驱逐段、新增方法 + helper）
- Test: `tests/unit/test_agent_pool_history.py`

- [ ] **Step 1: 写失败测试**

```python
# tests/unit/test_agent_pool_history.py
"""bot/agent_pool 的 per-user 历史 deque 单测（窗口/TTL/截断/驱逐）。"""
import pytest

from bot.agent_pool import AgentPool, _HISTORY_MAXLEN, _HISTORY_CONTENT_CAP, _HISTORY_TTL_SECONDS


def _u(i):  return {"role": "user", "content": f"u{i}"}
def _a(i):  return {"role": "assistant", "content": f"a{i}"}


def test_append_then_get_roundtrip():
    p = AgentPool(max_size=10)
    p.append_turn("ou_a", _u(1), _a(1))
    hist = p.get_history("ou_a")
    assert hist == [{"role": "user", "content": "u1"},
                    {"role": "assistant", "content": "a1"}]


def test_get_empty_for_unknown_user():
    p = AgentPool(max_size=10)
    assert p.get_history("nobody") == []


def test_window_caps_at_maxlen():
    p = AgentPool(max_size=10)
    for i in range(7):                       # 7 轮 = 14 条 > maxlen(12)
        p.append_turn("ou_a", _u(i), _a(i))
    hist = p.get_history("ou_a")
    assert len(hist) == _HISTORY_MAXLEN      # 12
    assert hist[0] == {"role": "user", "content": "u1"}   # 第 0 轮被挤掉


def test_content_capped():
    p = AgentPool(max_size=10)
    long = "x" * (_HISTORY_CONTENT_CAP + 500)
    p.append_turn("ou_a", {"role": "user", "content": long}, _a(1))
    assert len(p.get_history("ou_a")[0]["content"]) == _HISTORY_CONTENT_CAP


def test_ttl_clears_stale_history(monkeypatch):
    import bot.agent_pool as ap
    clock = {"t": 1000.0}
    monkeypatch.setattr(ap.time, "monotonic", lambda: clock["t"])
    p = AgentPool(max_size=10)
    p.append_turn("ou_a", _u(1), _a(1))
    clock["t"] = 1000.0 + _HISTORY_TTL_SECONDS + 1   # 超过 TTL
    assert p.get_history("ou_a") == []


def test_empty_user_id_is_noop():
    p = AgentPool(max_size=10)
    p.append_turn("", _u(1), _a(1))
    assert p.get_history("") == []


def test_clear_history():
    p = AgentPool(max_size=10)
    p.append_turn("ou_a", _u(1), _a(1))
    p.clear_history("ou_a")
    assert p.get_history("ou_a") == []
```

- [ ] **Step 2: 跑测试确认失败**

Run: `pytest tests/unit/test_agent_pool_history.py -v`
Expected: FAIL — `ImportError: cannot import name '_HISTORY_MAXLEN'`

- [ ] **Step 3: 改实现**

3a. 在 `bot/agent_pool.py` 顶部 import 区（11-13 行附近）补 `import time` 和 `deque`：
```python
import time
from collections import OrderedDict, deque
```

3b. 在 `_CAR_BOOKING_SKILL = _load_car_booking_skill()`（162 行）之后、`class AgentPool` 之前，加模块常量 + helper：
```python
# ── per-user 多轮对话历史（内存 deque，方案 B）─────────────────────────────
_HISTORY_TURNS = 6                       # 最近 N 轮（user+assistant 成对）
_HISTORY_MAXLEN = _HISTORY_TURNS * 2     # deque 条目上限 = 12
_HISTORY_TTL_SECONDS = 1800              # 30 分钟空闲 TTL
_HISTORY_CONTENT_CAP = 800               # 单条 content 截断上限


def _cap_content(msg: dict) -> dict:
    """只保留 role/content 并截断 content（防病态长消息撑爆上下文）。"""
    content = str(msg.get("content", ""))
    if len(content) > _HISTORY_CONTENT_CAP:
        content = content[:_HISTORY_CONTENT_CAP]
    return {"role": msg.get("role", "user"), "content": content}
```

3c. 在 `AgentPool.__init__`（168-171 行）末尾补两个字段：
```python
    def __init__(self, max_size: int = 100) -> None:
        self._max_size = max_size
        self._pool: OrderedDict[str, AIAgent] = OrderedDict()
        self._lock = threading.Lock()
        self._history: OrderedDict[str, deque] = OrderedDict()  # user_id -> deque(maxlen=12)
        self._history_touch: dict[str, float] = {}              # user_id -> monotonic last-use
```

3d. 在 `get_or_create` 的驱逐段（216-220 行），驱逐 agent 时连带清历史：
```python
            if len(self._pool) > self._max_size:
                evicted_id, evicted_agent = self._pool.popitem(last=False)
                self._history.pop(evicted_id, None)
                self._history_touch.pop(evicted_id, None)
                evicted_sid = getattr(evicted_agent, "session_id", None)
                if evicted_sid:
                    session_evict(evicted_sid)
```
（其余驱逐逻辑——`_SKILL_INJECTED_SESSIONS.discard` 等——保持不变。）

3e. 在 `size` 方法（233-235 行）之后、`agent_pool = AgentPool(...)`（238 行）之前，加三个历史方法：
```python
    def get_history(self, user_id: str) -> list[dict]:
        """返回最近 N 轮 user/assistant dict；空闲超 TTL 则清空返 []。"""
        if not user_id:
            return []
        with self._lock:
            dq = self._history.get(user_id)
            if not dq:
                return []
            last = self._history_touch.get(user_id, 0.0)
            if time.monotonic() - last > _HISTORY_TTL_SECONDS:
                self._history.pop(user_id, None)
                self._history_touch.pop(user_id, None)
                return []
            self._history_touch[user_id] = time.monotonic()
            return list(dq)

    def append_turn(self, user_id: str, user_msg: dict, assistant_msg: dict) -> None:
        """追加一轮（user+assistant 纯文本，跳过 tool 轮）。user_id 为空则忽略。"""
        if not user_id:
            return
        u, a = _cap_content(user_msg), _cap_content(assistant_msg)
        with self._lock:
            dq = self._history.get(user_id)
            if dq is None:
                dq = deque(maxlen=_HISTORY_MAXLEN)
                self._history[user_id] = dq
            dq.append(u)
            dq.append(a)
            self._history_touch[user_id] = time.monotonic()

    def clear_history(self, user_id: str) -> None:
        with self._lock:
            self._history.pop(user_id, None)
            self._history_touch.pop(user_id, None)
```

- [ ] **Step 4: 跑测试确认通过**

Run: `pytest tests/unit/test_agent_pool_history.py -v`
Expected: PASS（7 passed）

- [ ] **Step 5: 提交**

```bash
git add bot/agent_pool.py tests/unit/test_agent_pool_history.py
git commit -m "feat(bot): agent_pool 加 per-user 多轮历史 deque（6轮/30min TTL/800字截断）"
```

---

## Task 4: handler 接线 — run_conversation + append_turn + dry_run_state.save

**Files:**
- Modify: `bot/handler.py`（`_run_agent` 195-262、`_handle` fast_path 段 130-134、新增 `_extract_latest_dry_run_args`）
- Test: `tests/unit/test_handler_multiturn.py`

- [ ] **Step 1: 写失败测试**

```python
# tests/unit/test_handler_multiturn.py
"""bot/handler 多轮接线单测：dry_run 快照提取 + run_conversation 带历史 + append_turn。"""
import pytest


# ── 1. 纯函数：从 captured 提取最近一条完整 dry_run 的 args ──────────────

def test_extract_latest_complete_dry_run_args():
    from bot import handler
    captured = [
        {"tool": "fetch_available_vehicles", "result": []},
        {"tool": "_dry_run_vehicle_reservation",
         "result": {"dry_run": True, "missing_fields": ["location"], "args": {"platform": "X"}}},
        {"tool": "_dry_run_vehicle_reservation",
         "result": {"dry_run": True, "args": {"platform": "Orin", "location": "上海"}}},
    ]
    args = handler._extract_latest_dry_run_args(captured)
    assert args == {"platform": "Orin", "location": "上海"}


def test_extract_returns_none_when_no_complete_dry_run():
    from bot import handler
    captured = [
        {"tool": "_dry_run_vehicle_reservation",
         "result": {"dry_run": True, "missing_fields": ["location"], "args": {}}},
    ]
    assert handler._extract_latest_dry_run_args(captured) is None


def test_extract_returns_none_on_empty():
    from bot import handler
    assert handler._extract_latest_dry_run_args([]) is None


# ── 2. _run_agent 把历史喂进 run_conversation 并在成功后 append_turn ──────

class _FakeAgent:
    def __init__(self):
        self.seen_history = None
    def run_conversation(self, message, conversation_history=None, stream_callback=None):
        self.seen_history = conversation_history
        if stream_callback:
            stream_callback("约")
        return {"final_response": "约好了 PNV1"}


def test_run_agent_threads_history_and_appends_turn(monkeypatch):
    from bot import handler
    from bot.agent_pool import agent_pool

    fake = _FakeAgent()
    monkeypatch.setattr(agent_pool, "get_or_create", lambda uid: fake)
    # 预置历史
    prior = [{"role": "user", "content": "我要约车"},
             {"role": "assistant", "content": "好的，要哪个平台？"}]
    monkeypatch.setattr(agent_pool, "get_history", lambda uid: prior)
    appended = {}
    monkeypatch.setattr(agent_pool, "append_turn",
                        lambda uid, u, a: appended.update(uid=uid, u=u, a=a))

    # 隔离飞书/OCL 外设
    import feishu.sender as sender_mod
    class _StreamCard:
        def __init__(self, chat_id): pass
        def append(self, d): pass
        def finalize_with_card(self, card): pass
        def finalize(self, text): pass
    monkeypatch.setattr(sender_mod, "StreamCard", _StreamCard)
    monkeypatch.setattr(handler, "ocl_apply",
                        lambda resp, uid, captured=None: type("R", (), {
                            "blocked": False, "card": None, "text": resp})())
    monkeypatch.setattr(handler, "_notify_applicants_from_captured", lambda c: None)

    handler._run_agent("chat1", "ou_a", 1, "张三", "Orin 吧", "msg1")

    assert fake.seen_history == prior          # 历史被透传
    assert appended["uid"] == "ou_a"
    assert appended["u"] == {"role": "user", "content": "Orin 吧"}   # 存原文（非 preamble）
    assert appended["a"] == {"role": "assistant", "content": "约好了 PNV1"}
```

- [ ] **Step 2: 跑测试确认失败**

Run: `pytest tests/unit/test_handler_multiturn.py -v`
Expected: FAIL — `AttributeError: module 'bot.handler' has no attribute '_extract_latest_dry_run_args'`

- [ ] **Step 3: 改实现**

3a. 在 `bot/handler.py` 的 `_notify_applicants_from_captured` 之前（361 行附近）新增纯函数：
```python
def _extract_latest_dry_run_args(captured: list) -> dict | None:
    """从本轮 captured 里取最近一条**完整**（无 missing_fields）dry_run 的 args。"""
    for entry in reversed(captured):
        if entry.get("tool") != "_dry_run_vehicle_reservation":
            continue
        res = entry.get("result")
        if isinstance(res, dict) and res.get("dry_run") and not res.get("missing_fields"):
            return res.get("args") or {}
    return None
```

3b. 在 `_run_agent` 里替换调用方式（216-227 行区段）。把：
```python
        agent_input = (replies.identity_preamble(user_id, role, name) + text)

        def _on_delta(delta: str) -> None:
            # 工具调用过程中也会触发 delta（空字符串），StreamCard 内部会忽略
            stream.append(delta)

        future = _executor.submit(ctx.run, agent.chat, agent_input, _on_delta)
        response = future.result(timeout=settings.AGENT_TIMEOUT_SECONDS)
        captured = tool_capture.read(session_id)
```
改为：
```python
        agent_input = (_now_preamble()
                       + replies.identity_preamble(user_id, role, name) + text)

        def _on_delta(delta: str) -> None:
            # 工具调用过程中也会触发 delta（空字符串），StreamCard 内部会忽略
            stream.append(delta)

        hist = agent_pool.get_history(user_id)   # 最近 N 轮（不含本轮）

        def _invoke():
            return agent.run_conversation(
                agent_input, conversation_history=hist, stream_callback=_on_delta)

        future = _executor.submit(ctx.run, _invoke)
        result = future.result(timeout=settings.AGENT_TIMEOUT_SECONDS)
        response = result["final_response"] if isinstance(result, dict) else str(result)
        captured = tool_capture.read(session_id)
        # 把本轮完整 dry_run 快照存入 dry_run_state，供下一轮 commit 守卫跨轮校验
        _dr_args = _extract_latest_dry_run_args(captured)
        if _dr_args:
            from bot import dry_run_state
            dry_run_state.save(user_id, _dr_args)
        # 成功一轮：把原始用户文本 + 助手回复写入多轮历史（跳过 preamble/时间）
        agent_pool.append_turn(
            user_id,
            {"role": "user", "content": text},
            {"role": "assistant", "content": response})
```

3c. 新增 `_now_preamble`（Part 4 用，放在 `_run_agent` 之前，195 行附近）：
```python
def _now_preamble() -> str:
    """每轮注入当前时间（取代常驻 system prompt 里写死的旧时间，避免池化 agent 时间漂移）。"""
    from bot.agent_pool import _now_cn
    return (f"当前时间：{_now_cn()}\n"
            "（相对日期换算：今天=N，今天+1=明天，今天+2=后天；周X=星期X）\n")
```

3d. 修正 `_run_agent` 的 docstring（200-203 行），删掉"agent.history 永久保留 / agent 已记住"的失实描述，改为：
```python
    """完整 agent 路径：取多轮历史 → AIAgent.run_conversation → OCL pipeline → 卡片 + 后处理。

    2026-06-30 多轮：经 agent_pool.get_history(user_id) 取最近 N 轮喂进
    run_conversation(conversation_history=...)，成功后 append_turn 写回；时间由
    _now_preamble 每轮注入（不再写死在常驻 system prompt 里）。
    """
```

- [ ] **Step 4: 跑测试确认通过**

Run: `pytest tests/unit/test_handler_multiturn.py -v`
Expected: PASS（5 passed）

- [ ] **Step 5: 提交**

```bash
git add bot/handler.py tests/unit/test_handler_multiturn.py
git commit -m "feat(bot): handler 接多轮历史 run_conversation + append_turn + dry_run_state.save"
```

---

## Task 5: fast_path 查询结果补进多轮历史

**Files:**
- Modify: `bot/handler.py`（`_handle` 的 fast_path 命中段 130-134）
- Test: `tests/unit/test_handler_multiturn.py`（追加）

- [ ] **Step 1: 追加失败测试**

```python
# 追加到 tests/unit/test_handler_multiturn.py

def test_fast_path_hit_records_history(monkeypatch):
    """fast_path 查询绕过 LLM，但其 Q&A 仍写入历史，保证'约刚才第一辆'能接上。"""
    from bot import handler
    from bot.agent_pool import agent_pool

    appended = []
    monkeypatch.setattr(agent_pool, "append_turn",
                        lambda uid, u, a: appended.append((uid, u, a)))
    handler._record_fast_path_history("ou_a", "查可用车", "📋 共 3 辆可用…")
    assert appended == [("ou_a",
                         {"role": "user", "content": "查可用车"},
                         {"role": "assistant", "content": "📋 共 3 辆可用…"})]
```

- [ ] **Step 2: 跑测试确认失败**

Run: `pytest tests/unit/test_handler_multiturn.py::test_fast_path_hit_records_history -v`
Expected: FAIL — `AttributeError: ... no attribute '_record_fast_path_history'`

- [ ] **Step 3: 改实现**

3a. 在 `_try_fast_query` 之前（278 行附近）加 helper：
```python
def _record_fast_path_history(user_id: str, text: str, reply: str) -> None:
    """fast_path 命中也写入多轮历史，保证后续对话能引用刚查到的结果。"""
    agent_pool.append_turn(
        user_id,
        {"role": "user", "content": text},
        {"role": "assistant", "content": reply})
```

3b. 在 `_handle` 的 fast_path 命中段（130-134 行）补一行记录。把：
```python
    fast = _try_fast_query(text, user_id, role)
    if fast:
        log.info("fast_path_handled text=%r", text[:50])
        sender.send_text_as_card(chat_id, fast)
        return
```
改为：
```python
    fast = _try_fast_query(text, user_id, role)
    if fast:
        log.info("fast_path_handled text=%r", text[:50])
        _record_fast_path_history(user_id, text, fast)
        sender.send_text_as_card(chat_id, fast)
        return
```

- [ ] **Step 4: 跑测试确认通过**

Run: `pytest tests/unit/test_handler_multiturn.py -v`
Expected: PASS（6 passed）

- [ ] **Step 5: 提交**

```bash
git add bot/handler.py tests/unit/test_handler_multiturn.py
git commit -m "feat(bot): fast_path 查询结果写入多轮历史（保证后续可引用）"
```

---

## Task 6: Part 4 时间锚点 + Part 3 prompt 放松规则 #1

**Files:**
- Modify: `bot/agent_pool.py`（`_FEISHU_SYSTEM_PROMPT_BASE` 去掉时间块 + 放松规则 #1；`get_or_create` 去掉 `.replace`）
- Modify: `bot/skills/car-booking/SKILL.md`（核心原则 #1）
- Modify: `tests/unit/test_system_prompt.py`（新增澄清策略断言）

- [ ] **Step 1: 写失败测试**

追加到 `tests/unit/test_system_prompt.py`：
```python
def test_prompt_no_longer_bakes_static_time():
    """时间已移到每轮 preamble，常驻 system prompt 不再含时间占位/写死时间。"""
    p = _prompt()
    assert "__NOW_CN__" not in p
    assert "当前时间" not in p


def test_prompt_permits_one_bounded_clarification():
    """放松后的规则 #1：意图可推断直接查；仅发散/缺关键信息时最多问一个、不连问。"""
    p = _prompt()
    assert "可推断" in p
    assert "最多问一个" in p or "最多一个" in p
    assert "不得连问" in p or "第二次" in p


def test_skill_core_principle_permits_clarification():
    s = _skill()
    assert "最多问一个" in s or "最多一个" in s
```

- [ ] **Step 2: 跑测试确认失败**

Run: `pytest tests/unit/test_system_prompt.py -k "time or clarif or principle" -v`
Expected: FAIL（当前 prompt 仍含 `__NOW_CN__`/`当前时间`，且无澄清措辞）

- [ ] **Step 3: 改实现**

3a. `bot/agent_pool.py` — 把 `_FEISHU_SYSTEM_PROMPT_BASE`（102 行起）的开头三行时间块删除，并放松规则 #1。改为：
```python
_FEISHU_SYSTEM_PROMPT_BASE = """你是飞书"约车助手"机器人。所有交互通过飞书文字消息完成——卡片只展示信息，**不点不动**，用户**打字**给你指令。

# 🚨 3 条必须遵守
1. **意图可推断就直接调工具查，别为能自己定的细节追问**（如没指定平台/车型 → 直接 `fetch_available_vehicles({})`）。**仅当**请求发散、或缺少无法默认补全的关键信息（缺车 / 缺时段 / 一句话含多个互斥意图）时，**最多问一个**澄清问题然后停下等用户回复；拿到答复立即执行，**不得连问第二次**。
2. **绝不编造**——所有数字、平台、状态必须从工具返回的 `data` 数组读。**绝对不要**假装"dry-run 通过"或"约车成功"——这些必须来自工具的真实返回。
3. **不查不要回答**——不知道就调工具查。
"""
```
> 即：删掉原 102-104 行的 `当前时间：__NOW_CN__ / （相对日期换算…）/ 空行`，原 105 行"你是飞书…"提到第一行；规则 #1（108 行）整句替换为上面放松版；规则 2、3 及其后（112 行起的"# 🚨 你的知识盲区"等）全部保持不变。

3b. `bot/agent_pool.py` `get_or_create`（186 行）去掉 `.replace`：
```python
            system_prompt = _FEISHU_SYSTEM_PROMPT_BASE
            if _CAR_BOOKING_SKILL:
                system_prompt += "\n\n# 操作手册（car-booking skill）\n\n" + _CAR_BOOKING_SKILL
```

3c. `bot/skills/car-booking/SKILL.md` 第 15 行核心原则 #1，把：
```markdown
1. **用户表达模糊时，先调工具查，不要反复问**。如"现在有什么车" → 立即调 `fetch_available_vehicles({})` 看返回。
```
改为：
```markdown
1. **意图可推断就直接选最合理默认调工具查**（如没指定平台/车型 → 直接 `fetch_available_vehicles({})`）；**仅当**发散或缺无法默认补全的关键信息（缺车/缺时段/一句话多个互斥意图）时**最多问一个**澄清问题再停下等回复，得到答复立即执行，不连问第二次。
```

- [ ] **Step 4: 跑测试确认通过**

Run: `pytest tests/unit/test_system_prompt.py -v`
Expected: PASS（原有断言 + 3 个新断言全过；`_now_cn` 仍在 agent_pool 中供 handler `_now_preamble` 调用）

- [ ] **Step 5: 提交**

```bash
git add bot/agent_pool.py bot/skills/car-booking/SKILL.md tests/unit/test_system_prompt.py
git commit -m "feat(prompt): 时间移到每轮 preamble + 放松规则#1 允许最多一个澄清问题"
```

---

## Task 7: 修正失实文档

**Files:**
- Modify: `CLAUDE.md`、`bot/CLAUDE.md`

- [ ] **Step 1: 改 `bot/CLAUDE.md`**

在 `agent_pool.py` 条目补充多轮历史职责，并删除/标注已删 FSM 文件的过时描述（intent_router/car_booking_fsm/return_fsm/card_action_handler/fast_path/car_state 已 deleted）。在 `agent_pool.py` 行追加：
```markdown
- `agent_pool.py` — LRU pool of `AIAgent`；额外维护 per-user 多轮对话历史 deque（最近 6 轮 / 30min 空闲 TTL / 800 字截断），由 `handler` 经 `get_history`/`append_turn` 读写，随 agent 一起 LRU 驱逐。
- `dry_run_state.py` — 跨轮 dry_run 快照（内存/600s TTL/仅 6 业务字段，脱敏）；commit 守卫在 tool_capture 查不到时按 openid 回落到此，保住多轮下的 dry_run→commit 不变量。
```

- [ ] **Step 2: 改 `CLAUDE.md`（跨会话记忆段附近）**

把"evicted instances are discarded（hermes-agent persists to ~/.hermes/state.db automatically）"这类描述更正为：当前未给 `AIAgent` 传 `session_db`，hermes **不**自动落盘；多轮历史由 `bot/agent_pool` 的内存 deque 承载（方案 B，不落盘）。

- [ ] **Step 3: 全量自检**

Run: `pytest tests/unit/ -q`
Expected: 全绿（含本计划新增/修改的全部测试）

Run: `python scripts/selfcheck.py`
Expected: 测试 + 编译 + 导入 + 配置漂移 + 文档/接线 全过（若 selfcheck 检测到 `_maybe_inject_skill` 等 dead code 告警，按其提示处理或确认为已知）

- [ ] **Step 4: 提交**

```bash
git add CLAUDE.md bot/CLAUDE.md
git commit -m "docs: 更正多轮历史(内存deque)/dry_run_state 描述，修正 state.db 失实说明"
```

---

## Self-Review

**1. Spec coverage（逐节核对）：**
- Part 1 多轮记忆 → Task 3（pool deque）+ Task 4（run_conversation/append_turn）+ Task 5（fast_path 入史）✅
- Part 2 dry_run 守卫跨轮 + fail-open 收紧 → Task 1（dry_run_state）+ Task 2（双源守卫 + 堵漏）✅
- Part 3 引导式澄清（放松规则#1，不接 clarify_callback）→ Task 6（agent_pool 规则#1 + SKILL.md）✅；确认全程未引入 `clarify_callback` ✅
- Part 4 时间锚点 → Task 6（移除常驻时间 + Task 4 的 `_now_preamble` 每轮注入）✅
- 安全不变量保留（L1/L2/email 注入/schema/ROLE_TOOLS）→ 无任务触碰这些文件 ✅
- 测试计划（test_commit_guard 轮界、dry_run_state、agent_pool 历史、test_system_prompt）→ Task 1/2/3/6 覆盖 ✅
- 文档修正（handler 注释 / CLAUDE.md）→ Task 4(3d) + Task 7 ✅

**2. Placeholder 扫描：** 无 TBD/TODO；每个代码步骤都给了完整可粘贴代码与确切命令/预期。✅

**3. 类型/命名一致性：**
- `dry_run_state.save/get/clear/set_clock/_store/_lock/_ALLOWED_FIELDS` 在 Task 1 定义，Task 2/4 与测试一致引用 ✅
- 守卫 helper `_scan_capture_for_dry_run` / `_match_dry_run_fields` 在 Task 2 定义并自洽（沿用既有 `_COMMIT_REQUIRED_FIELDS` / `_commit_arg_key` / `_DRY_RUN_LOOKBACK_SECONDS`）✅
- `agent_pool.get_history/append_turn/clear_history` + 常量 `_HISTORY_MAXLEN/_HISTORY_CONTENT_CAP/_HISTORY_TTL_SECONDS` 在 Task 3 定义，Task 4/5 与测试一致 ✅
- `handler._extract_latest_dry_run_args` / `_now_preamble` / `_record_fast_path_history` 在 Task 4/5 定义并被同任务测试引用 ✅
- `_now_cn` 仍保留在 `agent_pool`（Task 6 仅删时间块，不删函数），`handler._now_preamble` 导入它 ✅

**已知依赖（非缺陷，spec §8 已述）：** 跨轮 commit 要求 dry_run 轮的 assistant 文本含 summary（6 字段），模型据此重建 commit args；守卫失败即闭（不符则拒并提示重 dry_run），安全。
