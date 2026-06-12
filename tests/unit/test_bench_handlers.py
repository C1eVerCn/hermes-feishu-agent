"""Unit tests for bench_tools.handlers normalization + filtering logic.
Added 2026-06-12 (code-review fixes #3, #6, #7)."""
import json
from unittest.mock import MagicMock, patch

import pytest

from bench_tools import handlers as h


def _fake_resp(data):
    r = MagicMock()
    r.is_success = True
    r.json.return_value = {"code": 200, "message": "ok", "data": data}
    return r


# ── #6: list_my_reservations status filtered CLIENT-SIDE ────────────────────

def _patched_post(data):
    return patch("bench_tools.handlers.httpx.post", return_value=_fake_resp(data))


@pytest.fixture(autouse=True)
def _ctx():
    with patch.object(h, "get_current_email", return_value="a@x.com"), \
         patch.object(h, "auth_headers", return_value={}):
        yield


def test_list_my_reservations_status_filtered_client_side():
    data = [{"benchNo": "C1", "status": 0}, {"benchNo": "C1", "status": 2},
            {"benchNo": "T1", "status": 3}, {"benchNo": "T1", "status": 4}]
    with _patched_post(data) as post:
        out = h.list_my_reservations({"status": [0, 1, 4]})
    got = sorted(d["status"] for d in json.loads(out)["data"])
    assert got == [0, 4]
    # 'status' must NOT be forwarded to the backend (it may reject arrays).
    assert "status" not in post.call_args.kwargs["json"]


def test_list_my_reservations_scalar_status_filter():
    data = [{"status": 0}, {"status": 1}]
    with _patched_post(data):
        out = h.list_my_reservations({"status": 0})
    assert [d["status"] for d in json.loads(out)["data"]] == [0]


def test_list_my_reservations_no_status_passthrough():
    data = [{"status": 0}, {"status": 2}, {"status": 3}]
    with _patched_post(data):
        out = h.list_my_reservations({})
    assert len(json.loads(out)["data"]) == 3


# ── #3: non-string required field must not crash dry_run ────────────────────

def test_dry_run_non_string_field_no_crash():
    res = h.dry_run_reserve_bench({
        "benchNo": 5, "startTime": "2026-06-11 17:00:00",
        "endTime": "2026-06-11 20:00:00", "taskName": "t", "testPurpose": "p",
    })
    parsed = json.loads(res)
    assert parsed.get("dry_run") is True
    assert not parsed.get("missing_fields")


# ── #7: single-key unwrap only for genuine payloads ─────────────────────────

def test_normalize_preserves_legit_jsonish_field_value():
    norm, _ = h._normalize_args({"taskName": '{"foo":1}'})
    assert norm["taskName"] == '{"foo":1}'


def test_normalize_unwraps_genuine_wrapper():
    norm, _ = h._normalize_args(
        {"reservation": '{"benchNo":"CT001","taskName":"t"}'})
    assert norm["benchNo"] == "CT001"
    assert norm["taskName"] == "t"
