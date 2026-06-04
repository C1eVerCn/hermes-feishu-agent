from infra.metrics import Metrics


def test_counter_increments():
    m = Metrics()
    m.inc("messages_received")
    m.inc("messages_received")
    snap = m.snapshot()
    assert snap["messages_received"] == 2


def test_counter_default_zero():
    m = Metrics()
    snap = m.snapshot()
    assert snap.get("messages_received", 0) == 0


def test_histogram_p50_p95():
    m = Metrics()
    for v in range(1, 101):   # 1..100
        m.record("llm_latency_seconds", float(v))
    snap = m.snapshot()
    assert 49 <= snap["llm_latency_seconds_p50"] <= 51
    assert 94 <= snap["llm_latency_seconds_p95"] <= 96


def test_snapshot_is_independent_copy():
    m = Metrics()
    m.inc("x")
    snap1 = m.snapshot()
    m.inc("x")
    snap2 = m.snapshot()
    assert snap1["x"] == 1
    assert snap2["x"] == 2
