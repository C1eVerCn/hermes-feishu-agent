"""OCL 权限测试：按角色（与 fmp 后端对齐 1~5）的工具访问控制（车辆预约域）。

2026-06-26：从线性 TOOL_MIN_ROLE(role>=min) 改为显式 ROLE_TOOLS（镜像 fmp sys_role_menu）。
fmp 5 角色非线性：司机(4)权限比工程师(1)还少、组管理员(5)≈调度员而非管理员。
"""
import json
import pytest

import ocl.identity as identity
import ocl.permission as perm


@pytest.fixture(autouse=True)
def _fresh(tmp_path, monkeypatch):
 f = tmp_path / "identity_map.json"
 f.write_text(json.dumps({
  "ou_eng":1,    # 工程师
  "ou_disp":2,   # 调度员
  "ou_admin":3,  # 管理员
  "ou_driver":4, # 司机
  "ou_gm":5,     # 组管理员
  }, ensure_ascii=False))
 monkeypatch.setattr(identity, "_MAP_FILE", str(f))
 identity._invalidate_role_overrides()
 monkeypatch.setattr(identity, "_resolve_open_id", lambda oid: ("", ""))
 yield


def test_role_mapping_resolves_correctly():
 assert identity.role_of("ou_eng") == 1
 assert identity.role_of("ou_disp") == 2
 assert identity.role_of("ou_admin") == 3
 assert identity.role_of("ou_driver") == 4
 assert identity.role_of("ou_gm") == 5


def test_unknown_user_role_is_zero():
 assert identity.role_of("ou_never_seen") == 0


# ── ROLE_TOOLS 结构正确性 ──────────────────────────────────────────────────
def test_all_tools_keys_are_registry_names():
 # 键 == car_tools/register.py 注册给 LLM 的 10 个工具名；后端 MCP 名不应混入
 assert "single_vehicle_reservation" not in perm.ALL_TOOLS
 assert {"fetch_available_vehicles", "_dry_run_vehicle_reservation",
         "_commit_vehicle_reservation", "approval_vehicle_reservation"} <= perm.ALL_TOOLS
 assert len(perm.ALL_TOOLS) == 10


def test_engineer_can_book_not_approve():
 assert perm.role_allows(1, "fetch_available_vehicles")
 assert perm.role_allows(1, "_dry_run_vehicle_reservation")
 assert perm.role_allows(1, "cancel_vehicle_reservation")
 assert perm.role_allows(1, "return_vehicle")
 assert not perm.role_allows(1, "approval_vehicle_reservation")
 assert not perm.role_allows(1, "fetch_user_approval")


def test_dispatcher_can_approve():
 assert perm.role_allows(2, "approval_vehicle_reservation")
 assert perm.role_allows(2, "fetch_user_approval")
 assert perm.role_allows(2, "fetch_available_vehicles")


def test_admin_can_use_everything():
 for t in perm.ALL_TOOLS:
  assert perm.role_allows(3, t), f"admin should access {t}"


def test_driver_only_helpers_no_booking_no_approval():
 """司机(4)：fmp 仅出车监控 → bot 只给助手工具，不可约车/审批。"""
 assert perm.role_allows(4, "get_user_context")
 assert perm.role_allows(4, "get_common_dictionary")
 assert not perm.role_allows(4, "fetch_available_vehicles")
 assert not perm.role_allows(4, "_dry_run_vehicle_reservation")
 assert not perm.role_allows(4, "approval_vehicle_reservation")


def test_group_manager_equals_dispatcher():
 """组管理员(5) ≈ 调度员：可约车 + 审批（但不是管理员）。"""
 assert perm.role_allows(5, "fetch_available_vehicles")
 assert perm.role_allows(5, "_dry_run_vehicle_reservation")
 assert perm.role_allows(5, "approval_vehicle_reservation")
 assert perm.role_allows(5, "fetch_user_approval")


def test_non_linear_no_overgrant():
 """关键回归：司机(4)不得因 4>=2 拿到审批；任何角色不得越权。"""
 assert not perm.role_allows(4, "approval_vehicle_reservation")  # 4>=2 但禁止
 assert not perm.role_allows(0, "fetch_available_vehicles")      # 非平台用户全禁


# ── is_tool_permitted（按 open_id）门控 ───────────────────────────────────
def test_engineer_blocked_from_approval_via_openid():
 assert perm.is_tool_permitted("ou_eng", "fetch_available_vehicles")
 assert not perm.is_tool_permitted("ou_eng", "approval_vehicle_reservation")


def test_dispatcher_can_approve_via_openid():
 assert perm.is_tool_permitted("ou_disp", "approval_vehicle_reservation")


def test_driver_via_openid_blocked_from_booking():
 assert not perm.is_tool_permitted("ou_driver", "fetch_available_vehicles")
 assert perm.is_tool_permitted("ou_driver", "get_user_context")


def test_unknown_tool_denied_for_all_users():
 for uid in ["ou_eng", "ou_disp", "ou_admin", "ou_driver", "ou_gm"]:
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
