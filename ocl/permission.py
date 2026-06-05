"""Tool ACL for the test-bench reservation bot.

Permissions are enforced server-side by the real RESTful API (by emailAddress),
so the local gate only verifies the tool is a known/registered one. Roles are no
longer checked here; the handler's identity gate (resolvable Feishu email) is what
distinguishes platform users from outsiders.
"""
import logging

from ocl import identity

log = logging.getLogger(__name__)

# Registered tools. Membership = the tool is allowed to dispatch; the real API
# decides whether THIS user may perform it. TOOL_MIN_ROLE is kept as the registry
# of known tools (values are advisory only, retained for reference/overrides).
TOOL_MIN_ROLE: dict[str, int] = {
    "list_architectures":     1,
    "list_available_benches": 1,
    "reserve_bench":          1,
    "cancel_reservation":     1,
    "return_bench":           1,
    "list_my_reservations":   1,
    "approve_reservation":    2,
    "list_my_approvals":      2,
}


def is_tool_permitted(open_id: str, tool_name: str) -> bool:
    """Allow any known tool; deny unknown tools. Real API enforces per-user rights."""
    return tool_name in TOOL_MIN_ROLE
