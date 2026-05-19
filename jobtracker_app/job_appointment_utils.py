"""
Shared logic for checking if a Job has a matching Appointment (by slot/time, calendar, location, assignee).
Uses the same manual check as slot_reserved_info: no reliance on Job.appointment relation.
"""
from datetime import timedelta

import pytz
from django.utils import timezone as django_timezone

from accounts.models import GHLAuthCredentials, Contact
from service_app.models import Appointment


def _get_job_slot_utc_and_location(job):
    """
    Resolve job's scheduled slot as UTC start/end and location_id (same logic as slot_reserved_info).
    Returns (job_start_time_utc, job_end_time_utc, location_id) or None if cannot resolve.
    """
    if not job.scheduled_at or not job.duration_hours:
        return None

    location_id = None
    credentials = None
    try:
        if job.submission and hasattr(job.submission, 'contact') and job.submission.contact:
            location_id = job.submission.contact.location_id
            credentials = GHLAuthCredentials.objects.filter(location_id=location_id).first()
        if not location_id or not credentials:
            credentials = GHLAuthCredentials.objects.first()
            if credentials:
                location_id = credentials.location_id or location_id
    except Exception:
        credentials = GHLAuthCredentials.objects.first()
        if credentials:
            location_id = credentials.location_id

    if not location_id or not credentials:
        return None

    from accounts.timezone_utils import get_pytz_timezone

    timezone_str = credentials.timezone if credentials.timezone else "America/Chicago"
    tz = get_pytz_timezone(timezone_str)

    try:
        job_start_time = job.scheduled_at
        if django_timezone.is_naive(job_start_time):
            job_start_time = tz.localize(job_start_time)
        else:
            naive_time = job_start_time.replace(tzinfo=None)
            job_start_time = tz.localize(naive_time)
        duration_hours = float(job.duration_hours)
        job_end_time = job_start_time + timedelta(hours=duration_hours)
        job_start_time_utc = job_start_time.astimezone(pytz.UTC)
        job_end_time_utc = job_end_time.astimezone(pytz.UTC)
        return (job_start_time_utc, job_end_time_utc, location_id)
    except (ValueError, TypeError, Exception):
        return None


def job_has_matching_appointment(job):
    """
    Check if any assignee of this job already has an Appointment matching the job's slot
    (same start/end time, calendar "Reccuring Service Calendar", location_id, assigned_user).
    Does not use job.appointment relation.
    """
    slot = _get_job_slot_utc_and_location(job)
    if not slot:
        return False
    job_start_utc, job_end_utc, location_id = slot

    for assignment in job.assignments.select_related('user').all():
        if not assignment.user:
            continue
        exists = Appointment.objects.filter(
            start_time=job_start_utc,
            end_time=job_end_utc,
            calendar__name="Reccuring Service Calendar",
            location_id=location_id,
            assigned_user=assignment.user,
        ).exists()
        if exists:
            return True
    return False


def get_assignee_ghl_ids_without_matching_appointment(job):
    """
    Return list of assigned user GHL IDs for which there is NO matching Appointment
    (same slot, calendar, location). Create in GHL only for these assignees.
    If slot cannot be resolved, returns all assignee GHL IDs (create for everyone).
    """
    all_ghl_ids = []
    for assignment in job.assignments.select_related('user').all():
        if assignment.user and assignment.user.ghl_user_id:
            all_ghl_ids.append(assignment.user.ghl_user_id)

    slot = _get_job_slot_utc_and_location(job)
    if not slot:
        return all_ghl_ids

    job_start_utc, job_end_utc, location_id = slot
    without = []
    for assignment in job.assignments.select_related('user').all():
        if not assignment.user or not assignment.user.ghl_user_id:
            continue
        exists = Appointment.objects.filter(
            start_time=job_start_utc,
            end_time=job_end_utc,
            calendar__name="Reccuring Service Calendar",
            location_id=location_id,
            assigned_user=assignment.user,
        ).exists()
        if not exists:
            without.append(assignment.user.ghl_user_id)
    return without


def _job_contact_dict(contact):
    """Build contact dict for API response."""
    if not contact:
        return None
    name = f"{getattr(contact, 'first_name', '') or ''} {getattr(contact, 'last_name', '') or ''}".strip()
    return {
        'id': getattr(contact, 'contact_id', None),
        'name': name or getattr(contact, 'name', None),
        'email': getattr(contact, 'email', None),
    }


def _resolve_job_contact(job):
    """
    Resolve a Contact instance for the job for appointment matching.
    Tries in order: job.contact FK, submission.contact, then Contact table by
    job.ghl_contact_id, then by job.customer_email.
    """
    contact = getattr(job, 'contact', None)
    if contact:
        return contact
    if getattr(job, 'submission_id', None):
        try:
            submission = getattr(job, 'submission', None)
            if submission and getattr(submission, 'contact', None):
                return submission.contact
        except Exception:
            pass
    ghl_id = getattr(job, 'ghl_contact_id', None)
    if ghl_id:
        contact = Contact.objects.filter(contact_id=ghl_id).first()
        if contact:
            return contact
    email = getattr(job, 'customer_email', None)
    if email:
        contact = Contact.objects.filter(email__iexact=email).first()
        if contact:
            return contact
    return None


def get_slot_reserved_info_for_job(job):
    """
    Same manual check as JobSerializer.get_slot_reserved_info: returns slot_reserved and
    appointment details if any assignee has a matching appointment (optionally filtered by
    job contact), else slot_reserved=False. Includes job_contact when available.
    Contact is resolved from job.contact, submission.contact, or Contact table by
    ghl_contact_id / customer_email.
    """
    slot = _get_job_slot_utc_and_location(job)
    if not slot:
        return None
    job_start_utc, job_end_utc, location_id = slot

    job_contact = _resolve_job_contact(job)

    base_filter = {
        'start_time': job_start_utc,
        'end_time': job_end_utc,
        'calendar__name': 'Reccuring Service Calendar',
        'location_id': location_id,
    }
    # if job_contact is not None:
    #     base_filter['contact'] = job_contact

    for assignment in job.assignments.select_related('user').all():
        if not assignment.user:
            continue
        try:
            appointment = Appointment.objects.filter(
                **base_filter,
                assigned_user=assignment.user,
            ).select_related('calendar', 'assigned_user', 'contact').first()

            appointment_debug = Appointment.objects.filter(
                **base_filter,
                assigned_user=assignment.user,
            ).select_related('calendar', 'assigned_user', 'contact')
            print("appointment_debug:", appointment_debug.count())
            for a in appointment_debug:
                print("appointment:", a.id)
                print("--------------------------------")
                print("calendar:", a.calendar.name, "contact:", a.contact.email if a.contact else None)
                print("--------------------------------")

            if appointment:
                result = {
                    'slot_reserved': True,
                    'appointment': {
                        'id': str(appointment.id),
                        'ghl_appointment_id': appointment.ghl_appointment_id,
                        'title': appointment.title,
                        'start_time': appointment.start_time.isoformat() if appointment.start_time else None,
                        'end_time': appointment.end_time.isoformat() if appointment.end_time else None,
                        'appointment_status': appointment.appointment_status,
                        'calendar_id': appointment.calendar.ghl_calendar_id if appointment.calendar else None,
                        'calendar_name': appointment.calendar.name if appointment.calendar else None,
                        'assigned_user': {
                            'id': str(appointment.assigned_user.id),
                            'name': appointment.assigned_user.get_full_name() or appointment.assigned_user.username,
                            'email': appointment.assigned_user.email,
                        } if appointment.assigned_user else None,
                        'contact': _job_contact_dict(appointment.contact),
                        'notes': appointment.notes,
                        'address': appointment.address,
                    },
                }
                if job_contact is not None:
                    result['job_contact'] = _job_contact_dict(job_contact)
                return result
        except Exception:
            continue

    out = {'slot_reserved': False, 'appointment': None}
    if job_contact is not None:
        out['job_contact'] = _job_contact_dict(job_contact)
    return out
