"""car_tools/mcp_client.py 单元测试：dispatch 到 booking_mcp_server 的 @mcp.tool()。

**新版（2026-06-18 修复递归 bug 后）：**
旧版走 hermes registry.get_entry(name).handler() → handler 内部又调 mcp_client.call
→ 无限递归。新版直接 dispatch 到 booking_mcp_server 的 @mcp.tool() Python 函数。
"""
import pytest

from car_tools import mcp_client
from car_tools.mcp_client import CarMcpClient, McpError, McpToolNotFound


# ── _resolve_tool / call 行为 ────────────────────────────────────────────

def test_resolve_tool_unknown_raises_mcp_tool_not_found():
    """未知工具名 → McpToolNotFound。"""
    client = CarMcpClient("car_booking")
    with pytest.raises(McpToolNotFound):
        client.call("nonexistent_tool", {})


def test_call_dispatches_to_booking_mcp_server(monkeypatch):
    """call() 应该把 args 展开为 kwargs 调 booking_mcp_server 的 @mcp.tool() 函数。"""
    captured = {}

    def fake_fetch(emailAddress: str = "", **kwargs):
        captured["emailAddress"] = emailAddress
        captured["kwargs"] = kwargs
        return {"items": [{"vehicleNo": "PNV000"}]}

    # 替换 _get_dispatch
    monkeypatch.setitem(mcp_client._get_dispatch(), "fetch_available_vehicles", fake_fetch)
    # 重置 email 注入（不在 caller）
    import ocl.tool_guard as _tg
    _tg.set_current_caller(_tg.CallerIdentity())
    client = CarMcpClient()
    result = client.call("fetch_available_vehicles", {"vehicleType": "DM2"})
    assert result == {"items": [{"vehicleNo": "PNV000"}]}
    # 不传 email → captured["emailAddress"] 仍应是 ""（函数默认）
    assert captured["kwargs"] == {"vehicleType": "DM2"}


def test_call_injects_email_address(monkeypatch):
    """call() 应从 CallerIdentity 注入 emailAddress。"""
    captured = {}

    def fake_fetch(**kwargs):
        captured.update(kwargs)
        return {}

    monkeypatch.setitem(mcp_client._get_dispatch(), "fetch_available_vehicles", fake_fetch)
    import ocl.tool_guard as _tg
    _tg.set_current_caller(_tg.CallerIdentity(openid="ou_x", email="x@y.com"))
    client = CarMcpClient()
    client.call("fetch_available_vehicles", {"vehicleType": "DM2"})
    assert captured["emailAddress"] == "x@y.com"
    # 还原
    _tg.set_current_caller(_tg.CallerIdentity())


def test_call_handler_raises_wraps_to_mcp_error(monkeypatch):
    """dispatch 函数抛异常 → 包成 McpError。"""
    def boom(**kwargs):
        raise ConnectionError("upstream down")

    monkeypatch.setitem(mcp_client._get_dispatch(), "fetch_x", boom)
    import ocl.tool_guard as _tg
    _tg.set_current_caller(_tg.CallerIdentity())
    client = CarMcpClient()
    with pytest.raises(McpError) as ei:
        client.call("fetch_x", {})
    assert "fetch_x" in str(ei.value)
    assert "upstream down" in str(ei.value)


# ── server_name ─────────────────────────────────────────────────────────

def test_server_name_defaults_to_settings():
    """不传 server_name 时从 settings.CAR_MCP_SERVER_NAME 取。"""
    from config.settings import settings
    client = CarMcpClient()
    assert client.server_name == settings.CAR_MCP_SERVER_NAME


def test_server_name_override():
    client = CarMcpClient("custom_server")
    assert client.server_name == "custom_server"


# ── 异常类继承 ─────────────────────────────────────────────────────────

def test_mcp_error_is_runtime_error():
    """McpError 继承 RuntimeError，便于通用 except 捕获。"""
    assert issubclass(McpError, RuntimeError)
    assert issubclass(McpToolNotFound, McpError)


# ── get_mcp_client / set_mcp_client 全局单例 ─────────────────────────────

def test_get_mcp_client_singleton():
    mcp_client._client = None
    a = mcp_client.get_mcp_client()
    b = mcp_client.get_mcp_client()
    assert a is b


def test_set_mcp_client_replaces():
    class CustomClient:
        def __init__(self):
            self.server_name = "custom"
    custom = CustomClient()
    mcp_client.set_mcp_client(custom)
    assert mcp_client.get_mcp_client() is custom
    # 还原
    mcp_client._client = None
