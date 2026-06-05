"""Tool ACL: real API enforces per-user permissions; the local gate only allows
known tools and denies unknown ones. Roles are no longer checked locally."""
import ocl.permission as perm


def test_known_tools_permitted_for_any_user():
    for tool in ("list_architectures", "list_available_benches", "reserve_bench",
                 "cancel_reservation", "return_bench", "list_my_reservations",
                 "approve_reservation", "list_my_approvals"):
        assert perm.is_tool_permitted("ou_anyone", tool), tool


def test_unknown_tool_denied():
    assert not perm.is_tool_permitted("ou_admin", "drop_database")
    assert not perm.is_tool_permitted("ou_admin", "")


def test_permission_does_not_depend_on_role():
    # even an unknown open_id may dispatch known tools — the API gates by email
    assert perm.is_tool_permitted("ou_never_seen", "approve_reservation")
