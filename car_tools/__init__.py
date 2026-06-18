"""车辆预约业务包。

架构（对齐参考项目 reservation_agent-test-agent_multigraph）：

  car_tools/booking_mcp_server.py   FastMCP stdio MCP server
    9 个 @mcp.tool() → httpx POST → 上游 fmp 后端
    ↕ stdio JSON-RPC
  hermes-agent（spawn 这个 server 作子进程）
    ↕ tools.registry.get_entry
  car_tools/mcp_client.py::call
    ↕
  car_tools/handlers.py
    _inject_caller (从 CallerIdentity 读 openid/email 注入)
    ↕
  car_tools/normalizers.py (raw → Pydantic strict)
    ↕
  LLM tool result

LLM-facing 工具由 car_tools/register.py 包成 hermes registry 内
``toolset="car"`` 工具集（用于 enabled_toolsets 过滤 + schema 控制，
如 emailAddress 字段从 LLM 视角移除）。

身份注入（CLAUDE.md 不变量）：
- LLM 永远看不到 emailAddress / openId / mobile
- car_tools/handlers._inject_caller 在每次 call 前从 contextvars 读 caller
"""

CAR_TOOLSET = "car"
