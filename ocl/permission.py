"""Role-based tool ACL for the test-bench reservation bot.
Each tool declares a minimum role; a user's role comes from ocl.identity.
Coarse gate only — fine-grained rules (same-group, status) live in the mock API.
"""
import logging

from ocl import identity

log = logging.getLogger(__name__)

# tool → minimum role required (1 普通, 2 调度员)
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
    min_role = TOOL_MIN_ROLE.get(tool_name)
    if min_role is None:
        return False  # unknown tool — deny
    role = identity.role_of(open_id)
    return role >= min_role
