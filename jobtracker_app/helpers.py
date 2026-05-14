import requests
from datetime import datetime
from zoneinfo import ZoneInfo

from accounts.models import GHLAuthCredentials, Contact
from jobtracker_app.models import Job


def get_or_create_product(access_token, location_id, product_name, custom_data=None):
    """
    Look up an existing product by name within the provided GHL location.
    If it does not exist, create a lightweight SERVICE product so the invoice
    payload can reference it.
    """
    headers = {
        'Accept': 'application/json',
        'Authorization': f'Bearer {access_token}',
        'Version': '2021-07-28'
    }

    search_url = (
        "https://services.leadconnectorhq.com/products/"
        f"?locationId={location_id}&search={product_name}"
    )

    try:
        response = requests.get(search_url, headers=headers)
        if response.status_code == 200:
            products = response.json().get('products', [])
            if products:
                product = products[0]
                return {
                    "productId": product.get('_id'),
                    "priceId": product.get("prices", [{}])[0].get("_id")
                }
    except Exception as exc:
        print(f"Error searching for product '{product_name}': {exc}")

    # Fallback: create the product so invoices can continue
    return create_product(access_token, location_id, product_name, custom_data or {})


def create_product(access_token, location_id, product_name, custom_data=None):
    headers = {
        'Accept': 'application/json',
        'Authorization': f'Bearer {access_token}',
        'Content-Type': 'application/json',
        'Version': '2021-07-28'
    }

    custom_data = custom_data or {}
    try:
        price = float(custom_data.get("price") or custom_data.get("Price") or 0)
    except (TypeError, ValueError):
        price = 0.0

    description = custom_data.get("description") or f"Auto-created product: {product_name}"
    slug = (
        product_name.lower()
        .replace(" ", "-")
        .replace("_", "-")
    )

    product_payload = {
        "name": product_name,
        "locationId": location_id,
        "description": description,
        "productType": "SERVICE",
        "availableInStore": True,
        "isTaxesEnabled": False,
        "isLabelEnabled": False,
        "slug": slug,
        "prices": [
            {
                "name": "Default",
                "type": "one_time",
                "amount": price,
                "currency": "USD",
            }
        ],
    }

    url = "https://services.leadconnectorhq.com/products/"

    try:
        response = requests.post(url, headers=headers, json=product_payload)
        print(response.json(), 'product_create_response')
        if response.status_code in (200, 201):
            product = response.json()
            out = {"productId": product.get("_id")}
            prices = product.get("prices") or []
            if prices and prices[0].get("_id"):
                out["priceId"] = prices[0]["_id"]
            return out
        print(f"Failed to create product {product_name}: {response.status_code} - {response.text}")
    except Exception as exc:
        print(f"Error creating product '{product_name}': {exc}")

    return None


def resolve_invoice_location_id_from_job(job):
    """
    Which GHL subaccount (location) owns this job — same precedence as
    jobtracker_app.signals._trigger_invoice_on_completion.
    """
    account = getattr(job, "account", None)
    if account and getattr(account, "location_id", None):
        return account.location_id
    contact = getattr(job, "contact", None)
    if contact and contact.location_id:
        return contact.location_id
    submission = getattr(job, "submission", None)
    if (
        submission
        and getattr(submission, "contact", None)
        and submission.contact.location_id
    ):
        return submission.contact.location_id
    if job.customer_email:
        contact_by_email = (
            Contact.objects.filter(email=job.customer_email)
            .exclude(location_id__isnull=True)
            .exclude(location_id="")
            .first()
        )
        if contact_by_email and contact_by_email.location_id:
            return contact_by_email.location_id
    return None


def resolve_ghl_credentials_for_invoice(data=None, job_id=None):
    """
    OAuth row to use for GHL invoice, contact search, product create, etc.

    Order: job from ``job_id`` (uses ``job.account`` or resolved location), then
    payload ``location_id`` (e.g. from external webhook without job row), then
    legacy ``GHLAuthCredentials.objects.first()``.
    """
    data = data or {}

    if job_id:
        job = (
            Job.objects.select_related(
                "account", "contact", "submission__contact"
            )
            .filter(id=job_id)
            .first()
        )
        if job:
            if getattr(job, "account_id", None) and job.account:
                return job.account
            loc = resolve_invoice_location_id_from_job(job)
            if loc:
                cred = GHLAuthCredentials.objects.filter(location_id=loc).first()
                if cred:
                    return cred
                print(f"⚠️ [INVOICE] No GHLAuthCredentials for job location_id={loc}")

    location_id = data.get("location_id")
    if location_id:
        cred = GHLAuthCredentials.objects.filter(location_id=location_id).first()
        if cred:
            return cred
        print(f"⚠️ [INVOICE] No GHLAuthCredentials for location_id={location_id}")

    print("⚠️ [INVOICE] Falling back to first GHLAuthCredentials (legacy)")
    return GHLAuthCredentials.objects.first()


def build_invoice_payload_from_job(job):
    """
    Construct the payload expected by the invoice flow based on a Job instance.
    Uses job.revised_total (total after discount) so the invoice reflects the
    amount the customer actually pays.
    """
    revised_total = float(job.revised_total) if hasattr(job, 'revised_total') else float(job.total_price or 0)
    items = job.items.all()
    services = []

    for item in items:
        name = None
        description = ""
        if item.service:
            name = item.service.name
            description = getattr(item.service, "description", "") or ""
        if not name:
            name = item.custom_name or job.title or "Service"
        description = description or job.description or ""

        services.append({
            "name": name,
            "description": description,
            "quantity": 1,
            "price": float(item.price or 0),
        })

    if not services:
        services.append({
            "name": job.title or "Service",
            "description": job.description or "",
            "quantity": 1,
            "price": revised_total,
        })
    else:
        # When job has a discount, invoice must show revised total. Use a single line.
        has_discount = (
            getattr(job, 'discount_type', None)
            and (float(job.discount_value or 0) > 0)
        )
        if has_discount:
            services = [{
                "name": job.title or "Service",
                "description": job.description or "",
                "quantity": 1,
                "price": revised_total,
            }]

    contact_email = job.customer_email
    contact_name = job.customer_name
    contact_phone = job.customer_phone
    company_name = None

    if job.ghl_contact_id and not contact_email:
        contact = Contact.objects.filter(contact_id=job.ghl_contact_id).first()
        if contact:
            contact_email = contact_email or contact.email
            contact_name = contact_name or f"{contact.first_name or ''} {contact.last_name or ''}".strip()
            contact_phone = contact_phone or contact.phone

    payload = {
        "customer_email": contact_email,
        "customer_name": contact_name,
        "customer_address": job.customer_address,
        "selected_services": services,
        "phone": contact_phone,
        "company_name": company_name,
    }

    location_id = resolve_invoice_location_id_from_job(job)
    if location_id:
        payload["location_id"] = location_id

    return payload


def update_contact(contact_id, data, credentials=None):
    url = f'https://services.leadconnectorhq.com/contacts/{contact_id}'
    if credentials is None:
        credentials = GHLAuthCredentials.objects.first()
    print(credentials, 'creee')

    headers = {
        'Authorization': f'Bearer {credentials.access_token}',
        'Content-Type': 'application/json',
        'Version':'2021-07-28'
    }

    try:
        response = requests.put(url, headers=headers, json=data)
        print(response.json(), 'responseeeeee')
        return response.json()
    except Exception as e:
        print(e, 'errorrr')
        return {'error':'Error while updating ghl contact'}


def search_ghl_contact(access_token, email, locationId):
    url = 'https://services.leadconnectorhq.com/contacts/'
    response = requests.get(
        url,
        headers={
            'Accept': 'application/json',
            'Authorization': f"Bearer {access_token}",
            'Version': '2021-07-28'
        },
        params={"query": email, "locationId": locationId}
    )
    print("Raw response:", response.status_code, response.text, response.json())
    return response.json().get("contacts", [])



def create_invoice(name, contact_id, services, credentials, customer_address, address, companyName, phoneNo, contactName):
    """
    Create an invoice in GHL for the given contact.

    Args:
        contact_id (str): GHL contact ID
        location_id (str): GHL location ID
        services (list): List of services (product objects)
        credentials: GHLAuthCredentials instance

    Returns:
        dict: Response from GHL API
    """
    url = "https://services.leadconnectorhq.com/invoices/"
    headers = {
        "Authorization": f"Bearer {credentials.access_token}",
        "Content-Type": "application/json",
        "Version": "2021-07-28"
    }

    contact = Contact.objects.filter(contact_id=contact_id).first()

    if not contact:
        return {"error": "Contact not found"}
    
    line_items = []

    for service in services:
        product_name = service.get("name", "Unnamed Service")
        print("Processing service:", product_name)  # DEBUG

        product_info = get_or_create_product(
            credentials.access_token,
            credentials.location_id,
            product_name,
            custom_data=service
        )
        if not product_info:
            print(f"Skipping service: {product_name} (no product info)")
            continue  # <-- change return to continue, so other services are still added

        line_item = {
            "name": product_name,
            "description": service.get("description", ""),
            "currency": "USD",
            "qty": service.get("quantity", 1),
            "amount": service.get("price", 0.0),
            "productId": product_info["productId"],
        }
        if product_info.get("priceId"):
            line_item["priceId"] = product_info["priceId"]

        if service.get("price", 0.0) > 0:
            line_item["taxes"] = [
                {
                    "_id": "sales-tax-8-25",
                    "name": "Sales Tax",
                    "rate": 8.25,
                    "calculation": "exclusive",
                    "description": "8.25% standard US sales tax"
                }
            ]

        line_items.append(line_item)

    print("Final line_items payload:", line_items)  # DEBUG

    discount= {
        "value":0,
        "type":'fixed' #percentage, fixed
    }

    contactDetails = {
        "id":contact_id,
        "name": contactName,
        "email": contact.email,
        "address":{"addressLine1":customer_address},
        "companyName": companyName,
        "phoneNo": phoneNo
    }

    businessDetails = {
        "logoUrl":'https://storage.googleapis.com/msgsndr/b8qvo7VooP3JD3dIZU42/media/683efc8fd5817643ff8194f0.jpeg',
        "name":"TruShine Window Cleaning",
    }

    sentTo = {
        "email":[contact.email]
    }

    issue_date = datetime.now(ZoneInfo("America/Chicago")).strftime("%Y-%m-%d")

    payload = {
        "altId": credentials.location_id,
        "altType":'location',
        "name": name,
        "businessDetails":businessDetails,
        "currency":"USD",
        "items": line_items,
        "discount":discount,
        "contactDetails":contactDetails,
        "issueDate":issue_date,
        "sentTo": sentTo,
        "liveMode":True,
        "tipsConfiguration":{
            "tipsEnabled": False,
            "tipsPercentage": []
        }
    }

    response = requests.post(url, headers=headers, json=payload)
    return response.json()

    

def send_invoice(invoiceId, credentials=None):
    url = f'https://services.leadconnectorhq.com/invoices/{invoiceId}/send'
    if credentials is None:
        credentials = GHLAuthCredentials.objects.first()
    
    headers = {
        'Authorization': f'Bearer {credentials.access_token}',
        'Version': '2021-07-28'
    }

    payload = {
        "altId": credentials.location_id,
        "altType":'location',
        "userId": credentials.user_id,
        "action":'email',
        "liveMode":True,
    }

    try:
        response = requests.post(url=url, headers=headers, json=payload)
        print('invoice_response', response.json())
        return response.json()
    except Exception as e:
        return {"error": str(e)}


def create_or_update_ghl_contact_from_job(job, location_id):
    """
    Create or update GHL contact from a Job when job is completed.
    This is called before sending the webhook to ensure contact exists in GHL.
    """
    try:
        print(f"🔹 [GHL CONTACT] Starting GHL contact sync for job {job.id}...")
        
        # Get credentials for the specific location
        try:
            credentials = GHLAuthCredentials.objects.get(location_id=location_id)
        except GHLAuthCredentials.DoesNotExist:
            print(f"❌ [GHL CONTACT] No GHLAuthCredentials found for location_id: {location_id}")
            return None
        except GHLAuthCredentials.MultipleObjectsReturned:
            print(f"⚠️ [GHL CONTACT] Multiple credentials found for location_id: {location_id}, using first")
            credentials = GHLAuthCredentials.objects.filter(location_id=location_id).first()
        
        token = credentials.access_token
        print(f"✅ [GHL CONTACT] Using token (truncated): {token[:10]}..., locationId: {location_id}")

        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {token}",
            "Version": "2021-07-28",
            "Content-Type": "application/json"
        }

        # Get contact information from job
        contact_email = job.customer_email
        contact_phone = job.customer_phone
        contact_name = job.customer_name
        
        if not contact_email and not contact_phone:
            print("❌ [GHL CONTACT] No email or phone found in job to search GHL contact.")
            return None

        # Try to get contact from submission if available
        ghl_contact_id = None
        if job.submission and job.submission.contact:
            ghl_contact_id = job.submission.contact.contact_id
            contact_email = contact_email or job.submission.contact.email
            contact_phone = contact_phone or job.submission.contact.phone
            contact_name = contact_name or f"{job.submission.contact.first_name} {job.submission.contact.last_name}".strip()
        
        # Also check if job has ghl_contact_id
        if not ghl_contact_id and job.ghl_contact_id:
            ghl_contact_id = job.ghl_contact_id

        # Step 1: Search for existing contact
        if ghl_contact_id:
            search_url = f"https://services.leadconnectorhq.com/contacts/{ghl_contact_id}"
            print(f"🔍 [GHL CONTACT] Searching by contact_id: {ghl_contact_id}")
        else:
            search_query = contact_email or contact_phone
            search_url = f"https://services.leadconnectorhq.com/contacts/?locationId={location_id}&query={search_query}"
            print(f"🔍 [GHL CONTACT] Searching by query: {search_query}")

        # Step 2: Fetch existing contact
        print(f"➡️ [GHL CONTACT] Sending GET request to {search_url}")
        search_response = requests.get(search_url, headers=headers)
        print(f"⬅️ [GHL CONTACT] Response [{search_response.status_code}]: {search_response.text}")

        if search_response.status_code != 200:
            print("❌ [GHL CONTACT] Failed to search GHL contact.")
            return None

        search_data = search_response.json()
        results = []

        # Handle both cases: list of contacts or single contact
        if "contacts" in search_data and isinstance(search_data["contacts"], list):
            results = search_data["contacts"]
            print(f"📋 [GHL CONTACT] Found {len(results)} contacts in search results.")
        elif "contact" in search_data and isinstance(search_data["contact"], dict):
            results = [search_data["contact"]]
            print("📋 [GHL CONTACT] Found 1 contact in search results.")
        elif isinstance(search_data, dict) and search_data.get("id"):
            results = [search_data]
            print("📋 [GHL CONTACT] Found 1 contact in search results (direct response).")
        else:
            print("ℹ️ [GHL CONTACT] No contacts found in GHL.")

        # Step 3: Update or create contact
        if results:
            ghl_contact_id = results[0].get("id") or results[0].get("_id")
            tags = results[0].get("tags", [])
            
            # Prepare update payload
            contact_payload = {}
            
            # Add job completed tag if not present
            if "job completed" not in [t.lower() for t in tags]:
                tags.append("job completed")
                contact_payload["tags"] = tags

            if contact_payload:
                print(f"✏️ [GHL CONTACT] Updating contact {ghl_contact_id} with payload: {contact_payload}")
                contact_response = requests.put(
                    f"https://services.leadconnectorhq.com/contacts/{ghl_contact_id}",
                    json=contact_payload,
                    headers=headers
                )
                print(f"⬅️ [GHL CONTACT] Update response [{contact_response.status_code}]: {contact_response.text}")
                
                if contact_response.status_code in [200, 201]:
                    print(f"✅ [GHL CONTACT] Contact updated successfully: {ghl_contact_id}")
                    return ghl_contact_id
        else:
            # Create new contact
            # Parse name if available
            first_name = ""
            last_name = ""
            if contact_name:
                name_parts = contact_name.split(maxsplit=1)
                first_name = name_parts[0] if name_parts else ""
                last_name = name_parts[1] if len(name_parts) > 1 else ""
            
            contact_payload = {
                "firstName": first_name,
                "email": contact_email,
                "phone": contact_phone,
                "locationId": location_id,
                "tags": ["job completed"]
            }
            
            if last_name:
                contact_payload["lastName"] = last_name
            
            print(f"➕ [GHL CONTACT] Creating new contact with payload: {contact_payload}")
            contact_response = requests.post(
                "https://services.leadconnectorhq.com/contacts/",
                json=contact_payload,
                headers=headers
            )
            
            print(f"⬅️ [GHL CONTACT] Create response [{contact_response.status_code}]: {contact_response.text}")

            if contact_response.status_code in [200, 201]:
                response_data = contact_response.json()
                ghl_contact_id = response_data.get("contact", {}).get("id") or response_data.get("id")
                print(f"✅ [GHL CONTACT] Contact created successfully: {ghl_contact_id}")
                return ghl_contact_id

        print("❌ [GHL CONTACT] Failed to create/update contact in GHL.")
        return None

    except Exception as e:
        print(f"🔥 [GHL CONTACT] Error syncing contact: {e}")
        return None


def link_jobs_to_contacts_by_ghl_id():
    """
    Find jobs that have no related Contact but do have ghl_contact_id,
    look up the Contact in the accounts Contact table by contact_id (= GHL ID),
    and set job.contact to that Contact.

    Returns:
        tuple: (jobs_updated_count, jobs_skipped_no_contact_in_db)
    """
    # Jobs with contact=None and non-empty ghl_contact_id
    jobs_missing_contact = Job.objects.filter(
        contact__isnull=True
    ).exclude(
        ghl_contact_id__isnull=True
    ).exclude(
        ghl_contact_id=""
    )
    
    print("jobs_missing_contact: ", jobs_missing_contact.count())
    ghl_ids = list(
        jobs_missing_contact.values_list("ghl_contact_id", flat=True).distinct()
    )
    if not ghl_ids:
        return 0, 0

    total_jobs = jobs_missing_contact.count()

    # Fetch matching Contacts by contact_id (GHL ID)
    contacts_by_ghl_id = {
        c.contact_id: c
        for c in Contact.objects.filter(contact_id__in=ghl_ids)
    }

    to_update = []
    for job in jobs_missing_contact:
        contact = contacts_by_ghl_id.get(job.ghl_contact_id)
        if contact:
            job.contact = contact
            to_update.append(job)

    if to_update:
        Job.objects.bulk_update(to_update, ["contact"], batch_size=500)

    linked = len(to_update)
    skipped = total_jobs - linked
    return linked, skipped