"""Shared JSON-file persistence helper.

A small reusable store for "small, append-mostly, read-rarely" mappings
backed by a JSON file. Used by `bot/reservation_store.py` and any other
module that needs a simple on-disk dict (BUGFIX #7 — was previously
re-implemented three times in the codebase).

Properties:
- Thread-safe: all reads/writes are protected by a module-level Lock
- Atomic: writes use `os.replace` on a tmp file so a crash mid-write
  never leaves a half-written file
- Tolerates missing file (returns empty mapping)
- Tolerates corrupt JSON (returns empty mapping + warning, rather than
  crashing the caller)
"""
import json
import logging
import os
import threading
from pathlib import Path
from typing import Any, Callable, Optional

log = logging.getLogger(__name__)

_LOCKS: dict[str, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()


def _lock_for(path: str) -> threading.Lock:
    with _LOCKS_GUARD:
        lk = _LOCKS.get(path)
        if lk is None:
            lk = threading.Lock()
            _LOCKS[path] = lk
        return lk


def _ensure_file(path: str) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    if not p.exists():
        p.write_text("{}", encoding="utf-8")


def _load(path: str) -> dict:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (json.JSONDecodeError, OSError) as e:
        log.warning("json_store_load_failed path=%s err=%s", path, e)
        return {}


def _save(path: str, data: dict) -> None:
    _ensure_file(path)
    p = Path(path)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, p)


class JsonStore:
    """Thread-safe JSON-file-backed mapping.

    Usage:
        store = JsonStore("data/foo.json")
        store.put("k", {"value": 1})
        assert store.get("k") == {"value": 1}
        assert store.find(lambda v: v.get("value") == 1) is not None
    """

    def __init__(self, path: str) -> None:
        self._path = path
        self._lock = _lock_for(path)

    @property
    def path(self) -> str:
        return self._path

    def _load(self) -> dict:
        with self._lock:
            return _load(self._path)

    def _save(self, data: dict) -> None:
        with self._lock:
            _save(self._path, data)

    def get(self, key: str) -> Optional[dict]:
        return self._load().get(key)

    def put(self, key: str, value: dict) -> None:
        if not key:
            return
        with self._lock:
            d = _load(self._path)
            d[key] = value
            _save(self._path, d)
            log.debug("json_store_put path=%s key=%s", self._path, key)

    def set_all(self, data: dict) -> None:
        """Replace the entire store contents. Atomic write."""
        with self._lock:
            _save(self._path, data)
            log.debug("json_store_set_all path=%s count=%d", self._path, len(data))

    def find(self, predicate: Callable[[dict], bool]) -> Optional[tuple[str, dict]]:
        """Return the LAST (key, value) pair whose value matches predicate, or
        None. O(n) — fine for small stores (<10k entries)."""
        last = None
        for k, v in self._load().items():
            if predicate(v):
                last = (k, v)
        return last

    def all(self) -> dict:
        return dict(self._load())
