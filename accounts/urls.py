from django.urls import path
from rest_framework.routers import DefaultRouter

from accounts import api_views
from accounts.views import (
    auth_connect,
    tokens,
    callback,
    sync_all_contacts_and_address,
    webhook_handler,
    sync_all_users,
)

router = DefaultRouter()
router.register(
    "location-management/locations",
    api_views.GHLLocationManagementViewSet,
    basename="location-management-location",
)

urlpatterns = [
    path("auth/connect/", auth_connect, name="oauth_connect"),
    path("auth/tokens/", tokens, name="oauth_tokens"),
    path("auth/callback/", callback, name="oauth_callback"),
    path("sync_contacts/", sync_all_contacts_and_address, name="sync_contacts"),
    path("sync_users/", sync_all_users, name="sync_users"),
    path("webhook/", webhook_handler, name="webhook"),
] + router.urls