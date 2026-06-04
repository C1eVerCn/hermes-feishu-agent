"""End-to-end tests for the test-bench reservation API via FastAPI TestClient."""
import pytest
from fastapi.testclient import TestClient

from mock_api import fake_db
from mock_api.main import app

client = TestClient(app)


@pytest.fixture(autouse=True)
def _reset():
    fake_db.reset()
    yield


def test_architectures():
    r = client.get("/fmp/testBenchReservationForAgent/architectures")
    assert r.status_code == 200
    body = r.json()
    assert body["code"] == 200
    assert "1.0架构" in body["data"]


def test_available_benches_unknown_user():
    r = client.post("/fmp/testBenchReservationForAgent/availableTestBenches",
                    json={"emailAddress": "ghost@example.com"})
    assert r.json()["code"] != 200


def test_reserve_and_my_reservations():
    bench = next(bn for bn, b in fake_db.benches.items()
                 if b["status"] == 1 and b["group_id"] == "G1")
    r = client.post("/fmp/testBenchReservationForAgent/reserveTestBench",
                    json={"emailAddress": "zhangsan@example.com", "benchNo": bench,
                          "startTime": "2099-01-01 09:00:00", "endTime": "2099-01-01 10:00:00",
                          "taskName": "t", "testPurpose": "p"})
    assert r.json()["code"] == 200

    r2 = client.post("/fmp/testBenchReservationForAgent/myReservations",
                     json={"emailAddress": "zhangsan@example.com"})
    body = r2.json()
    assert body["code"] == 200
    assert any(rec["benchNo"] == bench for rec in body["data"])


def test_approve_flow():
    # TJ001 seeded pending; it belongs to group G2 → scheduler2 owns G2
    r = client.post("/fmp/testBenchReservationForAgent/approve",
                    json={"emailAddress": "scheduler2@example.com", "benchNo": "TJ001",
                          "approvalResult": 1, "approvalRemark": "同意"})
    assert r.json()["code"] == 200


def test_return_flow():
    r = client.post("/fmp/testBenchReservationForAgent/returnTestBench",
                    json={"emailAddress": "zhangsan@example.com", "benchNo": "TJ006",
                          "returnLocation": "A区3号位"})
    assert r.json()["code"] == 200


def test_response_envelope_shape():
    r = client.get("/fmp/testBenchReservationForAgent/architectures")
    body = r.json()
    assert set(body.keys()) == {"code", "message", "data"}
