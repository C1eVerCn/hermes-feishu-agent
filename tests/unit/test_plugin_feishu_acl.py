"""Unit tests for hermes_plugins/feishu_acl/ — the pre_tool_call hook callback
against the role-based permission model."""
import json
import pytest
from unittest.mock import MagicMock

import ocl.permission as perm
import ocl.identity as identity


@pytest.fixture(autouse=True)
def _fresh_state(tmp_path, monkeypatch):
    f = tmp_path / "identity_map.json"
    f.write_text(json.dumps({
        # ou_alice → role 1: may use list/reserve tools, not approve
        "ou_alice": 1,
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


def test_hook_allows_permitted_tool_for_role1_user():
    from ocl.session_map import register
    register("feishu_ou_alice", "ou_alice")
    hook = _load_hook()
    assert hook(tool_name="list_my_reservations", session_id="feishu_ou_alice") is None


def test_hook_blocks_approve_for_role1_user():
    from ocl.session_map import register
    register("feishu_ou_alice", "ou_alice")
    hook = _load_hook()
    result = hook(tool_name="approve_reservation", session_id="feishu_ou_alice")
    assert result is not None
    assert result["action"] == "block"
    assert "权限不足" in result["message"]


def test_hook_passes_when_no_session_id():
    hook = _load_hook()
    assert hook(tool_name="approve_reservation", session_id="") is None


def test_hook_passes_when_user_not_in_map():
    hook = _load_hook()
    assert hook(tool_name="approve_reservation", session_id="unknown_session") is None


def test_hook_passes_on_permission_check_exception(monkeypatch):
    from ocl.session_map import register
    register("feishu_ou_alice", "ou_alice")
    monkeypatch.setattr(perm, "is_tool_permitted",
                        lambda uid, tool: (_ for _ in ()).throw(RuntimeError("boom")))
    hook = _load_hook()
    assert hook(tool_name="approve_reservation", session_id="feishu_ou_alice") is None


def test_register_function_registers_pre_tool_call_hook():
    import importlib
    mod = importlib.import_module("hermes_plugins.feishu_acl")
    mock_ctx = MagicMock()
    mod.register(mock_ctx)
    names = [c.args[0] for c in mock_ctx.register_hook.call_args_list]
    assert "pre_tool_call" in names
