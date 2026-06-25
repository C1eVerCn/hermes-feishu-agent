"""open_id → {email, name} 的身份解析层。

通过飞书 Contact API +内存缓存解析用户身份；角色覆盖从 data/identity_map.json 读取。
后台按 emailAddress 做权限校验；本地 OCL 只做粗粒度 role-based 工具门控。

历史版本需要在 identity_map.json 里手动维护每个用户的 open_id→email。
当前版本：email/name 由飞书 API 自动解析，文件里只存管理员手动覆盖的角色。
"""
import json
import os
import threading
import logging

import lark_oapi as lark
from lark_oapi.api.contact.v3 import GetUserRequest

from config.settings import settings

log = logging.getLogger(__name__)

_MAP_FILE = os.path.join(os.path.dirname(__file__), "..", "data", "identity_map.json")
_lock = threading.Lock()

# ── in-memory caches (open_id → str) ─────────────────────────────────────
_email_cache: dict[str, str] = {}
_name_cache: dict[str, str] = {}
# 负向缓存：尝试解析过但没有结果的 open_id
_miss_cache: set[str] = set()
# 反向索引：email → open_id（从 identity_map.json v2 schema 扫出来）
# 用于 Feishu v3 不支持 email→open_id lookup 时本地直查
_email_to_oid_cache: dict[str, str] = {}
# 手机号是第二识别符（邮箱为主）：open_id → mobile 及反向 mobile → open_id，
# 同样从 identity_map.json v2 schema 扫出来（飞书 contact v3 默认无 mobile 权限）。
_mobile_cache: dict[str, str] = {}
_mobile_to_oid_cache: dict[str, str] = {}

# ── role overrides from identity_map.json ─────────────────────────────────
_role_overrides: dict[str, int] = {}
_role_overrides_loaded: bool = False
_role_overrides_mtime: float | None = None

# ── lazily-initialised Feishu client ──────────────────────────────────────
_client: lark.Client | None = None


def _get_client() -> lark.Client:
    global _client
    if _client is None:
        # timeout 单位秒——之前 lark SDK 默认 ~6s × retries=3 ≈ 30s+，
        # 容器内偶发 DNS 抖动会被 urllib3 放大成 30s+ 卡顿
        # 把单次调用卡到 2s：失败立即进 negative cache，后续消息不重试
        _client = (
            lark.Client.builder()
            .app_id(settings.FEISHU_APP_ID)
            .app_secret(settings.FEISHU_APP_SECRET)
            .timeout(2.0)
            .build()
        )
    return _client


def _load_role_overrides() -> dict[str, int]:
    """Load open_id→role from identity_map.json, reloading when the file
    changes on disk. bot.identity_admin writes new users to the SAME file, so
    without mtime-based invalidation a freshly-registered colleague stays at
    role 0 in this cache → their tool calls get wrongly blocked (the cache was
    loaded once at startup and never refreshed)."""
    global _role_overrides_loaded, _role_overrides_mtime
    try:
        mtime = os.path.getmtime(_MAP_FILE)
    except OSError:
        mtime = None
    if _role_overrides_loaded and mtime == _role_overrides_mtime:
        return _role_overrides
    # (re)load — clear first so deletions on disk are reflected
    _role_overrides.clear()
    _email_to_oid_cache.clear()
    _mobile_cache.clear()
    _mobile_to_oid_cache.clear()
    try:
        with open(_MAP_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            # accept both new format {open_id: 3} and old format {open_id: {role: 3, ...}}
            for k, v in data.items():
                if isinstance(v, dict):
                    _role_overrides[k] = v.get("role", 0)
                    # 同时建反向 email → open_id 索引，绕过 Feishu v3 不支持的
                    # email→open_id lookup（99992402 field validation failed）。
                    # 来源：管理员手动设置的 admin_assign 条目（如 chenyihang）
                    email = v.get("email") or ""
                    if email and email not in _email_to_oid_cache:
                        _email_to_oid_cache[email] = k
                    # 手机号同样建正向/反向索引（第二识别符）。
                    mobile = (v.get("mobile") or "").strip()
                    if mobile:
                        _mobile_cache[k] = mobile
                        _mobile_to_oid_cache.setdefault(mobile, k)
                elif isinstance(v, int):
                    _role_overrides[k] = v
    except Exception:
        pass
    _role_overrides_mtime = mtime
    _role_overrides_loaded = True
    return _role_overrides


def _invalidate_role_overrides() -> None:
    global _role_overrides_loaded, _role_overrides_mtime
    _role_overrides.clear()
    _email_to_oid_cache.clear()
    _mobile_cache.clear()
    _mobile_to_oid_cache.clear()
    _role_overrides_loaded = False
    _role_overrides_mtime = None


def _resolve_open_id(open_id: str) -> tuple[str, str]:
    """Call Feishu Contact API. Returns (email, name) or ('', '')."""
    try:
        client = _get_client()
        req = (
            GetUserRequest.builder()
            .user_id(open_id)
            .user_id_type("open_id")
            .build()
        )
        resp = client.contact.v3.user.get(req)
        if resp.success() and resp.data and resp.data.user:
            u = resp.data.user
            email = getattr(u, "email", "") or ""
            name = getattr(u, "name", "") or ""
            log.info("feishu_user_resolved open_id=%s email=%s name=%s", open_id, email, name)
            return email, name
        log.warning("feishu_user_resolve_failed open_id=%s code=%s msg=%s", open_id, resp.code, resp.msg)
    except Exception:
        log.exception("feishu_user_resolve_error open_id=%s", open_id)
    return "", ""


# ── public API ──────────────────────────────────────────────────────────────

def email_of(open_id: str) -> str:
    if not open_id:
        return ""
    with _lock:
        if open_id in _email_cache:
            return _email_cache[open_id]
        if open_id in _miss_cache:
            return ""
        # unlock while calling external API
    email, name = _resolve_open_id(open_id)
    with _lock:
        if email:
            _email_cache[open_id] = email
            _name_cache[open_id] = name
            return email
        _miss_cache.add(open_id)
        return ""


def mobile_of(open_id: str) -> str:
    """open_id → 手机号（第二识别符）。

    来源优先级：identity_map.json（管理员/同步写入的 ``mobile`` 字段）。飞书 contact v3
    默认无 mobile 权限，故不从飞书 API 解析（拿到权限后可在此补充，与 email_of 同构）。
    无配置返回 ""。
    """
    if not open_id:
        return ""
    _load_role_overrides()  # 确保 _mobile_cache 已填充（mtime 变化时重载）
    with _lock:
        return _mobile_cache.get(open_id, "")


def open_id_of_mobile(mobile: str) -> str:
    """反向：手机号 → open_id（本地 identity_map 索引）。未知返回 ""。"""
    mobile = (mobile or "").strip()
    if not mobile:
        return ""
    _load_role_overrides()
    with _lock:
        return _mobile_to_oid_cache.get(mobile, "")


def build_caller_identity(open_id: str) -> "CallerIdentity":
    """构造一个 CallerIdentity（openid + email + mobile）。

    email/mobile 解析失败时仍然构造对象（openid 不会空），业务侧 caller.is_authenticated
    即可判定是否已认证。"""
    from ocl.tool_guard import CallerIdentity
    return CallerIdentity(
        openid=open_id,
        email=email_of(open_id) if open_id else "",
        mobile=mobile_of(open_id) or None,
    )


def name_of(open_id: str) -> str:
    if not open_id:
        return ""
    with _lock:
        if open_id in _name_cache:
            return _name_cache[open_id]
    # trigger resolution via email_of (which populates both caches)
    email = email_of(open_id)
    if not email:
        return ""
    with _lock:
        return _name_cache.get(open_id, "")


def open_id_of(email: str) -> str:
    """Reverse lookup: email → open_id.

    Reads the local index built from identity_map.json v2 schema
    (admin-assigned records carry an `email` field). Avoids the
    Feishu v3 BatchGetIdUser API which rejects `user_id_type=email`
    with code 99992402. Returns "" if email is unknown locally —
    the caller (notify) can then fall through to other strategies.
    """
    if not email:
        return ""
    _load_role_overrides()  # ensures _email_to_oid_cache is populated
    with _lock:
        return _email_to_oid_cache.get(email, "")


def role_of(open_id: str) -> int:
    """Return the user's role override: 0 unknown, 1 普通, 2 调度员, 3 管理员.

    With a real backend, permissions are enforced server-side by emailAddress,
    so this only reflects admin-assigned overrides in identity_map.json. Returns
    0 when no override exists (the bot does not gate by role locally).
    """
    if not open_id:
        return 0
    overrides = _load_role_overrides()
    return overrides.get(open_id, 0)


def lookup(open_id: str) -> dict | None:
    """Return {email, name, role} or None. Kept for backward compat."""
    if not open_id:
        return None
    email = email_of(open_id)
    if not email:
        return None
    return {
        "email": email,
        "name": name_of(open_id),
        "role": role_of(open_id),
    }


def set_role(open_id: str, role: int) -> None:
    """Write a role override for open_id. Email/name come from the API."""
    if not open_id:
        return
    with _lock:
        _load_role_overrides()
        _role_overrides[open_id] = role
        overrides = dict(_role_overrides)
    os.makedirs(os.path.dirname(_MAP_FILE), exist_ok=True)
    with open(_MAP_FILE, "w", encoding="utf-8") as f:
        json.dump(overrides, f, ensure_ascii=False, indent=2)
    log.info("role_override_set open_id=%s role=%d", open_id, role)
