"""Unit tests for mock_tools/register.py — verifies the 8 test-bench reservation
tools are registered under the 'testbench' toolset, and emailAddress is never a
tool-facing parameter (it is injected server-side)."""
import pytest


@pytest.fixture(autouse=True)
def _patch_settings(monkeypatch):
    monkeypatch.setenv("FEISHU_APP_ID", "test_id")
    monkeypatch.setenv("FEISHU_APP_SECRET", "test_secret")
    monkeypatch.setenv("MINIMAX_API_KEY", "test_key")


_EXPECTED = {
    "list_architectures", "list_available_benches", "reserve_bench",
    "cancel_reservation", "approve_reservation", "list_my_reservations",
    "list_my_approvals", "return_bench",
}


def test_all_eight_tools_registered_under_testbench():
    import mock_tools.register  # noqa: F401 — side effect: registers tools
    from tools.registry import registry
    registered = set(registry.get_tool_names_for_toolset("testbench"))
    assert _EXPECTED.issubset(registered), f"Missing: {_EXPECTED - registered}"


def test_registered_tools_have_correct_toolset_and_callable_handler():
    import mock_tools.register  # noqa: F401
    from tools.registry import registry
    for name in _EXPECTED:
        entry = registry.get_entry(name)
        assert entry is not None, f"Tool '{name}' not found"
        assert entry.toolset == "testbench"
        assert callable(entry.handler)


def test_email_not_in_any_schema():
    import mock_tools.register  # noqa: F401
    from tools.registry import registry
    for name in _EXPECTED:
        entry = registry.get_entry(name)
        props = entry.schema["function"]["parameters"].get("properties", {})
        assert "emailAddress" not in props, f"{name} schema must not expose emailAddress"


def test_tool_schemas_are_valid_openai_format():
    import mock_tools.register  # noqa: F401
    from tools.registry import registry
    for name in _EXPECTED:
        schema = registry.get_entry(name).schema
        assert schema.get("type") == "function"
        fn = schema.get("function", {})
        assert "name" in fn and "description" in fn and "parameters" in fn
