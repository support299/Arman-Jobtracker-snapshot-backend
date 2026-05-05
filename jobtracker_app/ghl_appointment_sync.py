"""
GHL Appointment Sync Utilities
Handles syncing appointments with GoHighLevel API
"""
import requests
from datetime import timedelta
from typing import Dict, Any, Optional, Tuple

import pytz
from django.utils import timezone as django_timezone

from accounts.models import GHLAuthCredentials, Calendar, Contact
from service_app.models import Appointment, User


def get_ghl_headers(access_token: str) -> Dict[str, str]:
    """Get headers for GHL API requests"""
    return {
        'Content-Type': 'application/json',
        'Accept': 'application/json',
        'Version': '2021-04-15',
        'Authorization': f'Bearer {access_token}'
    }


def get_ghl_credentials() -> Optional[GHLAuthCredentials]:
    """Get GHL credentials from database"""
    return GHLAuthCredentials.objects.first()


def get_ghl_credentials_for_appointment(appointment: Appointment) -> Optional[GHLAuthCredentials]:
    """Resolve OAuth credentials for an appointment (location/account aware)."""
    if appointment.account_id:
        acc = GHLAuthCredentials.objects.filter(pk=appointment.account_id).first()
        if acc:
            return acc
    if appointment.location_id:
        cred = GHLAuthCredentials.objects.filter(location_id=appointment.location_id).first()
        if cred:
            return cred
    return get_ghl_credentials()


def parse_ghl_appointment_id_from_create_response(data: Any) -> Optional[str]:
    """Extract GHL appointment id from calendars/events/appointments POST response."""
    if not isinstance(data, dict):
        return None
    ghl_appointment_id = (
        data.get('appointmentId')
        or data.get('id')
        or (
            data.get('appointment', {}).get('id')
            if isinstance(data.get('appointment'), dict)
            else None
        )
    )
    if not ghl_appointment_id and 'event' in data:
        event = data.get('event', {})
        if isinstance(event, dict):
            ghl_appointment_id = event.get('id')
    return ghl_appointment_id


def _parse_ghl_error_message(response: requests.Response) -> str:
    """User-facing message from a failed GHL API response body."""
    try:
        data = response.json()
        if isinstance(data, dict):
            return (
                data.get('message')
                or data.get('error')
                or data.get('msg')
                or response.text
            )
    except ValueError:
        pass
    return (response.text or '').strip() or 'GoHighLevel request failed'


def compute_job_appointment_utc_window(job) -> Optional[Tuple[Any, Any, GHLAuthCredentials, str]]:
    """
    Resolve job scheduled_at + duration into UTC start/end and credentials/location_id.
    Mirrors slot/time handling used when posting to GHL.
    """
    from jobtracker_app.models import Job

    if not job.scheduled_at:
        return None

    location_id = None
    try:
        job_with_relations = Job.objects.select_related('submission__contact', 'account').get(id=job.id)
        if job_with_relations.submission and job_with_relations.submission.contact:
            location_id = job_with_relations.submission.contact.location_id
        if not location_id and job_with_relations.account and getattr(
            job_with_relations.account, 'location_id', None
        ):
            location_id = job_with_relations.account.location_id
        if not location_id:
            credentials_fb = GHLAuthCredentials.objects.first()
            if credentials_fb:
                location_id = credentials_fb.location_id
    except Job.DoesNotExist:
        return None

    if not location_id:
        print('❌ [JOB APPOINTMENT WINDOW] Could not resolve location_id')
        return None

    try:
        credentials = GHLAuthCredentials.objects.get(location_id=location_id)
    except GHLAuthCredentials.DoesNotExist:
        print(f'❌ [JOB APPOINTMENT WINDOW] No GHLAuthCredentials for location_id: {location_id}')
        return None
    except GHLAuthCredentials.MultipleObjectsReturned:
        credentials = GHLAuthCredentials.objects.filter(location_id=location_id).first()

    try:
        timezone_str = credentials.timezone if credentials.timezone else 'America/Chicago'
        tz = pytz.timezone(timezone_str)
    except Exception:
        tz = pytz.timezone('America/Chicago')

    try:
        job_start_time = job.scheduled_at
        if django_timezone.is_naive(job_start_time):
            job_start_time = tz.localize(job_start_time)
        else:
            naive_time = job_start_time.replace(tzinfo=None)
            job_start_time = tz.localize(naive_time)
        duration_hours = float(job.duration_hours) if job.duration_hours else 1.0
        job_end_time = job_start_time + timedelta(hours=duration_hours)
        start_time_utc = job_start_time.astimezone(pytz.UTC)
        end_time_utc = job_end_time.astimezone(pytz.UTC)
        return (start_time_utc, end_time_utc, credentials, location_id)
    except (ValueError, TypeError, Exception) as e:
        print(f'❌ [JOB APPOINTMENT WINDOW] Error converting timezone: {e}')
        return None


def format_datetime_for_ghl(dt) -> Optional[str]:
    """Format datetime to GHL API format (ISO 8601 with timezone)"""
    if not dt:
        return None
    if isinstance(dt, str):
        return dt
    # Convert to ISO format with timezone
    return dt.isoformat()


def map_appointment_status_to_ghl(status: Optional[str]) -> Optional[str]:
    """Map our appointment status to GHL status"""
    if not status:
        return None
    
    # GHL uses the same status values
    status_mapping = {
        'new': 'new',
        'confirmed': 'confirmed',
        'cancelled': 'cancelled',
        'showed': 'showed',
        'noshow': 'noshow',
        'invalid': 'invalid',
    }
    return status_mapping.get(status, status)


def get_assigned_user_ghl_id(appointment: Appointment) -> Optional[str]:
    """Get GHL user ID from assigned user"""
    if appointment.assigned_user:
        return appointment.assigned_user.ghl_user_id
    elif appointment.ghl_assigned_user_id:
        return appointment.ghl_assigned_user_id
    return None


def create_appointment_in_ghl(appointment: Appointment) -> Optional[str]:
    """
    Create appointment in GHL and return the GHL appointment ID
    
    Args:
        appointment: Appointment instance to create in GHL
        
    Returns:
        GHL appointment ID if successful, None otherwise
    """
    credentials = get_ghl_credentials_for_appointment(appointment)
    if not credentials:
        print("❌ No GHLAuthCredentials found. Cannot sync appointment to GHL.")
        return None
    
    # Skip if this is already a GHL appointment (has ghl_appointment_id that's not local)
    if appointment.ghl_appointment_id and not appointment.ghl_appointment_id.startswith('local_'):
        print(f"⚠️ Appointment {appointment.id} already has GHL ID: {appointment.ghl_appointment_id}")
        return appointment.ghl_appointment_id
    
    if not appointment.start_time or not appointment.end_time:
        print(f"⚠️ Appointment {appointment.id} missing start_time or end_time. Cannot sync to GHL.")
        return None
    
    headers = get_ghl_headers(credentials.access_token)
    url = 'https://services.leadconnectorhq.com/calendars/events/appointments'
    
    # Build payload
    payload = {
        'title': appointment.title or 'Appointment',
        'appointmentStatus': map_appointment_status_to_ghl(appointment.appointment_status),
        'startTime': format_datetime_for_ghl(appointment.start_time),
        'endTime': format_datetime_for_ghl(appointment.end_time),
        'locationId': appointment.location_id or credentials.location_id,
        'ignoreDateRange': False,
        'toNotify': False,
        'ignoreFreeSlotValidation': True,
    }
    
    # Add optional fields
    if appointment.calendar:
        payload['calendarId'] = appointment.calendar.ghl_calendar_id
    
    if appointment.ghl_contact_id:
        payload['contactId'] = appointment.ghl_contact_id
    
    if appointment.address:
        payload['address'] = appointment.address
        payload['meetingLocationType'] = 'custom'
        payload['meetingLocationId'] = 'custom_0'
        payload['overrideLocationConfig'] = True
    
    if appointment.notes:
        payload['description'] = appointment.notes
    
    # Add assigned user
    assigned_user_ghl_id = get_assigned_user_ghl_id(appointment)
    if assigned_user_ghl_id:
        payload['assignedUserId'] = assigned_user_ghl_id
    
    try:
        response = requests.post(url, json=payload, headers=headers)
        
        if response.status_code in [200, 201]:
            data = response.json()
            ghl_appointment_id = parse_ghl_appointment_id_from_create_response(data)
            
            if ghl_appointment_id:
                print(f"✅ Created appointment in GHL: {ghl_appointment_id}")
                return ghl_appointment_id
            else:
                print(f"⚠️ GHL API response missing appointment ID. Response: {response.text}")
                return None
        else:
            print(f"❌ Failed to create appointment in GHL: {response.status_code} - {response.text}")
            return None
            
    except Exception as e:
        print(f"❌ Error creating appointment in GHL: {str(e)}")
        return None


def update_appointment_in_ghl(
    appointment: Appointment, changed_fields: Optional[Dict[str, Any]] = None
) -> Tuple[bool, Optional[str]]:
    """
    Update appointment in GHL.

    Returns:
        (True, None) on success, or (False, error_message) on failure.
    """
    credentials = get_ghl_credentials_for_appointment(appointment)
    if not credentials:
        msg = "No GHL credentials found. Cannot sync appointment to GHL."
        print(f"❌ {msg}")
        return False, msg
    
    # All appointments should have a GHL appointment ID (they come from GHL webhooks)
    if not appointment.ghl_appointment_id:
        msg = f"Appointment {appointment.id} missing ghl_appointment_id. Cannot update in GHL."
        print(f"❌ {msg}")
        return False, msg
    
    # Skip if this is a local appointment (shouldn't happen in normal flow, but handle gracefully)
    if appointment.ghl_appointment_id.startswith('local_'):
        msg = (
            f"Appointment {appointment.id} has local ID. Cannot update in GHL without real GHL appointment ID."
        )
        print(f"⚠️ {msg}")
        return False, msg
    
    headers = get_ghl_headers(credentials.access_token)
    url = f'https://services.leadconnectorhq.com/calendars/events/appointments/{appointment.ghl_appointment_id}'
    
    # Build payload - only include changed fields if provided
    if changed_fields:
        payload = {}
        
        # Map our field names to GHL field names
        field_mapping = {
            'title': 'title',
            'appointment_status': 'appointmentStatus',
            'start_time': 'startTime',
            'end_time': 'endTime',
            'address': 'address',
            'notes': 'description',
            # calendar_id is now a ForeignKey, handled separately
            'ghl_contact_id': 'contactId',
            'assigned_user': 'assignedUserId',
            'ghl_assigned_user_id': 'assignedUserId',
        }
        
        for field, value in changed_fields.items():
            ghl_field = field_mapping.get(field)
            if ghl_field:
                if field == 'appointment_status':
                    payload[ghl_field] = map_appointment_status_to_ghl(value)
                elif field in ['start_time', 'end_time']:
                    payload[ghl_field] = format_datetime_for_ghl(value)
                elif field == 'assigned_user':
                    # Get GHL user ID from User object
                    if value:
                        # value is a User instance from Django ORM
                        if isinstance(value, User):
                            payload[ghl_field] = value.ghl_user_id if value.ghl_user_id else None
                        else:
                            # Fallback: try to get user by ID if value is not a User instance
                            try:
                                user = User.objects.get(id=value)
                                payload[ghl_field] = user.ghl_user_id if user.ghl_user_id else None
                            except (User.DoesNotExist, TypeError, AttributeError):
                                payload[ghl_field] = None
                    else:
                        # Clear assigned user
                        payload[ghl_field] = None
                elif field == 'ghl_assigned_user_id':
                    payload[ghl_field] = value
                else:
                    payload[ghl_field] = value
        
        # Handle calendar field separately (ForeignKey)
        if 'calendar' in changed_fields:
            calendar = changed_fields.get('calendar')
            if calendar:
                # calendar is a Calendar object
                if hasattr(calendar, 'ghl_calendar_id'):
                    payload['calendarId'] = calendar.ghl_calendar_id
                else:
                    # If it's just an ID, try to get the Calendar object
                    try:
                        from accounts.models import Calendar
                        calendar_obj = Calendar.objects.get(ghl_calendar_id=calendar)
                        payload['calendarId'] = calendar_obj.ghl_calendar_id
                    except (Calendar.DoesNotExist, TypeError, AttributeError):
                        payload['calendarId'] = calendar if isinstance(calendar, str) else None
            else:
                payload['calendarId'] = None
        
        # If address is being updated, add location config
        if 'address' in payload and payload['address']:
            payload['meetingLocationType'] = 'custom'
            payload['meetingLocationId'] = 'custom_0'
            payload['overrideLocationConfig'] = True
    else:
        # Send all fields if no changed_fields provided
        payload = {
            'title': appointment.title or 'Appointment',
            'appointmentStatus': map_appointment_status_to_ghl(appointment.appointment_status),
            'startTime': format_datetime_for_ghl(appointment.start_time),
            'endTime': format_datetime_for_ghl(appointment.end_time),
            'ignoreDateRange': False,
            'toNotify': False,
            'ignoreFreeSlotValidation': True,
        }
        
        if appointment.calendar:
            payload['calendarId'] = appointment.calendar.ghl_calendar_id
        
        if appointment.ghl_contact_id:
            payload['contactId'] = appointment.ghl_contact_id
        
        if appointment.address:
            payload['address'] = appointment.address
            payload['meetingLocationType'] = 'custom'
            payload['meetingLocationId'] = 'custom_0'
            payload['overrideLocationConfig'] = True
        
        if appointment.notes:
            payload['description'] = appointment.notes
        
        assigned_user_ghl_id = get_assigned_user_ghl_id(appointment)
        if assigned_user_ghl_id:
            payload['assignedUserId'] = assigned_user_ghl_id
    
    try:
        response = requests.put(url, json=payload, headers=headers)
        
        if response.status_code in [200, 201, 204]:
            print(f"✅ Updated appointment in GHL: {appointment.ghl_appointment_id}")
            return True, None
        err_msg = _parse_ghl_error_message(response)
        print(f"❌ Failed to update appointment in GHL: {response.status_code} - {response.text}")
        return False, err_msg
            
    except Exception as e:
        msg = str(e)
        print(f"❌ Error updating appointment in GHL: {msg}")
        return False, msg


def delete_appointment_from_ghl(appointment: Appointment) -> bool:
    """
    Delete appointment from GHL
    
    Args:
        appointment: Appointment instance to delete from GHL
        
    Returns:
        True if successful, False otherwise
    """
    credentials = get_ghl_credentials_for_appointment(appointment)
    if not credentials:
        print("❌ No GHLAuthCredentials found. Cannot sync appointment to GHL.")
        return False
    
    # Skip if this is a local appointment (not synced to GHL)
    if not appointment.ghl_appointment_id or appointment.ghl_appointment_id.startswith('local_'):
        print(f"⚠️ Appointment {appointment.id} is local, not in GHL. Skipping delete.")
        return True
    
    headers = get_ghl_headers(credentials.access_token)
    url = f'https://services.leadconnectorhq.com/calendars/events/{appointment.ghl_appointment_id}'
    
    try:
        response = requests.delete(url, headers=headers, json={})
        
        if response.status_code in [200, 204]:
            print(f"✅ Deleted appointment from GHL: {appointment.ghl_appointment_id}")
            return True
        else:
            print(f"❌ Failed to delete appointment from GHL: {response.status_code} - {response.text}")
            return False
            
    except Exception as e:
        print(f"❌ Error deleting appointment from GHL: {str(e)}")
        return False


def create_ghl_appointment_from_job(job) -> Optional[Appointment]:
    """
    Post appointment(s) to GHL for a confirmed job and persist the first created event locally
    with job link (OneToOne). Extra assignees still get GHL events; only the first is linked.
    """
    from jobtracker_app.job_appointment_utils import get_assignee_ghl_ids_without_matching_appointment

    print(f"📅 [CREATE APPOINTMENT FROM JOB] Starting for job {job.id}")

    if Appointment.objects.filter(job_id=job.id).exists():
        existing = Appointment.objects.get(job=job)
        print(
            f"ℹ️ [CREATE APPOINTMENT FROM JOB] Job {job.id} already linked to appointment "
            f"{existing.id}, skipping create"
        )
        return existing

    assigned_user_ghl_ids = get_assignee_ghl_ids_without_matching_appointment(job)
    if not assigned_user_ghl_ids:
        print(
            f"⚠️ [CREATE APPOINTMENT FROM JOB] All assignees already have matching appointment(s) "
            f"for job {job.id}, skipping GHL create"
        )
        return None

    window = compute_job_appointment_utc_window(job)
    if not window:
        return None
    start_time_utc, end_time_utc, credentials, location_id = window

    ghl_contact_id = job.ghl_contact_id
    if not ghl_contact_id and job.submission and job.submission.contact:
        ghl_contact_id = job.submission.contact.contact_id

    if not ghl_contact_id:
        print("⚠️ [CREATE APPOINTMENT FROM JOB] No GHL contact ID found for job")

    print(
        f"📍 [CREATE APPOINTMENT FROM JOB] Creating appointment for {len(assigned_user_ghl_ids)} "
        f"assignee(s): {assigned_user_ghl_ids}"
    )

    calendar = Calendar.objects.filter(
        name="Reccuring Service Calendar",
        account__location_id=location_id,
    ).first()
    calendar_id = calendar.ghl_calendar_id if calendar else None
    if calendar:
        print(f"📅 [CREATE APPOINTMENT FROM JOB] Found calendar: {calendar.name} (ID: {calendar_id})")
    else:
        print(
            f"⚠️ [CREATE APPOINTMENT FROM JOB] Calendar 'Reccuring Service Calendar' not found "
            f"for location_id: {location_id}"
        )

    print(
        f"🕐 [CREATE APPOINTMENT FROM JOB] Time conversion: {job.scheduled_at} (job) -> "
        f"{start_time_utc} (UTC start)"
    )

    start_time_str = format_datetime_for_ghl(start_time_utc)
    end_time_str = format_datetime_for_ghl(end_time_utc)

    payload = {
        "title": job.title or "Job Appointment",
        "meetingLocationType": "custom",
        "meetingLocationId": "custom_0",
        "overrideLocationConfig": True,
        "appointmentStatus": "confirmed",
        "description": job.description or job.notes or "",
        "address": job.customer_address or "Zoom",
        "ignoreDateRange": False,
        "ignoreFreeSlotValidation": True,
        "locationId": location_id,
        "startTime": start_time_str,
        "endTime": end_time_str,
    }
    if calendar_id:
        payload["calendarId"] = calendar_id
    if ghl_contact_id:
        payload["contactId"] = ghl_contact_id

    headers = get_ghl_headers(credentials.access_token)
    url = "https://services.leadconnectorhq.com/calendars/events/appointments"
    assignee_ids_to_use = assigned_user_ghl_ids if assigned_user_ghl_ids else [None]

    linked_appointment = None
    created_any = False

    try:
        for assigned_user_ghl_id in assignee_ids_to_use:
            req_payload = {**payload}
            if assigned_user_ghl_id:
                req_payload["assignedUserId"] = assigned_user_ghl_id
            print(
                f"📤 [CREATE APPOINTMENT FROM JOB] Creating appointment in GHL for job {job.id}"
                + (f" (assignee: {assigned_user_ghl_id})" if assigned_user_ghl_id else " (no assignee)")
            )
            response = requests.post(url, json=req_payload, headers=headers)

            if response.status_code not in [200, 201]:
                print(
                    f"❌ [CREATE APPOINTMENT FROM JOB] Failed to create appointment in GHL for "
                    f"assignee {assigned_user_ghl_id}: {response.status_code} - {response.text}"
                )
                continue

            data = response.json()
            print(f"✅ [CREATE APPOINTMENT FROM JOB] GHL API response: {data}")
            created_any = True
            ghl_appt_id = parse_ghl_appointment_id_from_create_response(data)
            if not ghl_appt_id:
                print(f"⚠️ [CREATE APPOINTMENT FROM JOB] Missing appointment id in response: {response.text}")
                continue

            if linked_appointment is None:
                contact_obj = None
                if ghl_contact_id:
                    try:
                        contact_obj = Contact.objects.get(contact_id=ghl_contact_id)
                    except Contact.DoesNotExist:
                        print(f"⚠️ [CREATE APPOINTMENT FROM JOB] Contact {ghl_contact_id} not found")

                assigned_user_obj = None
                if assigned_user_ghl_id:
                    try:
                        assigned_user_obj = User.objects.get(ghl_user_id=assigned_user_ghl_id)
                    except User.DoesNotExist:
                        print(f"⚠️ [CREATE APPOINTMENT FROM JOB] User {assigned_user_ghl_id} not found")

                linked_appointment = Appointment.objects.create(
                    ghl_appointment_id=ghl_appt_id,
                    account=credentials,
                    location_id=location_id,
                    title=payload.get("title"),
                    address=payload.get("address"),
                    calendar=calendar,
                    appointment_status="confirmed",
                    notes=payload.get("description"),
                    ghl_contact_id=ghl_contact_id,
                    ghl_assigned_user_id=assigned_user_ghl_id or None,
                    start_time=start_time_utc,
                    end_time=end_time_utc,
                    created_from_backend=True,
                    job=job,
                    assigned_user=assigned_user_obj,
                )
                if contact_obj:
                    linked_appointment.contact = contact_obj
                    linked_appointment.save(update_fields=["contact"])
                print(
                    f"✅ [CREATE APPOINTMENT FROM JOB] Saved local appointment {linked_appointment.id} "
                    f"for job {job.id}"
                )
            else:
                print(
                    f"ℹ️ [CREATE APPOINTMENT FROM JOB] Extra GHL appointment {ghl_appt_id} created "
                    f"(job already linked to first)"
                )

        if not created_any:
            return None
        return linked_appointment

    except Exception as e:
        print(f"❌ [CREATE APPOINTMENT FROM JOB] Error creating appointment in GHL: {str(e)}")
        return None


def sync_linked_appointment_from_job(job) -> Tuple[bool, Optional[str]]:
    """
    When a job's schedule or details change, PUT to GHL first, then update the linked Appointment row.
    Returns (success, error_message). On failure, the local appointment row is left unchanged.
    """
    appt = Appointment.objects.filter(job_id=job.id).first()
    if not appt:
        return True, None

    window = compute_job_appointment_utc_window(job)
    if window:
        start_time_utc, end_time_utc, credentials, location_id = window
    else:
        start_time_utc = appt.start_time
        end_time_utc = appt.end_time
        credentials = get_ghl_credentials_for_appointment(appt)
        location_id = appt.location_id or ""

    new_title = job.title or "Job Appointment"
    new_address = job.customer_address or "Zoom"

    changed_fields = {}
    if start_time_utc and appt.start_time != start_time_utc:
        changed_fields["start_time"] = start_time_utc
    if end_time_utc and appt.end_time != end_time_utc:
        changed_fields["end_time"] = end_time_utc
    if appt.title != new_title:
        changed_fields["title"] = new_title
    if appt.address != new_address:
        changed_fields["address"] = new_address

    if not changed_fields:
        return True, None

    if appt.ghl_appointment_id and not str(appt.ghl_appointment_id).startswith("local_"):
        ok, err = update_appointment_in_ghl(appt, changed_fields=changed_fields)
        if not ok:
            return False, err

    if start_time_utc:
        appt.start_time = start_time_utc
    if end_time_utc:
        appt.end_time = end_time_utc
    appt.title = new_title
    appt.address = new_address
    if location_id:
        appt.location_id = location_id
    if credentials:
        appt.account = credentials
    appt._skip_ghl_sync = True
    appt.save()
    return True, None
