"""ISO 3166-1 alpha-2 country code → ISO 4217 currency code for accounts.Location."""

DEFAULT_CURRENCY = "USD"

# Keys must match Location.COUNTRY_CHOICES codes.
COUNTRY_TO_CURRENCY = {
    "AE": "AED",
    "AR": "ARS",
    "AT": "EUR",
    "AU": "AUD",
    "BE": "EUR",
    "BR": "BRL",
    "CA": "CAD",
    "CH": "CHF",
    "CL": "CLP",
    "CN": "CNY",
    "CO": "COP",
    "CZ": "CZK",
    "DE": "EUR",
    "DK": "DKK",
    "ES": "EUR",
    "FI": "EUR",
    "FR": "EUR",
    "GB": "GBP",
    "HK": "HKD",
    "ID": "IDR",
    "IE": "EUR",
    "IN": "INR",
    "IT": "EUR",
    "JP": "JPY",
    "KR": "KRW",
    "KW": "KWD",
    "MX": "MXN",
    "MY": "MYR",
    "NL": "EUR",
    "NO": "NOK",
    "NZ": "NZD",
    "OM": "OMR",
    "PE": "PEN",
    "PH": "PHP",
    "PL": "PLN",
    "PT": "EUR",
    "QA": "QAR",
    "SA": "SAR",
    "SE": "SEK",
    "SG": "SGD",
    "TH": "THB",
    "TW": "TWD",
    "US": "USD",
    "VN": "VND",
    "ZA": "ZAR",
}


def currency_for_country(country_code):
    """Return ISO 4217 currency for a country code, or DEFAULT_CURRENCY if unknown."""
    if not country_code:
        return DEFAULT_CURRENCY
    return COUNTRY_TO_CURRENCY.get(str(country_code).strip().upper(), DEFAULT_CURRENCY)


def currency_for_ghl_location(location):
    """Resolve currency from an accounts.Location instance (or None → default)."""
    if location is None:
        return DEFAULT_CURRENCY
    stored = (getattr(location, "currency", None) or "").strip()
    if stored:
        return stored.upper()
    return currency_for_country(getattr(location, "country", None))
