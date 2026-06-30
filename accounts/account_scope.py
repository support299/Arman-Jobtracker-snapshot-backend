"""
Account scoping for multi-tenant / multi-account support.

Resolves the current request's account (GHLAuthCredentials) from:
- Authenticated + location_id in request: account for that location (iframe context)
- Authenticated without location_id: request.user.account (agency users need location_id)
- Unauthenticated: location_id from query/body/header, else DEFAULT_LOCATION_ID
"""
from typing import Optional

from decouple import config

from accounts.models import GHLAuthCredentials
from accounts.user_access import resolve_account_for_user, user_can_access_location

DEFAULT_LOCATION_ID = config("DEFAULT_LOCATION_ID", default="")


def _get_explicit_location_id_from_request(request) -> Optional[str]:
    """location_id from query, body, or X-Location-Id header (no default fallback)."""
    location_id = request.query_params.get("location_id")
    if location_id:
        return location_id.strip()
    if hasattr(request, "data") and isinstance(getattr(request, "data", None), dict):
        location_id = request.data.get("location_id")
        if location_id:
            return location_id.strip()
    location_id = request.META.get("HTTP_X_LOCATION_ID")
    if location_id:
        return location_id.strip()
    return None


def _get_location_id_from_request(request) -> Optional[str]:
    """Explicit location_id or DEFAULT_LOCATION_ID (for unauthenticated public routes)."""
    explicit = _get_explicit_location_id_from_request(request)
    if explicit:
        return explicit
    default = (DEFAULT_LOCATION_ID or "").strip()
    return default or None


def get_account_from_request(request, allow_superadmin_override: bool = True) -> Optional[GHLAuthCredentials]:
    """
    Resolve the account for this request and set request.account.

    Authenticated iframe traffic should pass location_id on every request so data is
    scoped to the subaccount open in GHL. Agency users require location_id.
    """
    account = None
    user = getattr(request, "user", None)
    is_authenticated = user and getattr(user, "is_authenticated", False)

    if is_authenticated:
        explicit_location_id = _get_explicit_location_id_from_request(request)

        if allow_superadmin_override and getattr(user, "is_superuser", False):
            account_id = request.query_params.get("account_id") or (
                request.data.get("account_id")
                if hasattr(request, "data") and isinstance(getattr(request, "data", None), dict)
                else None
            )
            if account_id:
                try:
                    account = GHLAuthCredentials.objects.get(pk=account_id)
                except (GHLAuthCredentials.DoesNotExist, ValueError):
                    pass
            if account is None and explicit_location_id:
                account = GHLAuthCredentials.objects.filter(
                    location_id=explicit_location_id,
                    is_active=True,
                ).first()

        if account is None and explicit_location_id:
            if user_can_access_location(user, explicit_location_id):
                account = GHLAuthCredentials.objects.filter(
                    location_id=explicit_location_id,
                    is_active=True,
                ).first()
            else:
                return None

        if account is None:
            account = resolve_account_for_user(user, location_id=None)
    else:
        location_id = _get_location_id_from_request(request)
        if location_id:
            account = GHLAuthCredentials.objects.filter(
                location_id=location_id,
                is_active=True,
            ).first()

    if account is not None:
        request.account = account
    return account
