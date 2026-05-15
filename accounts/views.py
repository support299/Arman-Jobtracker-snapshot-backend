from decouple import config
import requests
from django.http import JsonResponse
import json
from django.shortcuts import redirect
from accounts.models import GHLAuthCredentials,Webhook
from django.views.decorators.csrf import csrf_exempt
import logging
from django.views import View
from django.utils.decorators import method_decorator
import traceback
from accounts.tasks import fetch_all_contacts_task,handle_webhook_event
from accounts.utils import sync_all_users_to_db,sync_custom_fields_to_db
from accounts.tasks import sync_calendars_from_ghl_task
from dashboard_app.tasks import sync_single_invoice_task,delete_invoice_task
from dashboard_app.models import Invoice
from accounts.utils import fetch_location_custom_fields





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
TOKEN_URL = "https://services.leadconnectorhq.com/oauth/token"
SCOPE = config("SCOPE")

def auth_connect(request):
    auth_url = ("https://marketplace.gohighlevel.com/oauth/chooselocation?response_type=code&"
                f"redirect_uri={GHL_REDIRECTED_URI}&"
                f"client_id={GHL_CLIENT_ID}&"
                f"scope={SCOPE}"
                )
    return redirect(auth_url)



def callback(request):
    
    code = request.GET.get('code')

    if not code:
        return JsonResponse({"error": "Authorization code not received from OAuth"}, status=400)

    return redirect(f'{config("BASE_URI")}/api/accounts/auth/tokens?code={code}')


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

    response = requests.post(TOKEN_URL, data=data)

    try:
        response_data = response.json()
        if not response_data:
            return

        obj, created = GHLAuthCredentials.objects.update_or_create(
            location_id= response_data.get("locationId"),
            defaults={
                "access_token": response_data.get("access_token"),
                "refresh_token": response_data.get("refresh_token"),
                "expires_in": response_data.get("expires_in"),
                "scope": response_data.get("scope"),
                "user_type": response_data.get("userType"),
                "company_id": response_data.get("companyId"),
                "user_id":response_data.get("userId"),

            }
        )
        # fetch_all_contacts_task.delay(response_data.get("locationId"), response_data.get("access_token"))
        # fetch_location_custom_fields(response_data.get("locationId"), response_data.get("access_token"))
        # sync_all_users_to_db(response_data.get("locationId"), response_data.get("access_token"))
        # sync_calendars_from_ghl_task.delay(response_data.get("locationId"), response_data.get("access_token"))

        location_id = response_data.get("locationId")
        access_token = response_data.get("access_token")
        
        fetch_all_contacts_task.delay(location_id, access_token)
        sync_custom_fields_to_db(location_id, access_token)
        sync_all_users_to_db(location_id, access_token)
        sync_calendars_from_ghl_task.delay(location_id, access_token)
        return JsonResponse({
            "message": "Authentication successful",
            "access_token": response_data.get('access_token'),
            "token_stored": True
        })
        
    except requests.exceptions.JSONDecodeError:
        return JsonResponse({
            "error": "Invalid JSON response from API",
            "status_code": response.status_code,
            "response_text": response.text[:500]
        }, status=500)
    

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
def webhook_handler(request):
    if request.method != "POST":
        return JsonResponse({"message": "Method not allowed"}, status=405)

    try:
        data = json.loads(request.body)
        print("Webhook data received:----- ", data)

        # Create Webhook record
        Webhook.objects.create(
            event=data.get("type", "unknown"),
            company_id=data.get("locationId", "unknown"),
            payload=data
        )

        # Dispatch async handler
        event_type = data.get("type")
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
                                except Exception as e:
                                    print(f"⚠️ Could not update invoice status for {event_type}: {e}")
                            sync_single_invoice_task.delay(location_id, invoice_id)
                            print(f"✅ Triggered invoice sync with fallback location_id")

        return JsonResponse({"message": "Webhook received"}, status=200)

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

    