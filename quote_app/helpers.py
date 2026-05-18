from decimal import Decimal

from accounts.models import GHLAuthCredentials, GHLCustomField
import requests
from decouple import config
from service_app.models import GlobalBasePrice


def resolve_ghl_credentials_for_submission(submission):
    """
    Resolve GHLAuthCredentials for a customer submission.

    Priority: submission.account -> contact.account -> lookup by contact.location_id.
    """
    account = getattr(submission, 'account', None)
    if account is not None:
        return account

    if getattr(submission, 'account_id', None):
        account = GHLAuthCredentials.objects.filter(pk=submission.account_id).first()
        if account:
            return account

    contact = getattr(submission, 'contact', None)
    if contact is None:
        return None

    contact_account = getattr(contact, 'account', None)
    if contact_account is not None:
        return contact_account

    if getattr(contact, 'account_id', None):
        account = GHLAuthCredentials.objects.filter(pk=contact.account_id).first()
        if account:
            return account

    location_id = (getattr(contact, 'location_id', None) or '').strip()
    if location_id:
        return GHLAuthCredentials.objects.filter(location_id=location_id).first()

    return None


def get_global_minimum_base_price_for_submission(submission) -> Decimal:
    """
    Return the account-scoped global minimum quote total for a submission.

    Uses submission.account (or contact/location fallbacks). Returns 0 if no account
    or no GlobalBasePrice row exists for that account.
    """
    account = resolve_ghl_credentials_for_submission(submission)
    if account is None:
        return Decimal('0.00')

    settings = GlobalBasePrice.objects.filter(account=account).first()
    if settings is None:
        return Decimal('0.00')

    return Decimal(settings.base_price or 0)


def resolve_location_id_for_submission(submission, credentials):
    """GHL location id for API calls (contact location preferred, then credentials)."""
    contact = getattr(submission, 'contact', None)
    if contact:
        contact_loc = (getattr(contact, 'location_id', None) or '').strip()
        if contact_loc:
            return contact_loc
    if credentials:
        return (getattr(credentials, 'location_id', None) or '').strip() or None
    return None


def create_or_update_ghl_contact(submission, is_submit=False):
    try:
        print("🔹 Starting GHL contact sync...")
        credentials = resolve_ghl_credentials_for_submission(submission)
        if not credentials:
            print("❌ No GHLAuthCredentials found for this submission (account or location_id).")
            return

        location_id = resolve_location_id_for_submission(submission, credentials)
        if not location_id:
            print("❌ No location_id available for this submission.")
            return

        token = credentials.access_token
        print(f"✅ Using token (truncated): {token[:10]}..., locationId: {location_id}")

        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {token}",
            "Version": "2021-07-28",
            "Content-Type": "application/json"
        }

        # Step 1: Determine search URL
        if submission.contact.contact_id:
            search_url = f"https://services.leadconnectorhq.com/contacts/{submission.contact.contact_id}"
            print(f"🔍 Searching by contact_id: {submission.contact.contact_id}")
        else:
            search_query = submission.contact.email or submission.contact.first_name
            if not search_query:
                print("❌ No identifier (email/first_name) to search GHL contact.")
                return
            search_url = f"https://services.leadconnectorhq.com/contacts/?locationId={location_id}&query={search_query}"
            print(f"🔍 Searching by query: {search_query}")

        # Step 2: Fetch existing contact
        print(f"➡️ Sending GET request to {search_url}")
        search_response = requests.get(search_url, headers=headers)
        print(f"⬅️ Response [{search_response.status_code}]: {search_response.text}")

        if search_response.status_code != 200:
            print("❌ Failed to search GHL contact.")
            return

        search_data = search_response.json()
        results = []

        # Handle both cases: list of contacts or single contact
        if "contacts" in search_data and isinstance(search_data["contacts"], list):
            results = search_data["contacts"]
            print(f"📋 Found {len(results)} contacts in search results.")
        elif "contact" in search_data and isinstance(search_data["contact"], dict):
            results = [search_data["contact"]]
            print("📋 Found 1 contact in search results.")
        else:
            print("ℹ️ No contacts found in GHL.")

        # Step 3: Build custom fields
        booking_url = f"{config('BASE_FRONTEND_URI')}/booking?submission_id={submission.id}"
        quote_url = (
            f"{config('BASE_FRONTEND_URI')}/quote/details/{submission.id}"
            f"?first_name={submission.contact.first_name}"
            f"&last_name={submission.contact.last_name}"
            f"&phone={submission.contact.phone}"
            f"&email={submission.contact.email}"
        )
        
        # Get Quote Link custom field using account and field name
        custom_fields = []
        try:
            quote_link_field = GHLCustomField.objects.get(
                account=credentials,
                field_name='Quote Link',
                is_active=True
            )
            quote_link_field.refresh_from_db()
            
            # Validate that we have a real field ID (not a placeholder)
            if quote_link_field.ghl_field_id and quote_link_field.ghl_field_id != 'ghl_field_id' and len(quote_link_field.ghl_field_id) >= 5:
                custom_fields.append({
                    "id": str(quote_link_field.ghl_field_id),
                    "field_value": quote_url if is_submit else booking_url
                })
                print(f"✅ [QUOTE LINK] Using custom field 'Quote Link' with ID: {quote_link_field.ghl_field_id}")
            else:
                print(f"⚠️ [QUOTE LINK] Invalid ghl_field_id value: '{quote_link_field.ghl_field_id}'. Skipping custom field update.")
        except GHLCustomField.DoesNotExist:
            print(f"⚠️ [QUOTE LINK] 'Quote Link' custom field not found for location_id: {location_id}")
        except Exception as e:
            print(f"❌ [QUOTE LINK] Error getting Quote Link field: {str(e)}")
        
        print(f"🛠 Custom fields prepared: {custom_fields}")

        # Step 4: Update or create contact
        if results:
            ghl_contact_id = results[0]["id"]
            tags = results[0].get("tags", [])
            contact_payload = {}

            # Only include customFields if we have fields to update
            if custom_fields:
                contact_payload["customFields"] = custom_fields

            if is_submit:
                if "quote accepted" not in tags:
                    tags.append("quote accepted")
                contact_payload["tags"] = tags
            else:
                if "quoted" not in tags:
                    tags.append("quoted")
                contact_payload["tags"] = tags

            print(f"✏️ Updating contact {ghl_contact_id} with payload: {contact_payload}")
            contact_response = requests.put(
                f"https://services.leadconnectorhq.com/contacts/{ghl_contact_id}",
                json=contact_payload,
                headers=headers
            )
        else:
            contact_payload = {
                "firstName": submission.contact.first_name,
                "email": submission.contact.email,
                "phone": submission.contact.phone,
                "locationId": location_id
            }
            # Only include customFields if we have fields to update
            if custom_fields:
                contact_payload["customFields"] = custom_fields
            print(f" Creating new contact with payload: {contact_payload}")
            contact_response = requests.post(
                "https://services.leadconnectorhq.com/contacts/",
                json=contact_payload,
                headers=headers
            )

        print(f"⬅️ Contact sync response [{contact_response.status_code}]: {contact_response.text}")

        if contact_response.status_code not in [200, 201]:
            print("❌ Failed to create/update contact in GHL.")
            return

        print("✅ Contact synced successfully.")

    except Exception as e:
        print(f"🔥 Error syncing contact: {e}")
