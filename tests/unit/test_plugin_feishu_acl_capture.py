"""Tests for the feishu_acl post_tool_call hook (capture) + register wiring."""
import hermes_plugins.feishu_acl as acl
import ocl.tool_capture as tc


def test_post_tool_call_records_into_capture():
    tc.clear("feishu_ou_x")
    acl._on_post_tool_call(
        tool_name="fetch_user_reservation",
        args={}, result='{"code":200,"data":[]}',
        session_id="feishu_ou_x", tool_call_id="c1",
    )
    items = tc.read("feishu_ou_x")
    assert len(items) == 1
    assert items[0]["tool"] == "fetch_user_reservation"
    assert items[0]["result"]["code"] == 200


def test_post_tool_call_no_session_is_noop():
    acl._on_post_tool_call(tool_name="t", args={}, result="{}", session_id="")  # must not raise


def test_register_registers_both_hooks():
    registered = []

    class Ctx:
        def register_hook(self, name, cb):
            registered.append(name)

    acl.register(Ctx())
    assert "pre_tool_call" in registered
    assert "post_tool_call" in registered
