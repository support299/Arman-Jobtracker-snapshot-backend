from django.contrib import admin

from .models import (
    GHLLocationIndex,
    GHLAuthCredentials,
    Address,
    Contact,
    Calendar,
    GHLCompanyAuth,
    Location,
)


@admin.register(GHLAuthCredentials)
class GHLAuthCredentialsAdmin(admin.ModelAdmin):
    list_display = ('user_id', 'location_id', 'company_name', 'company_id')
    search_fields = ('user_id', 'location_id', 'company_name', 'company_id')
@admin.register(Location)
class LocationAdmin(admin.ModelAdmin):
    """GHL location snapshots synced via LocationServices / pull_ghl_locations."""

    list_display = (
        "name",
        "country_label",
        "currency",
        "city",
        "state",
        "timezone",
        "company_id",
        "email",
        "is_active",
        "date_added",
    )
    list_display_links = ("name",)
    list_filter = ("country", "timezone", "automatic_mobile_app_invite", "is_active")
    search_fields = (
        "id",
        "name",
        "email",
        "phone",
        "city",
        "address",
        "company_id",
        "domain",
    )
    ordering = ("name",)
    readonly_fields = ("id", "date_added", "currency")
    date_hierarchy = "date_added"
    save_on_top = True

    fieldsets = (
        (
            "Identity",
            {
                "fields": ("id", "company_id", "name", "domain", "date_added", "is_active"),
            },
        ),
        (
            "Address",
            {
                "fields": (
                    "address",
                    "city",
                    "state",
                    "country",
                    "currency",
                    "postal_code",
                    "timezone",
                ),
                "description": "Country is stored as ISO code (e.g. US). Labels shown in list via country column.",
            },
        ),
        (
            "Primary contact",
            {
                "fields": (
                    "first_name",
                    "last_name",
                    "email",
                    "phone",
                    "website",
                    "automatic_mobile_app_invite",
                ),
            },
        ),
    )

    @admin.display(description="Country", ordering="country")
    def country_label(self, obj):
        return obj.get_country_display() if obj else ""


# Register your models here.
admin.site.register(GHLLocationIndex)
admin.site.register(GHLCompanyAuth)
admin.site.register(Address)
admin.site.register(Contact)
admin.site.register(Calendar)