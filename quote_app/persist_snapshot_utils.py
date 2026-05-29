"""
Deep-clone a CustomerSubmission as an immutable persisted snapshot of the original proposal.
"""
from __future__ import annotations

from django.db import transaction
from django.utils import timezone

from .models import (
    CustomerSubmission,
    CustomerServiceSelection,
    CustomerPackageQuote,
    CustomerQuestionResponse,
    CustomerOptionResponse,
    CustomerSubQuestionResponse,
    CustomService,
    QuoteSchedule,
    CustomerSubmissionImage,
)


def _clone_submission_graph(source: CustomerSubmission, *, new_submission_kwargs: dict) -> CustomerSubmission:
    """Copy the full submission graph into a new CustomerSubmission."""
    new_sub = CustomerSubmission.objects.create(**new_submission_kwargs)

    sel_map: dict = {}

    for old_sel in CustomerServiceSelection.objects.filter(submission=source).select_related(
        "service", "selected_package"
    ):
        new_sel = CustomerServiceSelection.objects.create(
            submission=new_sub,
            service=old_sel.service,
            selected_package=old_sel.selected_package,
            question_adjustments=old_sel.question_adjustments,
            surcharge_applicable=old_sel.surcharge_applicable,
            surcharge_amount=old_sel.surcharge_amount,
            final_base_price=old_sel.final_base_price,
            final_sqft_price=old_sel.final_sqft_price,
            final_total_price=old_sel.final_total_price,
        )
        sel_map[old_sel.pk] = new_sel

    for old_pkg in CustomerPackageQuote.objects.filter(service_selection__submission=source).select_related(
        "service_selection", "package"
    ):
        new_sel = sel_map.get(old_pkg.service_selection_id)
        if not new_sel:
            continue
        CustomerPackageQuote.objects.create(
            service_selection=new_sel,
            package=old_pkg.package,
            base_price=old_pkg.base_price,
            sqft_price=old_pkg.sqft_price,
            question_adjustments=old_pkg.question_adjustments,
            surcharge_amount=old_pkg.surcharge_amount,
            total_price=old_pkg.total_price,
            included_features=list(old_pkg.included_features or []),
            excluded_features=list(old_pkg.excluded_features or []),
            is_selected=old_pkg.is_selected,
        )

    qr_map: dict = {}

    for old_qr in CustomerQuestionResponse.objects.filter(service_selection__submission=source).select_related(
        "service_selection", "question"
    ):
        new_sel = sel_map.get(old_qr.service_selection_id)
        if not new_sel:
            continue
        new_qr = CustomerQuestionResponse.objects.create(
            service_selection=new_sel,
            question=old_qr.question,
            yes_no_answer=old_qr.yes_no_answer,
            text_answer=old_qr.text_answer,
            price_adjustment=old_qr.price_adjustment,
        )
        qr_map[old_qr.pk] = new_qr

    for old_opt in CustomerOptionResponse.objects.filter(
        question_response__service_selection__submission=source
    ).select_related("question_response", "option"):
        new_qr = qr_map.get(old_opt.question_response_id)
        if not new_qr:
            continue
        CustomerOptionResponse.objects.create(
            question_response=new_qr,
            option=old_opt.option,
            quantity=old_opt.quantity,
            price_adjustment=old_opt.price_adjustment,
        )

    for old_sq in CustomerSubQuestionResponse.objects.filter(
        question_response__service_selection__submission=source
    ).select_related("question_response", "sub_question"):
        new_qr = qr_map.get(old_sq.question_response_id)
        if not new_qr:
            continue
        CustomerSubQuestionResponse.objects.create(
            question_response=new_qr,
            sub_question=old_sq.sub_question,
            answer=old_sq.answer,
            price_adjustment=old_sq.price_adjustment,
        )

    for old_cs in CustomService.objects.filter(purchase=source):
        CustomService.objects.create(
            purchase=new_sub,
            product_name=old_cs.product_name,
            description=old_cs.description,
            is_active=old_cs.is_active,
            price=old_cs.price,
        )

    for old_img in CustomerSubmissionImage.objects.filter(submission=source):
        CustomerSubmissionImage.objects.create(
            submission=new_sub,
            image=old_img.image,
            caption=old_img.caption,
            uploaded_by=old_img.uploaded_by,
            ghl_file_id=old_img.ghl_file_id,
            ghl_file_url=old_img.ghl_file_url,
        )

    try:
        old_schedule = source.quote_schedule
    except QuoteSchedule.DoesNotExist:
        old_schedule = None

    if old_schedule:
        QuoteSchedule.objects.create(
            submission=new_sub,
            first_time=old_schedule.first_time,
            quoted_by=old_schedule.quoted_by,
            scheduled_date=old_schedule.scheduled_date,
            is_submitted=old_schedule.is_submitted,
            notes=old_schedule.notes,
            appointment_id=old_schedule.appointment_id,
        )

    return new_sub


def clone_submission_as_persisted_snapshot(source: CustomerSubmission) -> tuple[CustomerSubmission, bool]:
    """
    Create (or return existing) immutable snapshot linked to the working submission.
    Returns (snapshot, created) where created is True when a new snapshot was made.
    """
    if source.is_persisted_snapshot:
        raise ValueError("Cannot create a snapshot of a snapshot.")

    try:
        return source.persisted_snapshot, False
    except CustomerSubmission.DoesNotExist:
        pass

    extra = dict(source.additional_data or {})
    extra["snapshot_created_at"] = timezone.now().isoformat()
    extra["snapshot_source_submission_id"] = str(source.pk)

    with transaction.atomic():
        snapshot = _clone_submission_graph(
            source,
            new_submission_kwargs={
                "account": source.account,
                "contact": source.contact,
                "address": source.address,
                "house_sqft": source.house_sqft,
                "location": source.location,
                "status": source.status,
                "quoted_by": source.quoted_by,
                "total_base_price": source.total_base_price,
                "total_adjustments": source.total_adjustments,
                "total_surcharges": source.total_surcharges,
                "quote_surcharge_applicable": source.quote_surcharge_applicable,
                "custom_service_total": source.custom_service_total,
                "final_total": source.final_total,
                "additional_data": extra,
                "expires_at": source.expires_at,
                "is_persisted_snapshot": True,
                "source_submission": source,
            },
        )

    return snapshot, True
