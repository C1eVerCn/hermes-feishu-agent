"""Persist reservation_id → applicant_open_id mappings for the cross-session
approval notification flow.

BUGFIX (#7): now delegates to the shared `infra.json_store.JsonStore`
helper instead of re-implementing the atomic-rename / lock / corrupt-
file-recovery pattern (was previously duplicated in `bot/identity_admin.py`
and `bot/dmz_memory.py`).
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
    discarding separators, seconds, and timezone. Lets the approval lookup
    match a reservation even when the bench API echoes startTime in a
    slightly different format than the user supplied
    (e.g. 'T' separator, dropped ':00' seconds, or a trailing offset)."""
    return re.sub(r"\D", "", s or "")[:12]


def _store() -> JsonStore:
    """Lazily-resolve the store path so monkeypatching `_FILE` in tests works."""
    return JsonStore(_FILE)


def save(reservation_id: str, applicant_open_id: str, applicant_email: str,
         bench_no: str, start_time: str, end_time: str = "",
         task_name: str = "") -> None:
    """Record a freshly-created reservation. Idempotent on reservation_id."""
    if not reservation_id:
        return
    _store().put(reservation_id, {
        "applicant_open_id": applicant_open_id,
        "applicant_email": applicant_email,
        "bench_no": bench_no,
        "start_time": start_time,
        "end_time": end_time,
        "task_name": task_name,
    })
    log.info("reservation_saved id=%s bench=%s applicant=%s",
             reservation_id, bench_no, applicant_open_id)


def get(reservation_id: str) -> Optional[dict]:
    """Return the mapping for a reservation, or None if unknown."""
    return _store().get(reservation_id)


def find_by_bench_and_time(bench_no: str, start_time: str) -> Optional[dict]:
    """Reverse lookup when we only have benchNo+startTime (e.g. from a
    stale approve button). Returns the most recent match (last-wins).

    Time comparison is format-tolerant (see `_norm_time`): the startTime the
    bench API puts on the approve button need not be byte-identical to the
    value the applicant originally submitted."""
    want_t = _norm_time(start_time)
    found = _store().find(
        lambda v: v.get("bench_no") == bench_no
        and _norm_time(v.get("start_time", "")) == want_t
    )
    return found[1] if found else None


def list_all() -> dict:
    return _store().all()
