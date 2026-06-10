
"""身份管理 v2 — 自动建档 / 角色分配 / 审计。

data/identity_map.json v2 schema:
{
  "ou_xxx_1": {
    "email": "user1@example.com",
    "name": "张三",
    "role": 1,
    "registered_at": "2026-06-09T...",
    "registered_via": "auto_first_contact" | "manual" | "admin_assign" | "feishu_org_sync",
    "note": ""
  },
  ...
}

来源说明：
- auto_first_contact: 首次发消息时自动建档（role=0 pending）
- manual: 管理员手动改 identity_map.json
- admin_assign: 管理员在飞书发"设置角色 <open_id> <1|2|3>"
- feishu_org_sync: 启动时拉全组织成员批量建档

安全铁律（不可变）：
1. emailAddress / API key 永不落盘（email 仅作"人读"显示用）
2. role 数值范围必须 ∈ {0, 1, 2, 3}
3. set_role 必须有 operator open_id（审计）
4. 任何写盘操作必须落 audit（data/identity_audit.jsonl）
"""
import json
import os
import time
import logging
import threading
from datetime import datetime
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)
_lock = threading.Lock()

# role 数值白名单
ALLOWED_ROLES = (0, 1, 2, 3)
ROLE_NAMES = {0: "待审核", 1: "普通用户", 2: "调度员", 3: "管理员"}

DEFAULT_DATA_DIR = os.path.join(
    os.path.dirname(os.path.dirname(__file__)), "data"
)
DEFAULT_MAP_FILE = os.path.join(DEFAULT_DATA_DIR, "identity_map.json")
DEFAULT_AUDIT_FILE = os.path.join(DEFAULT_DATA_DIR, "identity_audit.jsonl")

_singleton: Optional["IdentityAdmin"] = None


def get_admin(map_file=None, audit_file=None):
    global _singleton
    if _singleton is None:
        _singleton = IdentityAdmin(
            map_file or DEFAULT_MAP_FILE,
            audit_file or DEFAULT_AUDIT_FILE,
        )
    return _singleton


class IdentityAdmin:
    """身份管理 v2。线程安全（持文件锁）。"""

    def __init__(self, map_file, audit_file):
        self._map_file = Path(map_file)
        self._audit_file = Path(audit_file)
        self._map_file.parent.mkdir(parents=True, exist_ok=True)
        self._audit_file.parent.mkdir(parents=True, exist_ok=True)
        self._data = self._load()

    def _load(self):
        if not self._map_file.exists():
            return {}
        try:
            with open(self._map_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                # 兼容 v1 (open_id → int) 与 v2 (open_id → dict)
                normalized = {}
                for k, v in data.items():
                    if isinstance(v, int):
                        normalized[k] = {
                            "email": "",
                            "name": "",
                            "role": v,
                            "registered_at": "",
                            "registered_via": "manual",
                            "note": "migrated_from_v1",
                        }
                    elif isinstance(v, dict):
                        normalized[k] = v
                return normalized
        except Exception as e:
            log.warning("identity_load_failed err=%s", e)
        return {}

    def _save(self):
        tmp_path = self._map_file.with_suffix(".json.tmp")
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(self._data, f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, self._map_file)

    def _audit(self, op, open_id, before, after, operator="system", note=""):
        record = {
            "ts": time.time(),
            "ts_human": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "op": op,
            "open_id": open_id,
            "operator": operator,
            "before": before,
            "after": after,
            "note": note,
        }
        with _lock:
            with open(self._audit_file, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + chr(10))

    # ── 读 API ──

    def get(self, open_id):
        return self._data.get(open_id)

    def get_role(self, open_id):
        rec = self._data.get(open_id)
        if rec is None:
            return 0
        return int(rec.get("role", 0))

    def list_all(self):
        return dict(self._data)

    def list_by_role(self, role):
        return {k: v for k, v in self._data.items() if int(v.get("role", 0)) == role}

    def list_pending(self):
        """返回 role=0（待审）的用户。"""
        return self.list_by_role(0)

    def is_platform_user(self, open_id):
        return open_id in self._data and self.get_role(open_id) > 0

    # ── 写 API ──

    def auto_register(self, open_id, email="", name=""):
        """首次接触时调用：建档 role=0 pending。

        已存在则不覆盖（避免盖掉 admin 设置的角色）。
        """
        with _lock:
            if open_id in self._data:
                return False, "already_registered"
            record = {
                "email": email or "",
                "name": name or "",
                "role": 0,
                "registered_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "registered_via": "auto_first_contact",
                "note": "",
            }
            self._data[open_id] = record
            self._save()
        self._audit("auto_register", open_id, None, record)
        return True, "created"

    def set_role(self, open_id, role, operator="system", note=""):
        """管理员设置角色。"""
        if role not in ALLOWED_ROLES:
            return False, f"invalid_role {role}, must be one of {ALLOWED_ROLES}"
        if not open_id:
            return False, "missing_open_id"
        with _lock:
            before = self._data.get(open_id, {})
            after = dict(before) if before else {"email": "", "name": "", "registered_via": "admin_assign"}
            after["role"] = role
            after["registered_at"] = after.get("registered_at") or datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            after["registered_via"] = "admin_assign"
            if note:
                after["note"] = note
            self._data[open_id] = after
            self._save()
        self._audit("set_role", open_id, before, after, operator=operator, note=note)
        return True, "ok"

    def update_profile(self, open_id, email=None, name=None, operator="system"):
        """更新 email/name（不触发角色变化）。"""
        with _lock:
            if open_id not in self._data:
                return False, "not_found"
            before = self._data[open_id].copy()
            after = dict(before)
            if email is not None:
                after["email"] = email
            if name is not None:
                after["name"] = name
            self._data[open_id] = after
            self._save()
        self._audit("update_profile", open_id, before, after, operator=operator)
        return True, "ok"

    def bulk_upsert_from_feishu_org(self, members):
        """启动时拉全组织成员批量建档。members: [{open_id, email, name}]。

        已有 entry 不覆盖角色（保留 admin 设置），但更新 email/name。
        """
        n_created = 0
        n_updated = 0
        for m in members:
            oid = m.get("open_id", "")
            if not oid:
                continue
            with _lock:
                if oid in self._data:
                    rec = self._data[oid]
                    changed = False
                    if m.get("email") and m.get("email") != rec.get("email"):
                        rec["email"] = m["email"]
                        changed = True
                    if m.get("name") and m.get("name") != rec.get("name"):
                        rec["name"] = m["name"]
                        changed = True
                    if changed:
                        n_updated += 1
                else:
                    self._data[oid] = {
                        "email": m.get("email", ""),
                        "name": m.get("name", ""),
                        "role": 1,
                        "registered_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                        "registered_via": "feishu_org_sync",
                        "note": "",
                    }
                    n_created += 1
        with _lock:
            self._save()
        return {"created": n_created, "updated": n_updated}

    def full_export(self):
        """导出全表供备份/审计。"""
        return {
            "exported_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "count": len(self._data),
            "by_role": {
                r: len(self.list_by_role(r)) for r in ALLOWED_ROLES
            },
            "users": self._data,
        }
