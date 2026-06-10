"""bot/dmz_memory.py单元测试。

不依赖 hermes 真实环境；用 unittest.mock 模拟 MemoryProvider 基类。
覆盖：偏好提取、工具调用提取、敏感字段过滤、TTL过期、prefetch缓存、错误模式累计。
"""
import json
import time
import pytest
from unittest.mock import patch, MagicMock
from pathlib import Path

# mock 掉 hermes 内部的 MemoryProvider 基类导入
import sys as _sys
_fake_module = _sys.modules.get("agent.memory_provider", None)
if _fake_module is None:
 import types
 m = types.ModuleType("agent.memory_provider")
 class _StubBase:
  def __init__(self, *a, **k):
   pass
 m.MemoryProvider = _StubBase
 _sys.modules["agent"] = types.ModuleType("agent")
 _sys.modules["agent.memory_provider"] = m
 _sys.modules["agent"].memory_provider = m

from bot.dmz_memory import (
 DMZMemoryProvider,
 _strip_sensitive,
 _extract_preferences,
 _extract_tool_calls,
 _hash_user,
 _SENSITIVE_KEYS,
)


# ──基础工具函数 ──
def test_hash_user_anonymous():
 assert _hash_user("") == "anonymous"


def test_hash_user_returns_16_chars():
 h = _hash_user("ou_test_user_123")
 assert len(h) ==16
 assert h != "ou_test_user_123"  # 不暴露明文


def test_hash_user_deterministic():
 assert _hash_user("ou_alice") == _hash_user("ou_alice")
 assert _hash_user("ou_alice") != _hash_user("ou_bob")


# ──敏感字段过滤 ──
def test_strip_sensitive_removes_blacklist():
 d = {"tool": "reserve_bench", "emailAddress": "x@y.com", "taskName": "T"}
 result = _strip_sensitive(d)
 assert "emailAddress" not in result
 assert result["tool"] == "reserve_bench"
 assert result["taskName"] == "T"


def test_strip_sensitive_recursive():
 d = {"outer": {"emailAddress": "secret", "ok": "value"}, "token": "abc"}
 result = _strip_sensitive(d)
 assert "emailAddress" not in result["outer"]
 assert "token" not in result
 assert result["outer"]["ok"] == "value"


def test_sensitive_keys_includes_critical():
 must_have = {"emailAddress", "token", "access_token", "password", "MINIMAX_API_KEY"}
 for k in must_have:
  assert k in _SENSITIVE_KEYS, f"{k} should be in blacklist"


# ──偏好提取 ──
def test_extract_preferences_finds_architecture():
 prefs = _extract_preferences("查一下 1.0架构的台架")
 assert "architectures_mentioned" in prefs
 assert "1.0架构" in prefs["architectures_mentioned"]


def test_extract_preferences_finds_bench():
 prefs = _extract_preferences("TJ002 现在能用吗")
 assert "benches_mentioned" in prefs
 assert "TJ002" in prefs["benches_mentioned"]


def test_extract_preferences_finds_event():
 prefs = _extract_preferences("hotupdate_filter_navi_construction_site 有几帧")
 assert "event_names_mentioned" in prefs
 assert any("hotupdate_filter_navi" in e for e in prefs["event_names_mentioned"])


def test_extract_preferences_no_match():
 prefs = _extract_preferences("你好，今天天气怎么样")
 assert prefs == {}


# ──工具调用提取 ──
def test_extract_tool_calls_basic():
 msgs = [{"role": "assistant", "tool_calls": [{"function": {"name": "list_benches", "arguments": '{"page": 1}'}}]}]
 records = _extract_tool_calls(msgs)
 assert len(records) ==1
 assert records[0]["tool"] == "list_benches"
 assert "page" in records[0]["args_keys"]


def test_extract_tool_calls_strips_sensitive_args():
 msgs = [{"role": "assistant", "tool_calls": [{"function": {"name": "approve", "arguments": '{"emailAddress": "x@y.com", "benchNo": "TJ001"}'}}]}]
 records = _extract_tool_calls(msgs)
 assert records[0]["args_keys"] == ["benchNo"]  # emailAddress 被剥


def test_extract_tool_calls_empty():
 assert _extract_tool_calls([]) == []
 assert _extract_tool_calls(None) == []


# ──DMZMemoryProvider ──
@pytest.fixture
def tmp_hermes_home(tmp_path, monkeypatch):
 monkeypatch.setenv("HERMES_HOME", str(tmp_path))
 return tmp_path


@pytest.fixture
def provider(tmp_hermes_home):
 p = DMZMemoryProvider()
 p.initialize("session-123", user_id="ou_alice", hermes_home=str(tmp_hermes_home))
 yield p
 p.shutdown()


def test_name_is_dmz():
 p = DMZMemoryProvider()
 assert p.name == "dmz"


def test_is_available_always_true():
 assert DMZMemoryProvider().is_available() is True


def test_initialize_creates_data_dir(tmp_hermes_home):
 p = DMZMemoryProvider()
 p.initialize("s1", user_id="ou_alice", hermes_home=str(tmp_hermes_home))
 expected = tmp_hermes_home / "dmz_memory" / _hash_user("ou_alice")
 assert p._data_dir == expected
 assert p._data_dir.exists()


def test_sync_turn_extracts_preferences(provider):
 provider.sync_turn("查 1.0架构的台架", "好的，已找到", messages=[])
 snap = provider.snapshot()
 assert "1.0架构" in snap["preferences"]["architectures_mentioned"]


def test_sync_turn_persists_to_disk(provider, tmp_hermes_home):
 provider.sync_turn("TJ002 用了吗", "在用", messages=[])
 provider.shutdown()
 json_file = provider._file_path()
 assert json_file.exists()
 data = json.loads(json_file.read_text())
 assert "TJ002" in data["preferences"]["benches_mentioned"]


def test_sync_turn_strips_email_before_save(provider, tmp_hermes_home):
 msgs = [{"role": "assistant", "tool_calls": [{"function": {"name": "approve", "arguments": '{"emailAddress": "secret@x.com", "benchNo": "TJ001"}'}}]}]
 provider.sync_turn("审批", "已批准", messages=msgs)
 provider.shutdown()
 data = json.loads(provider._file_path().read_text())
 all_text = json.dumps(data, ensure_ascii=False)
 assert "secret@x.com" not in all_text
 assert "emailAddress" not in all_text


def test_prefetch_with_no_mem_returns_empty(provider):
 assert provider.prefetch("查台架") == ""


def test_prefetch_returns_relevant_prefs(provider):
 provider.sync_turn("查 1.0架构的台架 TJ002", "好的", messages=[])
 result = provider.prefetch("1.0架构的台架列表")
 assert "1.0架构" in result


def test_prefetch_caches_for_same_query(provider):
 provider.sync_turn("查 TJ002", "好的", messages=[])
 r1 = provider.prefetch("TJ002")
 r2 = provider.prefetch("TJ002")
 assert r1 == r2


def test_reload_after_shutdown(tmp_hermes_home):
 p1 = DMZMemoryProvider()
 p1.initialize("s1", user_id="ou_alice", hermes_home=str(tmp_hermes_home))
 p1.sync_turn("TJ005", "ok", messages=[])
 p1.shutdown()
 p2 = DMZMemoryProvider()
 p2.initialize("s2", user_id="ou_alice", hermes_home=str(tmp_hermes_home))
 snap = p2.snapshot()
 assert "TJ005" in snap["preferences"]["benches_mentioned"]


def test_ttl_expiry(tmp_hermes_home, monkeypatch):
 p = DMZMemoryProvider()
 p.initialize("s1", user_id="ou_alice", hermes_home=str(tmp_hermes_home))
 p.sync_turn("TJ099", "ok", messages=[])
 p.shutdown()
 # 模拟时间过去 31 天
 fake_now = [time.time() + 31 * 86400]
 monkeypatch.setattr("bot.dmz_memory.time.time", lambda: fake_now[0])
 p2 = DMZMemoryProvider()
 p2.initialize("s2", user_id="ou_alice", hermes_home=str(tmp_hermes_home))
 assert p2.snapshot() == {}  # 过期清空


def test_anonymous_user_does_not_persist(tmp_hermes_home):
 p = DMZMemoryProvider()
 p.initialize("s1", user_id="", hermes_home=str(tmp_hermes_home))
 p.sync_turn("TJ001", "ok", messages=[])
 p.shutdown()
 files = list(p._data_dir.glob("memory.json"))
 assert len(files) ==0  # 匿名用户不落盘


def test_get_tool_schemas_empty():
 p = DMZMemoryProvider()
 assert p.get_tool_schemas() == []


def test_error_patterns_aggregated(provider):
 provider.sync_turn("审批", "HTTP400 权限不足", messages=[])
 provider.sync_turn("再试审批", "HTTP400 权限不足", messages=[])
 provider.sync_turn("试一次", "HTTP400 权限不足", messages=[])
 snap = provider.snapshot()
 errs = snap["error_patterns"]
 assert any(e.get("count",0) >=3 for e in errs)


def test_recent_actions_truncated_to_20(provider):
 msgs_list = []
 for i in range(30):
  msgs_list.append([{"role": "assistant", "tool_calls": [{"function": {"name": f"tool_{i}", "arguments": "{}"}}]}])
 for msgs in msgs_list:
  provider.sync_turn("x", "y", messages=msgs)
 snap = provider.snapshot()
 assert len(snap["recent_actions"]) ==20  # _MAX_RECENT_ACTIONS


def test_clear_removes_file(provider):
 provider.sync_turn("TJ001", "ok", messages=[])
 provider.shutdown()
 assert provider._file_path().exists()
 provider.clear()
 assert not provider._file_path().exists()
 assert provider.snapshot() == {}


def test_initialize_anonymous_uses_anonymous_dir(tmp_hermes_home):
 p = DMZMemoryProvider()
 p.initialize("s1", user_id="", hermes_home=str(tmp_hermes_home))
 assert p._data_dir.name == "anonymous"


def test_system_prompt_block_contains_safety_note():
 p = DMZMemoryProvider()
 block = p.system_prompt_block()
 assert "DMZ" in block
 assert "敏感" in block or "不存" in block


def test_handle_tool_call_raises():
 p = DMZMemoryProvider()
 import pytest
 with pytest.raises(NotImplementedError):
  p.handle_tool_call("any", {})

