"""
GHL contact custom fields required by the Job Tracker app (non-address fields).

Address-related fields (Property Sqft, street/city/state groups, etc.) are intentionally
excluded and must continue to come from the GHL snapshot / manual setup.
"""

from __future__ import annotations

import logging
from typing import TypedDict

import requests

logger = logging.getLogger(__name__)

GHL_API_VERSION = "2021-07-28"
GHL_CUSTOM_FIELDS_URL = "https://services.leadconnectorhq.com/locations/{location_id}/customFields"


class RequiredCustomField(TypedDict):
    field_name: str
    data_type: str
    field_type: str


# Exact `name` values used throughout the codebase via GHLCustomField.field_name lookups.
REQUIRED_APP_CUSTOM_FIELDS: list[RequiredCustomField] = [
    {"field_name": "Quote Link", "data_type": "TEXT", "field_type": "url"},
    {"field_name": "Quote Value", "data_type": "TEXT", "field_type": "text"},
    {"field_name": "Decline Date", "data_type": "DATE", "field_type": "date"},
    {"field_name": "Decline Notes", "data_type": "LARGE_TEXT", "field_type": "text"},
    {"field_name": "Decline Reason", "data_type": "TEXT", "field_type": "text"},
    {"field_name": "Payment Method", "data_type": "TEXT", "field_type": "text"},
    {"field_name": "Estimate Status", "data_type": "TEXT", "field_type": "text"},
    {"field_name": "Job Location", "data_type": "TEXT", "field_type": "text"},
    {"field_name": "Job Title", "data_type": "TEXT", "field_type": "text"},
    {"field_name": "Job Status", "data_type": "TEXT", "field_type": "text"},
    {"field_name": "Technician Name", "data_type": "TEXT", "field_type": "text"},
    {"field_name": "Job Completed Date", "data_type": "DATE", "field_type": "date"},
]


def _ghl_headers(access_token: str) -> dict[str, str]:
    return {
        "Accept": "application/json",
        "Authorization": f"Bearer {access_token}",
        "Version": GHL_API_VERSION,
        "Content-Type": "application/json",
    }


def _normalize_field_name(name: str | None) -> str:
    return (name or "").strip().casefold()


def create_ghl_custom_field(
    location_id: str,
    access_token: str,
    field_def: RequiredCustomField,
) -> dict:
    """
    Create a single contact custom field in GHL.

    Returns the created customField object from the API response.
    Raises requests.HTTPError on API failure.
    """
    url = GHL_CUSTOM_FIELDS_URL.format(location_id=location_id)
    payload = {
        "name": field_def["field_name"],
        "dataType": field_def["data_type"],
        "model": "contact",
    }
    response = requests.post(url, json=payload, headers=_ghl_headers(access_token), timeout=30)
    response.raise_for_status()
    data = response.json()
    custom_field = data.get("customField") or data
    field_id = custom_field.get("id")
    if not field_id:
        raise ValueError(
            f"GHL create custom field response missing id for '{field_def['field_name']}'"
        )
    return custom_field


def ensure_app_custom_fields(location_id: str, access_token: str) -> dict:
    """
    Ensure all app-required (non-address) custom fields exist in the subaccount.

    Creates any missing fields via the GHL API, then returns a summary dict.
    """
    # Local import avoids circular dependency (utils imports this module).
    from accounts.utils import fetch_location_custom_fields

    summary = {
        "created": [],
        "skipped_existing": [],
        "failed": [],
    }

    if not location_id or not access_token:
        summary["failed"].append({"field_name": "*", "error": "missing location_id or access_token"})
        return summary

    try:
        existing_fields = fetch_location_custom_fields(location_id, access_token)
    except Exception as exc:
        logger.exception(
            "Failed to fetch custom fields before ensure location_id=%s",
            location_id,
        )
        summary["failed"].append({"field_name": "*", "error": str(exc)})
        return summary

    existing_names = {
        _normalize_field_name(info.get("name"))
        for info in existing_fields.values()
        if info.get("name")
    }

    for field_def in REQUIRED_APP_CUSTOM_FIELDS:
        field_name = field_def["field_name"]
        normalized = _normalize_field_name(field_name)
        if normalized in existing_names:
            summary["skipped_existing"].append(field_name)
            continue

        try:
            created_field = create_ghl_custom_field(location_id, access_token, field_def)
            created_name = created_field.get("name") or field_name
            summary["created"].append(created_name)
            existing_names.add(_normalize_field_name(created_name))
            print(
                f"✅ [CUSTOM FIELDS ENSURE] Created '{created_name}' "
                f"(id={created_field.get('id')}) for location_id={location_id}"
            )
        except requests.HTTPError as exc:
            error_detail = exc.response.text if exc.response is not None else str(exc)
            summary["failed"].append({"field_name": field_name, "error": error_detail})
            print(
                f"❌ [CUSTOM FIELDS ENSURE] Failed to create '{field_name}' "
                f"for location_id={location_id}: {error_detail}"
            )
        except Exception as exc:
            summary["failed"].append({"field_name": field_name, "error": str(exc)})
            print(
                f"❌ [CUSTOM FIELDS ENSURE] Failed to create '{field_name}' "
                f"for location_id={location_id}: {exc}"
            )

    print(
        f"✅ [CUSTOM FIELDS ENSURE] location_id={location_id} "
        f"created={len(summary['created'])} "
        f"skipped={len(summary['skipped_existing'])} "
        f"failed={len(summary['failed'])}"
    )
    return summary
