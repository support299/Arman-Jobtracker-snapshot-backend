"""Create/update GHLAuthCredentials with duplicate location_id handling."""
from __future__ import annotations

import logging
from typing import Any, Dict, Tuple

from django.apps import apps
from django.db import models, transaction

from accounts.models import GHLAuthCredentials

logger = logging.getLogger(__name__)


def _reassign_ghl_credentials_fks(from_account: GHLAuthCredentials, to_account: GHLAuthCredentials) -> None:
    """Point related rows from a duplicate credentials row to the canonical row."""
    if from_account.pk == to_account.pk:
        return
    for model in apps.get_models():
        for field in model._meta.get_fields():
            if not isinstance(field, models.ForeignKey):
                continue
            if field.related_model is not GHLAuthCredentials:
                continue
            model.objects.filter(**{field.name: from_account}).update(**{field.name: to_account})


def _dedupe_credentials_for_location(location_id: str) -> GHLAuthCredentials | None:
    """
    If multiple rows share location_id, keep the best one and remove extras.

    Prefers active rows, then most recently updated.
    """
    rows = list(
        GHLAuthCredentials.objects.filter(location_id=location_id)
        .order_by("-is_active", "-updated_at", "-id")
    )
    if len(rows) <= 1:
        return rows[0] if rows else None

    canonical = rows[0]
    for duplicate in rows[1:]:
        _reassign_ghl_credentials_fks(duplicate, canonical)
        logger.warning(
            "Removing duplicate GHLAuthCredentials pk=%s location_id=%s (keeping pk=%s)",
            duplicate.pk,
            location_id,
            canonical.pk,
        )
        duplicate.delete()
    return canonical


def upsert_ghl_credentials(token_data: Dict[str, Any]) -> Tuple[GHLAuthCredentials, bool]:
    """
    Create or update credentials for a GHL location.

    Safe when duplicate rows exist for the same location_id (e.g. after uninstall/reinstall).
    """
    location_id = (token_data.get("locationId") or "").strip()
    if not location_id:
        raise ValueError("OAuth token payload missing locationId")

    user_id = (token_data.get("userId") or "").strip()
    if not user_id:
        raise ValueError(f"OAuth token payload missing userId for location_id={location_id}")

    defaults = {
        "access_token": token_data.get("access_token") or "",
        "refresh_token": token_data.get("refresh_token") or "",
        "expires_in": token_data.get("expires_in") or 0,
        "scope": token_data.get("scope") or "",
        "user_type": token_data.get("userType") or "",
        "company_id": token_data.get("companyId") or "",
        "user_id": user_id,
        "is_active": True,
    }

    with transaction.atomic():
        existing = _dedupe_credentials_for_location(location_id)
        if existing:
            for key, value in defaults.items():
                setattr(existing, key, value)
            existing.save()
            return existing, False

        obj = GHLAuthCredentials.objects.create(location_id=location_id, **defaults)
        return obj, True
