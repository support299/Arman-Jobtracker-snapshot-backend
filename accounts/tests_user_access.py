"""Tests for multi-account location access and agency users."""
from django.test import SimpleTestCase, TestCase

from accounts.models import GHLAuthCredentials
from accounts.user_access import (
    extract_ghl_user_metadata,
    ghl_user_is_agency,
    user_can_access_location,
)
from service_app.models import User


class GhlMetadataTests(SimpleTestCase):
    def test_extract_agency_metadata(self):
        payload = {
            "roles": {
                "type": "agency",
                "role": "admin",
                "locationIds": ["loc-a", "loc-b"],
                "restrictSubAccount": True,
            },
            "companyId": "company-1",
        }
        meta = extract_ghl_user_metadata(payload)
        self.assertEqual(meta["ghl_user_type"], "agency")
        self.assertTrue(meta["ghl_restrict_sub_account"])
        self.assertEqual(meta["ghl_location_ids"], ["loc-a", "loc-b"])
        self.assertEqual(meta["ghl_company_id"], "company-1")
        self.assertTrue(ghl_user_is_agency(payload))


class UserAccessTests(TestCase):
    def setUp(self):
        self.account_a = GHLAuthCredentials.objects.create(
            user_id="u1",
            access_token="tok",
            refresh_token="ref",
            expires_in=3600,
            location_id="loc-a",
            company_id="company-1",
            is_active=True,
        )
        self.account_b = GHLAuthCredentials.objects.create(
            user_id="u2",
            access_token="tok2",
            refresh_token="ref2",
            expires_in=3600,
            location_id="loc-b",
            company_id="company-1",
            is_active=True,
        )
        self.worker = User.objects.create_user(
            username="worker@example.com",
            email="worker@example.com",
            password="pass",
            role=User.ROLE_WORKER,
            account=self.account_a,
        )
        self.agency = User.objects.create_user(
            username="agency@example.com",
            email="agency@example.com",
            password="pass",
            role=User.ROLE_AGENCY,
            ghl_user_type="agency",
            ghl_company_id="company-1",
        )

    def test_account_user_only_accesses_own_location(self):
        self.assertTrue(user_can_access_location(self.worker, "loc-a"))
        self.assertFalse(user_can_access_location(self.worker, "loc-b"))

    def test_agency_user_accesses_onboarded_locations(self):
        self.assertTrue(user_can_access_location(self.agency, "loc-a"))
        self.assertTrue(user_can_access_location(self.agency, "loc-b"))

    def test_restricted_agency_user_limited_to_location_ids(self):
        restricted = User.objects.create_user(
            username="restricted@example.com",
            email="restricted@example.com",
            password="pass",
            role=User.ROLE_AGENCY,
            ghl_user_type="agency",
            ghl_company_id="company-1",
            ghl_restrict_sub_account=True,
            ghl_location_ids=["loc-a"],
        )
        self.assertTrue(user_can_access_location(restricted, "loc-a"))
        self.assertFalse(user_can_access_location(restricted, "loc-b"))
