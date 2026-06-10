"""JWT token manager for bench reservation API (fmp-app @9013).

The fmp-app's JwtAuthFilter does:
  1. Verify token signature with `auth.jwt.secret` (from nacos `dmz-fmp-dev.yml`).
  2. Extract `sub` claim → look up Employee by primary key in DB.
  3. If not found → 401 "用户不存在".

This module generates and caches a JWT for an internal service account so the
agent can call the bench API. Token lifetime matches fmp-app's `expire-hours`
(default 12h); we refresh at half-life to avoid edge-of-expiry 401s.

The DEV secret and DEV employee id (234234 = zxs admin) are hardcoded as a
fallback for local dev convenience. In production, set BENCH_JWT_SECRET and
BENCH_JWT_SUB env vars.
"""
import logging
import threading
import time

import jwt

log = logging.getLogger(__name__)

# Dev defaults from nacos `dmz-fmp-dev.yml`:
#   auth.jwt.secret: Hqmy9DUe38Zz4eWV1oxv3nYGgjkYQCaTyo6cUcMCnZGrAz9VP1PoU/gk4+WkM2TP
#   auth.jwt.expire-hours: 12
# Internal service account = admin `zxs@immotors.com` (id=234234).
_DEV_SECRET = "Hqmy9DUe38Zz4eWV1oxv3nYGgjkYQCaTyo6cUcMCnZGrAz9VP1PoU/gk4+WkM2TP"
_DEV_SUB = "234234"

# Refresh at half of expire-hours (12h) to avoid edge-of-expiry issues.
_TOKEN_LIFETIME_SECONDS = 12 * 3600
_REFRESH_AT_SECONDS = 10 * 3600

_lock = threading.Lock()
_cached_token: str = ""
_cached_issued_at: float = 0.0


def _generate_token(secret: str, sub: str) -> str:
    now = int(time.time())
    payload = {
        "sub": sub,
        "role": "operator",
        "iat": now,
        "exp": now + _TOKEN_LIFETIME_SECONDS,
    }
    return jwt.encode(payload, secret, algorithm="HS256")


def get_token() -> str:
    """Return a cached JWT, refreshing at half-life.

    Reads `BENCH_JWT_SECRET` and `BENCH_JWT_SUB` from env; falls back to
    hardcoded dev defaults (with a one-time warning on first call).
    """
    global _cached_token, _cached_issued_at
    import os

    now = time.time()
    with _lock:
        if _cached_token and (now - _cached_issued_at) < _REFRESH_AT_SECONDS:
            return _cached_token

        secret = os.getenv("BENCH_JWT_SECRET", "").strip() or _DEV_SECRET
        sub = os.getenv("BENCH_JWT_SUB", "").strip() or _DEV_SUB

        if not _cached_token:
            log.info("bench_jwt_init sub=%s lifetime=%dh (set BENCH_JWT_SECRET in prod)",
                     sub, _TOKEN_LIFETIME_SECONDS // 3600)
        else:
            log.info("bench_jwt_refresh sub=%s (half-life reached)", sub)

        _cached_token = _generate_token(secret, sub)
        _cached_issued_at = now
        return _cached_token


def auth_headers() -> dict[str, str]:
    """Helper for handlers: returns the Authorization header dict."""
    return {"Authorization": f"Bearer {get_token()}"}
