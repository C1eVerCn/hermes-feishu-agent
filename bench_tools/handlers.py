"""台架预约工具 handler。

通过 httpx 直接调用真实台架预约后端（Docker容器，端口9013），无 mock 层。
emailAddress 由服务端从 thread-local注入（不暴露给 LLM）——参考 ocl.tool_guard.get_current_email()。

需要 Authorization: Bearer <JWT>，由 bench_tools.jwt_auth 提供。JwtAuthFilter
会按 JWT sub → employee.id 查 user 找员工记录。dev 默认 sub=234234（zxs admin）。
"""
import json
import logging

import httpx

from config.settings import settings
from ocl.tool_guard import get_current_email
from bench_tools.jwt_auth import auth_headers

log = logging.getLogger(__name__)

BASE = settings.BENCH_API_BASE_URL
_PREFIX = "/fmp/testBenchReservationForAgent"


def _ok(resp) -> str:
 if resp.is_success:
  return json.dumps(resp.json(), ensure_ascii=False)
 return json.dumps({"error": f"HTTP {resp.status_code}: {resp.text[:300]}"}, ensure_ascii=False)


def list_architectures(args: dict, **_) -> str:
 r = httpx.get(f"{BASE}{_PREFIX}/architectures", headers=auth_headers(), timeout=10)
 return _ok(r)


def list_available_benches(args: dict, **_) -> str:
 body = {"emailAddress": get_current_email()}
 if args.get("architecture"):
  body["architecture"] = args["architecture"]
 if args.get("needParkingTest") is not None:
  body["needParkingTest"] = args["needParkingTest"]
 r = httpx.post(f"{BASE}{_PREFIX}/availableTestBenches", headers=auth_headers(), json=body, timeout=10)
 return _ok(r)


def reserve_bench(args: dict, **_) -> str:
 body = {"emailAddress": get_current_email(), **args}
 r = httpx.post(f"{BASE}{_PREFIX}/reserveTestBench", headers=auth_headers(), json=body, timeout=10)
 return _ok(r)


def cancel_reservation(args: dict, **_) -> str:
 body = {"emailAddress": get_current_email(), **args}
 r = httpx.post(f"{BASE}{_PREFIX}/cancel", headers=auth_headers(), json=body, timeout=10)
 return _ok(r)


def approve_reservation(args: dict, **_) -> str:
 body = {"emailAddress": get_current_email(), **args}
 r = httpx.post(f"{BASE}{_PREFIX}/approve", headers=auth_headers(), json=body, timeout=10)
 return _ok(r)


def list_my_reservations(args: dict, **_) -> str:
 body = {"emailAddress": get_current_email(), **args}
 r = httpx.post(f"{BASE}{_PREFIX}/myReservations", headers=auth_headers(), json=body, timeout=10)
 return _ok(r)


def list_my_approvals(args: dict, **_) -> str:
 body = {"emailAddress": get_current_email(), **args}
 r = httpx.post(f"{BASE}{_PREFIX}/myApprovals", headers=auth_headers(), json=body, timeout=10)
 return _ok(r)


def return_bench(args: dict, **_) -> str:
 body = {"emailAddress": get_current_email(), **args}
 r = httpx.post(f"{BASE}{_PREFIX}/returnTestBench", headers=auth_headers(), json=body, timeout=10)
 return _ok(r)

