"""Serialize GHL account context for login responses (business timezone, not employee)."""
from accounts.currency import currency_for_ghl_location
from accounts.models import Location as GHLLocation
from accounts.timezone_utils import DEFAULT_ACCOUNT_TIMEZONE


def serialize_user_ghl_account(user):
    """
    Return public GHL account fields for the user's linked account, or None.
    Used on login so the frontend can format job/appointment times and currency per location.
    """
    account = getattr(user, 'account', None)
    if account is None:
        return None
    tz = (getattr(account, 'timezone', None) or '').strip() or DEFAULT_ACCOUNT_TIMEZONE
    loc_id = (getattr(account, 'location_id', None) or '').strip()
    ghl_location = GHLLocation.objects.filter(pk=loc_id).first() if loc_id else None
    return {
        'id': account.pk,
        'location_id': account.location_id,
        'timezone': tz,
        'currency': currency_for_ghl_location(ghl_location),
        'account_name': getattr(account, 'company_name', None) or '',
    }
