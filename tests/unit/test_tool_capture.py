import threading
import ocl.tool_capture as tc


def test_record_then_read_in_order():
    tc.clear("s1")
    tc.record("s1", "fetch_user_reservation", {"code": 200, "data": [{"vehicleNo": "PNV332"}]})
    tc.record("s1", "fetch_available_vehicles", {"code": 200, "data": ["PNV332"]})
    items = tc.read("s1")
    assert [i["tool"] for i in items] == ["fetch_user_reservation", "fetch_available_vehicles"]
    assert items[0]["result"]["data"][0]["vehicleNo"] == "PNV332"


def test_sessions_isolated():
    tc.clear("a"); tc.clear("b")
    tc.record("a", "t", {"code": 200})
    assert tc.read("b") == []


def test_clear_empties_session():
    tc.record("s2", "t", {"code": 200})
    tc.clear("s2")
    assert tc.read("s2") == []


def test_record_coerces_json_string():
    tc.clear("s3")
    tc.record("s3", "t", '{"code": 200, "data": []}')
    assert tc.read("s3")[0]["result"]["code"] == 200


def test_record_tolerates_non_json_string():
    tc.clear("s3b")
    tc.record("s3b", "t", "not json")
    assert tc.read("s3b")[0]["result"] == "not json"


def test_thread_safe_concurrent_record():
    tc.clear("s4")

    def worker():
        for _ in range(50):
            tc.record("s4", "t", {"code": 200})

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(tc.read("s4")) == 200
