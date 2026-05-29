from datetime import datetime
from decimal import Decimal
import uuid
import requests

from celery import shared_task
from django.utils import timezone

from accounts.models import GHLAuthCredentials
from .helpers import (
    build_invoice_payload_from_job,
    create_invoice,
    resolve_ghl_credentials_for_invoice,
    save_job_invoice_info,
    search_ghl_contact,
    send_invoice,
    trip_surcharge_amount_for_job,
    update_contact,
)
from .models import Job


def _normalize_invoice_identifier(value):
    if value is None:
        return ""
    return str(value).strip()


def _is_uuid_like(value):
    candidate = _normalize_invoice_identifier(value)
    if not candidate:
        return False
    try:
        uuid.UUID(candidate)
        return True
    except (ValueError, TypeError, AttributeError):
        return False


def _pick_best_invoice_identifier(candidates, *, allow_uuid_fallback=True):
    normalized = [_normalize_invoice_identifier(candidate) for candidate in candidates]
    normalized = [candidate for candidate in normalized if candidate]
    if not normalized:
        return ""
    for candidate in normalized:
        if not _is_uuid_like(candidate):
            return candidate
    return normalized[0] if allow_uuid_fallback else ""


def _extract_invoice_reference_data(response_data):
    response_data = response_data if isinstance(response_data, dict) else {}
    invoice_payload = response_data.get("invoice")
    invoice_payload = invoice_payload if isinstance(invoice_payload, dict) else {}

    # New webhook contract: `ghl_invoice_id` is the canonical GHL `_id`.
    ghl_invoice_id = _pick_best_invoice_identifier(
        [
            response_data.get("ghl_invoice_id"),
            invoice_payload.get("ghl_invoice_id"),
            invoice_payload.get("invoice_id"),
            invoice_payload.get("_id"),
            response_data.get("invoice_id"),
            response_data.get("_id"),
            response_data.get("id"),
            invoice_payload.get("id"),
        ],
        allow_uuid_fallback=False,
    )

    public_invoice_id = _pick_best_invoice_identifier(
        [
            response_data.get("public_invoice_id"),
            response_data.get("id"),
            invoice_payload.get("id"),
        ],
    )

    invoice_url = (
        _normalize_invoice_identifier(response_data.get("invoice_url"))
        or _normalize_invoice_identifier(invoice_payload.get("invoice_url"))
        or _normalize_invoice_identifier(invoice_payload.get("url"))
    )

    if not invoice_url and public_invoice_id and _is_uuid_like(public_invoice_id):
        invoice_url = f"https://workorder.theservicepilot.com/invoice/{public_invoice_id}/"

    return {
        "ghl_invoice_id": ghl_invoice_id,
        "public_invoice_id": public_invoice_id,
        "invoice_url": invoice_url,
    }


@shared_task
def update_jobs_to_service_due():
    """
    Update jobs with status 'confirmed' to 'service_due' 
    when their scheduled_at time has passed.
    """
    now = timezone.now()
    
    # Find all confirmed jobs where scheduled_at has passed
    jobs_to_update = Job.objects.filter(
        status='confirmed',
        scheduled_at__lte=now,
        scheduled_at__isnull=False
    )
    
    # Update status to service_due
    count = jobs_to_update.update(status='service_due')
    
    print(f"Updated {count} job(s) from 'confirmed' to 'service_due'")
    return f"Updated {count} job(s)"

def _process_invoice_payload(data, job_id=None):
    customer_email = data.get("customer_email")
    customer_name = data.get("customer_name")
    services = data.get("selected_services", [])
    customer_address = data.get("customer_address")

    if job_id is None and isinstance(data, dict):
        raw_job = data.get("job_id")
        if raw_job is not None:
            job_id = str(raw_job)

    if not customer_email:
        print("No customer email in invoice payload.")
        return {"error": "Customer email missing"}

    credentials = resolve_ghl_credentials_for_invoice(data=data, job_id=job_id)
    if not credentials:
        print("No GHL credentials resolved for invoice (location/job).")
        return {"error": "GHL account credentials not found for this job or location"}

    print(f"📍 [INVOICE] Using GHL account location_id={credentials.location_id}")

    # Search contact
    contacts = search_ghl_contact(credentials.access_token, customer_email, credentials.location_id)
    if not contacts:
        print(f"No GHL contact found for email: {customer_email}")
        return {"error": f"Contact not found for {customer_email}"}

    contact_id = contacts[0].get("id") or contacts[0].get("_id")

    companyName = contacts[0].get("companyName")
    phoneNo = contacts[0].get("phone")
    contactName = contacts[0].get("contactName")
    address = {
        "address1": contacts[0].get("address1"),
        "city": contacts[0].get("city"),
        "state": contacts[0].get("state"),
        "postalCode": contacts[0].get("postalCode"),
        "country": contacts[0].get("country"),
    }

    print("companyName", companyName)
    tags = contacts[0].get("tags")
    if not contact_id:
        print("Contact found, but ID missing.")
        return {"error": "Invalid contact data"}
    
    print("Contact found,", contact_id)
    invoice_name = f"Invoice for {customer_name or customer_email} - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"

    response = create_invoice(
        name=invoice_name,
        contact_id=contact_id,
        services=services,
        credentials=credentials,
        customer_address=customer_address,
        address=address,
        companyName=companyName,
        phoneNo=phoneNo,
        contactName=contactName,
    )

    print("Invoice response:", response)
    print("Tags before check:", tags)
    if response and not response.get("error"):
        invoice_id = response.get("_id")

        existing_tags = tags if isinstance(tags, list) else []
        print("Existing tags:", existing_tags)

        invoice_sent = False
        try:
            if "card authorized" not in [t.lower() for t in existing_tags]:
                print("Card not authorized → sending invoice...")
                send_resp = send_invoice(invoice_id, credentials=credentials)
                print("Send invoice response:", send_resp)
                invoice_sent = bool(send_resp and not (isinstance(send_resp, dict) and send_resp.get("error")))
            else:
                print("Card authorized → skipping invoice send.")
                send_resp = "skipped"
        except Exception as e:
            print("Error sending invoice:", e)
            send_resp = None

        if job_id and invoice_id:
            try:
                save_job_invoice_info(job_id, invoice_id, invoice_sent=invoice_sent)
                print(f"Invoice saved to job {job_id}: id={invoice_id}, sent={invoice_sent}")
            except Exception as e:
                print(f"Error saving invoice to job {job_id}: {str(e)}")

        updated_tags = list(set(existing_tags + ["Invoice Created"]))
        payload = {"tags": updated_tags}
        update_resp = update_contact(contact_id, payload, credentials=credentials)
        print("Contact update response:", update_resp)

        return {
            "invoice": response,
            "contact_update": update_resp,
            "invoice_send": send_resp
        }

    return response


def _mark_job_completion_processed(job_id):
    """Helper function to mark job as completion processed"""
    try:
        Job.objects.filter(id=job_id).update(completion_processed=True)
    except Exception as e:
        print(f"Error marking job {job_id} as processed: {str(e)}")


@shared_task
def handle_webhook_event(data):
    try:
        return _process_invoice_payload(data)
    except Exception as e:
        print(f"Error handling webhook event: {str(e)}")
        return {"error": str(e)}


@shared_task
def handle_completed_job_invoice(job_id):
    try:
        job = (
            Job.objects.select_related(
                "account",
                "contact",
                "submission__contact",
                "submission__location",
            )
            .prefetch_related("items__service")
            .filter(id=job_id)
            .first()
        )
        if not job:
            return {"error": f"Job {job_id} not found"}

        payload = build_invoice_payload_from_job(job)
        result = _process_invoice_payload(payload, job_id=str(job_id))
        
        # Mark job as processed only if invoice was successfully created
        if result and not result.get("error"):
            _mark_job_completion_processed(job_id)
        
        return result
    except Exception as e:
        print(f"Error handling completed job invoice: {str(e)}")
        return {"error": str(e)}


@shared_task
def send_job_completion_webhook(job_id):
    """
    Send job completion webhook to external API when location_id matches.
    """
    print(f"🚀 [START] send_job_completion_webhook | job_id={job_id}")

    try:
        print("🔍 Fetching job with related submission, contact, items, and services")

        job = (
            Job.objects.select_related("submission__contact", "submission__location", "contact")
            .prefetch_related("items__service")
            .filter(id=job_id)
            .first()
        )

        if not job:
            print(f"❌ Job not found | job_id={job_id}")
            return {"error": f"Job {job_id} not found"}

        print(f"✅ Job found | id={job.id} | status={job.status}")

        # --------------------------------------------------
        # Resolve location_id
        # --------------------------------------------------
        location_id = None

        if job.submission and job.submission.contact:
            location_id = job.submission.contact.location_id
            print(f"📍 Location ID from submission contact: {location_id}")
        else:
            print("⚠️ No submission/contact found, falling back to credentials")
            credentials = GHLAuthCredentials.objects.first()
            if credentials:
                location_id = credentials.location_id
                print(f"📍 Location ID from credentials: {location_id}")
            else:
                print("❌ No GHL credentials found")

        if not location_id:
            print("❌ Location ID could not be resolved")
            return {"error": "Location ID not found in job submission contact or credentials"}

        # --------------------------------------------------
        # Validate location_id
        # --------------------------------------------------
        REQUIRED_LOCATION_ID = "b8qvo7VooP3JD3dIZU42"
        print(f"🔎 Validating location_id | required={REQUIRED_LOCATION_ID} | found={location_id}")

        if location_id != REQUIRED_LOCATION_ID:
            print("⛔ Location ID mismatch — webhook will not be sent")
            return {
                "error": f"Location ID {location_id} does not match required location"
            }

        # --------------------------------------------------
        # Build selected services
        # --------------------------------------------------
        print("🛠️ Building selected_services payload")
        selected_services = []

        for item in job.items.all():
            service_data = {
                "id": str(item.service.id) if item.service else None,
                "name": (
                    item.service.name
                    if item.service
                    else item.custom_name or "Custom Service"
                ),
                "price": float(item.price)
            }
            selected_services.append(service_data)
            print(f"   ➕ Added service: {service_data}")

        trip_amount = trip_surcharge_amount_for_job(job)
        if trip_amount > Decimal("0.00"):
            trip_line = {
                "id": None,
                "name": "Trip Surcharge",
                "price": float(trip_amount),
            }
            selected_services.append(trip_line)
            print(f"   ➕ Added trip surcharge line: {trip_line}")

        # --------------------------------------------------
        # Resolve GHL contact id
        # --------------------------------------------------
        ghl_contact_id = (job.ghl_contact_id or "").strip()
        if not ghl_contact_id and job.contact:
            ghl_contact_id = (job.contact.contact_id or "").strip()
        if not ghl_contact_id and job.submission and job.submission.contact:
            ghl_contact_id = (job.submission.contact.contact_id or "").strip()

        if ghl_contact_id:
            print(f"👤 GHL contact ID resolved: {ghl_contact_id}")
        else:
            print("⚠️ No GHL contact ID found on job, linked contact, or submission")

        # --------------------------------------------------
        # Build webhook payload
        # --------------------------------------------------
        payload = {
            "customer_email": job.customer_email or "",
            "selected_services": selected_services,
            "location_id": location_id,
            "job_id": job_id
        }

        if ghl_contact_id:
            payload["ghl_contact_id"] = ghl_contact_id

        if getattr(job, 'discount_type', None) and (float(job.discount_value or 0) > 0):
            payload["discount"] = {
                "value": float(job.discount_value),
                "type": job.discount_type
            }

        if job.customer_name:
            payload["customer_name"] = job.customer_name

        if job.customer_address:
            payload["customer_address"] = job.customer_address

        print("📦 Final webhook payload:")
        print(payload)

        # --------------------------------------------------
        # Validate required fields
        # --------------------------------------------------
        if not payload.get("customer_email"):
            print("❌ Validation failed: customer_email is missing")
            return {"error": "customer_email is required"}

        if not payload.get("selected_services"):
            print("❌ Validation failed: selected_services is empty")
            return {"error": "selected_services is required"}

        # --------------------------------------------------
        # Send webhook
        # --------------------------------------------------
        url = "https://workorder.theservicepilot.com/api/webhook/"
        # url = "http://localhost:8000/api/webhook/"
        headers = {"Content-Type": "application/json"}

        print(f"🌐 Sending POST request to {url}")
        response = requests.post(url, json=payload, headers=headers, timeout=30)

        print(f"📨 Webhook response status: {response.status_code}")
        print(f"📨 Webhook response body: {response.text}")

        # --------------------------------------------------
        # Handle response
        # --------------------------------------------------
        if response.status_code in [200, 201]:
            print(f"✅ Webhook sent successfully | job_id={job_id}")
            
            # Extract invoice URL/ID from response
            invoice_url = None
            try:
                response_data = response.json() if response.content else {}
                print(f"📋 Webhook response data: {response_data}")

                invoice_ref = _extract_invoice_reference_data(response_data)
                ghl_invoice_id = invoice_ref["ghl_invoice_id"]
                invoice_url = invoice_ref["invoice_url"] or None

                if ghl_invoice_id:
                    save_job_invoice_info(
                        job_id,
                        ghl_invoice_id,
                        invoice_sent=True,
                        invoice_url=invoice_url,
                    )
                    print(
                        f"✅ Invoice saved to job {job_id}: "
                        f"ghl_invoice_id={ghl_invoice_id}, invoice_url={invoice_url}"
                    )
                elif invoice_url:
                    Job.objects.filter(id=job_id).update(invoice_url=invoice_url)
                    print(f"✅ Invoice URL saved to job {job_id}: {invoice_url}")
                else:
                    print("⚠️ No invoice ID/URL found in webhook response")
            except Exception as e:
                print(f"⚠️ Error extracting invoice URL from response: {str(e)}")
            
            Job.objects.filter(id=job_id).update(completion_processed=True)
            print("✅ Job marked as completion_processed=True")

            return {
                "success": True,
                "status_code": response.status_code,
                "response": response_data,
                "invoice_url": invoice_url
            }

        print("❌ Webhook failed — will allow retry")
        return {
            "error": f"Webhook API returned status {response.status_code}",
            "status_code": response.status_code,
            "response": response.text
        }

    except requests.exceptions.RequestException as e:
        print(f"🚨 Request exception occurred: {str(e)}")
        return {"error": f"Request error: {str(e)}"}

    except Exception as e:
        print(f"🔥 Unexpected error occurred: {str(e)}")
        return {"error": str(e)}

    finally:
        print(f"🏁 [END] send_job_completion_webhook | job_id={job_id}")