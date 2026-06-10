"""OCL权限测试：基于角色（1/2/3）的工具访问控制。"""
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


#角色映射正确性
def test_role_mapping_resolves_correctly():
 assert identity.role_of("ou_alice") ==1
 assert identity.role_of("ou_bob") ==2
 assert identity.role_of("ou_carol") ==3


def test_unknown_user_role_is_zero():
 assert identity.role_of("ou_never_seen") ==0


# TOOL_MIN_ROLE 配置正确性
def test_tool_min_role_bench_tools():
 assert perm.TOOL_MIN_ROLE["list_architectures"] ==1
 assert perm.TOOL_MIN_ROLE["reserve_bench"] ==1
 assert perm.TOOL_MIN_ROLE["approve_reservation"] ==2
 assert perm.TOOL_MIN_ROLE["list_my_approvals"] ==2


def test_tool_min_role_vlm_tools():
 # 查询类→1
 assert perm.TOOL_MIN_ROLE["list_event_names"] ==1
 assert perm.TOOL_MIN_ROLE["get_frame"] ==1
 # 下载元数据→2
 assert perm.TOOL_MIN_ROLE["download_bag_metadata"] ==2
 #同步控制→3
 assert perm.TOOL_MIN_ROLE["sync_execute"] ==3
 assert perm.TOOL_MIN_ROLE["trigger_sync_async"] ==3
 assert perm.TOOL_MIN_ROLE["sync_status"] ==3


#核心门控逻辑
def test_role1_user_can_use_level1_tools():
 for t in ["list_architectures", "reserve_bench", "list_event_names", "get_frame"]:
  assert perm.is_tool_permitted("ou_alice", t), f"role1 should access {t}"


def test_role1_user_blocked_from_level2_tools():
 for t in ["approve_reservation", "list_my_approvals", "download_bag_metadata", "frame_image_url"]:
  assert not perm.is_tool_permitted("ou_alice", t), f"role1 should NOT access {t}"


def test_role1_user_blocked_from_level3_tools():
 for t in ["sync_execute", "trigger_sync_async", "sync_status"]:
  assert not perm.is_tool_permitted("ou_alice", t), f"role1 should NOT access {t}"


def test_role2_user_can_use_level2_tools():
 for t in ["approve_reservation", "download_bag_metadata", "frame_image_url"]:
  assert perm.is_tool_permitted("ou_bob", t), f"role2 should access {t}"


def test_role2_user_blocked_from_level3_tools():
 for t in ["sync_execute", "trigger_sync_async", "sync_status"]:
  assert not perm.is_tool_permitted("ou_bob", t), f"role2 should NOT access {t}"


def test_role3_admin_can_access_all():
 for t in perm.TOOL_MIN_ROLE:
  assert perm.is_tool_permitted("ou_carol", t), f"admin should access {t}"


def test_unknown_tool_denied_for_all_users():
 for uid in ["ou_alice", "ou_bob", "ou_carol"]:
  assert not perm.is_tool_permitted(uid, "drop_database")
  assert not perm.is_tool_permitted(uid, "")


def test_no_user_set_passes_for_internal_calls():
 "内部/系统调用无 open_id 时跳过门控（hermes内部任务）。"
 assert perm.is_tool_permitted("", "list_architectures")
 assert perm.is_tool_permitted("", "approve_reservation")


def test_unknown_open_id_role_zero_denies_known_tools():
 "identity_map 中无该用户 → role=0 →拒绝所有已知工具。"
 assert not perm.is_tool_permitted("ou_never_seen", "list_architectures")
 assert not perm.is_tool_permitted("ou_never_seen", "sync_status")

