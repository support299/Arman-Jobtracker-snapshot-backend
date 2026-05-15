# models.py
from django.db import models
from django.utils import timezone
import uuid
from django.contrib.postgres.fields import ArrayField, JSONField


class GHLAuthCredentials(models.Model):
    user_id = models.CharField(max_length=255, unique=True)
    access_token = models.TextField()
    refresh_token = models.TextField()
    expires_in = models.IntegerField()
    scope = models.TextField(null=True, blank=True)
    user_type = models.CharField(max_length=50, null=True, blank=True)
    company_id = models.CharField(max_length=255, null=True, blank=True)
    location_id = models.CharField(max_length=255, null=True, blank=True)
    timezone = models.CharField(max_length=100, null=True, blank=True, default="America/Chicago")
    company_name = models.CharField(
        max_length=255,
        blank=True,
        null=True,
        help_text="Business name shown on GHL invoices for this account/location.",
    )
    company_logo_url = models.URLField(
        max_length=500,
        blank=True,
        null=True,
        help_text="Public URL for logo shown on GHL invoices (same bucket CDN URLs work well).",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    is_active = models.BooleanField(default=True)

    def __str__(self):
        return f"{self.user_id} - {self.company_id}"



class GHLCompanyAuth(models.Model):
    """
    Company-level (agency-wide) GHL OAuth credentials, captured at OAuth time so we can
    later call /oauth/installedLocations to discover sub-accounts added to the marketplace
    install AFTER the original OAuth (which only captures what was installed at that moment).

    One row per GHL company_id. Saved during the bulk install branch in core.views.tokens.
    """
    company_id = models.CharField(max_length=255, primary_key=True)
    access_token = models.TextField()
    refresh_token = models.TextField(blank=True, default="")
    expires_in = models.IntegerField(default=0)
    scope = models.TextField(blank=True, default="")
    user_id = models.CharField(max_length=255, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"GHLCompanyAuth: {self.company_id}"
    

class GHLCustomField(models.Model):
    """Store custom field names and their GHL IDs for each account"""
    FIELD_TYPE_CHOICES = [
        ('text', 'Text'),
        ('url', 'URL'),
        ('number', 'Number'),
        ('date', 'Date'),
        ('dropdown', 'Dropdown'),
        ('checkbox', 'Checkbox'),
    ]
    
    account = models.ForeignKey(
        GHLAuthCredentials,
        on_delete=models.CASCADE,
        related_name='custom_fields',
        help_text="The account this custom field belongs to"
    )
    field_name = models.CharField(
        max_length=255,
        help_text="Human-readable name of the custom field (e.g., 'Quote URL', 'Invoice URL')"
    )
    ghl_field_id = models.CharField(
        max_length=255,
        help_text="The GHL custom field ID used in API calls"
    )
    field_type = models.CharField(
        max_length=50,
        choices=FIELD_TYPE_CHOICES,
        default='text',
        help_text="Type of the custom field"
    )
    description = models.TextField(
        blank=True,
        null=True,
        help_text="Optional description of what this field is used for"
    )
    is_active = models.BooleanField(
        default=True,
        help_text="Whether this custom field mapping is currently active"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    class Meta:
        db_table = 'ghl_custom_fields'
        unique_together = ['account', 'ghl_field_id']
        ordering = ['field_name']
        indexes = [
            models.Index(fields=['account', 'is_active']),
            models.Index(fields=['ghl_field_id']),
        ]
    
    def __str__(self):
        return f"{self.account.user_id} - {self.field_name} ({self.ghl_field_id})"


class GHLMediaStorage(models.Model):
    """Store GHL media storage name and GHL ID linked to GHLAuthCredentials (location)"""
    credentials = models.ForeignKey(
        GHLAuthCredentials,
        on_delete=models.CASCADE,
        related_name='media_storages',
        help_text="The GHL credentials (location) this media storage belongs to"
    )
    name = models.CharField(
        max_length=255,
        help_text="Display name of the media storage"
    )
    ghl_id = models.CharField(
        max_length=255,
        help_text="The GHL media storage ID used in API calls"
    )
    location_id = models.CharField(
        max_length=255,
        blank=True,
        null=True,
        help_text="GHL location ID (often same as credentials.location_id)"
    )
    is_active = models.BooleanField(
        default=True,
        help_text="Whether this media storage mapping is currently active"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'ghl_media_storages'
        unique_together = ['credentials', 'ghl_id']
        ordering = ['name']
        indexes = [
            models.Index(fields=['credentials', 'is_active']),
            models.Index(fields=['ghl_id']),
        ]

    def __str__(self):
        return f"{self.credentials.user_id} - {self.name} ({self.ghl_id})"


class Contact(models.Model):
    account = models.ForeignKey(
        GHLAuthCredentials,
        on_delete=models.CASCADE,
        related_name='contacts',
        null=True,
        blank=True,
        help_text='GHL account this contact belongs to (for multi-account onboarding)',
    )
    contact_id = models.CharField(max_length=100, unique=True)
    first_name = models.CharField(max_length=100, blank=True, null=True)
    last_name = models.CharField(max_length=100, blank=True, null=True)
    phone = models.CharField(max_length=15, blank=True, null=True)
    email = models.EmailField(blank=True, null=True)
    dnd = models.BooleanField(default=False)
    country = models.CharField(max_length=50, blank=True, null=True)
    company_name = models.CharField(max_length=100, blank=True, null=True)
    date_added = models.DateTimeField(blank=True, null=True)
    tags = models.JSONField(default=list, blank=True)
    custom_fields = models.JSONField(default=list, blank=True)
    location_id = models.CharField(max_length=100)
    timestamp = models.DateTimeField(blank=True, null=True)

    def __str__(self):
        return f"{self.first_name} {self.last_name} ({self.email}) - {self.contact_id}"
    

class Webhook(models.Model):
    event = models.CharField(max_length=100)
    company_id = models.CharField(max_length=100)
    payload = models.JSONField()  # Store the entire raw payload
    received_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.event} - {self.company_id}"
    

class Address(models.Model):
    PROPERTY_TYPE_CHOICES = [
        ('residential', 'Residential'),
        ('commercial', 'Commercial'),
    ]
    contact = models.ForeignKey(Contact, on_delete=models.CASCADE, related_name='contact_location')
    address_id = models.CharField(max_length=500)
    name = models.CharField(max_length=100, blank=True, null=True, help_text="e.g. Home, Office, etc.")
    order = models.PositiveIntegerField(default=0, help_text="Order of this location for the contact")
    state = models.CharField(max_length=100, blank=True, null=True)
    street_address = models.CharField(max_length=255, blank=True, null=True)
    city = models.CharField(max_length=100, blank=True, null=True)
    postal_code = models.CharField(max_length=20, blank=True, null=True)
    gate_code = models.CharField(max_length=20, blank=True, null=True)
    number_of_floors = models.PositiveIntegerField(blank=True, null=True)
    property_sqft = models.PositiveIntegerField(blank=True, null=True)
    property_type = models.CharField(max_length=20, choices=PROPERTY_TYPE_CHOICES, blank=True, null=True)

    
    def get_full_address(self):
        """Returns a single string of the full address."""
        parts = [self.street_address, self.city, self.state, self.postal_code]
        # Filter out None or empty values and join with a comma and space
        return ', '.join(filter(None, parts))

    

    def __str__(self):
        return f"{self.street_address}, {self.city}, {self.state}"


class Calendar(models.Model):
    """Store GHL calendar information"""
    ghl_calendar_id = models.CharField(max_length=255, unique=True, help_text="GHL calendar ID")
    account = models.ForeignKey(
        GHLAuthCredentials,
        on_delete=models.CASCADE,
        related_name='calendars',
        null=True,
        blank=True,
        help_text="The GHL account this calendar belongs to"
    )
    name = models.CharField(max_length=255, help_text="Calendar name")
    description = models.TextField(blank=True, null=True, help_text="Calendar description")
    widget_type = models.CharField(max_length=50, blank=True, null=True, help_text="Widget type (e.g., 'default')")
    calendar_type = models.CharField(max_length=50, blank=True, null=True, help_text="Calendar type (e.g., 'round_robin')")
    widget_slug = models.CharField(max_length=255, blank=True, null=True, help_text="Widget slug for the calendar")
    group_id = models.CharField(max_length=255, blank=True, null=True, help_text="Group ID if calendar belongs to a group")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'ghl_calendars'
        ordering = ['name']
        indexes = [
            models.Index(fields=['account']),
            models.Index(fields=['ghl_calendar_id']),
        ]

    def __str__(self):
        return f"{self.name} ({self.ghl_calendar_id})"


class GHLLocationIndex(models.Model):
    """Store location index mappings (parentId to order) for each GHL account"""
    account = models.ForeignKey(
        GHLAuthCredentials,
        on_delete=models.CASCADE,
        related_name='location_indices',
        help_text="The GHL account this location index belongs to"
    )
    parent_id = models.CharField(
        max_length=255,
        help_text="GHL custom field parent ID (e.g., 'address_0', 'QmYk134LkK2hownvL1sE')"
    )
    order = models.PositiveIntegerField(
        help_text="Order/position of this location (0, 1, 2, etc.)"
    )
    name = models.CharField(
        max_length=255,
        blank=True,
        null=True,
        help_text="Optional name/description for this location index"
    )
    is_active = models.BooleanField(
        default=True,
        help_text="Whether this location index mapping is currently active"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'ghl_location_indices'
        unique_together = ['account', 'parent_id']
        ordering = ['account', 'order']
        indexes = [
            models.Index(fields=['account', 'is_active']),
            models.Index(fields=['account', 'order']),
            models.Index(fields=['parent_id']),
        ]

    def __str__(self):
        return f"{self.account.user_id} - {self.parent_id} (Order: {self.order})"


class Location(models.Model):
    COUNTRY_CHOICES = [
        ('AE', 'United Arab Emirates'),
        ('AR', 'Argentina'),
        ('AT', 'Austria'),
        ('AU', 'Australia'),
        ('BE', 'Belgium'),
        ('BR', 'Brazil'),
        ('CA', 'Canada'),
        ('CH', 'Switzerland'),
        ('CL', 'Chile'),
        ('CN', 'China'),
        ('CO', 'Colombia'),
        ('CZ', 'Czech Republic'),
        ('DE', 'Germany'),
        ('DK', 'Denmark'),
        ('ES', 'Spain'),
        ('FI', 'Finland'),
        ('FR', 'France'),
        ('GB', 'United Kingdom'),
        ('HK', 'Hong Kong'),
        ('ID', 'Indonesia'),
        ('IE', 'Ireland'),
        ('IN', 'India'),
        ('IT', 'Italy'),
        ('JP', 'Japan'),
        ('KR', 'South Korea'),
        ('KW', 'Kuwait'),
        ('MX', 'Mexico'),
        ('MY', 'Malaysia'),
        ('NL', 'Netherlands'),
        ('NO', 'Norway'),
        ('NZ', 'New Zealand'),
        ('OM', 'Oman'),
        ('PE', 'Peru'),
        ('PH', 'Philippines'),
        ('PL', 'Poland'),
        ('PT', 'Portugal'),
        ('QA', 'Qatar'),
        ('SA', 'Saudi Arabia'),
        ('SE', 'Sweden'),
        ('SG', 'Singapore'),
        ('TH', 'Thailand'),
        ('TW', 'Taiwan'),
        ('US', 'United States'),
        ('VN', 'Vietnam'),
        ('ZA', 'South Africa'),
    ]

    id = models.CharField(primary_key=True, max_length=50)
    company_id = models.CharField(max_length=50)
    name = models.CharField(max_length=255)
    address = models.CharField(max_length=255)
    city = models.CharField(max_length=100)
    state = models.CharField(max_length=100)
    country = models.CharField(max_length=10)
    postal_code = models.CharField(max_length=20)
    website = models.CharField(max_length=255, null=True, blank=True)
    timezone = models.CharField(max_length=100)
    first_name = models.CharField(max_length=100)
    last_name = models.CharField(max_length=100)
    email = models.EmailField()
    phone = models.CharField(max_length=20)
    automatic_mobile_app_invite = models.BooleanField(default=False)
    date_added = models.DateTimeField()
    domain = models.CharField(max_length=255)
    is_active = models.BooleanField(default=True)

    class Meta:
        db_table = 'ghl_api_locations'

    def get_country_display(self):
        """Label for serializers/admin; unknown codes fall back to the stored value."""
        raw = (self.country or "").strip()
        if not raw:
            return ""
        return dict(self.COUNTRY_CHOICES).get(raw.upper(), raw)

    def save(self, *args, **kwargs):
        if self.country:
            self.country = self.country.strip().upper()
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.name} "
