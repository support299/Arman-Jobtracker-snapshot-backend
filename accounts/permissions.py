"""
DRF permissions for account-scoped access.

Use AccountScopedPermission on views that must operate only on the current account.
It resolves request.account (from user.account or location_id for unauthenticated)
and denies access when account cannot be resolved.
"""
from rest_framework import permissions

from accounts.account_scope import get_account_from_request


class AccountScopedPermission(permissions.BasePermission):
    """
    Resolve and set request.account; deny if not possible.

    - Authenticated: request.account = request.user.account (or override for superuser).
    - Unauthenticated: request.account from location_id (query, body, or X-Location-Id header).

    Add to permission_classes for any view that must be scoped to a single account.
    """

    message = "Account context is required. Provide location_id (query/body/header) or authenticate with an account."

    def has_permission(self, request, view):
        account = get_account_from_request(request, allow_superadmin_override=True)
        if account is None:
            return False
        return True


class IsAdminPermission(permissions.BasePermission):
    """Allow only authenticated admin users (service_app.User.is_admin)."""

    def has_permission(self, request, view):
        user = getattr(request, "user", None)
        return bool(
            user
            and user.is_authenticated
            and getattr(user, "is_admin", False)
        )
