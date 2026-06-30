from unittest.mock import Mock, patch

import requests
from django.test import TestCase

from accounts.ghl_custom_fields import (
    REQUIRED_APP_CUSTOM_FIELDS,
    ensure_app_custom_fields,
)


class EnsureAppCustomFieldsTests(TestCase):
    @patch("accounts.utils.fetch_location_custom_fields")
    @patch("accounts.ghl_custom_fields.requests.post")
    def test_creates_only_missing_fields(self, post_mock, fetch_mock):
        existing_field_id = "existing-quote-link-id"
        fetch_mock.return_value = {
            existing_field_id: {
                "name": "Quote Link",
                "fieldKey": "contact.quote_link",
                "parentId": None,
            }
        }

        created_ids = iter(
            f"created-field-{index}"
            for index in range(len(REQUIRED_APP_CUSTOM_FIELDS) - 1)
        )

        def post_side_effect(*args, **kwargs):
            payload = kwargs.get("json") or {}
            response = Mock()
            response.raise_for_status = Mock()
            response.json.return_value = {
                "customField": {
                    "id": next(created_ids),
                    "name": payload.get("name"),
                }
            }
            return response

        post_mock.side_effect = post_side_effect

        summary = ensure_app_custom_fields("loc-1", "token-1")

        self.assertIn("Quote Link", summary["skipped_existing"])
        self.assertEqual(len(summary["created"]), len(REQUIRED_APP_CUSTOM_FIELDS) - 1)
        self.assertEqual(summary["failed"], [])
        self.assertEqual(post_mock.call_count, len(REQUIRED_APP_CUSTOM_FIELDS) - 1)

        first_call_payload = post_mock.call_args_list[0].kwargs["json"]
        self.assertEqual(first_call_payload["model"], "contact")
        self.assertEqual(first_call_payload["name"], "Quote Value")
        self.assertEqual(first_call_payload["dataType"], "TEXT")

    @patch("accounts.utils.fetch_location_custom_fields")
    @patch("accounts.ghl_custom_fields.requests.post")
    def test_skips_all_when_fields_already_exist(self, post_mock, fetch_mock):
        fetch_mock.return_value = {
            f"field-{index}": {
                "name": field_def["field_name"],
                "fieldKey": f"contact.{field_def['field_name'].lower().replace(' ', '_')}",
                "parentId": None,
            }
            for index, field_def in enumerate(REQUIRED_APP_CUSTOM_FIELDS)
        }

        summary = ensure_app_custom_fields("loc-1", "token-1")

        self.assertEqual(summary["created"], [])
        self.assertEqual(len(summary["skipped_existing"]), len(REQUIRED_APP_CUSTOM_FIELDS))
        self.assertEqual(summary["failed"], [])
        post_mock.assert_not_called()

    @patch("accounts.utils.fetch_location_custom_fields")
    @patch("accounts.ghl_custom_fields.requests.post")
    def test_continues_when_one_create_fails(self, post_mock, fetch_mock):
        fetch_mock.return_value = {}

        def post_side_effect(*args, **kwargs):
            payload = kwargs.get("json") or {}
            response = Mock()
            if payload.get("name") == "Quote Value":
                response.raise_for_status.side_effect = requests.HTTPError(
                    response=Mock(status_code=400, text="permission denied")
                )
                return response
            response.raise_for_status = Mock()
            response.json.return_value = {
                "customField": {
                    "id": f"id-{payload.get('name')}",
                    "name": payload.get("name"),
                }
            }
            return response

        post_mock.side_effect = post_side_effect

        summary = ensure_app_custom_fields("loc-1", "token-1")

        self.assertIn("Quote Link", summary["created"])
        self.assertEqual(
            summary["failed"],
            [{"field_name": "Quote Value", "error": "permission denied"}],
        )
        self.assertEqual(post_mock.call_count, len(REQUIRED_APP_CUSTOM_FIELDS))

    @patch("accounts.ghl_custom_fields.ensure_app_custom_fields")
    @patch("accounts.utils.fetch_location_custom_fields")
    def test_sync_custom_fields_to_db_runs_ensure_first(self, fetch_mock, ensure_mock):
        from accounts.models import GHLAuthCredentials, GHLCustomField
        from accounts.utils import sync_custom_fields_to_db

        credentials = GHLAuthCredentials.objects.create(
            user_id="user-1",
            access_token="token",
            refresh_token="refresh",
            expires_in=3600,
            scope="scope",
            location_id="loc-sync",
            company_id="company-1",
        )
        ensure_mock.return_value = {"created": ["Quote Link"], "skipped_existing": [], "failed": []}
        fetch_mock.return_value = {
            "field-1": {
                "name": "Quote Link",
                "fieldKey": "contact.quote_link",
                "parentId": None,
            }
        }

        result = sync_custom_fields_to_db("loc-sync", "token")

        ensure_mock.assert_called_once_with("loc-sync", "token")
        fetch_mock.assert_called_once_with("loc-sync", "token")
        self.assertEqual(result["total"], 1)
        self.assertTrue(GHLCustomField.objects.filter(account=credentials, field_name="Quote Link").exists())
