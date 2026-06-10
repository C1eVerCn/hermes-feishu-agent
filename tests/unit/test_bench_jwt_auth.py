"""Unit tests for bench_tools/jwt_auth.py — JWT generation, caching, refresh."""
import time
from unittest.mock import patch
import jwt

import bench_tools.jwt_auth as ja


def _reset_cache():
    ja._cached_token = ""
    ja._cached_issued_at = 0.0


def test_generates_valid_jwt_with_correct_claims(monkeypatch):
    _reset_cache()
    monkeypatch.setenv("BENCH_JWT_SECRET", "test-secret-123")
    monkeypatch.setenv("BENCH_JWT_SUB", "u-001")

    token = ja.get_token()
    # Decode WITHOUT verify to inspect claims (we just generated it).
    claims = jwt.decode(token, options={"verify_signature": False})
    assert claims["sub"] == "u-001"
    assert claims["role"] == "operator"
    assert claims["exp"] - claims["iat"] == 12 * 3600  # 12h lifetime


def test_uses_dev_defaults_when_env_unset(monkeypatch):
    _reset_cache()
    monkeypatch.delenv("BENCH_JWT_SECRET", raising=False)
    monkeypatch.delenv("BENCH_JWT_SUB", raising=False)

    token = ja.get_token()
    claims = jwt.decode(token, options={"verify_signature": False})
    # Dev sub = 234234 (zxs admin)
    assert claims["sub"] == "234234"


def test_caches_token_within_refresh_window(monkeypatch):
    _reset_cache()
    monkeypatch.setenv("BENCH_JWT_SECRET", "test-secret")

    t1 = ja.get_token()
    t2 = ja.get_token()
    assert t1 == t2  # cached, no re-generation


def test_refreshes_after_half_life(monkeypatch):
    _reset_cache()
    monkeypatch.setenv("BENCH_JWT_SECRET", "test-secret")

    t1 = ja.get_token()
    # Simulate 11h elapsed (beyond _REFRESH_AT_SECONDS = 10h)
    fake_now = ja._cached_issued_at + 11 * 3600
    with patch.object(ja.time, "time", return_value=fake_now):
        t2 = ja.get_token()
    assert t1 != t2, "Token must regenerate past half-life"
    # New token starts from fake_now
    claims = jwt.decode(t2, options={"verify_signature": False})
    assert claims["iat"] == int(fake_now)


def test_thread_safe_under_concurrent_get_token(monkeypatch):
    """Multiple threads calling get_token must not generate duplicate tokens
    or leak partial state."""
    import threading
    _reset_cache()
    monkeypatch.setenv("BENCH_JWT_SECRET", "test-secret")

    tokens: list[str] = []
    errors: list[Exception] = []

    def worker():
        try:
            tokens.append(ja.get_token())
        except Exception as e:
            errors.append(e)

    threads = [threading.Thread(target=worker) for _ in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors
    assert all(tok == tokens[0] for tok in tokens), "All threads must get same cached token"


def test_auth_headers_returns_bearer_format(monkeypatch):
    _reset_cache()
    monkeypatch.setenv("BENCH_JWT_SECRET", "test-secret")

    h = ja.auth_headers()
    assert "Authorization" in h
    assert h["Authorization"].startswith("Bearer ")
    # Token portion must be a valid JWT
    token = h["Authorization"].removeprefix("Bearer ")
    claims = jwt.decode(token, options={"verify_signature": False})
    assert claims["sub"] == "234234"  # dev default
