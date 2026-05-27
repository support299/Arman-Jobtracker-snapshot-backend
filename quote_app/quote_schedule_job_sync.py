"""
Build / upsert Job + JobServiceItem rows from a CustomerSubmission + QuoteSchedule.
Used by QuoteSchedule signal and reschedule-from-job flow.
"""
from __future__ import annotations

from decimal import Decimal

from django.db import transaction

from service_app.models import User

from quote_app.helpers import (
    get_global_minimum_base_price_for_submission,
    update_ghl_quote_value_for_submission,
)
from .models import CustomService, CustomerServiceSelection, CustomerPackageQuote


def _quantize_currency(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.01"))


def resolve_user_from_reference(reference: str):
    if not reference:
        return None
    ref = reference.strip()
    if not ref:
        return None

    lookup_filters = [{"email__iexact": ref}, {"username__iexact": ref}]
    for filters in lookup_filters:
        try:
            return User.objects.filter(**filters).first()
        except Exception:
            continue
    return None


def compute_job_defaults_and_items(submission, quote_schedule):
    """
    Returns (job_defaults dict, job_items list, quoted_by_user).
    job_defaults does not include submission, status, or account.
    """
    contact = submission.contact
    address = submission.address

    customer_name = ""
    customer_email = None
    customer_phone = None
    ghl_contact_id = None

    if contact:
        customer_name = f"{contact.first_name or ''} {contact.last_name or ''}".strip()
        customer_email = getattr(contact, "email", None)
        customer_phone = getattr(contact, "phone", None)
        ghl_contact_id = getattr(contact, "contact_id", None)

    customer_address = address.get_full_address() if address else None

    selected_services = CustomerServiceSelection.objects.filter(
        submission=submission,
        selected_package__isnull=False,
    ).select_related("service", "selected_package")

    job_items = []
    total_price = Decimal("0.00")
    total_duration = Decimal("0.00")
    total_surcharge_from_quotes = Decimal("0.00")
    trip_charge_enabled = False
    default_item_duration = Decimal("0.50")

    for service_selection in selected_services:
        selected_quote = CustomerPackageQuote.objects.filter(
            service_selection=service_selection,
            is_selected=True,
        ).order_by("-created_at").first()

        if not selected_quote and service_selection.selected_package:
            selected_quote = CustomerPackageQuote.objects.filter(
                service_selection=service_selection,
                package=service_selection.selected_package,
            ).order_by("-created_at").first()

        if not selected_quote:
            continue

        if hasattr(service_selection.service, "settings"):
            try:
                trip_charge_enabled = trip_charge_enabled or bool(
                    service_selection.service.settings.apply_trip_charge_to_bid
                )
            except Exception:
                pass

        price = _quantize_currency(Decimal(selected_quote.total_price))
        total_surcharge_from_quotes += _quantize_currency(Decimal(selected_quote.surcharge_amount or 0))
        job_items.append(
            {
                "service": service_selection.service,
                "custom_name": None,
                "price": price,
                "duration_hours": default_item_duration,
            }
        )
        total_price += price
        total_duration += default_item_duration

    custom_services = CustomService.objects.filter(purchase=submission, is_active=True)
    for custom_service in custom_services:
        price = _quantize_currency(Decimal(custom_service.price))
        job_items.append(
            {
                "service": None,
                "custom_name": custom_service.product_name,
                "price": price,
                "duration_hours": default_item_duration,
            }
        )
        total_price += price
        total_duration += default_item_duration

    try:
        minimum_total = _quantize_currency(get_global_minimum_base_price_for_submission(submission))
        if minimum_total > Decimal("0.00") and total_price < minimum_total:
            adjustment_amount = minimum_total - total_price
            if adjustment_amount > Decimal("0.00"):
                job_items.append(
                    {
                        "service": None,
                        "custom_name": "Adjustments",
                        "price": _quantize_currency(adjustment_amount),
                        "duration_hours": Decimal("0.00"),
                    }
                )
                total_price = minimum_total
    except Exception:
        pass

    surcharge_amount = _quantize_currency(total_surcharge_from_quotes)
    if surcharge_amount <= Decimal("0.00"):
        surcharge_amount = Decimal(submission.total_surcharges or 0).quantize(Decimal("0.01"))
    if (
        surcharge_amount <= Decimal("0.00")
        and trip_charge_enabled
        and submission.location
        and submission.location.trip_surcharge
    ):
        surcharge_amount = _quantize_currency(Decimal(submission.location.trip_surcharge))
    total_price = _quantize_currency(total_price + surcharge_amount)
    total_duration = total_duration.quantize(Decimal("0.01"))

    quoted_by_user = submission.quoted_by
    created_by_email = None
    if quoted_by_user:
        created_by_email = getattr(quoted_by_user, "email", None)
    else:
        quoted_by_user = resolve_user_from_reference(quote_schedule.quoted_by)
    if quoted_by_user:
        created_by_email = getattr(quoted_by_user, "email", None)
    elif quote_schedule.quoted_by and "@" in quote_schedule.quoted_by:
        created_by_email = quote_schedule.quoted_by

    job_defaults = {
        "title": customer_name or "Accepted Quote",
        "description": "Quote accepted and converted to job.",
        "priority": "medium",
        "duration_hours": total_duration,
        "scheduled_at": quote_schedule.scheduled_date,
        "total_price": total_price,
        "total_surcharge": surcharge_amount,
        "customer_name": customer_name or None,
        "customer_phone": customer_phone,
        "customer_email": customer_email,
        "customer_address": customer_address,
        "ghl_contact_id": ghl_contact_id,
        "notes": quote_schedule.notes,
        "created_by_email": created_by_email,
    }
    if contact:
        job_defaults["contact"] = contact
    if address:
        job_defaults["address"] = address

    return job_defaults, job_items, quoted_by_user


def upsert_job_items(job, job_items):
    from jobtracker_app.models import JobServiceItem

    job.items.all().delete()
    items_to_create = [
        JobServiceItem(
            job=job,
            service=item["service"],
            custom_name=item["custom_name"],
            price=item["price"],
            duration_hours=item["duration_hours"],
        )
        for item in job_items
    ]
    if items_to_create:
        JobServiceItem.objects.bulk_create(items_to_create)


def create_reschedule_pending_job(submission, quote_schedule):
    """
    After cloning submission (accepted) + QuoteSchedule, create Job(status=reschedule_pending).
    """
    from jobtracker_app.models import Job

    job_defaults, job_items, quoted_by_user = compute_job_defaults_and_items(submission, quote_schedule)
    job_defaults["description"] = "Reschedule pending — awaiting staff confirmation."

    with transaction.atomic():
        job = Job.objects.create(
            submission=submission,
            **job_defaults,
            status="reschedule_pending",
            account=getattr(submission, "account", None),
            **({"quoted_by": quoted_by_user} if quoted_by_user else {}),
        )
        upsert_job_items(job, job_items)
    return job


def sync_job_when_quote_schedule_submitted(submission, quote_schedule):
    """
    When QuoteSchedule.is_submitted is True: create or update Job to to_convert (prefers existing
    reschedule_pending or to_convert row for this submission).
    Ensures submission.status is accepted.
    """
    from jobtracker_app.models import Job

    job_defaults, job_items, quoted_by_user = compute_job_defaults_and_items(submission, quote_schedule)
    job_defaults["description"] = "Quote accepted and converted to job."

    with transaction.atomic():
        existing_job = (
            Job.objects.filter(submission=submission, status="reschedule_pending").first()
            or Job.objects.filter(submission=submission, status="to_convert").first()
        )

        if existing_job:
            job = existing_job
            for attr, value in job_defaults.items():
                setattr(job, attr, value)
            if quoted_by_user:
                job.quoted_by = quoted_by_user
            job.status = "to_convert"
            if getattr(submission, "account_id", None) and not job.account_id:
                job.account_id = submission.account_id
            job.save()
        else:
            job = Job.objects.create(
                submission=submission,
                **job_defaults,
                status="to_convert",
                account=getattr(submission, "account", None),
                **({"quoted_by": quoted_by_user} if quoted_by_user else {}),
            )

        upsert_job_items(job, job_items)

        if submission.status != "accepted":
            submission.status = "accepted"
            submission.save(update_fields=["status"])

    try:
        update_ghl_quote_value_for_submission(submission)
    except Exception as exc:
        print(f'⚠️ [QUOTE VALUE] Failed to sync on quote schedule submit: {exc}')

    return job
