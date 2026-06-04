"""Auth is intentionally minimal for the mock: identity/role is carried by
emailAddress in the request body (the real upstream resolves role by email).
We keep an optional static bearer token so existing config keeps working,
but do not enforce roles at the HTTP layer."""
import logging

log = logging.getLogger(__name__)


def generate_static_tokens() -> dict[str, str]:
    # Kept for backwards-compat with main.startup; returns an empty dev token set.
    return {"dev": "mock-dev-token"}
