"""Location-aware login for GHL iframe (email + location_id)."""
from django.contrib.auth import authenticate
from django.db.models import Q

from accounts.models import GHLAuthCredentials
from accounts.user_access import user_can_access_location
from service_app.models import User


def resolve_login_user(username: str, password: str, location_id: str | None = None):
    """
    Authenticate a user for iframe or standalone login.

    When location_id is provided, prefer the account-scoped user for that subaccount,
    or an agency user who may access that location. Falls back to default Django auth.
    """
    username = (username or "").strip()
    password = password or ""
    location_id = (location_id or "").strip()

    if not username or not password:
        return None

    if location_id:
        account = GHLAuthCredentials.objects.filter(
            location_id=location_id,
            is_active=True,
        ).first()
        if account:
            candidates = User.objects.filter(
                Q(username__iexact=username) | Q(email__iexact=username),
                is_active=True,
            ).filter(
                Q(account=account) | Q(role=User.ROLE_AGENCY)
            )
            for user in candidates:
                if user.check_password(password) and user_can_access_location(user, location_id):
                    return user

    user = authenticate(username=username, password=password)
    if user and user.is_active:
        if location_id and not user_can_access_location(user, location_id):
            return None
        return user

    email_matches = User.objects.filter(
        Q(username__iexact=username) | Q(email__iexact=username),
        is_active=True,
    )
    for candidate in email_matches:
        if candidate.check_password(password):
            if location_id and not user_can_access_location(candidate, location_id):
                continue
            return candidate

    return None
