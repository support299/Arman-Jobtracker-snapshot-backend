"""DRF viewsets for accounts app (JWT + account scope)."""

from urllib.parse import quote, urlparse

from django.conf import settings as django_settings
from django.db import models
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import ValidationError
from rest_framework.response import Response

from accounts.models import Location
from accounts.permissions import AccountScopedPermission, IsSuperuserPermission
from accounts.serializers import GHLLocationManagementSerializer


class GHLLocationManagementViewSet(viewsets.ModelViewSet):
    """
    CRUD for GHL location rows (`accounts.Location`), scoped to the request account.

    Scope: rows where `company_id` matches `request.account.company_id`, or where
    `id` matches `request.account.location_id` when company_id is empty.

    DELETE `/:pk/` toggles `is_active` (soft deactivate/reactivate); it does not remove rows.

    POST .../onboard/ returns the GHL chooselocation OAuth URL.
    """

    serializer_class = GHLLocationManagementSerializer
    permission_classes = [AccountScopedPermission, IsSuperuserPermission]
    queryset = Location.objects.all()
    lookup_field = "pk"

    @staticmethod
    def _frontend_base_url(request):
        origin = (request.headers.get("Origin") or "").strip()
        if origin:
            return origin.rstrip("/")
        referer = request.headers.get("Referer") or ""
        if referer:
            parsed = urlparse(referer)
            if parsed.scheme and parsed.netloc:
                return f"{parsed.scheme}://{parsed.netloc}".rstrip("/")
        return ""

    @action(detail=False, methods=["post"], url_path="onboard")
    def onboard(self, request):
        base = self._frontend_base_url(request)
        if not base:
            return Response(
                {
                    "detail": (
                        "Could not determine frontend URL. Send an Origin header, "
                        "or a Referer header, when requesting the connect URL."
                    )
                },
                status=status.HTTP_400_BAD_REQUEST,
            )
        path = getattr(django_settings, "GHL_LOCATION_CONNECT_REDIRECT_PATH", "/api/accounts/auth/callback/") or "/"
        if not path.startswith("/"):
            path = "/" + path
        redirect_uri = f"{base}{path}"

        client_id = getattr(django_settings, "GHL_CLIENT_ID", "") or ""
        scope = getattr(django_settings, "GHL_OAUTH_SCOPE", "") or ""
        if not client_id or not scope:
            return Response(
                {
                    "detail": (
                        "GHL OAuth is not configured: set GHL_CLIENT_ID and "
                        "SCOPE in the environment."
                    )
                },
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )

        q_redirect = quote(redirect_uri, safe="")
        q_scope = quote(scope, safe="")
        auth_url = (
            "https://marketplace.gohighlevel.com/oauth/chooselocation?"
            "response_type=code&"
            f"redirect_uri={q_redirect}&"
            f"client_id={quote(client_id, safe='')}&"
            f"scope={q_scope}"
        )
        return Response({"auth_url": auth_url})

    def _scoped_queryset(self):
        account = getattr(self.request, "account", None)
        qs = Location.objects.all()
        if account is None:
            return qs.none()
        cid = (account.company_id or "").strip()
        lid = (account.location_id or "").strip()
        if cid:
            return qs.filter(company_id=cid)
        if lid:
            return qs.filter(pk=lid)
        return qs.none()

    def get_queryset(self):
        qs = self._scoped_queryset()
        if self.action == "list":
            search = self.request.query_params.get("search")
            if search:
                s = search.strip()
                qs = qs.filter(
                    models.Q(name__icontains=s)
                    | models.Q(address__icontains=s)
                    | models.Q(city__icontains=s)
                    | models.Q(email__icontains=s)
                    | models.Q(id__icontains=s)
                )
        return qs.order_by("name")


    def perform_destroy(self, instance):
        """Toggle `is_active` (DELETE does not remove the row)."""
        instance.is_active = not instance.is_active
        instance.save()
