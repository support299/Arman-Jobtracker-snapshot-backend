"""Shared account/location timezone helpers (GHLAuthCredentials.timezone)."""
import pytz

DEFAULT_ACCOUNT_TIMEZONE = 'America/Chicago'


def get_pytz_timezone(tz_name=None, default=DEFAULT_ACCOUNT_TIMEZONE):
    name = ((tz_name or default) or default).strip() or default
    try:
        return pytz.timezone(name)
    except Exception:
        return pytz.timezone(default)


def get_pytz_for_account(account):
    tz_name = getattr(account, 'timezone', None) if account else None
    return get_pytz_timezone(tz_name)


def get_pytz_for_request(request):
    return get_pytz_for_account(getattr(request, 'account', None))
