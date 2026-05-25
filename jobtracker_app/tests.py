from datetime import datetime, timedelta
from decimal import Decimal

from django.test import SimpleTestCase, TestCase
from django.utils import timezone
from rest_framework import status
from rest_framework.authtoken.models import Token
from rest_framework.test import APITestCase

from accounts.models import GHLAuthCredentials
from service_app.models import User
from .tasks import _extract_invoice_reference_data
from .helpers import save_job_invoice_info
from .models import Job, JobAssignment, JobServiceItem


class InvoiceReferenceExtractionTests(SimpleTestCase):
    def test_uses_direct_ghl_invoice_id_from_webhook_response(self):
        ref = _extract_invoice_reference_data(
            {
                "message": "Webhook received",
                "ghl_invoice_id": "6a149ba70560acdb3d887f62",
                "invoice_url": "http://localhost:5173/invoice/example",
                "invoice_token": "public-token",
            }
        )

        self.assertEqual(ref["ghl_invoice_id"], "6a149ba70560acdb3d887f62")
        self.assertEqual(ref["invoice_url"], "http://localhost:5173/invoice/example")

    def test_prefers_nested_ghl_invoice_id_over_public_uuid(self):
        ref = _extract_invoice_reference_data(
            {
                "id": "46779fb8-21b7-46db-bb4a-133596c6a1df",
                "invoice_url": "https://workorder.theservicepilot.com/invoice/46779fb8-21b7-46db-bb4a-133596c6a1df/",
                "invoice": {
                    "id": "46779fb8-21b7-46db-bb4a-133596c6a1df",
                    "invoice_id": "6a149ba70560acdb3d887f62",
                },
            }
        )

        self.assertEqual(ref["ghl_invoice_id"], "6a149ba70560acdb3d887f62")
        self.assertEqual(
            ref["invoice_url"],
            "https://workorder.theservicepilot.com/invoice/46779fb8-21b7-46db-bb4a-133596c6a1df/",
        )

    def test_invoice_token_is_not_used_as_ghl_invoice_id(self):
        ref = _extract_invoice_reference_data(
            {
                "message": "Webhook received",
                "invoice_token": "public-token-only",
                "invoice_url": "http://localhost:5173/invoice/example",
            }
        )

        self.assertEqual(ref["ghl_invoice_id"], "")
        self.assertEqual(ref["invoice_url"], "http://localhost:5173/invoice/example")

    def test_falls_back_to_public_uuid_only_for_invoice_url(self):
        ref = _extract_invoice_reference_data(
            {
                "id": "46779fb8-21b7-46db-bb4a-133596c6a1df",
            }
        )

        self.assertEqual(ref["ghl_invoice_id"], "")
        self.assertEqual(
            ref["invoice_url"],
            "https://workorder.theservicepilot.com/invoice/46779fb8-21b7-46db-bb4a-133596c6a1df/",
        )


class JobInvoiceStatusTests(TestCase):
    def setUp(self):
        self.account = GHLAuthCredentials.objects.create(
            user_id="test-account",
            access_token="access-token",
            refresh_token="refresh-token",
            expires_in=3600,
            location_id="test-location",
        )
        self.job = Job.objects.create(
            account=self.account,
            title="Invoice job",
            total_price=Decimal("100.00"),
            status="completed",
        )

    def test_save_job_invoice_info_marks_sent_when_invoice_sent_true(self):
        save_job_invoice_info(
            str(self.job.id),
            "ghl-invoice-1",
            invoice_sent=True,
            invoice_url="https://workorder.theservicepilot.com/invoice/public-1/",
        )

        self.job.refresh_from_db()
        self.assertEqual(self.job.invoice_id, "ghl-invoice-1")
        self.assertEqual(self.job.invoice_status, "sent")
        self.assertEqual(
            self.job.invoice_url,
            "https://workorder.theservicepilot.com/invoice/public-1/",
        )

    def test_save_job_invoice_info_does_not_downgrade_existing_status(self):
        self.job.invoice_id = "ghl-invoice-1"
        self.job.invoice_status = "sent"
        self.job.save(update_fields=["invoice_id", "invoice_status"])

        save_job_invoice_info(
            str(self.job.id),
            "ghl-invoice-1",
            invoice_sent=False,
            invoice_url="https://workorder.theservicepilot.com/invoice/public-1/",
        )

        self.job.refresh_from_db()
        self.assertEqual(self.job.invoice_status, "sent")


class JobConvertToSeriesTests(APITestCase):
    def setUp(self):
        self.account = GHLAuthCredentials.objects.create(
            user_id='test-account',
            access_token='access-token',
            refresh_token='refresh-token',
            expires_in=3600,
            location_id='test-location',
        )
        self.admin = User.objects.create_user(
            username='admin',
            email='admin@example.com',
            password='password',
            role=User.ROLE_MANAGER,
            account=self.account,
        )
        self.token = Token.objects.create(user=self.admin)
        self.client.credentials(HTTP_AUTHORIZATION=f'Token {self.token.key}')

        self.base_dt = timezone.make_aware(datetime(2026, 5, 29, 11, 0))
        self.job = Job.objects.create(
            account=self.account,
            title='Accepted quote',
            description='Quote accepted and converted to job.',
            duration_hours=Decimal('1.50'),
            scheduled_at=self.base_dt,
            total_price=Decimal('300.00'),
            customer_name='Test Customer',
            status='to_convert',
        )
        JobServiceItem.objects.create(
            job=self.job,
            custom_name='Window cleaning',
            price=Decimal('300.00'),
            duration_hours=Decimal('1.50'),
        )
        JobAssignment.objects.create(job=self.job, user=self.admin, role='lead')

    def test_convert_to_series_reuses_placeholder_as_first_occurrence(self):
        response = self.client.post(
            f'/api/job/jobs/{self.job.id}/convert-to-series/',
            {
                'title': 'Updated recurring job',
                'description': 'Updated before converting.',
                'duration_hours': '2.00',
                'total_price': '450.00',
                'repeat_every': 1,
                'repeat_unit': 'day',
                'occurrences': 3,
                'items': [
                    {
                        'custom_name': 'Updated service',
                        'price': '450.00',
                        'duration_hours': '2.00',
                    }
                ],
                'assignments': [
                    {
                        'user': self.admin.id,
                        'role': 'technician',
                    }
                ],
            },
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.job.refresh_from_db()
        series_id = response.data['series_id']
        jobs = list(Job.objects.filter(series_id=series_id).order_by('series_sequence'))

        self.assertEqual(len(jobs), 3)
        self.assertEqual(jobs[0].id, self.job.id)
        self.assertEqual(self.job.status, 'pending')
        self.assertEqual(self.job.job_type, 'recurring')
        self.assertEqual(self.job.title, 'Updated recurring job')
        self.assertEqual(self.job.description, 'Updated before converting.')
        self.assertEqual(self.job.duration_hours, Decimal('2.00'))
        self.assertEqual(self.job.total_price, Decimal('450.00'))
        self.assertEqual(self.job.series_sequence, 1)
        self.assertFalse(Job.objects.filter(id=self.job.id, status='to_convert').exists())
        self.assertEqual([job.scheduled_at for job in jobs], [
            self.base_dt,
            self.base_dt + timedelta(days=1),
            self.base_dt + timedelta(days=2),
        ])
        self.assertEqual([job.title for job in jobs], ['Updated recurring job'] * 3)
        self.assertEqual([job.items.count() for job in jobs], [1, 1, 1])
        self.assertEqual([job.items.first().custom_name for job in jobs], ['Updated service'] * 3)
        self.assertEqual([job.assignments.count() for job in jobs], [1, 1, 1])
        self.assertEqual([job.assignments.first().role for job in jobs], ['technician'] * 3)

    def test_convert_to_series_rejects_non_placeholder_jobs(self):
        self.job.status = 'pending'
        self.job.save(update_fields=['status'])

        response = self.client.post(
            f'/api/job/jobs/{self.job.id}/convert-to-series/',
            {
                'repeat_every': 1,
                'repeat_unit': 'day',
                'occurrences': 3,
            },
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
