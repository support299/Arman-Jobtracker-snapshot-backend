from rest_framework import viewsets, permissions, status
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied
from rest_framework.response import Response
from rest_framework.views import APIView
from django.shortcuts import get_object_or_404
from django.db.models import Q, Sum, Count
from django.utils import timezone
from decimal import Decimal
from datetime import datetime, timedelta

from accounts.permissions import AccountScopedPermission
from accounts.mixins import AccountScopedQuerysetMixin
from service_app.models import User
from .access import (
    payroll_can_view_employees,
    payroll_can_view_team_data,
    payroll_has_admin_access,
    payroll_can_manage_time_off,
)
from .models import (
    EmployeeProfile,
    CollaborationRate,
    EmployeeTimeOff,
    TimeEntry,
    Payout,
    PayrollSettings,
)
from .serializers import (
    EmployeeProfileSerializer,
    CollaborationRateCreateSerializer,
    EmployeeTimeOffSerializer,
    AvailableEmployeeSerializer,
    TimeEntrySerializer,
    PayoutSerializer,
    PayrollSettingsSerializer,
)

# Minimum duration (in hours) to include in today_entries API. Entries with total_hours
# below this are excluded from the response (e.g. accidental check-in/out within 60 seconds).
MIN_DURATION_HOURS_FOR_TODAY = Decimal('1') / 60  # 60 seconds = 1 minute


def _payroll_can_view_team_data(user):
    """
    Scope for account-wide payroll reads: payouts list, time-entries/today,
    time-entries/active-session, and reports. Requires payroll admin access plus flag.
    """
    return payroll_can_view_team_data(user)


def _payroll_is_admin(user):
    """Payroll-only admin access. Managers remain self-only in payroll."""
    return payroll_has_admin_access(user)


def _employees_can_view_team(user):
    """Managers can list/read all employee profiles (view-only)."""
    return payroll_can_view_employees(user)


def _time_off_can_view_team(user):
    """Managers and supervisors can read team time-off data."""
    return payroll_can_manage_time_off(user)


def _resolve_calculator_user_id(identifier, account):
    """
    Resolve a user identifier from calculator payload to a User in the given account.
    Accepts: integer primary key, UUID (matched to ghl_user_id), email, or username.
    Returns (User, None) if found, (None, error_message) if invalid or not found.
    """
    from accounts.user_access import users_queryset_for_account

    if identifier is None:
        return None, None
    if account is None:
        return None, "Account context is required"
    s = str(identifier).strip()
    if not s:
        return None, "User ID cannot be empty"

    visible_users = users_queryset_for_account(account).exclude(is_superuser=True)

    # Try integer ID (User.pk is integer)
    try:
        uid = int(s)
        user = visible_users.filter(pk=uid).first()
        if user:
            return user, None
        return None, f"User with ID {uid} not found in this account"
    except (ValueError, TypeError):
        pass
    # Try UUID / GHL-style id as ghl_user_id
    if len(s) == 36 and s.count("-") == 4 and all(c in "0123456789abcdefABCDEF-" for c in s):
        user = visible_users.filter(ghl_user_id=s).first()
        if user:
            return user, None
        return None, f"User with ID {s} not found in this account (tried ghl_user_id)"
    # Try email or username
    user = visible_users.filter(Q(email=s) | Q(username=s)).first()
    if user:
        return user, None
    return None, f"User '{s}' not found in this account"


class IsAdminOrEmployeePermission(permissions.BasePermission):
    """Permission for admin or employee access"""
    def has_permission(self, request, view):
        return request.user and request.user.is_authenticated


class IsPayrollAdminPermission(permissions.BasePermission):
    """Permission for payroll admins only."""
    def has_permission(self, request, view):
        return (
            request.user and 
            request.user.is_authenticated and 
            _payroll_is_admin(request.user)
        )


class EmployeeProfileViewSet(AccountScopedQuerysetMixin, viewsets.ModelViewSet):
    """
    ViewSet for managing employee profiles (scoped to current account).

    - Worker: read only their own profile.
    - Manager: view-only; list/retrieve all employees in account (filters apply).
    - Payroll admin (supervisor): full CRUD on all employees in account.
    """
    queryset = EmployeeProfile.objects.all().select_related('user').prefetch_related('user__collaboration_rates')
    serializer_class = EmployeeProfileSerializer
    permission_classes = [AccountScopedPermission, IsAdminOrEmployeePermission]
    account_lookup = "account"
    
    def get_permissions(self):
        # Only payroll admins can create/update/delete profiles
        # Normal users can read their own profile
        if self.request.method in ['POST', 'PUT', 'PATCH', 'DELETE']:
            return [AccountScopedPermission(), IsPayrollAdminPermission()]
        return super().get_permissions()
    
    def get_queryset(self):
        from accounts.user_access import users_queryset_for_account

        account = getattr(self.request, "account", None)
        if account is None:
            return EmployeeProfile.objects.none()

        queryset = (
            EmployeeProfile.objects.filter(
                user__in=users_queryset_for_account(account)
            )
            .select_related("user")
            .prefetch_related("user__collaboration_rates")
        )
        user = self.request.user
        
        # Workers: own profile only. Managers/supervisors: full account directory.
        if not _employees_can_view_team(user):
            queryset = queryset.filter(user=user)
        else:
            queryset = queryset.exclude(user__is_superuser=True)
        
        # Search (available when user can see multiple profiles)
        search = self.request.query_params.get('search', None)
        if search:
            queryset = queryset.filter(
                Q(user__first_name__icontains=search) |
                Q(user__last_name__icontains=search) |
                Q(user__email__icontains=search) |
                Q(phone__icontains=search) |
                Q(department__icontains=search) |
                Q(position__icontains=search) |
                Q(pay_scale_type__icontains=search)
            )
        
        # Filter by status (EmployeeProfile status)
        status_filter = self.request.query_params.get('status', None)
        if status_filter:
            queryset = queryset.filter(status=status_filter)
        
        # Filter by user status (user.is_active)
        user_status = self.request.query_params.get('is_active', None)
        if user_status is not None:
            # Convert string to boolean - handle various formats
            user_status_lower = str(user_status).lower()
            if user_status_lower in ('true', '1', 'active', 'yes'):
                queryset = queryset.filter(user__is_active=True)
            elif user_status_lower in ('false', '0', 'inactive', 'no'):
                queryset = queryset.filter(user__is_active=False)
        
        # Filter by pay scale type
        pay_scale = self.request.query_params.get('pay_scale_type', None)
        if pay_scale:
            queryset = queryset.filter(pay_scale_type=pay_scale)
        
        return queryset
    
    def get_object(self):
        """Override to ensure users can only access their own profile."""
        obj = super().get_object()
        user = self.request.user
        
        if _employees_can_view_team(user):
            return obj
        
        if obj.user != user:
            raise PermissionDenied("You do not have permission to access this employee profile.")
        
        return obj
    
    def perform_create(self, serializer):
        account = getattr(self.request, 'account', None)
        serializer.save(account=account)
    
    @action(detail=True, methods=['post'], url_path='collaboration-rates')
    def update_collaboration_rates(self, request, pk=None):
        """Update collaboration rates for an employee"""
        employee_profile = self.get_object()
        employee = employee_profile.user
        
        # Delete existing rates
        CollaborationRate.objects.filter(employee=employee).delete()
        
        # Create new rates
        rates_data = request.data.get('rates', [])
        created_rates = []
        for rate_data in rates_data:
            rate_data['employee'] = employee.id
            serializer = CollaborationRateCreateSerializer(data=rate_data)
            if serializer.is_valid():
                rate = serializer.save(employee=employee)
                created_rates.append(rate)
            else:
                return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
        
        return Response({
            'message': 'Collaboration rates updated successfully',
            'rates': CollaborationRateCreateSerializer(created_rates, many=True).data
        })


class TimeEntryViewSet(AccountScopedQuerysetMixin, viewsets.ModelViewSet):
    """
    Time clock entries. Account-scoped: list/detail only for current account.
    - Payroll admin: see all time entries in that account (optionally filter by employee).
    - Everyone else: see only their own time entries in that account.
    """
    queryset = TimeEntry.objects.all().select_related('employee')
    serializer_class = TimeEntrySerializer
    permission_classes = [AccountScopedPermission, IsAdminOrEmployeePermission]
    account_lookup = "account"
    
    def get_queryset(self):
        from accounts.user_access import time_entries_queryset_for_account

        account = getattr(self.request, "account", None)
        if account is None:
            return TimeEntry.objects.none()

        queryset = time_entries_queryset_for_account(account).select_related("employee")
        user = self.request.user
        # Non-admin: only their own entries within the account
        if not _payroll_is_admin(user):
            queryset = queryset.filter(employee=user)
        else:
            # Exclude super admin (superuser) from time entry list for admins
            queryset = queryset.exclude(employee__is_superuser=True)
        
        # Filter by date
        date = self.request.query_params.get('date', None)
        if date:
            try:
                date_obj = datetime.strptime(date, '%Y-%m-%d').date()
                queryset = queryset.filter(
                    check_in_time__date=date_obj
                )
            except ValueError:
                pass
        
        # Filter by employee (admin only)
        employee_id = self.request.query_params.get('employee', None)
        if employee_id and _payroll_is_admin(user):
            queryset = queryset.filter(employee_id=employee_id)
        
        return queryset.order_by('-check_in_time')
    
    @action(detail=False, methods=['post'], url_path='check-in')
    def check_in(self, request):
        """
        Check in for hourly employees.
        
        Normal users: Check in themselves (no employee_id needed)
        Admin users: Can check in any employee by providing employee_id in payload
        
        Payload (normal user):
        {
            "notes": "Optional notes"
        }
        
        Payload (admin user):
        {
            "employee_id": 123,  // Required: User ID to check in
            "notes": "Optional notes"
        }
        """
        user = request.user
        is_admin = _payroll_is_admin(user)
        
        # Determine which employee to check in
        account = getattr(request, 'account', None)
        if is_admin:
            employee_id = request.data.get('employee_id')
            if not employee_id:
                return Response({
                    'error': 'employee_id is required for admin users'
                }, status=status.HTTP_400_BAD_REQUEST)
            
            from accounts.user_access import get_visible_user_by_id

            target_employee = get_visible_user_by_id(account, employee_id)
            if target_employee is None:
                return Response({
                    'error': f'Employee with ID {employee_id} not found'
                }, status=status.HTTP_404_NOT_FOUND)
        else:
            target_employee = user
        
        if account is None:
            return Response(
                {'error': 'Account context is required.'},
                status=status.HTTP_403_FORBIDDEN,
            )

        from accounts.user_access import time_entries_queryset_for_account

        # Check if employee has active session in this subaccount only
        active_entry = time_entries_queryset_for_account(account).filter(
            employee=target_employee,
            status='checked_in',
        ).first()
        
        if active_entry:
            return Response({
                'error': f'{target_employee.get_full_name() or target_employee.username} already has an active session. Please check out first.'
            }, status=status.HTTP_400_BAD_REQUEST)
        
        # Create new time entry scoped to the current subaccount
        entry = TimeEntry.objects.create(
            account=account,
            employee=target_employee,
            check_in_time=timezone.now(),
            notes=request.data.get('notes', '')
        )
        
        serializer = self.get_serializer(entry)
        return Response(serializer.data, status=status.HTTP_201_CREATED)
    
    @action(detail=False, methods=['get'], url_path='active-session')
    def active_session(self, request):
        """
        Get active check-in session.
        
        Limited payroll scope: Get their own active session (no employee_id needed).
        Team payroll scope: Can query any employee's active session or all active sessions.
        
        Query params (limited scope): None
        
        Query params (supervisor with payroll_can_view_team_data=True):
        - employee_id: Optional - Get specific employee's active session
        - all: Optional - Set to 'true' to get all active sessions
        
        Examples:
        - GET /api/payroll/time-entries/active-session/ (limited scope - their own)
        - GET /api/payroll/time-entries/active-session/?employee_id=123 (team scope - specific employee)
        - GET /api/payroll/time-entries/active-session/?all=true (team scope - all active sessions)
        """
        user = request.user
        can_view_team = _payroll_can_view_team_data(user)
        
        account = getattr(request, 'account', None)
        from accounts.user_access import get_visible_user_by_id, time_entries_queryset_for_account

        if account is None:
            return Response(
                {'error': 'Account context is required.'},
                status=status.HTTP_403_FORBIDDEN,
            )

        scoped_entries = time_entries_queryset_for_account(account)

        if can_view_team:
            # Admin can query all active sessions or a specific employee's (within account)
            get_all = request.query_params.get('all', '').lower() == 'true'
            employee_id = request.query_params.get('employee_id')
            
            if get_all:
                active_entries = scoped_entries.filter(
                    status='checked_in'
                ).exclude(employee__is_superuser=True).select_related('employee').order_by('-check_in_time')
                
                result = []
                for entry in active_entries:
                    elapsed_time = None
                    if entry.check_in_time:
                        delta = timezone.now() - entry.check_in_time
                        elapsed_time = Decimal(str(delta.total_seconds() / 3600)).quantize(Decimal('0.01'))
                    
                    serializer = self.get_serializer(entry)
                    result.append({
                        'active': True,
                        'entry': serializer.data,
                        'elapsed_hours': float(elapsed_time) if elapsed_time else 0
                    })
                
                return Response({
                    'active_sessions': result,
                    'count': len(result)
                })
            elif employee_id:
                target_employee = get_visible_user_by_id(account, employee_id)
                if target_employee is None:
                    return Response({
                        'error': f'Employee with ID {employee_id} not found'
                    }, status=status.HTTP_404_NOT_FOUND)
                
                active_entry = scoped_entries.filter(
                    employee=target_employee,
                    status='checked_in',
                ).first()
            else:
                active_entries = scoped_entries.filter(
                    status='checked_in'
                ).exclude(employee__is_superuser=True).select_related('employee').order_by('-check_in_time')
                
                result = []
                for entry in active_entries:
                    elapsed_time = None
                    if entry.check_in_time:
                        delta = timezone.now() - entry.check_in_time
                        elapsed_time = Decimal(str(delta.total_seconds() / 3600)).quantize(Decimal('0.01'))
                    
                    serializer = self.get_serializer(entry)
                    result.append({
                        'active': True,
                        'entry': serializer.data,
                        'elapsed_hours': float(elapsed_time) if elapsed_time else 0
                    })
                
                return Response({
                    'active_sessions': result,
                    'count': len(result)
                })
        else:
            # Non-admin: only their own active session in this subaccount
            active_entry = scoped_entries.filter(
                employee=user,
                status='checked_in',
            ).first()
        
        if not active_entry:
            return Response({'active': False})
        
        serializer = self.get_serializer(active_entry)
        elapsed_time = None
        if active_entry.check_in_time:
            delta = timezone.now() - active_entry.check_in_time
            elapsed_time = Decimal(str(delta.total_seconds() / 3600)).quantize(Decimal('0.01'))
        
        return Response({
            'active': True,
            'entry': serializer.data,
            'elapsed_hours': float(elapsed_time) if elapsed_time else 0
        })
    
    @action(detail=True, methods=['post'], url_path='check-out')
    def check_out(self, request, pk=None):
        """Check out and complete time entry (entry is account-scoped via get_queryset when using detail route)."""
        account = getattr(request, 'account', None)
        from accounts.user_access import time_entries_queryset_for_account

        if account is None:
            return Response(
                {'error': 'Account context is required.'},
                status=status.HTTP_403_FORBIDDEN,
            )

        entry = get_object_or_404(time_entries_queryset_for_account(account), pk=pk)
        
        # Verify ownership (unless admin)
        user = request.user
        if not _payroll_is_admin(user) and entry.employee != user:
            return Response({
                'error': 'You can only check out your own time entries.'
            }, status=status.HTTP_403_FORBIDDEN)
        
        if entry.status == 'checked_out':
            return Response({
                'error': 'This time entry is already checked out.'
            }, status=status.HTTP_400_BAD_REQUEST)
        
        # Update entry
        entry.check_out_time = timezone.now()
        entry.total_hours = entry.calculate_hours()
        entry.status = 'checked_out'
        if 'notes' in request.data:
            entry.notes = request.data.get('notes', entry.notes)
        entry.save()
        
        # Create hourly payout if employee has hourly rate
        try:
            profile = entry.employee.employee_profile
            if profile.pay_scale_type == 'hourly' and profile.hourly_rate and entry.total_hours:
                amount = (entry.total_hours * profile.hourly_rate).quantize(Decimal('0.01'))
                Payout.objects.create(
                    account=entry.account or account,
                    employee=entry.employee,
                    payout_type='hourly',
                    amount=amount,
                    time_entry=entry,
                    notes=f"Hourly payout for {entry.total_hours} hours"
                )
        except EmployeeProfile.DoesNotExist:
            pass
        
        serializer = self.get_serializer(entry)
        return Response(serializer.data)
    
    @action(detail=False, methods=['get'], url_path='today')
    def today_entries(self, request):
        """
        Get today's time entries.
        
        Limited payroll scope: Get their own today's entries (no employee_id needed).
        Team payroll scope: Can view all today's entries or filter by specific employee.
        
        Query params (limited scope): None
        
        Query params (supervisor with payroll_can_view_team_data=True):
        - employee_id: Optional - Filter by specific employee ID
        - all: Optional - Set to 'true' to get all employees' entries (default when team scope)
        
        Examples:
        - GET /api/payroll/time-entries/today/ (limited scope - their own)
        - GET /api/payroll/time-entries/today/?employee_id=123 (team scope - specific employee)
        - GET /api/payroll/time-entries/today/?all=true (team scope - all employees)
        """
        user = request.user
        can_view_team = _payroll_can_view_team_data(user)
        account = getattr(request, 'account', None)
        from accounts.user_access import get_visible_user_by_id, time_entries_queryset_for_account

        if account is None:
            return Response(
                {'error': 'Account context is required.'},
                status=status.HTTP_403_FORBIDDEN,
            )

        scoped_entries = time_entries_queryset_for_account(account)
        today = timezone.now().date()
        
        if can_view_team:
            employee_id = request.query_params.get('employee_id')
            get_all = request.query_params.get('all', 'true').lower() == 'true'
            
            queryset = scoped_entries.filter(
                check_in_time__date=today
            ).exclude(employee__is_superuser=True).filter(
                Q(total_hours__isnull=True) | Q(total_hours__gte=MIN_DURATION_HOURS_FOR_TODAY)
            ).select_related('employee').order_by('-check_in_time')
            
            if employee_id:
                target_employee = get_visible_user_by_id(account, employee_id)
                if target_employee is None:
                    return Response({
                        'error': f'Employee with ID {employee_id} not found'
                    }, status=status.HTTP_404_NOT_FOUND)
                queryset = queryset.filter(employee=target_employee)
            # If get_all is true (default), show all entries (no additional filter)
        else:
            # Non-admin: only their own entries in this subaccount
            queryset = scoped_entries.filter(
                employee=user,
                check_in_time__date=today
            ).filter(
                Q(total_hours__isnull=True) | Q(total_hours__gte=MIN_DURATION_HOURS_FOR_TODAY)
            ).select_related('employee').order_by('-check_in_time')
        
        serializer = self.get_serializer(queryset, many=True)
        return Response({
            'entries': serializer.data,
            'count': queryset.count(),
            'date': today.isoformat()
        })


class EmployeeTimeOffViewSet(AccountScopedQuerysetMixin, viewsets.ModelViewSet):
    """
    Employee calendar time off (single day or inclusive range).

    Coverage (single day: ``coverage``; multi-day: ``start_day_coverage`` /
    ``end_day_coverage`` on first/last day; middle days are full off):
    ``full_day``, ``half_day_am``, ``half_day_pm``, ``custom`` (requires times).

    - Worker: create/list/retrieve/update/delete only their own entries.
    - Manager / supervisor: full team time off (create, edit, delete for any employee).
    - Optional ?employee=<user_id> filter on list.
    - Optional calendar window: ?from_date=YYYY-MM-DD&to_date=YYYY-MM-DD returns
      entries that overlap that range.
    """
    queryset = EmployeeTimeOff.objects.all().select_related('employee')
    serializer_class = EmployeeTimeOffSerializer
    permission_classes = [AccountScopedPermission, IsAdminOrEmployeePermission]
    account_lookup = 'employee__account'

    def get_queryset(self):
        from accounts.user_access import users_queryset_for_account

        account = getattr(self.request, "account", None)
        if account is None:
            return EmployeeTimeOff.objects.none()

        queryset = EmployeeTimeOff.objects.filter(
            employee__in=users_queryset_for_account(account)
        ).select_related("employee")
        user = self.request.user
        if not _time_off_can_view_team(user):
            queryset = queryset.filter(employee=user)
        else:
            queryset = queryset.exclude(employee__is_superuser=True)
            employee_id = self.request.query_params.get('employee')
            if employee_id:
                queryset = queryset.filter(employee_id=employee_id)

        from_date = self.request.query_params.get('from_date')
        to_date = self.request.query_params.get('to_date')
        if from_date and to_date:
            try:
                from_d = datetime.strptime(from_date, '%Y-%m-%d').date()
                to_d = datetime.strptime(to_date, '%Y-%m-%d').date()
                queryset = queryset.filter(start_date__lte=to_d, end_date__gte=from_d)
            except ValueError:
                pass
        elif from_date:
            try:
                from_d = datetime.strptime(from_date, '%Y-%m-%d').date()
                queryset = queryset.filter(end_date__gte=from_d)
            except ValueError:
                pass
        elif to_date:
            try:
                to_d = datetime.strptime(to_date, '%Y-%m-%d').date()
                queryset = queryset.filter(start_date__lte=to_d)
            except ValueError:
                pass

        return queryset.order_by('-start_date', '-created_at')

    def perform_create(self, serializer):
        from accounts.user_access import users_queryset_for_account

        user = self.request.user
        account = getattr(self.request, "account", None)
        if payroll_can_manage_time_off(user):
            employee = serializer.validated_data.get("employee")
            if employee and account:
                visible = users_queryset_for_account(account)
                if not visible.filter(pk=employee.pk).exists():
                    raise PermissionDenied(
                        "That employee is not in the current account."
                    )
            serializer.save()
        else:
            serializer.save(employee=user)

    def get_object(self):
        obj = super().get_object()
        user = self.request.user
        if not _time_off_can_view_team(user) and obj.employee != user:
            raise PermissionDenied('You can only access your own time off entries.')
        return obj

    @action(
        detail=False,
        methods=['get'],
        url_path='available-employees',
    )
    def available_employees(self, request):
        """
        Employees in the current account who have **no** time off overlapping
        the window (inclusive).

        Query (choose one):
        - date=YYYY-MM-DD — single day (not off on that day)
        - start_date=YYYY-MM-DD & end_date=YYYY-MM-DD — any inclusive range

        Optional time window (half-day / custom aware):
        - period=am|pm — only treat AM or PM as the slot to check
        - from_time=HH:MM & to_time=HH:MM — custom slot (overrides period)
        """
        from .time_off_utils import (
            employee_ids_off_in_window,
            parse_period_param,
            parse_time_param,
        )
        account = getattr(request, 'account', None)
        if not account:
            return Response(
                {'error': 'Account context is required.'},
                status=status.HTTP_403_FORBIDDEN,
            )

        date_s = (request.query_params.get('date') or '').strip()
        if date_s:
            try:
                start_d = end_d = datetime.strptime(date_s, '%Y-%m-%d').date()
            except ValueError:
                return Response(
                    {'error': 'date must be a valid date (YYYY-MM-DD).'},
                    status=status.HTTP_400_BAD_REQUEST,
                )
        else:
            start_s = request.query_params.get('start_date')
            end_s = request.query_params.get('end_date')
            if not start_s or not end_s:
                return Response(
                    {
                        'error': (
                            'Provide date=YYYY-MM-DD for a single day, or both '
                            'start_date and end_date (YYYY-MM-DD).'
                        ),
                    },
                    status=status.HTTP_400_BAD_REQUEST,
                )
            try:
                start_d = datetime.strptime(start_s, '%Y-%m-%d').date()
                end_d = datetime.strptime(end_s, '%Y-%m-%d').date()
            except ValueError:
                return Response(
                    {
                        'error': (
                            'start_date and end_date must be valid dates (YYYY-MM-DD).'
                        ),
                    },
                    status=status.HTTP_400_BAD_REQUEST,
                )

        if end_d < start_d:
            return Response(
                {'error': 'end_date must be on or after start_date.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        check_start = None
        check_end = None
        from_time_s = (request.query_params.get('from_time') or '').strip()
        to_time_s = (request.query_params.get('to_time') or '').strip()
        period_s = (request.query_params.get('period') or '').strip()

        if from_time_s or to_time_s:
            if not from_time_s or not to_time_s:
                return Response(
                    {
                        'error': (
                            'Provide both from_time and to_time (HH:MM), or use period=am|pm.'
                        ),
                    },
                    status=status.HTTP_400_BAD_REQUEST,
                )
            try:
                check_start = parse_time_param(from_time_s, 'from_time')
                check_end = parse_time_param(to_time_s, 'to_time')
            except ValueError as exc:
                return Response(
                    {'error': str(exc)},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            if check_end <= check_start:
                return Response(
                    {'error': 'to_time must be after from_time.'},
                    status=status.HTTP_400_BAD_REQUEST,
                )
        elif period_s:
            try:
                check_start, check_end = parse_period_param(period_s)
            except ValueError as exc:
                return Response(
                    {'error': str(exc)},
                    status=status.HTTP_400_BAD_REQUEST,
                )

        from accounts.user_access import users_queryset_for_account

        visible_users = users_queryset_for_account(account)
        time_off_qs = EmployeeTimeOff.objects.filter(employee__in=visible_users)
        off_employee_ids = employee_ids_off_in_window(
            time_off_qs,
            start_d,
            end_d,
            check_start=check_start,
            check_end=check_end,
        )

        queryset = (
            visible_users.filter(is_superuser=False)
            .exclude(pk__in=list(off_employee_ids))
            .order_by('first_name', 'last_name', 'id')
        )
        if not _time_off_can_view_team(request.user):
            queryset = queryset.filter(pk=request.user.pk)

        employees = list(queryset)
        serializer = AvailableEmployeeSerializer(employees, many=True)
        payload = {
            'start_date': start_d.isoformat(),
            'end_date': end_d.isoformat(),
            'employees': serializer.data,
            'count': len(employees),
        }
        if check_start is not None:
            payload['from_time'] = check_start.isoformat()
            payload['to_time'] = check_end.isoformat()
        elif period_s:
            payload['period'] = period_s.lower()
        return Response(payload)


class PayoutViewSet(AccountScopedQuerysetMixin, viewsets.ModelViewSet):
    """
    Payouts. Account-scoped: list/detail only for current account.
    - Supervisor with payroll_can_view_team_data=True: all payouts (optional ?employee=).
    - Otherwise: only that user’s payouts (worker scope).
    """
    queryset = Payout.objects.all().select_related('employee', 'job', 'time_entry')
    serializer_class = PayoutSerializer
    permission_classes = [AccountScopedPermission, IsAdminOrEmployeePermission]
    account_lookup = "account"
    
    def get_permissions(self):
        if self.request.method in ['PUT', 'PATCH', 'DELETE']:
            return [AccountScopedPermission(), IsPayrollAdminPermission()]
        return super().get_permissions()
    
    def get_queryset(self):
        from accounts.user_access import payouts_queryset_for_account

        account = getattr(self.request, "account", None)
        if account is None:
            return Payout.objects.none()

        queryset = payouts_queryset_for_account(account).select_related(
            'employee', 'job', 'time_entry'
        )
        user = self.request.user
        # Limited scope: only their own payouts within the account
        if not _payroll_can_view_team_data(user):
            queryset = queryset.filter(employee=user)
        else:
            # Exclude super admin (superuser) from payout list for admins
            queryset = queryset.exclude(employee__is_superuser=True)
        
        # Filter by employee (team-scope payroll only)
        employee_id = self.request.query_params.get('employee', None)
        if employee_id and _payroll_can_view_team_data(user):
            queryset = queryset.filter(employee_id=employee_id)
        
        # Filter by payout type
        payout_type = self.request.query_params.get('type', None)
        if payout_type:
            queryset = queryset.filter(payout_type=payout_type)
        
        # Filter by date range
        start_date = self.request.query_params.get('start_date', None)
        end_date = self.request.query_params.get('end_date', None)
        if start_date:
            try:
                start = datetime.strptime(start_date, '%Y-%m-%d')
                queryset = queryset.filter(created_at__gte=start)
            except ValueError:
                pass
        if end_date:
            try:
                end = datetime.strptime(end_date, '%Y-%m-%d') + timedelta(days=1)
                queryset = queryset.filter(created_at__lt=end)
            except ValueError:
                pass
        
        # Filter by project title (for manual entries)
        project_title = self.request.query_params.get('project_title', None)
        if project_title:
            queryset = queryset.filter(project_title__icontains=project_title)
        
        return queryset.order_by('-created_at')
    
    def _get_employee_filter(self):
        """Helper: employees to include (account-scoped, excluding superusers)."""
        from accounts.user_access import users_queryset_for_account

        user = self.request.user
        account = getattr(self.request, 'account', None)
        employee_id = self.request.query_params.get('employee', None)
        
        base = users_queryset_for_account(account).exclude(is_superuser=True) if account else User.objects.none()
        if not _payroll_can_view_team_data(user):
            return base.filter(pk=user.id)
        if employee_id:
            return base.filter(pk=employee_id)
        return base
    
    def _get_time_entry_queryset(self):
        """Get filtered TimeEntry queryset matching the same filters as payouts"""
        account = getattr(self.request, 'account', None)
        from accounts.user_access import time_entries_queryset_for_account

        employee_filter = self._get_employee_filter()
        
        # Base queryset with employee filter
        time_entries = time_entries_queryset_for_account(account).filter(
            employee__in=employee_filter,
            status='checked_out'  # Only count completed time entries
        )
        
        # Apply date range filters (same as payouts)
        start_date = self.request.query_params.get('start_date', None)
        end_date = self.request.query_params.get('end_date', None)
        
        if start_date:
            try:
                start = datetime.strptime(start_date, '%Y-%m-%d')
                time_entries = time_entries.filter(check_in_time__gte=start)
            except ValueError:
                pass
        
        if end_date:
            try:
                end = datetime.strptime(end_date, '%Y-%m-%d') + timedelta(days=1)
                time_entries = time_entries.filter(check_in_time__lt=end)
            except ValueError:
                pass
        
        return time_entries
    
    def list(self, request, *args, **kwargs):
        """List payouts with aggregated statistics"""
        # Get filtered queryset (before pagination for accurate totals)
        queryset = self.filter_queryset(self.get_queryset())
        
        # Calculate aggregated statistics from filtered payouts (before pagination)
        total_payouts = queryset.aggregate(total=Sum('amount'))['total'] or Decimal('0.00')
        
        # Project payouts total
        project_payouts = queryset.filter(payout_type='project')
        project_total = project_payouts.aggregate(total=Sum('amount'))['total'] or Decimal('0.00')
        
        # Hourly payouts total
        hourly_payouts = queryset.filter(payout_type='hourly')
        hourly_total = hourly_payouts.aggregate(total=Sum('amount'))['total'] or Decimal('0.00')
        
        # Average payout
        payout_count = queryset.count()
        average_payout = (total_payouts / Decimal(str(payout_count))) if payout_count > 0 else Decimal('0.00')
        
        # Calculate total hours worked from TimeEntry model
        time_entries_queryset = self._get_time_entry_queryset()
        total_hours_result = time_entries_queryset.aggregate(
            total_hours=Sum('total_hours')
        )
        total_hours_worked = total_hours_result['total_hours'] or Decimal('0.00')
        
        # Prepare totals data
        totals_data = {
            'total_payouts': float(total_payouts),
            'project_total_payouts': float(project_total),
            'hourly_total_payouts': float(hourly_total),
            'total_hours_worked': float(total_hours_worked),
            'average_payout': float(average_payout),
            'payout_count': payout_count,
        }
        
        # Paginate if needed
        page = self.paginate_queryset(queryset)
        if page is not None:
            serializer = self.get_serializer(page, many=True)
            # Get paginated response and add totals
            response = self.get_paginated_response(serializer.data)
            # Add totals to the response data
            response.data['totals'] = totals_data
            return response
        
        # Non-paginated response
        serializer = self.get_serializer(queryset, many=True)
        return Response({
            'results': serializer.data,
            'totals': totals_data
        })
    
    def create(self, request, *args, **kwargs):
        """Prevent direct creation - use calculator endpoint instead"""
        return Response({
            'error': 'Payouts should be created using the /api/payroll/calculator/ endpoint'
        }, status=status.HTTP_405_METHOD_NOT_ALLOWED)


class CalculatorView(APIView):
    """
    Manual calculator for creating payouts (scoped to current account).
    Works the same way as automatic payroll creation for project payouts,
    and also supports hourly payouts.
    
    Common payload:
    - type: 'project' (default) or 'hourly'
    - project_title: Optional text label used in payout records
    
    Project payload:
    - quoted_by_user_id: User ID who created the quote
    - assignee_user_ids: List of assignee user IDs (required)
    - job_date_time: Job date and time (ISO format, optional)
    - is_first_time: Boolean indicating if it's a first-time project (optional)
    - project_value: Project value (decimal, required)
    
    Hourly payload:
    - employee_ids: List of hourly employee IDs (required)
    - job_date: Date of the job (YYYY-MM-DD, required)
    - start_time: HH:MM or HH:MM:SS (required)
    - end_time: HH:MM or HH:MM:SS (required)
    """
    permission_classes = [AccountScopedPermission, IsPayrollAdminPermission]
    
    def post(self, request):
        """Create manual payouts using the same logic as automatic payroll"""
        calculation_type = (request.data.get('type') or 'project').lower()
        if calculation_type not in ('project', 'hourly'):
            return Response(
                {'error': "type must be either 'project' or 'hourly'"},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        if calculation_type == 'hourly':
            return self._create_hourly_payouts(request)
        return self._create_project_payouts(request)
    
    def _create_project_payouts(self, request):
        quoted_by_user_id = request.data.get('quoted_by_user_id')
        assignee_user_ids = request.data.get('assignee_user_ids', [])
        job_date_time = request.data.get('job_date_time')
        project_title = request.data.get('project_title', '')
        is_first_time = request.data.get('is_first_time', False)
        project_value = request.data.get('project_value')
        
        # Validate required fields
        if not project_value:
            return Response({
                'error': 'project_value is required'
            }, status=status.HTTP_400_BAD_REQUEST)
        
        if not assignee_user_ids:
            return Response({
                'error': 'assignee_user_ids is required (at least one assignee)'
            }, status=status.HTTP_400_BAD_REQUEST)
        
        # Parse project value
        try:
            project_value = Decimal(str(project_value))
            if project_value <= 0:
                return Response({
                    'error': 'project_value must be greater than 0'
                }, status=status.HTTP_400_BAD_REQUEST)
        except (ValueError, TypeError):
            return Response({
                'error': 'project_value must be a valid number'
            }, status=status.HTTP_400_BAD_REQUEST)
        
        # Parse job date/time if provided
        job_datetime_note = ''
        if job_date_time:
            try:
                from django.utils.dateparse import parse_datetime
                job_datetime = parse_datetime(job_date_time)
                if not job_datetime:
                    job_datetime = datetime.strptime(job_date_time, '%Y-%m-%d')
                job_datetime_note = job_datetime.strftime('%Y-%m-%d %H:%M')
            except (ValueError, TypeError):
                return Response({
                    'error': 'job_date_time must be a valid ISO datetime or date string'
                }, status=status.HTTP_400_BAD_REQUEST)
        
        account = getattr(request, 'account', None)
        if not account:
            return Response({'error': 'Account context is required.'}, status=status.HTTP_403_FORBIDDEN)
        
        # Get assignees (must belong to current account, exclude superusers)
        # Accept integer user ID, email, or username (User.pk is integer; UUIDs return clear error)
        assignees = []
        for assignee_id in assignee_user_ids:
            assignee, err = _resolve_calculator_user_id(assignee_id, account)
            if err:
                return Response({'error': f'Assignee: {err}'}, status=status.HTTP_400_BAD_REQUEST)
            if assignee:
                assignees.append(assignee)
        
        if not assignees:
            return Response({
                'error': 'No valid assignees found'
            }, status=status.HTTP_400_BAD_REQUEST)
        
        # Get quoted_by user if provided (optional; must belong to current account, exclude superusers)
        # If quoted_by_user_id cannot be resolved, we skip the bonus payout and add a warning (request still succeeds)
        quoted_by_user = None
        quoted_by_warning = None
        if quoted_by_user_id:
            quoted_by_user, err = _resolve_calculator_user_id(quoted_by_user_id, account)
            if err:
                quoted_by_warning = f'Quoted by user: {err}. Bonus payout for quoted-by was skipped.'
        
        # Get payroll settings for current account
        settings = PayrollSettings.get_settings(account)
        
        # Get number of assigned employees
        employee_count = len(assignees)
        
        created_payouts = []
        errors = []
        
        # Step 1: Create payouts for each assigned employee (based on their collaboration rates)
        for assignee in assignees:
            # Check if employee has profile and is project-based
            try:
                profile = assignee.employee_profile
                if profile.pay_scale_type != 'project':
                    errors.append(f'{assignee.get_full_name() or assignee.username} is not configured for project payouts (hourly employee)')
                    continue  # Skip hourly employees
            except EmployeeProfile.DoesNotExist:
                errors.append(f'{assignee.get_full_name() or assignee.username} does not have an employee profile')
                continue  # Skip employees without profile
            
            # Get collaboration rate for this team size
            try:
                collaboration_rate = CollaborationRate.objects.get(
                    employee=assignee,
                    member_count=employee_count
                )
                rate_percentage = collaboration_rate.percentage
            except CollaborationRate.DoesNotExist:
                errors.append(f'{assignee.get_full_name() or assignee.username} does not have a collaboration rate configured for {employee_count} member(s)')
                continue  # Skip if no rate found
            
            # Calculate payout amount based on employee's individual rate
            amount = (project_value * rate_percentage) / Decimal('100')
            amount = amount.quantize(Decimal('0.01'))
            
            schedule_note = f" | Scheduled: {job_datetime_note}" if job_datetime_note else ''
            # Create project payout for this assignee
            payout = Payout.objects.create(
                account=account,
                employee=assignee,
                payout_type='project',
                amount=amount,
                project_value=project_value,
                rate_percentage=rate_percentage,
                project_title=project_title,
                notes=f"Manual project payout: {project_title} (Rate: {rate_percentage}% for {employee_count} member(s)){schedule_note}"
            )
            created_payouts.append(payout)
        
        # Step 2: Create bonus payout for quoted_by person (separate from assignee payouts)
        if quoted_by_user:
            # Determine bonus type
            bonus_type = 'bonus_first_time' if is_first_time else 'bonus_quoted_by'
            
            # Get bonus percentage from settings
            if is_first_time:
                bonus_percentage = settings.first_time_bonus_percentage
            else:
                bonus_percentage = settings.quoted_by_bonus_percentage
            
            # Calculate bonus amount
            bonus_amount = (project_value * bonus_percentage) / Decimal('100')
            bonus_amount = bonus_amount.quantize(Decimal('0.01'))
            
            bonus_note = f" | Scheduled: {job_datetime_note}" if job_datetime_note else ''
            # Create bonus payout for quoted_by person
            # Note: If quoted_by is also an assignee, they will have TWO payouts:
            # 1. Their assignee payout (created above)
            # 2. This bonus payout
            bonus_payout = Payout.objects.create(
                account=account,
                employee=quoted_by_user,
                payout_type=bonus_type,
                amount=bonus_amount,
                project_value=project_value,
                rate_percentage=bonus_percentage,
                project_title=project_title,
                notes=f"Manual {bonus_type} bonus: {project_title}{bonus_note}"
            )
            created_payouts.append(bonus_payout)
        
        # Serialize created payouts
        serializer = PayoutSerializer(created_payouts, many=True)
        
        response_data = {
            'message': f'Successfully created {len(created_payouts)} payout(s)',
            'payouts': serializer.data,
        }
        warnings = list(errors)
        if quoted_by_warning:
            warnings.append(quoted_by_warning)
        if warnings:
            response_data['warnings'] = warnings

        status_code = status.HTTP_201_CREATED if created_payouts else status.HTTP_400_BAD_REQUEST
        if not created_payouts:
            response_data['error'] = 'No payouts were created. Please review the warnings.'
        return Response(response_data, status=status_code)
    
    def _create_hourly_payouts(self, request):
        project_title = request.data.get('project_title', '')
        job_date = request.data.get('job_date')
        start_time = request.data.get('start_time')
        end_time = request.data.get('end_time')
        employee_ids = request.data.get('employee_ids', [])
        
        if not employee_ids:
            return Response({
                'error': 'employee_ids is required (at least one employee)'
            }, status=status.HTTP_400_BAD_REQUEST)
        
        if not all([job_date, start_time, end_time]):
            return Response({
                'error': 'job_date, start_time, and end_time are required for hourly payouts'
            }, status=status.HTTP_400_BAD_REQUEST)
        
        # Parse date and time inputs
        try:
            job_date_value = datetime.strptime(job_date, '%Y-%m-%d').date()
        except (ValueError, TypeError):
            return Response({
                'error': 'job_date must be in YYYY-MM-DD format'
            }, status=status.HTTP_400_BAD_REQUEST)
        
        time_formats = ['%H:%M:%S', '%H:%M']
        start_dt = None
        end_dt = None
        for fmt in time_formats:
            if start_dt is None:
                try:
                    start_dt = datetime.strptime(start_time, fmt).time()
                except ValueError:
                    start_dt = None
            if end_dt is None:
                try:
                    end_dt = datetime.strptime(end_time, fmt).time()
                except ValueError:
                    end_dt = None
        if start_dt is None or end_dt is None:
            return Response({
                'error': 'start_time and end_time must be in HH:MM or HH:MM:SS format'
            }, status=status.HTTP_400_BAD_REQUEST)
        
        start_datetime = datetime.combine(job_date_value, start_dt)
        end_datetime = datetime.combine(job_date_value, end_dt)
        duration_seconds = (end_datetime - start_datetime).total_seconds()
        if duration_seconds <= 0:
            return Response({
                'error': 'end_time must be later than start_time'
            }, status=status.HTTP_400_BAD_REQUEST)
        
        hours_decimal = (Decimal(duration_seconds) / Decimal('3600')).quantize(Decimal('0.01'))
        
        # Convert to timezone-aware datetimes (UTC)
        if timezone.is_naive(start_datetime):
            start_datetime = timezone.make_aware(start_datetime)
        else:
            start_datetime = start_datetime.astimezone(timezone.utc)
        
        if timezone.is_naive(end_datetime):
            end_datetime = timezone.make_aware(end_datetime)
        else:
            end_datetime = end_datetime.astimezone(timezone.utc)
        
        # Prepare employees
        account = getattr(request, 'account', None)
        if not account:
            return Response({'error': 'Account context is required.'}, status=status.HTTP_403_FORBIDDEN)
        
        created_payouts = []
        errors = []
        for employee_id in employee_ids:
            employee, err = _resolve_calculator_user_id(employee_id, account)
            if err or not employee:
                errors.append(f'Employee {employee_id}: {err or "not found"}')
                continue
            
            try:
                profile = employee.employee_profile
            except EmployeeProfile.DoesNotExist:
                errors.append(f'{employee.get_full_name() or employee.username} does not have an employee profile')
                continue
            
            if profile.pay_scale_type != 'hourly' or not profile.hourly_rate:
                errors.append(f'{employee.get_full_name() or employee.username} is not configured for hourly payouts')
                continue
            
            # Create TimeEntry record first
            time_entry = TimeEntry.objects.create(
                account=account,
                employee=employee,
                check_in_time=start_datetime,
                check_out_time=end_datetime,
                total_hours=hours_decimal,
                status='checked_out',
                notes=f"Manual entry: {project_title or 'job'} on {job_date}"
            )
            
            # Calculate payout amount
            amount = (hours_decimal * profile.hourly_rate).quantize(Decimal('0.01'))
            
            # Create payout linked to TimeEntry
            payout = Payout.objects.create(
                account=account,
                employee=employee,
                payout_type='hourly',
                amount=amount,
                time_entry=time_entry,
                project_title=project_title,
                notes=f"Manual hourly payout: {hours_decimal} hours @ ${profile.hourly_rate} = ${amount}"
            )
            created_payouts.append(payout)
        
        serializer = PayoutSerializer(created_payouts, many=True)
        response_data = {
            'message': f'Successfully created {len(created_payouts)} hourly payout(s)',
            'hours': float(hours_decimal),
            'payouts': serializer.data
        }
        if errors:
            response_data['warnings'] = errors
        
        if not created_payouts:
            response_data['error'] = 'No payouts were created. Please review the warnings.'
            return Response(response_data, status=status.HTTP_400_BAD_REQUEST)
        
        return Response(response_data, status=status.HTTP_201_CREATED)


class ReportsView(APIView):
    """Reports view combining time entries and payouts (scoped to current account)."""
    permission_classes = [AccountScopedPermission, IsAdminOrEmployeePermission]
    
    def get(self, request):
        """Get combined reports"""
        user = request.user
        account = getattr(request, 'account', None)
        if not account:
            return Response({'error': 'Account context is required.'}, status=status.HTTP_403_FORBIDDEN)
        can_view_team = _payroll_can_view_team_data(user)
        
        employee_id = request.query_params.get('employee', None)
        payout_type = request.query_params.get('type', None)
        start_date = request.query_params.get('start_date', None)
        end_date = request.query_params.get('end_date', None)
        project_title = request.query_params.get('project_title', None)
        
        from accounts.user_access import payouts_queryset_for_account, time_entries_queryset_for_account, users_queryset_for_account

        base_users = users_queryset_for_account(account).exclude(is_superuser=True)
        if can_view_team and employee_id:
            employee_filter = base_users.filter(pk=employee_id)
        elif not can_view_team:
            employee_filter = base_users.filter(pk=user.id)
        else:
            employee_filter = base_users
        
        payouts_query = payouts_queryset_for_account(account).filter(employee__in=employee_filter)
        
        if payout_type:
            payouts_query = payouts_query.filter(payout_type=payout_type)
        if start_date:
            try:
                start = datetime.strptime(start_date, '%Y-%m-%d')
                payouts_query = payouts_query.filter(created_at__gte=start)
            except ValueError:
                pass
        if end_date:
            try:
                end = datetime.strptime(end_date, '%Y-%m-%d') + timedelta(days=1)
                payouts_query = payouts_query.filter(created_at__lt=end)
            except ValueError:
                pass
        if project_title:
            payouts_query = payouts_query.filter(
                Q(project_title__icontains=project_title) |
                Q(job__title__icontains=project_title)
            )
        
        payouts = payouts_query.select_related('employee', 'job', 'time_entry')
        
        # Get time entries (for hourly employees)
        time_entries_query = time_entries_queryset_for_account(account).filter(
            employee__in=employee_filter,
            status='checked_out'
        )
        
        if start_date:
            try:
                start = datetime.strptime(start_date, '%Y-%m-%d')
                time_entries_query = time_entries_query.filter(check_in_time__gte=start)
            except ValueError:
                pass
        if end_date:
            try:
                end = datetime.strptime(end_date, '%Y-%m-%d') + timedelta(days=1)
                time_entries_query = time_entries_query.filter(check_in_time__lt=end)
            except ValueError:
                pass
        
        time_entries = time_entries_query.select_related('employee')
        
        # Calculate totals
        total_earnings = payouts.aggregate(total=Sum('amount'))['total'] or Decimal('0.00')
        
        # Serialize data
        payout_serializer = PayoutSerializer(payouts, many=True)
        time_entry_serializer = TimeEntrySerializer(time_entries, many=True)
        
        return Response({
            'payouts': payout_serializer.data,
            'time_entries': time_entry_serializer.data,
            'total_earnings': float(total_earnings),
            'payout_count': payouts.count(),
            'time_entry_count': time_entries.count()
        })


class PayrollSettingsViewSet(viewsets.ModelViewSet):
    """ViewSet for payroll settings (admin only, one per account)."""
    queryset = PayrollSettings.objects.all()
    serializer_class = PayrollSettingsSerializer
    permission_classes = [AccountScopedPermission, IsPayrollAdminPermission]
    
    def get_object(self):
        """Return the settings instance for the current account."""
        account = getattr(self.request, 'account', None)
        return PayrollSettings.get_settings(account)
    
    def get_queryset(self):
        """List: return only the current account's settings."""
        account = getattr(self.request, 'account', None)
        if not account:
            return PayrollSettings.objects.none()
        return PayrollSettings.objects.filter(account=account)
    
    def list(self, request, *args, **kwargs):
        """Return the singleton instance as a list"""
        instance = self.get_object()
        serializer = self.get_serializer(instance)
        return Response([serializer.data])
    
    def retrieve(self, request, *args, **kwargs):
        """Return the singleton instance"""
        instance = self.get_object()
        serializer = self.get_serializer(instance)
        return Response(serializer.data)
    
    def perform_create(self, serializer):
        account = getattr(self.request, 'account', None)
        serializer.save(account=account)
    
    def perform_update(self, serializer):
        # get_object() already returns account-scoped instance
        serializer.save()
