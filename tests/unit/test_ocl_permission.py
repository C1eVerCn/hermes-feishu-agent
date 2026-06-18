"""OCL权限测试：基于角色（1/2/3）的工具访问控制（车辆预约域）。"""
import json
import pytest

import ocl.identity as identity
import ocl.permission as perm


@pytest.fixture(autouse=True)
def _fresh(tmp_path, monkeypatch):
 f = tmp_path / "identity_map.json"
 f.write_text(json.dumps({
  "ou_alice":1, # 普通用户
  "ou_bob":2, #调度员
  "ou_carol":3, #管理员
  }, ensure_ascii=False))
 monkeypatch.setattr(identity, "_MAP_FILE", str(f))
 identity._invalidate_role_overrides()
 monkeypatch.setattr(identity, "_resolve_open_id", lambda oid: ("", ""))
 yield


def test_role_mapping_resolves_correctly():
 assert identity.role_of("ou_alice") ==1
 assert identity.role_of("ou_bob") ==2
 assert identity.role_of("ou_carol") ==3


def test_unknown_user_role_is_zero():
 assert identity.role_of("ou_never_seen") ==0


# TOOL_MIN_ROLE 配置正确性（车辆预约域）
def test_tool_min_role_query_tools():
 assert perm.TOOL_MIN_ROLE["fetch_available_vehicles"] ==1
 assert perm.TOOL_MIN_ROLE["fetch_user_reservation"] ==1


def test_tool_min_role_booking_tools():
 assert perm.TOOL_MIN_ROLE["single_vehicle_reservation"] ==1  # 业务下单（commit） — 实际 LLM 不可见，仅 card_action_handler 调用
 assert perm.TOOL_MIN_ROLE["_commit_vehicle_reservation"] ==1
 assert perm.TOOL_MIN_ROLE["cancel_vehicle_reservation"] ==1
 assert perm.TOOL_MIN_ROLE["return_vehicle"] ==1


def test_tool_min_role_approval_tools_role2():
 assert perm.TOOL_MIN_ROLE["approval_vehicle_reservation"] ==2
 assert perm.TOOL_MIN_ROLE["fetch_user_approval"] ==2


def test_tool_min_role_assistants():
 assert perm.TOOL_MIN_ROLE["get_user_context"] ==1
 assert perm.TOOL_MIN_ROLE["get_common_dictionary"] ==1


# 核心门控逻辑
def test_role1_user_can_use_level1_tools():
 for t in ["fetch_available_vehicles", "single_vehicle_reservation",
          "cancel_vehicle_reservation", "return_vehicle",
          "fetch_user_reservation", "get_user_context",
          "get_common_dictionary"]:
  assert perm.is_tool_permitted("ou_alice", t), f"role1 should access {t}"


def test_role1_user_blocked_from_level2_tools():
 """role=1 普通用户不能审批（min_role=2）。"""
 for t in ["approval_vehicle_reservation", "fetch_user_approval"]:
  assert not perm.is_tool_permitted("ou_alice", t), f"role1 should NOT access {t}"


def test_role2_user_can_use_level1_and_level2_tools():
 for t in ["fetch_available_vehicles", "approval_vehicle_reservation",
          "fetch_user_approval"]:
  assert perm.is_tool_permitted("ou_bob", t), f"role2 should access {t}"


def test_role3_admin_can_access_all():
 for t in perm.TOOL_MIN_ROLE:
  assert perm.is_tool_permitted("ou_carol", t), f"admin should access {t}"


def test_unknown_tool_denied_for_all_users():
 for uid in ["ou_alice", "ou_bob", "ou_carol"]:
  assert not perm.is_tool_permitted(uid, "drop_database")
  assert not perm.is_tool_permitted(uid, "")


def test_no_user_set_passes_for_internal_calls():
 "内部/系统调用无 open_id 时跳过门控。"
 assert perm.is_tool_permitted("", "fetch_available_vehicles")
 assert perm.is_tool_permitted("", "_commit_vehicle_reservation")


def test_unknown_open_id_role_zero_denies_known_tools():
 "identity_map 中无该用户 → role=0 → 拒绝所有已知工具。"
 assert not perm.is_tool_permitted("ou_never_seen", "fetch_available_vehicles")
 assert not perm.is_tool_permitted("ou_never_seen", "approval_vehicle_reservation")
