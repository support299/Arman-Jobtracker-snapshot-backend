"""Serialize GHL account context for login responses (business timezone, not employee)."""
from accounts.timezone_utils import DEFAULT_ACCOUNT_TIMEZONE


def serialize_user_ghl_account(user):
    """
    Return public GHL account fields for the user's linked account, or None.
    Used on login so the frontend can format job/appointment times per location.
    """
    account = getattr(user, 'account', None)
    if account is None:
        return None
    tz = (getattr(account, 'timezone', None) or '').strip() or DEFAULT_ACCOUNT_TIMEZONE
    return {
        'id': account.pk,
        'location_id': account.location_id,
        'timezone': tz,
        'account_name': getattr(account, 'company_name', None) or '',
    }
