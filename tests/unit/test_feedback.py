"""bot/feedback.py 单元测试。"""
import json
import time
from pathlib import Path

import bot.feedback as fb


def test_user_hash_anonymous():
 assert fb._user_hash("") == "anonymous"
 assert fb._user_hash(None) == "anonymous"


def test_user_hash_deterministic():
 assert fb._user_hash("ou_xyz") == fb._user_hash("ou_xyz")
 assert fb._user_hash("ou_xyz") != fb._user_hash("ou_abc")


def test_user_hash_length_16():
 assert len(fb._user_hash("ou_xyz")) ==16


def test_strip_sensitive_removes_email():
 data = {"emailAddress": "a@b.com", "name": "alice"}
 r = fb._strip_sensitive(data)
 assert r["emailAddress"] == "[REDACTED]"
 assert r["name"] == "alice"


def test_strip_sensitive_recursive_dict():
 data = {"user": {"emailAddress": "x@y.com", "ok":1}}
 r = fb._strip_sensitive(data)
 assert r["user"]["emailAddress"] == "[REDACTED]"
 assert r["user"]["ok"] ==1


def test_strip_sensitive_list():
 r = fb._strip_sensitive([{"password": "x"}, {"ok":2}])
 assert r[0]["password"] == "[REDACTED]"
 assert r[1]["ok"] ==2


def test_strip_sensitive_string_with_email():
 r = fb._strip_sensitive("contact me at test@example.com")
 assert "[REDACTED]" in r
 assert "test@example.com" not in r


def test_truncate_short():
 assert fb._truncate("hello") == "hello"


def test_truncate_long():
 long_text = "x" *200
 r = fb._truncate(long_text, n=50)
 assert len(r) ==53


def test_truncate_empty():
 assert fb._truncate("") == ""
 assert fb._truncate(None) == ""


def test_record_card_action(tmp_path, monkeypatch):
 monkeypatch.setattr(fb, "_root", lambda x: tmp_path)
 before = time.time()
 fb.record_card_action("ou_test", "approve", "approve_reservation", ["benchNo","approvalResult"], True, "")
 op_file = tmp_path / "operations" / (time.strftime("%Y-%m-%d") + ".jsonl")
 assert op_file.exists()
 lines = op_file.read_text().splitlines()
 assert len(lines) >=1
 ev = json.loads(lines[-1])
 assert ev["user_hash"] == fb._user_hash("ou_test")
 assert ev["action"] == "approve"
 assert ev["tool"] == "approve_reservation"
 assert ev["success"] is True
 assert ev["args_keys"] == ["benchNo","approvalResult"]
 assert ev["ts"] >= before


def test_record_card_action_failure(tmp_path, monkeypatch):
 monkeypatch.setattr(fb, "_root", lambda x: tmp_path)
 fb.record_card_action("ou_x", "cancel", "cancel_reservation", ["benchNo"], False, "HTTP 400: 未找到")
 op_file = tmp_path / "operations" / (time.strftime("%Y-%m-%d") + ".jsonl")
 lines = op_file.read_text().splitlines()
 last = json.loads(lines[-1])
 assert last["action"] == "cancel"
 assert last["success"] is False
 assert "未找到" in last["error"]


def test_record_feedback(tmp_path, monkeypatch):
 monkeypatch.setattr(fb, "_root", lambda x: tmp_path)
 fb.record_feedback("ou_user", "help", {"text": "请告诉我权限", "extra": "x"})
 fb_file = tmp_path / "feedback" / (time.strftime("%Y-%m-%d") + ".jsonl")
 lines = fb_file.read_text().splitlines()
 last = json.loads(lines[-1])
 assert last["kind"] == "help"
 assert last["text"] == "请告诉我权限"
 assert last["context_keys"] == ["extra", "text"]


def test_strip_email_in_feedback_payload(tmp_path, monkeypatch):
 monkeypatch.setattr(fb, "_root", lambda x: tmp_path)
 fb.record_feedback("ou_y", "bug", {"text": "邮箱 a@b.com 没收到", "tool": "reserve_bench"})
 fb_file = tmp_path / "feedback" / (time.strftime("%Y-%m-%d") + ".jsonl")
 last = json.loads(fb_file.read_text().splitlines()[-1])
 assert "a@b.com" not in last["text"]
 assert "[REDACTED]" in last["text"]


def test_anonymous_user_recorded_anonymously(tmp_path, monkeypatch):
 monkeypatch.setattr(fb, "_root", lambda x: tmp_path)
 fb.record_card_action("", "cancel", "cancel_reservation", [], True, "")
 op_file = tmp_path / "operations" / (time.strftime("%Y-%m-%d") + ".jsonl")
 last = json.loads(op_file.read_text().splitlines()[-1])
 assert last["user_hash"] == "anonymous"


def test_weekly_report_empty(tmp_path):
 r = fb.weekly_report(data_dir=str(tmp_path / "wb"))
 assert r["window_days"] ==7
 assert r["operations"]["total"] ==0
 assert r["feedback"]["total"] ==0


def test_weekly_report_counts_ops(tmp_path, monkeypatch):
 monkeypatch.setattr(fb, "_root", lambda x: tmp_path)
 fb.record_card_action("u1", "approve", "approve_reservation", ["a"], True, "")
 fb.record_card_action("u1", "cancel", "cancel_reservation", ["b"], False, "err")
 fb.record_card_action("u2", "approve", "approve_reservation", ["a"], True, "")
 r = fb.weekly_report(data_dir=str(tmp_path))
 assert r["operations"]["total"] ==3
 assert r["operations"]["success"] ==2
 assert r["operations"]["fail"] ==1
 assert r["operations"]["by_tool"]["approve_reservation"] ==2
 assert r["operations"]["by_action"]["approve"] ==2


def test_weekly_report_counts_feedback(tmp_path, monkeypatch):
 monkeypatch.setattr(fb, "_root", lambda x: tmp_path)
 fb.record_feedback("u1", "help", {"text": "?"})
 fb.record_feedback("u1", "help", {"text": "?"})
 fb.record_feedback("u1", "permission_query", {"text": "?"})
 r = fb.weekly_report(data_dir=str(tmp_path))
 assert r["feedback"]["total"] ==3
 assert r["feedback"]["by_kind"]["help"] ==2
 assert r["feedback"]["by_kind"]["permission_query"] ==1


def test_ensure_dirs_creates_three_subdirs(tmp_path, monkeypatch):
 monkeypatch.setattr(fb, "_root", lambda x: tmp_path)
 fb._ensure_dirs(tmp_path)
 assert (tmp_path / "operations").exists()
 assert (tmp_path / "feedback").exists()
 assert (tmp_path / ".archive").exists()

