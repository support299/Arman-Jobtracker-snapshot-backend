from django.db.models.signals import pre_save, post_save, pre_delete
from django.dispatch import receiver
from django.utils import timezone

from .models import Job
from .tasks import handle_completed_job_invoice
# Appointment signals removed - sync logic moved to AppointmentViewSet
# from service_app.models import Appointment
# from .ghl_appointment_sync import (
#     create_appointment_in_ghl,
#     update_appointment_in_ghl,
#     delete_appointment_from_ghl
# )
from accounts.models import GHLAuthCredentials, GHLCustomField, Contact
import requests


@receiver(pre_save, sender=Job)
def _store_previous_status(sender, instance, **kwargs):
    """Store previous job fields to detect changes"""
    if not instance.pk:
        instance._previous_status = None
        instance._previous_title = None
        instance._previous_customer_address = None
        instance._previous_scheduled_at = None
        instance._previous_duration_hours = None
        return

    try:
        previous = sender.objects.get(pk=instance.pk)
        instance._previous_status = previous.status
        instance._previous_title = previous.title
        instance._previous_customer_address = previous.customer_address
        instance._previous_scheduled_at = previous.scheduled_at
        instance._previous_duration_hours = previous.duration_hours
    except sender.DoesNotExist:
        instance._previous_status = None
        instance._previous_title = None
        instance._previous_customer_address = None
        instance._previous_scheduled_at = None
        instance._previous_duration_hours = None

@receiver(post_save, sender=Job)
def _create_appointment_on_confirmed(sender, instance, created, **kwargs):
    """
    Create appointment in GHL when job status becomes 'confirmed'.
    This only happens once when status changes to 'confirmed'.
    Uses manual check (same as slot_reserved_info) to see if job already has matching appointment(s).
    """
    from .job_appointment_utils import job_has_matching_appointment

    if job_has_matching_appointment(instance):
        print(f"⚠️ [APPOINTMENT] Job {instance.id} already has matching appointment(s) (manual check), skipping GHL create")
        return

    if created:
        # If job is created with 'confirmed' status directly
        if instance.status == 'confirmed':
            print(f"🆕 [APPOINTMENT] Job created with confirmed status | job_id={instance.id}")
            from .ghl_appointment_sync import create_ghl_appointment_from_job
            create_ghl_appointment_from_job(instance)
        return
    
    previous_status = getattr(instance, "_previous_status", None)
    
    # Only act when job status transitions to 'confirmed'
    if instance.status == 'confirmed' and previous_status != 'confirmed':
        print(f"✅ [APPOINTMENT] Job transitioned to CONFIRMED | job_id={instance.id} | previous={previous_status}")
        
        # Create appointment in GHL
        from .ghl_appointment_sync import create_ghl_appointment_from_job
        create_ghl_appointment_from_job(instance)


@receiver(post_save, sender=Job)
def _sync_linked_appointment_on_job_schedule_change(sender, instance, created, **kwargs):
    """Update linked Appointment + GHL when scheduled_at or duration_hours changes."""
    if created:
        return

    if getattr(instance, '_skip_linked_appointment_sync', False):
        return

    prev_sched = getattr(instance, '_previous_scheduled_at', None)
    prev_dur = getattr(instance, '_previous_duration_hours', None)
    prev_title = getattr(instance, '_previous_title', None)
    prev_addr = getattr(instance, '_previous_customer_address', None)

    if (
        prev_sched == instance.scheduled_at
        and prev_dur == instance.duration_hours
        and prev_title == instance.title
        and prev_addr == instance.customer_address
    ):
        return

    from .ghl_appointment_sync import sync_linked_appointment_from_job

    ok, err = sync_linked_appointment_from_job(instance)
    if not ok:
        print(f"⚠️ [SYNC JOB APPOINTMENT] GHL sync failed after job save: {err}")


@receiver(post_save, sender=Job)
def _trigger_invoice_on_completion(sender, instance, created, **kwargs):
    print(f"🔔 [SIGNAL] post_save triggered | job_id={instance.id} | created={created}")

    if created:
        print("🆕 Job was just created — skipping completion logic")
        return

    previous_status = getattr(instance, "_previous_status", None)
    print(f"📊 Job status check | previous={previous_status} | current={instance.status}")

    # --------------------------------------------------
    # Only act when job transitions to 'completed'
    # --------------------------------------------------
    if instance.status == 'completed' and previous_status != 'completed':
        print(f"✅ Job transitioned to COMPLETED | job_id={instance.id}")

        # --------------------------------------------------
        # Prevent duplicate processing
        # --------------------------------------------------
        if instance.completion_processed:
            print(
                f"⚠️ Completion already processed — skipping | "
                f"job_id={instance.id}"
            )
            return

        # --------------------------------------------------
        # Resolve location_id from job: account, contact, submission.contact, or lookup by customer_email
        # --------------------------------------------------
        location_id = None
        try:
            job_with_relations = (
                Job.objects
                .select_related('account', 'contact', 'submission__contact')
                .get(id=instance.id)
            )

            # 1) Job's account (GHLAuthCredentials)
            if job_with_relations.account and job_with_relations.account.location_id:
                location_id = job_with_relations.account.location_id
                print(f"📍 location_id from job.account: {location_id}")
            # 2) Job's direct contact
            elif job_with_relations.contact and job_with_relations.contact.location_id:
                location_id = job_with_relations.contact.location_id
                print(f"📍 location_id from job.contact: {location_id}")
            # 3) Submission's contact
            elif job_with_relations.submission and job_with_relations.submission.contact and job_with_relations.submission.contact.location_id:
                location_id = job_with_relations.submission.contact.location_id
                print(f"📍 location_id from job.submission.contact: {location_id}")
            # 4) Lookup Contact by job.customer_email
            elif job_with_relations.customer_email:
                contact_by_email = (
                    Contact.objects
                    .filter(email=job_with_relations.customer_email)
                    .exclude(location_id__isnull=True)
                    .exclude(location_id='')
                    .first()
                )
                if contact_by_email:
                    location_id = contact_by_email.location_id
                    print(f"📍 location_id from Contact lookup (customer_email): {location_id}")
            if not location_id:
                print("⚠️ Could not resolve location_id from job.account, contact, submission.contact, or customer_email")

        except Job.DoesNotExist:
            print("❌ Job not found while resolving location_id")

        # --------------------------------------------------
        # Decide which async task to trigger
        # --------------------------------------------------
        REQUIRED_LOCATION_ID = "b8qvo7VooP3JD3dIZU42"
        print(
            f"🔎 Evaluating routing | "
            f"location_id={location_id} | required={REQUIRED_LOCATION_ID}"
        )

        if location_id == REQUIRED_LOCATION_ID:
            print(
                f"🌐 Routing to EXTERNAL WEBHOOK | "
                f"job_id={instance.id}"
            )
            from .tasks import send_job_completion_webhook
            send_job_completion_webhook.delay(str(instance.id))
        else:
            print(
                f"🧾 Routing to INVOICE HANDLER | "
                f"job_id={instance.id}"
            )
            handle_completed_job_invoice.delay(str(instance.id))

        # --------------------------------------------------
        # Mark completion as processed
        # --------------------------------------------------
        print(
            f"🧷 Marking job as completion_processed=True | "
            f"job_id={instance.id}"
        )

        instance.completion_processed = True
        Job.objects.filter(id=instance.id).update(completion_processed=True)

    else:
        print(
            f"No action taken | "
            f"status={instance.status} | previous={previous_status}"
        )


@receiver(post_save, sender=Job)
def _update_ghl_custom_fields_on_job_change(sender, instance, created, **kwargs):
    """Update GHL contact custom fields when job status, title, or address changes"""
    
    # Skip if job was just created (no previous values to compare)
    if created:
        return
    
    # Check if any relevant fields changed
    previous_status = getattr(instance, "_previous_status", None)
    previous_title = getattr(instance, "_previous_title", None)
    previous_customer_address = getattr(instance, "_previous_customer_address", None)
    
    status_changed = previous_status != instance.status
    title_changed = previous_title != instance.title
    address_changed = previous_customer_address != instance.customer_address
    
    # Only proceed if at least one relevant field changed
    if not (status_changed or title_changed or address_changed):
        return
    
    print(f"🔄 [GHL CUSTOM FIELDS] Job fields changed | job_id={instance.id}")
    print(f"   Status: {previous_status} → {instance.status}")
    print(f"   Title: {previous_title} → {instance.title}")
    print(f"   Address: {previous_customer_address} → {instance.customer_address}")
    
    # Get GHL contact ID
    if not instance.ghl_contact_id:
        print("⚠️ [GHL CUSTOM FIELDS] No ghl_contact_id found, skipping update")
        return
    
    # Get location_id by mapping with contact using ghl_contact_id
    location_id = None
    try:
        # First, try to get location_id from contact using ghl_contact_id
        contact = Contact.objects.filter(contact_id=instance.ghl_contact_id).first()
        if contact:
            location_id = contact.location_id
            print(f"📍 [GHL CUSTOM FIELDS] Location ID from contact: {location_id}")
        else:
            # Fallback: try to get from submission contact if available
            print("⚠️ [GHL CUSTOM FIELDS] Contact not found by ghl_contact_id, trying submission...")
            try:
                job_with_relations = (
                    Job.objects
                    .select_related('submission__contact')
                    .get(id=instance.id)
                )
                
                if job_with_relations.submission and job_with_relations.submission.contact:
                    location_id = job_with_relations.submission.contact.location_id
                    print(f"📍 [GHL CUSTOM FIELDS] Location ID from submission contact: {location_id}")
                else:
                    print("⚠️ [GHL CUSTOM FIELDS] No submission/contact found for job")
                    return
            except Job.DoesNotExist:
                print("❌ [GHL CUSTOM FIELDS] Job not found while resolving location_id")
                return
    except Exception as e:
        print(f"❌ [GHL CUSTOM FIELDS] Error resolving location_id: {str(e)}")
        return
    
    if not location_id:
        print("❌ [GHL CUSTOM FIELDS] Could not resolve location_id")
        return
    
    # Find GHLAuthCredentials by location_id
    try:
        credentials = GHLAuthCredentials.objects.get(location_id=location_id)
        print(f"✅ [GHL CUSTOM FIELDS] Found credentials for location_id: {location_id}")
    except GHLAuthCredentials.DoesNotExist:
        print(f"❌ [GHL CUSTOM FIELDS] No GHLAuthCredentials found for location_id: {location_id}")
        return
    except GHLAuthCredentials.MultipleObjectsReturned:
        print(f"⚠️ [GHL CUSTOM FIELDS] Multiple credentials found for location_id: {location_id}, using first")
        credentials = GHLAuthCredentials.objects.filter(location_id=location_id).first()
    
    # Get custom field mappings for this account
    custom_fields_mapping = {}
    try:
        job_location_field = GHLCustomField.objects.get(
            account=credentials,
            field_name='Job Location',
            is_active=True
        )
        custom_fields_mapping['job_location'] = job_location_field.ghl_field_id
    except GHLCustomField.DoesNotExist:
        print("⚠️ [GHL CUSTOM FIELDS] 'Job Location' custom field not found")
    
    try:
        job_title_field = GHLCustomField.objects.get(
            account=credentials,
            field_name='Job Title',
            is_active=True
        )
        custom_fields_mapping['job_title'] = job_title_field.ghl_field_id
    except GHLCustomField.DoesNotExist:
        print("⚠️ [GHL CUSTOM FIELDS] 'Job Title' custom field not found")
    
    try:
        job_status_field = GHLCustomField.objects.get(
            account=credentials,
            field_name='Job Status',
            is_active=True
        )
        custom_fields_mapping['job_status'] = job_status_field.ghl_field_id
    except GHLCustomField.DoesNotExist:
        print("⚠️ [GHL CUSTOM FIELDS] 'Job Status' custom field not found")

    # Technician Name: used only when status is 'on_the_way' (lookup by field name + account/location)
    if status_changed and instance.status == 'on_the_way':
        try:
            technician_name_field = GHLCustomField.objects.get(
                account=credentials,
                field_name='Technician Name',
                is_active=True
            )
            custom_fields_mapping['technician_name'] = technician_name_field.ghl_field_id
        except GHLCustomField.DoesNotExist:
            print("⚠️ [GHL CUSTOM FIELDS] 'Technician Name' custom field not found")
    
    if not custom_fields_mapping:
        print("❌ [GHL CUSTOM FIELDS] No custom field mappings found, skipping update")
        return
    
    # Build custom fields payload
    custom_fields = []
    
    # Add Job Location (customer_address)
    if 'job_location' in custom_fields_mapping and instance.customer_address:
        custom_fields.append({
            "id": custom_fields_mapping['job_location'],
            "field_value": instance.customer_address
        })
        print(f"   📍 Adding Job Location: {instance.customer_address}")
    
    # Add Job Title
    if 'job_title' in custom_fields_mapping and instance.title:
        custom_fields.append({
            "id": custom_fields_mapping['job_title'],
            "field_value": instance.title
        })
        print(f"   📝 Adding Job Title: {instance.title}")
    
    # Add Job Status
    if 'job_status' in custom_fields_mapping and instance.status:
        # Map internal status to display-friendly status
        status_display = dict(Job.STATUS_CHOICES).get(instance.status, instance.status)
        custom_fields.append({
            "id": custom_fields_mapping['job_status'],
            "field_value": status_display
        })
        print(f"   📊 Adding Job Status: {status_display}")

    # Add "Job Completed Date" (6XTylwoqW6k15Dznugee) only when status is completed and job is one-time
    if status_changed and instance.status == 'completed' and instance.job_type == 'one_time':
        JOB_COMPLETED_DATE_FIELD_ID = '6XTylwoqW6k15Dznugee'
        today_str = timezone.now().strftime('%Y-%m-%d')  # ISO date for GHL date field
        custom_fields.append({
            "id": JOB_COMPLETED_DATE_FIELD_ID,
            "field_value": today_str
        })
        print(f"   📅 Adding Job Completed Date (one-time job): {today_str}")

    # Add Technician Name only when status is on_the_way (first assignee only)
    if 'technician_name' in custom_fields_mapping and instance.status == 'on_the_way':
        first_assignment = (
            instance.assignments.select_related('user').order_by('created_at').first()
        )
        if first_assignment and first_assignment.user:
            technician_display = (
                first_assignment.user.get_full_name() or first_assignment.user.username or ''
            ).strip()
            if technician_display:
                custom_fields.append({
                    "id": custom_fields_mapping['technician_name'],
                    "field_value": technician_display
                })
                print(f"   👤 Adding Technician Name: {technician_display}")
        else:
            print("   ⚠️ [GHL CUSTOM FIELDS] No assignee found for job, skipping Technician Name")
    
    if not custom_fields:
        print("⚠️ [GHL CUSTOM FIELDS] No custom fields to update")
        return
    
    # Update GHL contact with custom fields
    update_data = {
        "customFields": custom_fields
    }
    
    print(f"🔄 [GHL CUSTOM FIELDS] Updating contact {instance.ghl_contact_id} with {len(custom_fields)} custom fields")
    
    # Update GHL contact with custom fields using direct API call
    url = f'https://services.leadconnectorhq.com/contacts/{instance.ghl_contact_id}'
    headers = {
        'Authorization': f'Bearer {credentials.access_token}',
        'Content-Type': 'application/json',
        'Version': '2021-07-28',
        'Accept': 'application/json'
    }
    
    try:
        response = requests.put(url, headers=headers, json=update_data)
        if response.status_code in [200, 201]:
            print(f"✅ [GHL CUSTOM FIELDS] Successfully updated GHL contact custom fields")
        else:
            print(f"❌ [GHL CUSTOM FIELDS] Failed to update GHL contact: {response.status_code} - {response.text}")
    except Exception as e:
        print(f"❌ [GHL CUSTOM FIELDS] Error updating GHL contact: {str(e)}")


# Appointment GHL Sync Signals
# NOTE: Appointment sync signals have been removed to prevent loops.
# Sync logic is now handled directly in AppointmentViewSet.update() and destroy() methods.
# This prevents infinite loops when:
# 1. We update an appointment from our system -> syncs to GHL -> GHL sends webhook -> updates our system
# 2. We delete an appointment from our system -> syncs to GHL -> GHL sends webhook -> tries to delete from our system
#
# The webhook handlers in accounts/tasks.py handle:
# - AppointmentCreate: creates appointment in our system
# - AppointmentUpdate: updates appointment in our system
# - AppointmentDelete: deletes appointment from our system
#
# The AppointmentViewSet handles:
# - update(): updates appointment in our system and syncs to GHL (with _skip_ghl_sync flag to prevent signal loops)
# - destroy(): deletes appointment from our system and syncs to GHL (with _skip_ghl_sync flag to prevent signal loops)

