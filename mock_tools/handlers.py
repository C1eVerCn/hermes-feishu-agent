"""Tool handlers that call the mock test-bench reservation API.
emailAddress is NOT a tool argument — it is injected from the current user's
thread-local context (set by bot/handler.py from open_id → identity)."""
import json
import logging

import httpx

from config.settings import settings
from ocl.tool_guard import get_current_email

log = logging.getLogger(__name__)

BASE = settings.MOCK_API_BASE_URL
_PREFIX = "/fmp/testBenchReservationForAgent"


def _ok(resp) -> str:
    if resp.is_success:
        return json.dumps(resp.json(), ensure_ascii=False)
    return json.dumps({"error": f"HTTP {resp.status_code}: {resp.text[:300]}"}, ensure_ascii=False)


def _email() -> str:
    return get_current_email()


def list_architectures(args: dict, **_) -> str:
    r = httpx.get(f"{BASE}{_PREFIX}/architectures", timeout=10)
    return _ok(r)


def list_available_benches(args: dict, **_) -> str:
    body = {"emailAddress": _email()}
    if args.get("architecture"):
        body["architecture"] = args["architecture"]
    if args.get("needParkingTest") is not None:
        body["needParkingTest"] = args["needParkingTest"]
    r = httpx.post(f"{BASE}{_PREFIX}/availableTestBenches", json=body, timeout=10)
    return _ok(r)


def reserve_bench(args: dict, **_) -> str:
    body = {"emailAddress": _email(), **args}
    r = httpx.post(f"{BASE}{_PREFIX}/reserveTestBench", json=body, timeout=10)
    return _ok(r)


def cancel_reservation(args: dict, **_) -> str:
    body = {"emailAddress": _email(), **args}
    r = httpx.post(f"{BASE}{_PREFIX}/cancel", json=body, timeout=10)
    return _ok(r)


def approve_reservation(args: dict, **_) -> str:
    body = {"emailAddress": _email(), **args}
    r = httpx.post(f"{BASE}{_PREFIX}/approve", json=body, timeout=10)
    return _ok(r)


def list_my_reservations(args: dict, **_) -> str:
    body = {"emailAddress": _email(), **args}
    r = httpx.post(f"{BASE}{_PREFIX}/myReservations", json=body, timeout=10)
    return _ok(r)


def list_my_approvals(args: dict, **_) -> str:
    body = {"emailAddress": _email(), **args}
    r = httpx.post(f"{BASE}{_PREFIX}/myApprovals", json=body, timeout=10)
    return _ok(r)


def return_bench(args: dict, **_) -> str:
    body = {"emailAddress": _email(), **args}
    r = httpx.post(f"{BASE}{_PREFIX}/returnTestBench", json=body, timeout=10)
    return _ok(r)
