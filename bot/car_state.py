"""per-user 车辆预约状态机。

每个用户挂起一个 CarPendingState（10 分钟 TTL），记录：
- 选定的车辆
- 已经 dry_run 收集到的字段（task_name / location / 审批参数等）
- 当前意图（booking / cancel / return / approve / records）

bot.handler 用 save/get/clear 推进；bot.card_action_handler 在 select_vehicle
/ confirm_booking 回调里 read+update；escape 关键词（"算了/换个/不订了"）触发
clear()。

实现仿 bot.dry_run_state：内存 dict + 锁 + monotonic time。
"""
import logging
import threading
import time
from dataclasses import dataclass, field, asdict
from typing import Optional

log = logging.getLogger(__name__)

_TTL_SECONDS = 600  # 10 minutes — 与 dry_run_state 对齐

_state: dict[str, "CarPendingState"] = {}
_lock = threading.Lock()


@dataclass
class CarPendingState:
    """per-user 挂起状态。"""
    user_id: str
    intent: str = ""               # 'booking' / 'cancel' / 'return' / 'approve' / 'records'
    # v2 FSM 状态字段
    state: str = "START"                # v2 新增 — FSM 当前状态名
    # booking slots
    vehicle_no: str = ""
    vehicle_type: str = ""                # 大类（427/Ccar/Dcar/...，fmp-app 兼容字段）
    vehicle_type_detail: str = ""         # v2 细分（DM0/CM0/427-M1/...，fmp-app 主过滤字段）
    chip: str = ""                        # v2 新增 — 芯片平台（Xavier/ADCU/Orin/Thor）
    platform: str = ""                    # TODO(Task 6): 旧字段，PR6 删 fast-path 时一并清除
    license_plate: str = ""
    start_time: str = ""
    end_time: str = ""
    duration_minutes: int = 0           # v2 新增 — 用车时长（分钟）
    time_range_start: str = ""          # v2 新增 — 时段起
    time_range_end: str = ""            # v2 新增 — 时段止
    task_name: str = ""
    location: str = ""
    remark: str = ""
    vin: str = ""
    # approve
    approved: Optional[bool] = None
    review_comment: str = ""
    # return
    return_location: str = ""
    key_position: str = ""
    change_module: str = ""
    vehicle_status: str = ""
    vehicle_status_description: str = ""
    # records filter
    records_status: str = ""
    # 最近的车辆列表缓存（用户上次"查可用车辆"的结果）。
    # 用于「约第N个」/「约XXX（后三位/后六位）」的文本选择路径：
    # handler 从该列表反查 vehicle_no，避开 LLM 解析。
    # 元素结构：{"vehicle_no": "PNV332", "vehicle_type": "DM2", "platform": "Xavier",
    #           "license_plate": "沪FPNV332", "vin": "..."}
    last_vehicles: list = field(default_factory=list)
    # 最近的时段候选（DURATION_CONFIRM 模糊匹配结果），元素：
    # {"start": "2026-06-17 14:00", "end": "2026-06-17 16:00", "label": "06-17 14:00 ~ 16:00"}
    last_slots: list = field(default_factory=list)
    # 用户的查询条件（缓存到卡片头部，handler 用来补回 car_state 的其他字段）
    last_query: dict = field(default_factory=dict)
    # DC-10 防循环计数器：FSM 同一状态连续重试次数，超过阈值则 abort
    retry_count: int = 0                # v2 新增
    # internal
    expires_at: float = field(default=0.0)

    def is_expired(self) -> bool:
        return self.expires_at > 0 and self.expires_at < time.monotonic()


def _now_expires() -> float:
    return time.monotonic() + _TTL_SECONDS


def save(user_id: str, **fields) -> None:
    """Create or overwrite the user's pending state. Pass keyword args for any
    CarPendingState fields; unspecified fields keep their dataclass defaults."""
    with _lock:
        cur = _state.get(user_id)
        if cur is None:
            cur = CarPendingState(user_id=user_id)
            _state[user_id] = cur
        for k, v in fields.items():
            if hasattr(cur, k):
                setattr(cur, k, v)
        cur.expires_at = _now_expires()
        log.info("car_state_saved user=%s intent=%s vehicle_no=%s",
                 user_id, cur.intent, cur.vehicle_no)


def update(user_id: str, **fields) -> None:
    """Update specific fields on the existing state. No-op if state is missing
    (caller should save() first). Used by the confirm-card handler to inject
    collected slot values."""
    with _lock:
        cur = _state.get(user_id)
        if cur is None:
            return
        for k, v in fields.items():
            if hasattr(cur, k):
                setattr(cur, k, v)
        cur.expires_at = _now_expires()


def get(user_id: str) -> Optional[CarPendingState]:
    """Return the pending state if not expired, else None. Expired entries are
    cleared as a side effect."""
    with _lock:
        cur = _state.get(user_id)
        if cur is None:
            return None
        if cur.is_expired():
            del _state[user_id]
            log.info("car_state_expired user=%s", user_id)
            return None
        return cur


def as_dict(user_id: str) -> dict:
    """Snapshot the user's pending state as a plain dict (drops expires_at)."""
    cur = get(user_id)
    if cur is None:
        return {}
    return {k: v for k, v in asdict(cur).items() if k not in ("expires_at",)}


def clear(user_id: str) -> None:
    """Remove the entry (after success / cancel / escape)."""
    with _lock:
        _state.pop(user_id, None)
