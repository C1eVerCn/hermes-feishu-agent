"""对 booking_mcp_server (FastMCP) 9 个 @mcp.tool() 工具的薄包装。

**关键修复（2026-06-18）：**
旧版 `call()` 走 hermes registry 拿 in-process handler → handler 内部又调
`mcp_client.call()` → 无限递归（log: maximum recursion depth exceeded）。

新版直接 dispatch 到 booking_mcp_server 的 @mcp.tool() Python 函数（不绕回
hermes registry），保留 L2 `guarded()` 包装（仍在 in-process handler 上层）。

调用路径：
  card_action_handler / FSM / handler._call_mcp
    → mcp_client.call(tool_name, args)
      → booking_mcp_server.{tool_name}(**args)  # @mcp.tool() 函数
        → _post() httpx → CAR_API_BASE_URL 上游 fmp 端点
      ← dict / list
    ← raw response
"""
import logging
from typing import Any, Optional

from config.settings import settings

log = logging.getLogger(__name__)


def _filter_to_signature(fn, args: dict) -> dict:
    """只保留 fn 显式声明的参数名（FastMCP 的 @mcp.tool() 包装后仍可 inspect）。

    若 fn 声明了 **kwargs 则不过滤（全部透传）。无法 inspect 时退化为不过滤。
    """
    try:
        import inspect
        params = inspect.signature(fn).parameters
        if any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values()):
            return args
        allowed = set(params.keys())
        return {k: v for k, v in args.items() if k in allowed}
    except (TypeError, ValueError):
        return args


class McpError(RuntimeError):
    """MCP 调用失败（非业务错误，如连接超时、工具未注册）。"""


class McpToolNotFound(McpError):
    """指定工具不在 booking_mcp_server 工具集内。"""


# tool_name → booking_mcp_server 的 @mcp.tool() 函数
# 延迟初始化（避免 import-time 副作用）
_DISPATCH: dict[str, Any] | None = None


def _get_dispatch() -> dict[str, Any]:
    global _DISPATCH
    if _DISPATCH is None:
        from car_tools import booking_mcp_server as _mcp
        _DISPATCH = {
            "fetch_available_vehicles":       _mcp.fetch_available_vehicles,
            "single_vehicle_reservation":     _mcp.single_vehicle_reservation,
            "cancel_vehicle_reservation":     _mcp.cancel_vehicle_reservation,
            "approval_vehicle_reservation":   _mcp.approval_vehicle_reservation,
            "return_vehicle":                 _mcp.return_vehicle,
            "fetch_user_reservation":         _mcp.fetch_user_reservation,
            "fetch_user_approval":            _mcp.fetch_user_approval,
            "get_user_context":               _mcp.get_user_context,
            "get_common_dictionary":          _mcp.get_common_dictionary,
        }
    return _DISPATCH


class CarMcpClient:
    """对 booking_mcp_server 9 个工具的薄包装。"""

    def __init__(self, server_name: Optional[str] = None):
        # server_name 仅用于日志/调试
        self.server_name = server_name or settings.CAR_MCP_SERVER_NAME

    def _resolve_tool(self, tool_name: str):
        """从 booking_mcp_server 取 @mcp.tool() 函数（不是 hermes registry 的 in-process handler）。"""
        dispatch = _get_dispatch()
        if tool_name not in dispatch:
            raise McpToolNotFound(f"MCP 工具 {tool_name!r} 不在 booking_mcp_server")
        return dispatch[tool_name]

    def call(self, tool_name: str, args: dict, timeout: float = 30) -> Any:
        """同步调用 booking_mcp_server 工具 → dict / list（业务侧 json.loads 解析）。"""
        fn = self._resolve_tool(tool_name)
        # 注入 emailAddress + mobile（2026-06-25 新版上游：邮箱/手机号至少一个即可鉴权；
        # booking_mcp_server 的工具签名都接受这两个 kwarg）。
        try:
            from ocl.tool_guard import get_current_caller
            caller = get_current_caller()
            if caller.email and "emailAddress" not in args:
                args = {**args, "emailAddress": caller.email}
            if caller.mobile and "mobile" not in args:
                args = {**args, "mobile": caller.mobile}
        except Exception:
            pass
        # 过滤掉目标函数签名不接受的 kwarg（上游签名变更时容错：如旧 reservationId /
        # fetch_available_vehicles 的 startTime/endTime 已被新版移除）。booking_mcp_server
        # 的工具都是显式具名参数（无 **kwargs），按签名过滤是安全的。
        args = _filter_to_signature(fn, args)
        try:
            return fn(**args)
        except Exception as e:
            log.warning("mcp_call_failed tool=%s err=%s", tool_name, e)
            raise McpError(f"{tool_name} 调用失败: {type(e).__name__}: {e}") from e


# ── 全局单例 + 测试钩子 ─────────────────────────────────────────────────────
_client: Optional[CarMcpClient] = None


def get_mcp_client() -> CarMcpClient:
    """返回全局单例。测试可通过 monkeypatch ``car_tools.mcp_client._client`` 替换。"""
    global _client
    if _client is None:
        _client = CarMcpClient()
    return _client


def set_mcp_client(client: CarMcpClient) -> None:
    """测试钩子：注入 fake client。"""
    global _client
    _client = client
