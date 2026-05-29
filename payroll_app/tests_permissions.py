from datetime import date
from decimal import Decimal

from rest_framework import status
from rest_framework.authtoken.models import Token
from rest_framework.test import APITestCase

from accounts.models import GHLAuthCredentials
from payroll_app.models import EmployeeProfile, EmployeeTimeOff, Payout
from service_app.models import User


class PayrollRolePermissionTests(APITestCase):
    def setUp(self):
        self.account = GHLAuthCredentials.objects.create(
            user_id='acct-1',
            access_token='token',
            refresh_token='refresh',
            expires_in=3600,
            location_id='loc-1',
        )

        self.manager = User.objects.create_user(
            username='manager',
            email='manager@example.com',
            password='password',
            role=User.ROLE_MANAGER,
            account=self.account,
        )
        self.supervisor = User.objects.create_user(
            username='supervisor',
            email='supervisor@example.com',
            password='password',
            role=User.ROLE_SUPERVISOR,
            account=self.account,
        )
        self.worker = User.objects.create_user(
            username='worker',
            email='worker@example.com',
            password='password',
            role=User.ROLE_WORKER,
            account=self.account,
        )

        self.manager_profile, _ = EmployeeProfile.objects.update_or_create(
            user=self.manager,
            defaults={
                'account': self.account,
                'department': 'Sales',
                'position': 'Manager',
                'pay_scale_type': 'project',
            },
        )
        EmployeeProfile.objects.update_or_create(
            user=self.worker,
            defaults={
                'account': self.account,
                'department': 'Ops',
                'position': 'Worker',
                'pay_scale_type': 'project',
            },
        )

        self.manager_payout = Payout.objects.create(
            employee=self.manager,
            payout_type='project',
            amount=Decimal('100.00'),
            project_title='Manager payout',
        )
        self.worker_payout = Payout.objects.create(
            employee=self.worker,
            payout_type='project',
            amount=Decimal('200.00'),
            project_title='Worker payout',
        )
        self.manager_time_off = EmployeeTimeOff.objects.create(
            employee=self.manager,
            start_date=date(2026, 6, 1),
            end_date=date(2026, 6, 1),
            kind='vacation',
            coverage='full_day',
        )
        self.worker_time_off = EmployeeTimeOff.objects.create(
            employee=self.worker,
            start_date=date(2026, 6, 4),
            end_date=date(2026, 6, 4),
            kind='personal',
            coverage='full_day',
        )

    def _authenticate(self, user):
        token, _ = Token.objects.get_or_create(user=user)
        self.client.credentials(HTTP_AUTHORIZATION=f'Token {token.key}')

    def _extract_results(self, data):
        if isinstance(data, dict) and 'results' in data:
            return data['results']
        return data

    def test_manager_employee_list_includes_all_employees_with_filters(self):
        self._authenticate(self.manager)

        all_response = self.client.get('/api/payroll/employees/')
        filtered_response = self.client.get(
            '/api/payroll/employees/?pay_scale_type=project'
        )

        self.assertEqual(all_response.status_code, status.HTTP_200_OK)
        self.assertEqual(filtered_response.status_code, status.HTTP_200_OK)
        all_results = self._extract_results(all_response.data)
        filtered_results = self._extract_results(filtered_response.data)
        all_user_ids = {row['user_id'] for row in all_results}
        filtered_user_ids = {row['user_id'] for row in filtered_results}
        self.assertEqual(all_user_ids, {self.manager.id, self.worker.id})
        self.assertEqual(filtered_user_ids, {self.manager.id, self.worker.id})
        for row in filtered_results:
            self.assertEqual(row['pay_scale_type'], 'project')

    def test_manager_payouts_are_self_only_in_payroll(self):
        self._authenticate(self.manager)

        response = self.client.get('/api/payroll/payouts/')

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        results = self._extract_results(response.data)
        payout_ids = {str(row['id']) for row in results}
        self.assertEqual(payout_ids, {str(self.manager_payout.id)})

    def test_manager_cannot_use_payroll_calculator(self):
        self._authenticate(self.manager)

        response = self.client.post('/api/payroll/calculator/', {}, format='json')

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_manager_can_manage_team_time_off(self):
        self._authenticate(self.manager)

        create_response = self.client.post(
            '/api/payroll/time-off/',
            {
                'employee': self.worker.id,
                'start_date': '2026-06-02',
                'end_date': '2026-06-02',
                'kind': 'personal',
                'coverage': 'full_day',
            },
            format='json',
        )
        update_response = self.client.patch(
            f'/api/payroll/time-off/{self.worker_time_off.id}/',
            {'notes': 'updated by manager'},
            format='json',
        )

        self.assertEqual(create_response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(create_response.data['employee'], self.worker.id)
        self.assertEqual(update_response.status_code, status.HTTP_200_OK)
        self.assertEqual(update_response.data['notes'], 'updated by manager')

    def test_manager_can_view_team_time_off(self):
        self._authenticate(self.manager)

        list_response = self.client.get('/api/payroll/time-off/')
        detail_response = self.client.get(
            f'/api/payroll/time-off/{self.worker_time_off.id}/'
        )

        self.assertEqual(list_response.status_code, status.HTTP_200_OK)
        results = self._extract_results(list_response.data)
        employee_ids = {row['employee'] for row in results}
        self.assertEqual(employee_ids, {self.manager.id, self.worker.id})
        self.assertEqual(detail_response.status_code, status.HTTP_200_OK)
        self.assertEqual(detail_response.data['employee'], self.worker.id)

    def test_worker_can_still_create_own_time_off(self):
        self._authenticate(self.worker)

        response = self.client.post(
            '/api/payroll/time-off/',
            {
                'start_date': '2026-06-03',
                'end_date': '2026-06-03',
                'kind': 'sick',
                'coverage': 'full_day',
            },
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data['employee'], self.worker.id)

    def test_supervisor_still_has_team_payroll_access(self):
        self._authenticate(self.supervisor)

        response = self.client.get('/api/payroll/employees/')

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        results = self._extract_results(response.data)
        user_ids = {row['user_id'] for row in results}
        self.assertEqual(user_ids, {self.manager.id, self.worker.id})
