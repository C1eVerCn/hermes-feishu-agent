"""tests for feishu/message_pump — 飞书消息出队投递（mock 一切外部，离线）。"""
import pytest

from feishu import message_pump as mp


@pytest.fixture(autouse=True)
def _stub_io(monkeypatch):
    """默认：email→open_id 命中，mobile 反查命中，发送成功。各用例可覆盖。"""
    monkeypatch.setattr(mp.notify, "_email_to_open_id", lambda e: "ou_from_email" if e else "")
    monkeypatch.setattr(mp.identity, "open_id_of_mobile", lambda m: "ou_from_mobile" if m else "")
    monkeypatch.setattr(mp.notify, "send_text_to_user", lambda oid, text: True)
    yield


# ── _resolve_open_id ──────────────────────────────────────────────────────
def test_resolve_prefers_email():
    assert mp._resolve_open_id("a@x.com", "13800138000") == "ou_from_email"


def test_resolve_falls_back_to_mobile(monkeypatch):
    monkeypatch.setattr(mp.notify, "_email_to_open_id", lambda e: "")  # email 解析失败
    assert mp._resolve_open_id("a@x.com", "13800138000") == "ou_from_mobile"


def test_resolve_miss_returns_empty(monkeypatch):
    monkeypatch.setattr(mp.notify, "_email_to_open_id", lambda e: "")
    monkeypatch.setattr(mp.identity, "open_id_of_mobile", lambda m: "")
    assert mp._resolve_open_id("a@x.com", "138") == ""


# ── _deliver_one ──────────────────────────────────────────────────────────
def test_deliver_success():
    status, err = mp._deliver_one({"receiverEmail": "a@x.com", "title": "审批", "content": "已批准"})
    assert status == mp._STATUS_OK and err == ""


def test_deliver_no_recipient_fails(monkeypatch):
    monkeypatch.setattr(mp.notify, "_email_to_open_id", lambda e: "")
    monkeypatch.setattr(mp.identity, "open_id_of_mobile", lambda m: "")
    status, err = mp._deliver_one({"receiverEmail": "a@x.com", "content": "x"})
    assert status == mp._STATUS_FAIL and "open_id" in err


def test_deliver_empty_content_fails():
    status, err = mp._deliver_one({"receiverEmail": "a@x.com", "title": "", "content": ""})
    assert status == mp._STATUS_FAIL


def test_deliver_send_returns_false():
    # 发送分块未全部成功 → 失败
    import feishu.message_pump as _mp
    _mp.notify.send_text_to_user = lambda oid, text: False
    status, err = mp._deliver_one({"receiverEmail": "a@x.com", "content": "x"})
    assert status == mp._STATUS_FAIL


# ── pump_once（mock client）────────────────────────────────────────────────
class _FakeClient:
    def __init__(self, pull_data):
        self._pull = pull_data
        self.reports = []

    def call(self, tool, args, timeout=30):
        if tool == "pull_pending_feishu_message":
            return {"code": 200, "data": self._pull}
        if tool == "report_feishu_message_result":
            self.reports.append(args)
            return {"code": 200, "data": None}
        raise AssertionError(f"unexpected tool {tool}")


def test_pump_once_delivers_and_reports():
    c = _FakeClient([
        {"id": "m1", "receiverEmail": "a@x.com", "title": "审批", "content": "已批准"},
        {"id": "m2", "receiverMobile": "13800138000", "content": "已驳回"},
    ])
    n = mp.pump_once(c, "secret-key", 50)
    assert n == 2
    # 两条都回报成功（status=1），且都带上 id
    assert {r["id"] for r in c.reports} == {"m1", "m2"}
    assert all(r["status"] == mp._STATUS_OK for r in c.reports)
    assert all(r["appKey"] == "secret-key" for r in c.reports)


def test_pump_once_reports_failure_on_unresolvable(monkeypatch):
    monkeypatch.setattr(mp.notify, "_email_to_open_id", lambda e: "")
    monkeypatch.setattr(mp.identity, "open_id_of_mobile", lambda m: "")
    c = _FakeClient([{"id": "m9", "receiverEmail": "ghost@x.com", "content": "x"}])
    mp.pump_once(c, "k", 50)
    assert len(c.reports) == 1
    assert c.reports[0]["status"] == mp._STATUS_FAIL
    assert c.reports[0]["errorMsg"]  # 失败必带原因


def test_pump_once_skips_blank_id():
    c = _FakeClient([{"id": "", "receiverEmail": "a@x.com", "content": "x"}])
    mp.pump_once(c, "k", 50)
    assert c.reports == []  # 无 id 跳过，不回报


def test_pump_once_pull_failure_returns_zero():
    class _Boom:
        def call(self, *a, **k):
            raise RuntimeError("network down")
    assert mp.pump_once(_Boom(), "k", 50) == 0  # 异常吞掉，不抛
