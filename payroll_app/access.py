from service_app.models import User


def payroll_has_admin_access(user):
    """
    Payroll-only admin scope.

    Managers still count as admins elsewhere in the product, but payroll treats
    them like workers. Supervisors and superusers keep payroll admin access.
    """
    if not getattr(user, "is_authenticated", False):
        return False
    if getattr(user, "is_superuser", False):
        return True
    return getattr(user, "role", None) == User.ROLE_SUPERVISOR


def payroll_can_view_team_data(user):
    """Whether the user can read team-wide payroll data."""
    if not payroll_has_admin_access(user):
        return False
    return getattr(user, "payroll_can_view_team_data", True)


def payroll_can_view_employees(user):
    """
    Whether the user can list/read all employee profiles in payroll.

    Managers are view-only elsewhere in payroll but may browse the full
    employee directory (with query filters). Supervisors/superusers use
    payroll admin rules.
    """
    if not getattr(user, "is_authenticated", False):
        return False
    if getattr(user, "is_superuser", False):
        return True
    role = getattr(user, "role", None)
    if role == User.ROLE_MANAGER:
        return True
    return payroll_has_admin_access(user)
