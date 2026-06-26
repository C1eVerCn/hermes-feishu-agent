"""bot/backend_role — 用户角色的**后端事实源**（fmp get_user_context）。

后端 `get_user_context(email)` 返回 ``data.role``（1 普通 / 2 调度员 / 3 管理员）。
本模块取它并缓存（默认 5 分钟），让 :func:`bot.handler._resolve_identity` 把后端角色
**同步进 identity_map**——OCL 仍照常读 identity_map（``ocl.identity.role_of``），透明拿到
后端角色，无需手动「设置角色」。

设计：
- 查到 role(1/2/3) → 返回它（事实源）。
- 后端不认识该用户（data:null）/ 无 role / 调用异常 → 返回 None：调用方**不降级**，
  保持原有 identity_map / auto-in-scope 行为（非 fmp 用户照旧）。
- 只读 fmp，不改 fmp（仅 get_user_context 查询）。
"""
import logging
import os
import time

log = logging.getLogger(__name__)

_TTL_SECONDS = float(os.getenv("BACKEND_ROLE_TTL", "300"))
# email -> (role | None, expires_monotonic)
_cache: dict[str, tuple[int | None, float]] = {}


def role_of_backend(email: str) -> int | None:
    """后端角色 1/2/3；后端不认识该用户或调用失败 → None（带 TTL 缓存）。"""
    email = (email or "").strip()
    if not email:
        return None
    now = time.monotonic()
    hit = _cache.get(email)
    if hit and hit[1] > now:
        return hit[0]
    role = _fetch(email)
    _cache[email] = (role, now + _TTL_SECONDS)
    return role


def _fetch(email: str) -> int | None:
    try:
        from car_tools import mcp_client
        r = mcp_client.get_mcp_client().call("get_user_context", {"emailAddress": email})
    except Exception:
        log.warning("backend_role fetch failed email=%s…", email[:3], exc_info=True)
        return None
    data = r.get("data") if isinstance(r, dict) else None
    if isinstance(data, dict):
        role = data.get("role")
        if isinstance(role, bool):
            return None
        if isinstance(role, int) and role in (1, 2, 3):
            return role
    return None  # data:null（非平台用户）/ 无合法 role → 不降级，交调用方兜底


def clear_cache() -> None:
    """测试钩子。"""
    _cache.clear()
