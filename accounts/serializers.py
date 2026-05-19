"""DRF serializers for accounts app API surfaces."""

from django.utils import timezone
from rest_framework import serializers

from accounts.models import Location


class GHLLocationManagementSerializer(serializers.ModelSerializer):
    """`accounts.Location` — GHL location snapshot (see LocationServices / pull_ghl_locations)."""

    date_added = serializers.DateTimeField(required=False)
    company_id = serializers.CharField(max_length=50, required=False, allow_blank=True)
    is_active = serializers.BooleanField(required=False, default=True)
    currency = serializers.CharField(max_length=3, read_only=True)

    class Meta:
        model = Location
        fields = [
            "id",
            "company_id",
            "name",
            "address",
            "city",
            "state",
            "country",
            "currency",
            "postal_code",
            "website",
            "timezone",
            "first_name",
            "last_name",
            "email",
            "phone",
            "automatic_mobile_app_invite",
            "date_added",
            "domain",
            "is_active",
        ]

    def create(self, validated_data):
        if not validated_data.get("date_added"):
            validated_data["date_added"] = timezone.now()
        return super().create(validated_data)
