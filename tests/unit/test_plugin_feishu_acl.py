"""feishu_acl插件的 pre_tool_call钩子测试 — role-based权限拦截。"""
import json
import pytest
from unittest.mock import MagicMock

import ocl.permission as perm
import ocl.identity as identity


@pytest.fixture(autouse=True)
def _fresh_state(tmp_path, monkeypatch):
 f = tmp_path / "identity_map.json"
 f.write_text(json.dumps({
  "ou_alice":1, # 普通用户
  "ou_bob":2, #调度员
  "ou_carol":3, #管理员
 }, ensure_ascii=False))
 monkeypatch.setattr(identity, "_MAP_FILE", str(f))
 identity._invalidate_role_overrides()
 monkeypatch.setattr(identity, "_resolve_open_id", lambda oid: ("", ""))
 import ocl.session_map as sm
 monkeypatch.setattr(sm, "_map", {})


def _load_hook():
 import importlib
 module = importlib.import_module("hermes_plugins.feishu_acl")
 return module._on_pre_tool_call


# 普通用户 (role=1)
def test_role1_user_can_use_level1_bench_tool():
 from ocl.session_map import register
 register("feishu_ou_alice", "ou_alice")
 hook = _load_hook()
 assert hook(tool_name="list_my_reservations", session_id="feishu_ou_alice") is None


def test_role1_user_blocked_from_level2_bench_tool():
 "approve_reservation 要求 role>=2，alice 是1，应该被拦截。"
 from ocl.session_map import register
 register("feishu_ou_alice", "ou_alice")
 hook = _load_hook()
 result = hook(tool_name="approve_reservation", session_id="feishu_ou_alice")
 assert result is not None
 assert result["action"] == "block"
 assert "权限不足" in result["message"]


def test_role1_user_blocked_from_level3_vlm_tool():
 "sync_execute 要求 role=3，alice 是1，应该被拦截。"
 from ocl.session_map import register
 register("feishu_ou_alice", "ou_alice")
 hook = _load_hook()
 result = hook(tool_name="sync_execute", session_id="feishu_ou_alice")
 assert result["action"] == "block"


#调度员 (role=2)
def test_role2_user_can_use_approve_reservation():
 from ocl.session_map import register
 register("feishu_ou_bob", "ou_bob")
 hook = _load_hook()
 assert hook(tool_name="approve_reservation", session_id="feishu_ou_bob") is None


def test_role2_user_blocked_from_level3_sync():
 from ocl.session_map import register
 register("feishu_ou_bob", "ou_bob")
 hook = _load_hook()
 result = hook(tool_name="sync_execute", session_id="feishu_ou_bob")
 assert result["action"] == "block"


#管理员 (role=3)
def test_role3_admin_can_access_all_tools():
 from ocl.session_map import register
 register("feishu_ou_carol", "ou_carol")
 hook = _load_hook()
 for tool in ["approve_reservation", "sync_execute", "trigger_sync_async", "sync_status"]:
  assert hook(tool_name=tool, session_id="feishu_ou_carol") is None, f"admin should access {tool}"


#通用拦截行为
def test_hook_blocks_unknown_tool():
 from ocl.session_map import register
 register("feishu_ou_carol", "ou_carol") # 即便 admin，编造的工具名也拦
 hook = _load_hook()
 result = hook(tool_name="drop_database", session_id="feishu_ou_carol")
 assert result is not None
 assert result["action"] == "block"


# Fail-open行为
def test_hook_passes_when_no_session_id():
 "无 session_id 让 Layer2 处理，fail-open。"
 hook = _load_hook()
 assert hook(tool_name="approve_reservation", session_id="") is None


def test_hook_passes_when_user_not_in_map():
 "session找不到对应 user，让 Layer2处理，fail-open。"
 hook = _load_hook()
 assert hook(tool_name="approve_reservation", session_id="unknown_session") is None


def test_hook_passes_on_permission_check_exception(monkeypatch):
 "权限检查抛异常时 fail-open（避免插件崩溃影响业务）。"
 from ocl.session_map import register
 register("feishu_ou_alice", "ou_alice")
 monkeypatch.setattr(perm, "is_tool_permitted",
  lambda uid, tool: (_ for _ in ()).throw(RuntimeError("boom")))
 hook = _load_hook()
 assert hook(tool_name="approve_reservation", session_id="feishu_ou_alice") is None


# 注册 sanity
def test_register_function_registers_pre_tool_call_hook():
 import importlib
 mod = importlib.import_module("hermes_plugins.feishu_acl")
 mock_ctx = MagicMock()
 mod.register(mock_ctx)
 names = [c.args[0] for c in mock_ctx.register_hook.call_args_list]
 assert "pre_tool_call" in names

