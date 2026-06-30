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
_clock: Callable[[], float] = _time.time


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
        return {"args": dict(snap["args"]), "ts": snap["ts"]}


def clear(openid: str) -> None:
    with _lock:
        _store.pop(openid, None)
