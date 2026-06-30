
import requests
from celery import shared_task
from accounts.ghl_credentials import upsert_ghl_credentials
from accounts.models import GHLAuthCredentials, Calendar, GHLCompanyAuth
from decouple import config
from concurrent.futures import ThreadPoolExecutor, as_completed
from collections import defaultdict
from accounts.utils import (
    fetch_all_contacts,
    create_or_update_contact,
    delete_contact,
    create_or_update_user_from_ghl,
    create_or_update_appointment_from_ghl,
    delete_appointment_from_ghl_webhook,
    sync_calendars_from_ghl as sync_calendars_from_ghl_utils
)
from datetime import datetime, timedelta
from django.utils import timezone as django_timezone
from django.utils.dateparse import parse_datetime
from service_app.models import Appointment, User, User
import pytz
import logging

logger = logging.getLogger(__name__)

GHL_OAUTH_TOKEN_URL = "https://services.leadconnectorhq.com/oauth/token"
GHL_LOCATION_TOKEN_URL = "https://services.leadconnectorhq.com/oauth/locationToken"
GHL_API_VERSION = "2021-07-28"


def _fetch_location_token(agency_access_token, company_id, location_id):
    """
    Exchange agency (company) access token for a location-level token via GHL API.
    Returns parsed JSON on success, or None on failure.
    """
    response = requests.post(
        GHL_LOCATION_TOKEN_URL,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
            "Version": GHL_API_VERSION,
            "Authorization": f"Bearer {agency_access_token}",
        },
        data={
            "companyId": company_id,
            "locationId": location_id,
        },
        timeout=30,
    )
    response.raise_for_status()
    return response.json()


def _upsert_location_credentials(token_data):
    """Persist location OAuth tokens from /oauth/locationToken response."""
    upsert_ghl_credentials(token_data)


def _refresh_location_tokens_for_company(company_auth):
    """
    After agency token refresh, fetch fresh location tokens for every onboarded
    subaccount (GHLAuthCredentials) belonging to this company.
    """
    company_id = company_auth.company_id
    agency_access_token = (company_auth.access_token or "").strip()
    if not agency_access_token:
        return {"success": 0, "errors": 0, "skipped": 0}

    location_rows = GHLAuthCredentials.objects.filter(company_id=company_id).exclude(
        location_id__isnull=True
    ).exclude(location_id="")

    loc_success = 0
    loc_errors = 0
    loc_skipped = 0

    for cred in location_rows:
        location_id = (cred.location_id or "").strip()
        if not location_id:
            loc_skipped += 1
            continue
        try:
            token_data = _fetch_location_token(agency_access_token, company_id, location_id)
            _upsert_location_credentials(token_data)
            loc_success += 1
            logger.info(
                "refresh_agency_tokens: refreshed location token company_id=%s location_id=%s",
                company_id,
                location_id,
            )
        except Exception as exc:
            loc_errors += 1
            logger.exception(
                "refresh_agency_tokens: location token failed company_id=%s location_id=%s: %s",
                company_id,
                location_id,
                exc,
            )

    return {"success": loc_success, "errors": loc_errors, "skipped": loc_skipped}


@shared_task
def refresh_agency_tokens():
    """
    Refresh company-level (agency) GHL OAuth tokens stored in GHLCompanyAuth.
    """
    company_auth_rows = GHLCompanyAuth.objects.all()
    logger.info("refresh_agency_tokens: processing %s company auth row(s)", company_auth_rows.count())

    success_count = 0
    error_count = 0

    for company_auth in company_auth_rows:
        try:
            refresh_token = (company_auth.refresh_token or "").strip()
            if not refresh_token:
                logger.warning(
                    "refresh_agency_tokens: missing refresh token for company_id=%s",
                    company_auth.company_id,
                )
                error_count += 1
                continue

            response = requests.post(
                GHL_OAUTH_TOKEN_URL,
                data={
                    "grant_type": "refresh_token",
                    "client_id": config("GHL_CLIENT_ID"),
                    "client_secret": config("GHL_CLIENT_SECRET"),
                    "refresh_token": refresh_token,
                },
                timeout=30,
            )
            response.raise_for_status()
            resp_data = response.json()

            access_token = (resp_data.get("access_token") or "").strip()
            if not access_token:
                logger.warning(
                    "refresh_agency_tokens: no access_token returned for company_id=%s body=%s",
                    company_auth.company_id,
                    resp_data,
                )
                error_count += 1
                continue

            company_auth.access_token = access_token
            company_auth.refresh_token = (resp_data.get("refresh_token") or company_auth.refresh_token or "").strip()
            expires_in = resp_data.get("expires_in")
            if isinstance(expires_in, int):
                company_auth.expires_in = expires_in
            company_auth.scope = resp_data.get("scope") or company_auth.scope or ""
            company_auth.user_id = resp_data.get("userId") or company_auth.user_id or ""
            company_auth.save(
                update_fields=[
                    "access_token",
                    "refresh_token",
                    "expires_in",
                    "scope",
                    "user_id",
                    "updated_at",
                ]
            )
            success_count += 1
            logger.info("refresh_agency_tokens: refreshed company_id=%s", company_auth.company_id)

            loc_stats = _refresh_location_tokens_for_company(company_auth)
            logger.info(
                "refresh_agency_tokens: location tokens company_id=%s success=%s errors=%s skipped=%s",
                company_auth.company_id,
                loc_stats["success"],
                loc_stats["errors"],
                loc_stats["skipped"],
            )
        except Exception as exc:
            error_count += 1
            logger.exception(
                "refresh_agency_tokens: failed for company_id=%s: %s",
                company_auth.company_id,
                exc,
            )

    logger.info(
        "refresh_agency_tokens: done success=%s errors=%s",
        success_count,
        error_count,
    )
    return {"success": success_count, "errors": error_count}



@shared_task
def fetch_all_contacts_task(location_id, access_token):
    """
    Celery task to fetch all contacts for a given location using the provided access token.
    """
    fetch_all_contacts(location_id, access_token)


def _get_account_from_webhook_data(data):
    """Resolve GHLAuthCredentials (location account) from webhook payload."""
    location_id = data.get("locationId") or data.get("location_id")
    if not location_id and "contact" in data:
        location_id = data["contact"].get("locationId")
    if not location_id and "appointment" in data:
        location_id = data["appointment"].get("locationId")
    if not location_id and "user" in data:
        location_id = data.get("locationId")
    if location_id:
        return GHLAuthCredentials.objects.filter(location_id=location_id).first()
    return None


@shared_task
def handle_webhook_event(data, event_type):
    try:
        # Resolve location account so create/update can set it on models
        account = _get_account_from_webhook_data(data)

        if event_type in ["ContactCreate", "ContactUpdate"]:
            create_or_update_contact(data)
        elif event_type == "ContactDelete":
            delete_contact(data)
        elif event_type == "UserCreate":
            create_or_update_user_from_ghl(data, account=account)
        elif event_type in ["AppointmentCreate", "AppointmentUpdate"]:
            location_id = data.get("locationId") or (data.get("appointment") or {}).get("locationId")
            appointment = create_or_update_appointment_from_ghl(data, location_id=location_id, account=account)
            if event_type == "AppointmentUpdate":
                update_quote_schedule_from_appointment(data, appointment)
        elif event_type == "AppointmentDelete":
            from accounts.utils import delete_appointment_from_ghl_webhook
            delete_appointment_from_ghl_webhook(data)
    except Exception as e:
        print(f"Error handling webhook event: {str(e)}")


def update_quote_schedule_from_appointment(webhook_data: dict, appointment=None):
    """
    Update QuoteSchedule when appointment is updated in GHL.
    
    Args:
        webhook_data (dict): Webhook payload data
        appointment (Appointment, optional): The appointment object if already created/updated
    """
    try:
        # Extract appointment ID from webhook data (handle nested structure)
        appointment_data = webhook_data.get("appointment", webhook_data)
        
        # Try multiple ways to get appointment ID
        ghl_appointment_id = (
            appointment_data.get("id") or 
            appointment_data.get("appointmentId") or
            webhook_data.get("calendar", {}).get("appointmentId")
        )
        
        # If we have the appointment object, use its ghl_appointment_id
        if not ghl_appointment_id and appointment:
            ghl_appointment_id = appointment.ghl_appointment_id
        
        if not ghl_appointment_id:
            print("⚠️ [QUOTE SCHEDULE] No appointment ID found in webhook data")
            return
        
        # Find QuoteSchedule by appointment_id
        from quote_app.models import QuoteSchedule
        
        quote_schedule = QuoteSchedule.objects.filter(appointment_id=ghl_appointment_id).first()
        
        if not quote_schedule:
            print(f"ℹ️ [QUOTE SCHEDULE] No QuoteSchedule found for appointment_id: {ghl_appointment_id}")
            return
        
        print(f"🔄 [QUOTE SCHEDULE] Updating QuoteSchedule for appointment_id: {ghl_appointment_id}")
        
        # Track if any fields were updated
        updated_fields = []
        
        # Update scheduled_date from startTime (convert to location timezone)
        start_time = appointment_data.get("startTime")
        if start_time:
            # Get location_id to find timezone (get it first before parsing)
            location_id = (
                webhook_data.get("locationId") or 
                appointment_data.get("locationId") or
                (appointment.location_id if appointment else None)
            )
            
            # Parse the datetime - handle UTC timezone from 'Z' suffix
            scheduled_date = parse_datetime(start_time)
            if scheduled_date:
                # If the string ends with 'Z', it's UTC - ensure we treat it as UTC
                if start_time.endswith('Z') or start_time.endswith('z'):
                    # parse_datetime might return naive, so explicitly localize to UTC
                    if django_timezone.is_naive(scheduled_date):
                        scheduled_date = pytz.UTC.localize(scheduled_date)
                    else:
                        # If already aware, ensure it's UTC
                        scheduled_date = scheduled_date.astimezone(pytz.UTC)
                
                # Convert to location timezone if location_id is available
                if location_id:
                    try:
                        # Get credentials for this location to get timezone
                        credentials = GHLAuthCredentials.objects.filter(location_id=location_id).first()
                        if credentials and credentials.timezone:
                            timezone_str = credentials.timezone
                            tz = pytz.timezone(timezone_str)
                            
                            # Convert UTC datetime to location timezone
                            # This will give us the local time (e.g., 12:00 PM CST if input was 18:00 UTC)
                            scheduled_date = scheduled_date.astimezone(tz)
                            
                            print(f"   🌍 Converted UTC to location timezone ({timezone_str}): {scheduled_date}")
                            print(f"   📅 Local time: {scheduled_date.strftime('%Y-%m-%d %I:%M %p %Z')}")
                        else:
                            print(f"   ⚠️ No timezone found for location_id {location_id}, keeping as UTC")
                    except Exception as e:
                        print(f"   ⚠️ Error converting timezone: {str(e)}, using UTC datetime")
                else:
                    print(f"   ⚠️ No location_id found, keeping as UTC")
                
                quote_schedule.scheduled_date = scheduled_date
                updated_fields.append("scheduled_date")
                print(f"   📅 Updated scheduled_date: {scheduled_date}")
        
        # Update notes if provided
        notes = appointment_data.get("notes")
        if notes is not None:
            quote_schedule.notes = notes
            updated_fields.append("notes")
            print(f"   📝 Updated notes: {notes}")
        
        # Update is_submitted if appointment status indicates it's confirmed/submitted
        appointment_status = appointment_data.get("appointmentStatus")
        if appointment_status:
            # You can customize this logic based on your business rules
            # For example, mark as submitted if status is "confirmed" or "showed"
            if appointment_status in ["confirmed", "showed"]:
                if not quote_schedule.is_submitted:
                    quote_schedule.is_submitted = True
                    updated_fields.append("is_submitted")
                    print(f"   ✅ Updated is_submitted: True (status: {appointment_status})")
        
        # Only save if there were updates
        if updated_fields:
            quote_schedule.save(update_fields=updated_fields)
            print(f"✅ [QUOTE SCHEDULE] Successfully updated QuoteSchedule for submission {quote_schedule.submission.id} (fields: {', '.join(updated_fields)})")
        else:
            print(f"ℹ️ [QUOTE SCHEDULE] No fields to update for QuoteSchedule {quote_schedule.id}")
        
    except Exception as e:
        print(f"❌ [QUOTE SCHEDULE] Error updating QuoteSchedule from appointment webhook: {str(e)}")
        import traceback
        traceback.print_exc()


@shared_task
def fetch_and_save_all_appointments(location_id=None):
    """
    Fetch all appointments from GHL API for all users (using their ghl_user_id)
    for the last 1 year to 2 years in the future and save them to the Appointment table.
    
    Args:
        location_id (str, optional): Location ID. If not provided, will use from credentials.
    
    Returns:
        dict: Summary with counts of created and updated appointments
    """
    from django.db import transaction
    from accounts.models import Contact
    
    try:
        # Get credentials
        credentials = GHLAuthCredentials.objects.first()
        if not credentials:
            raise ValueError("No GHLAuthCredentials found in database")
        
        # Use location_id from credentials if not provided
        if not location_id:
            location_id = credentials.location_id
        
        if not location_id:
            raise ValueError("location_id is required")
        
        access_token = credentials.access_token
        
        # Fetch all users with ghl_user_id
        users = User.objects.filter(ghl_user_id__isnull=False).exclude(ghl_user_id='')
        for user in users:
            print(f"User: {user.ghl_user_id}, {user.first_name} {user.last_name}")
        user_ghl_ids = list(users.values_list('ghl_user_id', flat=True))
        
        if not user_ghl_ids:
            print("No users with ghl_user_id found. Skipping appointment fetch.")
            return {
                "created": 0,
                "updated": 0,
                "total": 0
            }
        
        print(f"Found {len(user_ghl_ids)} users with ghl_user_id. Fetching appointments for each...")
        
        # Calculate time range: 1 year ago to 2 years in the future
        now = django_timezone.now()
        start_time = now - timedelta(days=365)  # 1 year ago
        end_time = now + timedelta(days=2000)    # 2 years in the future
        
        # Convert to milliseconds timestamp
        start_time_ms = int(start_time.timestamp() * 1000)
        end_time_ms = int(end_time.timestamp() * 1000)
        
        print(f"Fetching appointments from {start_time} to {end_time}")
        print(f"Timestamps: {start_time_ms} to {end_time_ms}")
        
        # API endpoint
        url = "https://services.leadconnectorhq.com/calendars/events"
        
        headers = {
            "Accept": "application/json",
            "Version": "2021-04-15",
            "Authorization": f"Bearer {access_token}"
        }
        
        # Collect all events from all users using parallel requests
        all_events = []
        
        def fetch_user_appointments(user_ghl_id):
            """Fetch appointments for a single user"""
            params = {
                "locationId": location_id,
                "userId": user_ghl_id,
                "startTime": start_time_ms,
                "endTime": end_time_ms
            }
            try:
                response = requests.get(url, headers=headers, params=params, timeout=30)
                if response.status_code != 200:
                    print(f"Error Response for user {user_ghl_id}: {response.status_code}")
                    return []
                data = response.json()
                events = data.get("events", [])
                print(f"Fetched {len(events)} appointments for user {user_ghl_id}")
                return events
            except Exception as e:
                print(f"Error fetching appointments for user {user_ghl_id}: {str(e)}")
                return []
        
        # Use ThreadPoolExecutor for parallel API calls (max 10 concurrent requests)
        print(f"Fetching appointments for {len(user_ghl_ids)} users in parallel...")
        with ThreadPoolExecutor(max_workers=10) as executor:
            future_to_user = {
                executor.submit(fetch_user_appointments, user_id): user_id 
                for user_id in user_ghl_ids
            }
            for future in as_completed(future_to_user):
                events = future.result()
                all_events.extend(events)
        
        print(f"Total fetched {len(all_events)} appointments from GHL API across all users")
        
        if not all_events:
            return {
                "created": 0,
                "updated": 0,
                "total": 0
            }
        
        # Parse all events into Appointment objects
        appointment_objects = []
        appointment_data_map = {}  # Store event data for relationship linking
        
        for event in all_events:
            try:
                ghl_appointment_id = event.get("id")
                if not ghl_appointment_id:
                    print(f"Skipping event without ID: {event.get('title', 'Unknown')}")
                    continue
                
                # Handle nested structure
                event_data = event
                if "appointment" in event:
                    event_data = event["appointment"]
                
                # Parse datetime fields
                start_time_dt = parse_datetime(event_data.get("startTime")) if event_data.get("startTime") else None
                end_time_dt = parse_datetime(event_data.get("endTime")) if event_data.get("endTime") else None
                date_added_dt = parse_datetime(event_data.get("dateAdded")) if event_data.get("dateAdded") else None
                date_updated_dt = parse_datetime(event_data.get("dateUpdated")) if event_data.get("dateUpdated") else None
                
                # Store calendar_id for bulk lookup later
                calendar_id_str = event_data.get("calendarId")
                
                # Create Appointment object (calendar will be set in bulk later)
                appointment = Appointment(
                    ghl_appointment_id=ghl_appointment_id,
                    location_id=location_id or event_data.get("locationId", ""),
                    title=event_data.get("title"),
                    address=event_data.get("address"),
                    calendar=None,  # Will be set in bulk after fetching all calendars
                    appointment_status=event_data.get("appointmentStatus"),
                    source=event_data.get("source"),
                    notes=event_data.get("notes") or event_data.get("description"),
                    ghl_contact_id=event_data.get("contactId"),
                    ghl_assigned_user_id=event_data.get("assignedUserId"),
                    start_time=start_time_dt,
                    end_time=end_time_dt,
                    date_added=date_added_dt,
                    date_updated=date_updated_dt,
                    users_ghl_ids=event_data.get("users", []),
                )
                
                appointment_objects.append(appointment)
                appointment_data_map[ghl_appointment_id] = event_data
                # Store calendar_id for bulk lookup
                if calendar_id_str:
                    appointment._calendar_id = calendar_id_str
                
            except Exception as e:
                print(f"Error parsing appointment {event.get('id', 'Unknown')}: {str(e)}")
                continue
        
        print(f"Parsed {len(appointment_objects)} appointments")
        
        # Bulk fetch all calendars needed
        calendar_ids = set()
        for appt in appointment_objects:
            if hasattr(appt, '_calendar_id') and appt._calendar_id:
                calendar_ids.add(appt._calendar_id)
        
        calendars_dict = {}
        if calendar_ids:
            calendars_dict = {
                cal.ghl_calendar_id: cal
                for cal in Calendar.objects.filter(ghl_calendar_id__in=calendar_ids)
            }
            print(f"Fetched {len(calendars_dict)} calendars in bulk")
        
        # Assign calendars to appointments
        for appt in appointment_objects:
            if hasattr(appt, '_calendar_id') and appt._calendar_id:
                appt.calendar = calendars_dict.get(appt._calendar_id)
        
        # Get existing appointments in bulk
        ghl_appointment_ids = [appt.ghl_appointment_id for appt in appointment_objects]
        existing_appointments = {
            appt.ghl_appointment_id: appt
            for appt in Appointment.objects.filter(ghl_appointment_id__in=ghl_appointment_ids)
        }
        
        # Separate into create and update lists
        appointments_to_create = []
        appointments_to_update = []
        
        for appointment in appointment_objects:
            if appointment.ghl_appointment_id in existing_appointments:
                # Update existing appointment
                existing = existing_appointments[appointment.ghl_appointment_id]
                # Update all fields
                existing.location_id = appointment.location_id
                existing.title = appointment.title
                existing.address = appointment.address
                existing.calendar = appointment.calendar
                existing.appointment_status = appointment.appointment_status
                existing.source = appointment.source
                existing.notes = appointment.notes
                existing.ghl_contact_id = appointment.ghl_contact_id
                existing.ghl_assigned_user_id = appointment.ghl_assigned_user_id
                existing.start_time = appointment.start_time
                existing.end_time = appointment.end_time
                existing.date_added = appointment.date_added
                existing.date_updated = appointment.date_updated
                existing.users_ghl_ids = appointment.users_ghl_ids
                appointments_to_update.append(existing)
            else:
                appointments_to_create.append(appointment)
        
        # Bulk operations
        created_count = 0
        updated_count = 0
        
        with transaction.atomic():
            # Bulk create new appointments
            if appointments_to_create:
                Appointment.objects.bulk_create(
                    appointments_to_create,
                    ignore_conflicts=True
                )
                created_count = len(appointments_to_create)
                print(f"Bulk created {created_count} appointments")
            
            # Bulk update existing appointments
            if appointments_to_update:
                Appointment.objects.bulk_update(
                    appointments_to_update,
                    fields=[
                        'location_id', 'title', 'address', 'calendar',
                        'appointment_status', 'source', 'notes', 'ghl_contact_id',
                        'ghl_assigned_user_id', 'start_time', 'end_time',
                        'date_added', 'date_updated', 'users_ghl_ids'
                    ]
                )
                updated_count = len(appointments_to_update)
                print(f"Bulk updated {updated_count} appointments")
        
        # Handle relationships in bulk after main operations
        # Use existing appointments dict (already fetched) and merge with newly created ones
        all_appointment_ids = ghl_appointment_ids
        # Get newly created appointments that weren't in existing_appointments
        newly_created_ids = set(ghl_appointment_ids) - set(existing_appointments.keys())
        appointments_dict = dict(existing_appointments)  # Start with existing
        
        # Fetch newly created appointments if any
        if newly_created_ids:
            newly_created = {
                appt.ghl_appointment_id: appt
                for appt in Appointment.objects.filter(ghl_appointment_id__in=newly_created_ids)
            }
            appointments_dict.update(newly_created)
        
        # Get all contacts and users in bulk
        contact_ids = set()
        user_ids = set()
        for event_data in appointment_data_map.values():
            if event_data.get("contactId"):
                contact_ids.add(event_data.get("contactId"))
            if event_data.get("assignedUserId"):
                user_ids.add(event_data.get("assignedUserId"))
            if event_data.get("users"):
                user_ids.update(event_data.get("users"))
        
        # Fetch contacts and users in bulk
        contacts_dict = {
            contact.contact_id: contact
            for contact in Contact.objects.filter(contact_id__in=contact_ids)
        } if contact_ids else {}
        
        users_dict = {
            user.ghl_user_id: user
            for user in User.objects.filter(ghl_user_id__in=user_ids)
        } if user_ids else {}
        
        # Link relationships
        appointments_to_update_relationships = []
        many_to_many_updates = {}  # appointment_id -> list of user objects
        
        for ghl_appointment_id, event_data in appointment_data_map.items():
            if ghl_appointment_id not in appointments_dict:
                continue
            
            appointment = appointments_dict[ghl_appointment_id]
            needs_update = False
            
            # Link contact
            contact_id = event_data.get("contactId")
            if contact_id and contact_id in contacts_dict:
                if appointment.contact_id != contacts_dict[contact_id].id:
                    appointment.contact = contacts_dict[contact_id]
                    needs_update = True
            elif appointment.contact_id is not None:
                appointment.contact = None
                needs_update = True
            
            # Link assigned user
            assigned_user_id = event_data.get("assignedUserId")
            if assigned_user_id and assigned_user_id in users_dict:
                if appointment.assigned_user_id != users_dict[assigned_user_id].id:
                    appointment.assigned_user = users_dict[assigned_user_id]
                    needs_update = True
            elif appointment.assigned_user_id is not None:
                appointment.assigned_user = None
                needs_update = True
            
            # Prepare users for many-to-many
            users_ghl_ids = event_data.get("users", [])
            if users_ghl_ids:
                users_to_add = [
                    users_dict[uid] for uid in users_ghl_ids
                    if uid in users_dict
                ]
                many_to_many_updates[appointment.id] = users_to_add
            
            if needs_update:
                appointments_to_update_relationships.append(appointment)
        
        # Bulk update relationships
        if appointments_to_update_relationships:
            with transaction.atomic():
                Appointment.objects.bulk_update(
                    appointments_to_update_relationships,
                    fields=['contact', 'assigned_user']
                )
                print(f"Updated relationships for {len(appointments_to_update_relationships)} appointments")
        
        # Handle many-to-many relationships (users) - optimized with bulk operations
        if many_to_many_updates:
            # Fetch all appointments in one query
            appointment_ids = list(many_to_many_updates.keys())
            appointments_for_m2m = Appointment.objects.filter(id__in=appointment_ids).prefetch_related('users')
            appointments_dict_m2m = {appt.id: appt for appt in appointments_for_m2m}
            
            # Use through model for bulk operations (more efficient)
            AppointmentUser = Appointment.users.through
            
            # Clear existing relationships in bulk
            AppointmentUser.objects.filter(appointment_id__in=appointment_ids).delete()
            
            # Create new relationships in bulk
            bulk_m2m_objects = []
            for appointment_id, users_list in many_to_many_updates.items():
                if appointment_id in appointments_dict_m2m and users_list:
                    for user in users_list:
                        bulk_m2m_objects.append(
                            AppointmentUser(appointment_id=appointment_id, user_id=user.id)
                        )
            
            if bulk_m2m_objects:
                AppointmentUser.objects.bulk_create(bulk_m2m_objects, ignore_conflicts=True)
            print(f"Updated many-to-many relationships for {len(many_to_many_updates)} appointments in bulk")
        
        # Delete appointments that exist in our app but not in GHL
        # Only delete appointments that:
        # 1. Have a ghl_appointment_id (are synced from GHL)
        # 2. Are within the time range we're syncing
        # 3. Are not in the fetched GHL appointments list
        deleted_count = 0
        if ghl_appointment_ids:
            # Get set of fetched GHL appointment IDs for quick lookup
            fetched_ghl_ids = set(ghl_appointment_ids)
            
            # Find appointments to delete:
            # - Have ghl_appointment_id (synced from GHL)
            # - Within the time range we're syncing
            # - Not in the fetched GHL appointments
            appointments_to_delete = Appointment.objects.filter(
                ghl_appointment_id__isnull=False
            ).exclude(
                ghl_appointment_id__in=fetched_ghl_ids
            ).filter(
                start_time__gte=start_time,
                start_time__lte=end_time
            )
            
            deleted_count = appointments_to_delete.count()
            if deleted_count > 0:
                with transaction.atomic():
                    appointments_to_delete.delete()
                    print(f"Deleted {deleted_count} appointments that no longer exist in GHL")
        
        print(f"Appointment sync completed: {created_count} created, {updated_count} updated, {deleted_count} deleted")
        
        return {
            "created": created_count,
            "updated": updated_count,
            "deleted": deleted_count,
            "total": len(all_events)
        }
        
    except Exception as e:
        print(f"Error fetching appointments: {str(e)}")
        raise



@shared_task
def sync_calendars_from_ghl_task(location_id, access_token):
    sync_calendars_from_ghl_utils(location_id, access_token)