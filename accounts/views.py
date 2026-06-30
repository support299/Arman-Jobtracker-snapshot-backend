import json
import logging
from decouple import config
import requests
from django.core.cache import cache
from django.http import JsonResponse
from django.shortcuts import redirect
from django.urls import reverse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

from accounts.ghl_credentials import upsert_ghl_credentials
from accounts.models import GHLAuthCredentials, GHLCompanyAuth, Location, Webhook
from accounts.oauth import build_ghl_marketplace_auth_url
from accounts.tasks import fetch_all_contacts_task, handle_webhook_event
from accounts.tasks import sync_calendars_from_ghl_task
from accounts.utils import (
    LocationServices,
    LocationServicesError,
    sync_all_users_to_db,
    sync_custom_fields_to_db,
)
from dashboard_app.tasks import sync_single_invoice_task, delete_invoice_task
from dashboard_app.models import Invoice
from jobtracker_app.helpers import update_job_invoice_status_by_invoice_id





logger = logging.getLogger(__name__)

# Map GHL invoice webhook event types to local Invoice status values
INVOICE_EVENT_STATUS_MAP = {
    "InvoicePaid": "paid",
    "InvoicePartiallyPaid": "partially_paid",
    "InvoiceSent": "sent",
    "InvoiceVoid": "void",
}


GHL_CLIENT_ID = config("GHL_CLIENT_ID")
GHL_CLIENT_SECRET = config("GHL_CLIENT_SECRET")
GHL_REDIRECTED_URI = config("GHL_REDIRECTED_URI")
GHL_VERSION_ID = config("GHL_VERSION_ID", default="").strip()
TOKEN_URL = "https://services.leadconnectorhq.com/oauth/token"
INSTALLED_LOCATIONS_URL = "https://services.leadconnectorhq.com/oauth/installedLocations"
LOCATION_TOKEN_URL = "https://services.leadconnectorhq.com/oauth/locationToken"
GHL_API_VERSION = "2021-07-28"
SCOPE = config("SCOPE")


def _upsert_ghl_credentials(token_data):
    return upsert_ghl_credentials(token_data)


def _sync_location_snapshot(credentials):
    try:
        location_obj, _ = LocationServices.pull_location(credentials.location_id)
    except (LocationServicesError, requests.RequestException, ValueError) as exc:
        logger.warning(
            "GHL OAuth tokens(): failed to sync location snapshot location_id=%s: %s",
            credentials.location_id,
            exc,
        )
        return

    changed = False
    if getattr(location_obj, "name", "") and credentials.company_name != location_obj.name:
        credentials.company_name = location_obj.name
        changed = True
    if getattr(location_obj, "timezone", "") and credentials.timezone != location_obj.timezone:
        credentials.timezone = location_obj.timezone
        changed = True
    if changed:
        credentials.save()


def _persist_company_auth(token_data):
    company_id = (token_data.get("companyId") or "").strip()
    if not company_id:
        return

    GHLCompanyAuth.objects.update_or_create(
        company_id=company_id,
        defaults={
            "access_token": token_data.get("access_token", "") or "",
            "refresh_token": token_data.get("refresh_token", "") or "",
            "expires_in": token_data.get("expires_in", 0) or 0,
            "scope": token_data.get("scope", "") or "",
            "user_id": token_data.get("userId", "") or "",
        },
    )


def _fetch_installed_locations(company_id, company_token):
    app_id = (GHL_CLIENT_ID.split("-")[0] if GHL_CLIENT_ID else "").strip()
    if not app_id:
        raise ValueError("GHL client id is not configured")

    response = requests.get(
        INSTALLED_LOCATIONS_URL,
        params={"companyId": company_id, "appId": app_id},
        headers={
            "Authorization": f"Bearer {company_token}",
            "Version": GHL_API_VERSION,
            "Accept": "application/json",
        },
        timeout=30,
    )
    response.raise_for_status()

    try:
        payload = response.json()
    except ValueError as exc:
        raise ValueError("installedLocations returned non-JSON data") from exc

    locations = payload.get("locations", []) or []
    logger.info(
        "GHL OAuth tokens(): installedLocations company_id=%s count=%s",
        company_id,
        len(locations),
    )
    return locations


def _prioritize_locations_for_company(company_id, locations):
    preferred_location_id = cache.get(f"ghl_bulk_oauth_primary:{company_id}")
    if not preferred_location_id:
        return locations

    preferred = None
    remaining = []
    for loc in locations:
        loc_id = (loc.get("_id") or loc.get("id") or "").strip()
        if loc_id == preferred_location_id and preferred is None:
            preferred = loc
        else:
            remaining.append(loc)

    if preferred is None:
        logger.info(
            "GHL OAuth tokens(): cached preferred location not present company_id=%s location_id=%s",
            company_id,
            preferred_location_id,
        )
        return locations

    logger.info(
        "GHL OAuth tokens(): prioritizing INSTALL-selected location company_id=%s location_id=%s",
        company_id,
        preferred_location_id,
    )
    return [preferred] + remaining


def _exchange_location_token(company_id, company_token, location_id):
    response = requests.post(
        LOCATION_TOKEN_URL,
        data={"companyId": company_id, "locationId": location_id},
        headers={
            "Authorization": f"Bearer {company_token}",
            "Version": GHL_API_VERSION,
            "Accept": "application/json",
        },
        timeout=30,
    )
    response.raise_for_status()

    try:
        payload = response.json()
    except ValueError as exc:
        raise ValueError(f"locationToken returned non-JSON for location_id={location_id}") from exc

    payload.setdefault("locationId", location_id)
    payload.setdefault("companyId", company_id)
    return payload


def _bootstrap_location(location_id, access_token):
    if not location_id or not access_token:
        return

    try:
        fetch_all_contacts_task.delay(location_id, access_token)
    except Exception:
        logger.exception("GHL OAuth tokens(): failed to enqueue contacts sync location_id=%s", location_id)

    try:
        sync_custom_fields_to_db(location_id, access_token)
    except Exception:
        logger.exception("GHL OAuth tokens(): custom field sync failed location_id=%s", location_id)

    try:
        sync_all_users_to_db(location_id, access_token)
    except Exception:
        logger.exception("GHL OAuth tokens(): user sync failed location_id=%s", location_id)

    try:
        sync_calendars_from_ghl_task.delay(location_id, access_token)
    except Exception:
        logger.exception("GHL OAuth tokens(): failed to enqueue calendar sync location_id=%s", location_id)


def _handle_install_webhook(data):
    location_id = (data.get("locationId") or "").strip()
    company_id = (data.get("companyId") or "").strip()
    user_id = (data.get("userId") or "").strip()

    if location_id and company_id:
        cache.set(f"ghl_bulk_oauth_primary:{company_id}", location_id, timeout=300)
        logger.info(
            "GHL INSTALL: cached primary location hint company_id=%s location_id=%s",
            company_id,
            location_id,
        )

    if not location_id or not company_id:
        return {"received": True, "skipped": "missing_ids"}

    if GHLAuthCredentials.objects.filter(location_id=location_id, is_active=True).exists():
        logger.info("GHL INSTALL: location %s already connected, skipping", location_id)
        return {"received": True, "skipped": "already_exists", "location_id": location_id}

    company_auth = GHLCompanyAuth.objects.filter(company_id=company_id).first()
    if not company_auth or not (company_auth.access_token or "").strip():
        logger.warning(
            "GHL INSTALL: no company-level token available company_id=%s location_id=%s",
            company_id,
            location_id,
        )
        return {"received": True, "skipped": "no_company_token", "location_id": location_id}

    try:
        token_data = _exchange_location_token(company_id, company_auth.access_token, location_id)
        if user_id and not (token_data.get("userId") or "").strip():
            token_data["userId"] = user_id

        credentials, created = _upsert_ghl_credentials(token_data)
        if data.get("companyName") and not credentials.company_name:
            credentials.company_name = data.get("companyName")
            credentials.save(update_fields=["company_name"])
        _sync_location_snapshot(credentials)
        _bootstrap_location(location_id, token_data.get("access_token") or company_auth.access_token)
        logger.info(
            "GHL INSTALL: %s credentials for location_id=%s company_id=%s",
            "created" if created else "updated",
            location_id,
            company_id,
        )
        return {
            "received": True,
            "action": "created" if created else "updated",
            "location_id": location_id,
        }
    except (requests.RequestException, ValueError) as exc:
        logger.warning(
            "GHL INSTALL: token exchange failed company_id=%s location_id=%s error=%s",
            company_id,
            location_id,
            exc,
        )
        return {"received": True, "skipped": "token_exchange_failed", "location_id": location_id}


def _handle_uninstall_webhook(data):
    location_id = (data.get("locationId") or "").strip()
    if not location_id:
        return {"received": True, "skipped": "missing_location_id"}

    deleted_credentials, _ = GHLAuthCredentials.objects.filter(location_id=location_id).update(is_active=False)
    deactivated_locations = Location.objects.filter(pk=location_id).update(is_active=False)
    logger.info(
        "GHL UNINSTALL: location_id=%s deleted_credentials=%s deactivated_locations=%s",
        location_id,
        deleted_credentials,
        deactivated_locations,
    )
    return {
        "received": True,
        "action": "uninstalled",
        "location_id": location_id,
        "deleted_credentials": deleted_credentials,
        "deactivated_locations": deactivated_locations,
    }


def auth_connect(request):
    redirect_uri = request.GET.get("redirect_uri") or GHL_REDIRECTED_URI
    auth_url = build_ghl_marketplace_auth_url(
        redirect_uri=redirect_uri,
        client_id=GHL_CLIENT_ID,
        scope=SCOPE,
        version_id=GHL_VERSION_ID,
    )
    return redirect(auth_url)



def callback(request):
    code = request.GET.get("code")

    if not code:
        return JsonResponse({"error": "Authorization code not received from OAuth"}, status=400)

    tokens_url = request.build_absolute_uri(reverse("oauth_tokens"))
    return redirect(f"{tokens_url}?code={code}")


def tokens(request):
    authorization_code = request.GET.get("code")

    if not authorization_code:
        return JsonResponse({"error": "Authorization code not found"}, status=400)

    redirect_uri = request.GET.get("redirect_uri") or GHL_REDIRECTED_URI

    data = {
        "grant_type": "authorization_code",
        "client_id": GHL_CLIENT_ID,
        "client_secret": GHL_CLIENT_SECRET,
        "redirect_uri": redirect_uri,
        "code": authorization_code,
    }

    response = requests.post(TOKEN_URL, data=data, timeout=30)

    try:
        response_data = response.json()
        if not response_data:
            return JsonResponse({"error": "Empty token response from API"}, status=502)

        error_code = (response_data.get("error") or "").strip()
        if not response.ok or error_code:
            detail = (
                response_data.get("error_description")
                or response_data.get("error")
                or (response.text or "")[:800]
            )
            logger.warning(
                "GHL OAuth tokens(): exchange failed status=%s error=%s detail=%s",
                response.status_code,
                error_code or "(none)",
                (detail or "")[:500],
            )
            user_error = "GHL OAuth token exchange failed."
            if error_code == "invalid_grant":
                user_error = (
                    "This authorization link was already used or expired. "
                    "Open the app again from GoHighLevel and retry."
                )
            return JsonResponse({"error": user_error, "detail": detail}, status=400)

        company_id = (response_data.get("companyId") or "").strip()
        company_token = response_data.get("access_token") or ""
        connected_location_ids = []
        company_level_oauth = not response_data.get("locationId") and bool(company_id)
        primary_token_data = response_data
        primary_location_id = (response_data.get("locationId") or "").strip()

        if company_level_oauth:
            _persist_company_auth(response_data)
            try:
                locations = _fetch_installed_locations(company_id, company_token)
                locations = _prioritize_locations_for_company(company_id, locations)
            except (requests.RequestException, ValueError) as exc:
                logger.exception(
                    "GHL OAuth tokens(): installedLocations failed company_id=%s",
                    company_id,
                )
                return JsonResponse(
                    {"error": f"Failed to fetch installed locations from GHL: {exc}"},
                    status=502,
                )

            if not locations:
                return JsonResponse(
                    {
                        "error": (
                            "Bulk install returned no approved subaccounts. "
                            "Open the app from a subaccount and try again."
                        )
                    },
                    status=400,
                )

            primary_token_data = None
            primary_location_id = ""
            for loc in locations:
                location_id = (loc.get("_id") or loc.get("id") or "").strip()
                if not location_id:
                    continue
                try:
                    location_token_data = _exchange_location_token(
                        company_id=company_id,
                        company_token=company_token,
                        location_id=location_id,
                    )
                    credentials, _ = _upsert_ghl_credentials(location_token_data)
                    _sync_location_snapshot(credentials)
                    connected_location_ids.append(location_id)
                    if primary_token_data is None:
                        primary_token_data = location_token_data
                        primary_location_id = location_id
                except (requests.RequestException, ValueError) as exc:
                    logger.info(
                        "GHL OAuth tokens(): skipped locationToken exchange location_id=%s error=%s",
                        location_id,
                        exc,
                    )

            if not primary_token_data or not primary_location_id:
                return JsonResponse(
                    {
                        "error": (
                            "OAuth succeeded, but no approved subaccount token could be created. "
                            "Open the app from a subaccount and retry."
                        )
                    },
                    status=400,
                )
            cache.delete(f"ghl_bulk_oauth_primary:{company_id}")
        else:
            if not primary_location_id:
                return JsonResponse(
                    {"error": "OAuth succeeded but no locationId was returned by GHL."},
                    status=400,
                )
            credentials, _ = _upsert_ghl_credentials(response_data)
            _sync_location_snapshot(credentials)
            connected_location_ids.append(primary_location_id)

        _bootstrap_location(primary_location_id, primary_token_data.get("access_token"))
        return JsonResponse({
            "message": "Authentication successful",
            "access_token": primary_token_data.get("access_token"),
            "token_stored": True,
            "location_id": primary_location_id,
            "company_id": company_id,
            "connected_location_ids": connected_location_ids,
            "connected_locations": len(connected_location_ids),
            "company_level_oauth": company_level_oauth,
        })

    except requests.exceptions.JSONDecodeError:
        return JsonResponse({
            "error": "Invalid JSON response from API",
            "status_code": response.status_code,
            "response_text": response.text[:500]
        }, status=500)

    except ValueError as exc:
        logger.warning("GHL OAuth tokens(): invalid token payload: %s", exc)
        return JsonResponse({"error": str(exc)}, status=400)
    

def sync_all_contacts_and_address(request):

    try:
        
        obj = GHLAuthCredentials.objects.first()
        fetch_all_contacts_task.delay(obj.location_id, obj.access_token)
        return JsonResponse({
            "message": "Authentication successful",
            "access_token": obj.access_token,
            "token_stored": True
        })
        
    except requests.exceptions.JSONDecodeError:
        return JsonResponse({
            "error": "Invalid JSON response from API",
        }, status=500)
    

@csrf_exempt
@require_http_methods(["POST"])
def webhook_handler(request):
    try:
        data = json.loads(request.body.decode("utf-8")) if request.body else {}
        print("Webhook data received:----- ", data)

        event_type = (data.get("type") or "").strip()
        event_type_upper = event_type.upper()
        location_id = (data.get("locationId") or "").strip()
        company_id = (data.get("companyId") or "").strip()

        # Create Webhook record
        Webhook.objects.create(
            event=event_type_upper or "unknown",
            company_id=company_id or location_id or "unknown",
            payload=data
        )

        if event_type_upper == "INSTALL":
            return JsonResponse(_handle_install_webhook(data), status=200)

        if event_type_upper == "UNINSTALL":
            return JsonResponse(_handle_uninstall_webhook(data), status=200)

        # Dispatch async handler
        handle_webhook_event.delay(data, event_type)

        invoice_events = [
            "InvoiceCreate", "InvoiceUpdate", "InvoiceDelete",
            "InvoicePaid", "InvoicePartiallyPaid", "InvoiceSent", "InvoiceVoid",
        ]
        if event_type in invoice_events:
            # Extract location_id - could be at root or in nested structure
            location_id = data.get("locationId")
            
            # Extract invoice_id from various possible locations in the payload
            invoice_id = None
            
            # Case 1: Invoice data is nested in "invoice" key
            invoice_obj = data.get("invoice")
            if isinstance(invoice_obj, dict):
                invoice_id = invoice_obj.get("_id") or invoice_obj.get("id")
            
            # Case 2: Invoice data IS the root payload (your case)
            # Check if root has _id (which indicates invoice data is at root)
            if not invoice_id:
                if "_id" in data and "invoiceNumber" in data:
                    # This looks like invoice data at root level
                    invoice_id = data.get("_id")
                    # If locationId is not at root, try to get from credentials
                    if not location_id:
                        # Try to get location_id from altId if it's a location type
                        if data.get("altType") == "location":
                            location_id = data.get("altId")
            
            # Case 3: Try other common locations
            if not invoice_id:
                invoice_id = data.get("invoiceId") or data.get("_id") or data.get("id")
            
            # Debug logging
            print(f"Invoice webhook - event_type: {event_type}, location_id: {location_id}, invoice_id: {invoice_id}")
            
            if location_id and invoice_id:
                if event_type == "InvoiceDelete":
                    # Delete invoice for delete event
                    delete_invoice_task.delay(invoice_id)
                    print(f"✅ Triggered invoice deletion for {event_type}: invoice_id={invoice_id}")
                else:
                    # Update local invoice status immediately for status-specific events
                    if event_type in INVOICE_EVENT_STATUS_MAP:
                        try:
                            invoice = Invoice.objects.filter(
                                invoice_id=invoice_id, location_id=location_id
                            ).first()
                            if invoice:
                                new_status = INVOICE_EVENT_STATUS_MAP[event_type]
                                invoice.status = new_status
                                invoice.save(update_fields=["status"])
                                print(f"✅ Updated invoice status to '{new_status}' for {event_type}: invoice_id={invoice_id}")
                            new_status = INVOICE_EVENT_STATUS_MAP[event_type]
                            job_count = update_job_invoice_status_by_invoice_id(invoice_id, new_status)
                            if job_count:
                                print(f"✅ Updated invoice_status on {job_count} job(s) for {event_type}: invoice_id={invoice_id}")
                        except Exception as e:
                            print(f"⚠️ Could not update invoice status for {event_type}: {e}")
                    # Sync invoice for create, update, paid, partially paid, sent, void
                    sync_single_invoice_task.delay(location_id, invoice_id)
                    print(f"✅ Triggered invoice sync for {event_type}: invoice_id={invoice_id}, location_id={location_id}")
            else:
                missing = []
                if not location_id:
                    missing.append("locationId")
                if not invoice_id:
                    missing.append("invoiceId/_id")
                print(f"❌ Missing required fields in webhook payload for {event_type}: {', '.join(missing)}")
                print(f"   Available keys in payload: {list(data.keys())}")
                # Try to extract location_id from credentials if invoice_id exists
                if invoice_id and not location_id:
                    credentials = GHLAuthCredentials.objects.first()
                    if credentials and credentials.location_id:
                        location_id = credentials.location_id
                        print(f"⚠️ Using location_id from credentials: {location_id}")
                        if event_type == "InvoiceDelete":
                            delete_invoice_task.delay(invoice_id)
                            print(f"✅ Triggered invoice deletion")
                        else:
                            if event_type in INVOICE_EVENT_STATUS_MAP:
                                try:
                                    invoice = Invoice.objects.filter(
                                        invoice_id=invoice_id, location_id=location_id
                                    ).first()
                                    if invoice:
                                        new_status = INVOICE_EVENT_STATUS_MAP[event_type]
                                        invoice.status = new_status
                                        invoice.save(update_fields=["status"])
                                        print(f"✅ Updated invoice status to '{new_status}' for {event_type}: invoice_id={invoice_id}")
                                    new_status = INVOICE_EVENT_STATUS_MAP[event_type]
                                    job_count = update_job_invoice_status_by_invoice_id(invoice_id, new_status)
                                    if job_count:
                                        print(f"✅ Updated invoice_status on {job_count} job(s) for {event_type}: invoice_id={invoice_id}")
                                except Exception as e:
                                    print(f"⚠️ Could not update invoice status for {event_type}: {e}")
                            sync_single_invoice_task.delay(location_id, invoice_id)
                            print(f"✅ Triggered invoice sync with fallback location_id")

        return JsonResponse({"message": "Webhook received"}, status=200)

    except (ValueError, TypeError, UnicodeDecodeError):
        logger.warning("Webhook error: invalid JSON body")
        return JsonResponse({"received": True, "skipped": "invalid_json"}, status=200)

    except Exception as e:
        print(f"Webhook error: {str(e)}")
        import traceback
        traceback.print_exc()
        return JsonResponse({"error": str(e)}, status=500)


def sync_all_users(request):
    """
    Manually sync all users from GHL API to local database.
    Fetches all users for the authenticated location and creates/updates them.
    """
    try:
        # Get the first available GHL credentials
        credentials = GHLAuthCredentials.objects.first()
        
        if not credentials:
            return JsonResponse({
                "error": "No GHL credentials found. Please authenticate first."
            }, status=400)
        
        if not credentials.location_id or not credentials.access_token:
            return JsonResponse({
                "error": "Invalid GHL credentials. Missing location_id or access_token."
            }, status=400)
        
        # Sync all users
        result = sync_all_users_to_db(credentials.location_id, credentials.access_token)
        
        return JsonResponse({
            "message": "Users synced successfully",
            "created": result["created"],
            "updated": result["updated"],
            "total": result["total"]
        }, status=200)
        
    except Exception as e:
        logger.error(f"Error syncing users: {str(e)}")
        return JsonResponse({
            "error": f"Failed to sync users: {str(e)}"
        }, status=500)

    