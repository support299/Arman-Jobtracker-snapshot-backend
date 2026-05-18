"""
Clone services (structure only, no pricing rules) from one GHL location account to another.

Reads only from the source account; creates new rows on the destination account.
Does not copy: QuestionPricing, SubQuestionPricing, OptionPricing, ServicePackageSizeMapping.
Service.price and Package.base_price are set to zero on the copies.
"""
from __future__ import annotations

from decimal import Decimal

from django.db import transaction

from accounts.models import GHLAuthCredentials
from service_app.models import (
    Feature,
    Package,
    PackageFeature,
    Question,
    QuestionOption,
    Service,
    ServiceSettings,
    SubQuestion,
)


def copy_service_tree(old_service: Service, dest_account: GHLAuthCredentials) -> Service:
    """Clone one service and related non-pricing structure onto ``dest_account``."""
    feat_map: dict = {}
    pkg_map: dict = {}
    q_map: dict = {}
    opt_map: dict = {}

    new_svc = Service.objects.create(
        account=dest_account,
        name=old_service.name,
        description=old_service.description,
        price=Decimal("0.00"),
        hours=old_service.hours,
        is_active=old_service.is_active,
        order=old_service.order,
        created_by=None,
    )

    try:
        os_ = old_service.settings
    except ServiceSettings.DoesNotExist:
        pass
    else:
        ServiceSettings.objects.create(
            service=new_svc,
            general_disclaimer=os_.general_disclaimer,
            bid_in_person_disclaimer=os_.bid_in_person_disclaimer,
            apply_area_minimum=os_.apply_area_minimum,
            apply_house_size_minimum=os_.apply_house_size_minimum,
            apply_trip_charge_to_bid=os_.apply_trip_charge_to_bid,
            enable_dollar_minimum=os_.enable_dollar_minimum,
        )

    for f in old_service.features.all().order_by("order", "name"):
        feat_map[f.pk] = Feature.objects.create(
            service=new_svc,
            name=f.name,
            description=f.description,
            is_active=f.is_active,
            order=f.order,
        )

    for p in old_service.packages.all().order_by("order", "name"):
        pkg_map[p.pk] = Package.objects.create(
            service=new_svc,
            name=p.name,
            base_price=Decimal("0.00"),
            order=p.order,
            is_active=p.is_active,
        )

    for pf in PackageFeature.objects.filter(package__service=old_service).select_related(
        "package", "feature"
    ):
        PackageFeature.objects.create(
            package=pkg_map[pf.package_id],
            feature=feat_map[pf.feature_id],
            is_included=pf.is_included,
            order=pf.order,
        )

    old_questions = list(old_service.questions.all().order_by("order", "created_at"))
    remaining = old_questions[:]
    safety = 0
    while remaining:
        safety += 1
        if safety > len(old_questions) + 10:
            raise RuntimeError(
                f"Question dependency loop or missing refs for service {old_service.name!r}; "
                f"remaining={[str(q.pk) for q in remaining]}"
            )
        still = []
        for oq in remaining:
            if oq.parent_question_id and oq.parent_question_id not in q_map:
                still.append(oq)
                continue
            if oq.condition_option_id and oq.condition_option_id not in opt_map:
                still.append(oq)
                continue

            parent = q_map[oq.parent_question_id] if oq.parent_question_id else None
            cond_opt = opt_map[oq.condition_option_id] if oq.condition_option_id else None

            nq = Question.objects.create(
                service=new_svc,
                parent_question=parent,
                condition_answer=oq.condition_answer,
                condition_option=cond_opt,
                question_text=oq.question_text,
                question_type=oq.question_type,
                order=oq.order,
                is_active=oq.is_active,
            )
            q_map[oq.pk] = nq

            for oo in oq.options.all().order_by("order", "option_text"):
                opt_map[oo.pk] = QuestionOption.objects.create(
                    question=nq,
                    option_text=oo.option_text,
                    order=oo.order,
                    is_active=oo.is_active,
                    allow_quantity=oo.allow_quantity,
                    max_quantity=oo.max_quantity,
                )

        if len(still) == len(remaining):
            raise RuntimeError(
                f"Could not copy questions for {old_service.name!r}; "
                f"remaining={[str(q.pk) for q in still]}"
            )
        remaining = still

    for oq in old_questions:
        nq = q_map[oq.pk]
        for sq in oq.sub_questions.all().order_by("order", "sub_question_text"):
            SubQuestion.objects.create(
                parent_question=nq,
                sub_question_text=sq.sub_question_text,
                order=sq.order,
                is_active=sq.is_active,
            )

    return new_svc


def copy_all_services_between_locations(
    source_location_id: str,
    dest_location_id: str,
    *,
    use_transaction: bool = True,
) -> list[Service]:
    """
    Copy every service from the account identified by ``source_location_id`` to
    the account for ``dest_location_id``.

    Only creates rows on the destination; the source account is not modified.
    """
    source = GHLAuthCredentials.objects.get(location_id=source_location_id)
    dest = GHLAuthCredentials.objects.get(location_id=dest_location_id)
    old_services = list(Service.objects.filter(account=source).order_by("order", "name"))

    def _run() -> list[Service]:
        created: list[Service] = []
        for s in old_services:
            created.append(copy_service_tree(s, dest))
        return created

    if use_transaction:
        with transaction.atomic():
            return _run()
    return _run()
