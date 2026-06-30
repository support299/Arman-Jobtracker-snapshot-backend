"""Authorization helpers for multi-account / iframe location context."""
from typing import Any, Dict, List, Optional

from django.db.models import Q, QuerySet

from accounts.models import GHLAuthCredentials
from service_app.models import User


def extract_ghl_user_metadata(user_data: Dict[str, Any]) -> Dict[str, Any]:
    """Extract agency/account metadata from a GHL user payload."""
    roles = user_data.get("roles") or {}
    user_type = (roles.get("type") or user_data.get("type") or "").strip().lower()
    location_ids: List[str] = roles.get("locationIds") or user_data.get("locationIds") or []
    if isinstance(location_ids, str):
        location_ids = [location_ids]
    company_id = (
        (user_data.get("companyId") or roles.get("companyId") or "").strip() or None
    )
    return {
        "ghl_user_type": user_type or None,
        "ghl_location_ids": [str(x) for x in location_ids if x],
        "ghl_restrict_sub_account": bool(roles.get("restrictSubAccount")),
        "ghl_company_id": company_id,
    }


def ghl_user_is_agency(user_data: Dict[str, Any]) -> bool:
    meta = extract_ghl_user_metadata(user_data)
    return (meta.get("ghl_user_type") or "").lower() == "agency"


def user_can_access_location(user: User, location_id: str) -> bool:
    """
    Return True if the user may operate in the given GHL location (subaccount).

    - Superusers: any onboarded location.
    - Agency users: onboarded locations per GHL company / locationIds restrictions.
    - Account users: only their linked account's location_id.
    """
    location_id = (location_id or "").strip()
    if not user or not getattr(user, "is_authenticated", True):
        return False
    if not location_id:
        return False
    if getattr(user, "is_superuser", False):
        return GHLAuthCredentials.objects.filter(location_id=location_id, is_active=True).exists()

    if user.is_agency_user or user.role == User.ROLE_AGENCY:
        return _agency_user_can_access_location(user, location_id)

    account = getattr(user, "account", None)
    return bool(account and account.location_id == location_id)


def _agency_user_can_access_location(user: User, location_id: str) -> bool:
    account = GHLAuthCredentials.objects.filter(
        location_id=location_id,
        is_active=True,
    ).first()
    if account is None:
        return False

    user_company = (getattr(user, "ghl_company_id", None) or "").strip()
    account_company = (getattr(account, "company_id", None) or "").strip()
    if user_company and account_company and user_company != account_company:
        return False

    if getattr(user, "ghl_restrict_sub_account", False):
        allowed = getattr(user, "ghl_location_ids", None) or []
        return location_id in allowed

    return True


def resolve_account_for_user(
    user: User,
    location_id: Optional[str] = None,
) -> Optional[GHLAuthCredentials]:
    """
    Resolve the active GHL account for login or API context.

    When location_id is provided, returns that account if the user may access it.
    Otherwise returns the user's linked account (account-scoped users only).
    """
    location_id = (location_id or "").strip()
    if location_id:
        account = GHLAuthCredentials.objects.filter(location_id=location_id, is_active=True).first()
        if account and user_can_access_location(user, location_id):
            return account
        return None

    if user.is_agency_user or user.role == User.ROLE_AGENCY:
        return None

    return getattr(user, "account", None)


def _agency_users_q_for_account(account: GHLAuthCredentials) -> Q:
    """ORM filter for agency users who may appear in this subaccount."""
    location_id = (account.location_id or "").strip()
    company_id = (getattr(account, "company_id", None) or "").strip()

    agency_base = Q(role=User.ROLE_AGENCY) | Q(ghl_user_type__iexact="agency")
    if company_id:
        agency_base &= (
            Q(ghl_company_id__isnull=True)
            | Q(ghl_company_id="")
            | Q(ghl_company_id=company_id)
        )

    unrestricted = agency_base & Q(ghl_restrict_sub_account=False)
    restricted = (
        agency_base
        & Q(ghl_restrict_sub_account=True)
        & Q(ghl_location_ids__contains=[location_id])
    )
    return unrestricted | restricted


def users_queryset_for_account(account: Optional[GHLAuthCredentials]) -> QuerySet:
    """
    Users visible in team/admin lists for a subaccount.

    Includes users bound to the account plus agency users allowed for this location.
    """
    if account is None:
        return User.objects.none()

    return User.objects.filter(
        Q(account=account) | _agency_users_q_for_account(account)
    ).distinct()


def employee_profiles_queryset_for_account(account: Optional[GHLAuthCredentials]):
    """Employee profiles for users visible in this subaccount."""
    from payroll_app.models import EmployeeProfile

    return EmployeeProfile.objects.filter(
        user__in=users_queryset_for_account(account)
    )


def get_visible_user_by_id(
    account: Optional[GHLAuthCredentials],
    user_id,
) -> Optional[User]:
    """Resolve a user pk within the current subaccount (includes agency users)."""
    if account is None or user_id is None:
        return None
    try:
        pk = int(user_id)
    except (TypeError, ValueError):
        return None
    return users_queryset_for_account(account).filter(pk=pk, is_superuser=False).first()


def time_entries_queryset_for_account(account: Optional[GHLAuthCredentials]):
    """Time clock entries scoped to a subaccount."""
    from payroll_app.models import TimeEntry

    if account is None:
        return TimeEntry.objects.none()
    return TimeEntry.objects.filter(account=account)


def payouts_queryset_for_account(account: Optional[GHLAuthCredentials]):
    """Payout records scoped to a subaccount."""
    from payroll_app.models import Payout

    if account is None:
        return Payout.objects.none()
    return Payout.objects.filter(account=account)
