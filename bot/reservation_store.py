"""Persist reservation → applicant_open_id mappings for the cross-session
approval notification flow (车辆预约域).

字段名沿用 bench_no 旧键以兼容 reservation_applicants.json 既有数据；语义上
已统一改为 vehicle_no（vehicles are the new benches）。
"""
import logging
import os
import re
from pathlib import Path
from typing import Optional

from infra.json_store import JsonStore

log = logging.getLogger(__name__)

_FILE = str(Path(os.path.dirname(os.path.dirname(__file__))) / "data" / "reservation_applicants.json")


def _norm_time(s: str) -> str:
    """Normalize a datetime string to its first 12 digits (yyyymmddHHMM),
    discarding separators, seconds, and timezone."""
    return re.sub(r"\D", "", s or "")[:12]


def _store() -> JsonStore:
    return JsonStore(_FILE)


def save(reservation_id: str, applicant_open_id: str, applicant_email: str,
         vehicle_no: str, start_time: str, end_time: str = "",
         task_name: str = "") -> None:
    """Record a freshly-created reservation. Idempotent on reservation_id."""
    if not reservation_id:
        return
    _store().put(reservation_id, {
        "applicant_open_id": applicant_open_id,
        "applicant_email": applicant_email,
        "vehicle_no": vehicle_no,        # 旧字段名 bench_no 已统一替换
        "start_time": start_time,
        "end_time": end_time,
        "task_name": task_name,
    })
    log.info("reservation_saved id=%s vehicle=%s applicant=%s",
             reservation_id, vehicle_no, applicant_open_id)


def get(reservation_id: str) -> Optional[dict]:
    return _store().get(reservation_id)


def find_by_vehicle_and_time(vehicle_no: str, start_time: str) -> Optional[dict]:
    """Reverse lookup when we only have vehicleNo + startTime (e.g. from a
    stale approve button). Returns the most recent match (last-wins)."""
    want_t = _norm_time(start_time)
    found = _store().find(
        lambda v: v.get("vehicle_no") == vehicle_no
        and _norm_time(v.get("start_time", "")) == want_t
    )
    return found[1] if found else None


# ── 兼容旧 bench API 调用（不删，但优先用 vehicle_no 字段） ────────────────

def find_by_bench_and_time(bench_no: str, start_time: str) -> Optional[dict]:
    """Backwards-compat alias (旧 card_action_handler 引用过)。"""
    return find_by_vehicle_and_time(bench_no, start_time)


def list_all() -> dict:
    return _store().all()
