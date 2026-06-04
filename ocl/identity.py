"""open_id ↔ {email, name, role} mapping for the Feishu test-bench bot.
Role: 0 = unknown/non-platform, 1 = 普通用户, 2 = 调度员, 3 = 管理员.
JSON-file persistence (data/identity_map.json), cached with simple invalidation.
"""
import json
import os
import threading

_MAP_FILE = os.path.join(os.path.dirname(__file__), "..", "data", "identity_map.json")
_lock = threading.Lock()
_cache: dict | None = None


def _invalidate_cache() -> None:
    global _cache
    _cache = None


def _load() -> dict:
    global _cache
    if _cache is not None:
        return _cache
    try:
        with open(_MAP_FILE, "r", encoding="utf-8") as f:
            _cache = json.load(f)
    except Exception:
        _cache = {}
    return _cache


def _save(data: dict) -> None:
    os.makedirs(os.path.dirname(_MAP_FILE), exist_ok=True)
    with open(_MAP_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    _invalidate_cache()


def lookup(open_id: str) -> dict | None:
    with _lock:
        return _load().get(open_id)


def email_of(open_id: str) -> str:
    info = lookup(open_id)
    return info["email"] if info else ""


def role_of(open_id: str) -> int:
    info = lookup(open_id)
    return info["role"] if info else 0


def name_of(open_id: str) -> str:
    info = lookup(open_id)
    return info["name"] if info else ""


def set_role(open_id: str, email: str, name: str, role: int) -> None:
    with _lock:
        data = dict(_load())
        data[open_id] = {"email": email, "name": name, "role": role}
        _save(data)
