from django.db import models
from django.core.exceptions import ValidationError
from decimal import Decimal
import uuid
from service_app.models import User


class EmployeeProfile(models.Model):
    """Extended employee information linked to User. Scoped to one GHL account."""
    PAY_SCALE_CHOICES = [
        ('hourly', 'Hourly'),
        ('project', 'Project'),
    ]
    
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    account = models.ForeignKey(
        'accounts.GHLAuthCredentials',
        on_delete=models.CASCADE,
        related_name='employee_profiles',
        null=True,
        blank=True,
        help_text='GHL account this employee belongs to (for multi-account onboarding)',
    )
    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name='employee_profile')
    
    # Basic Info
    phone = models.CharField(max_length=20, blank=True, null=True)
    address = models.TextField(blank=True, null=True)
    date_of_birth = models.DateField(blank=True, null=True)
    hire_date = models.DateField(blank=True, null=True)
    emergency_contact_name = models.CharField(max_length=100, blank=True, null=True)
    emergency_contact_number = models.CharField(max_length=20, blank=True, null=True)
    department = models.CharField(max_length=100)
    position = models.CharField(max_length=100)
    timezone = models.CharField(max_length=50, default='America/Chicago')
    
    # Pay Scale Settings
    pay_scale_type = models.CharField(max_length=20, choices=PAY_SCALE_CHOICES)
    hourly_rate = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        null=True, 
        blank=True,
        help_text="Required if pay_scale_type is 'hourly'"
    )
    
    # Administrator Access
    is_administrator = models.BooleanField(
        default=False,
        help_text="Grants access to view stats, edit records, and manage time entries"
    )
    
    # Status
    status = models.CharField(
        max_length=20,
        choices=[('active', 'Active'), ('inactive', 'Inactive')],
        default='active'
    )
    
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    class Meta:
        db_table = 'employee_profiles'
        ordering = ['-created_at']
    
    def __str__(self):
        return f"{self.user.get_full_name() or self.user.username} - {self.department}"
    
    def clean(self):
        if self.pay_scale_type == 'hourly' and not self.hourly_rate:
            raise ValidationError({'hourly_rate': 'Hourly rate is required for hourly employees'})

    def save(self, *args, **kwargs):
        if self.account_id is None and self.user_id:
            self.account_id = getattr(self.user, 'account_id', None)
        super().save(*args, **kwargs)


class EmployeeTimeOff(models.Model):
    """
    Calendar time off for an employee: single day (start_date == end_date)
    or inclusive date range. Supports full day, half day (AM/PM), and custom
    hours on one day or on the first/last day of a multi-day range.
    """
    KIND_CHOICES = [
        ('day_off', 'Day off'),
        ('vacation', 'Vacation'),
        ('sick', 'Sick'),
        ('personal', 'Personal'),
        ('other', 'Other'),
    ]
    COVERAGE_CHOICES = [
        ('full_day', 'Full day'),
        ('half_day_am', 'Half day (morning)'),
        ('half_day_pm', 'Half day (afternoon)'),
        ('custom', 'Custom hours'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    employee = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name='time_off_entries',
    )
    start_date = models.DateField()
    end_date = models.DateField(
        help_text='Last day off (inclusive).'
    )
    kind = models.CharField(
        max_length=20,
        choices=KIND_CHOICES,
        default='day_off',
    )
    # Single-day (start_date == end_date): how much of that day is off.
    coverage = models.CharField(
        max_length=20,
        choices=COVERAGE_CHOICES,
        default='full_day',
        help_text='Used when start_date equals end_date.',
    )
    # Multi-day range: coverage for the first and last calendar days.
    start_day_coverage = models.CharField(
        max_length=20,
        choices=COVERAGE_CHOICES,
        default='full_day',
    )
    end_day_coverage = models.CharField(
        max_length=20,
        choices=COVERAGE_CHOICES,
        default='full_day',
    )
    # Custom window on start_date (single-day coverage or start_day_coverage).
    start_time = models.TimeField(null=True, blank=True)
    end_time = models.TimeField(null=True, blank=True)
    # Custom window on end_date when it differs from start_date.
    end_start_time = models.TimeField(null=True, blank=True)
    end_end_time = models.TimeField(null=True, blank=True)
    notes = models.TextField(blank=True, null=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'employee_time_off'
        ordering = ['-start_date', '-created_at']
        indexes = [
            models.Index(fields=['employee', 'start_date', 'end_date']),
        ]

    def __str__(self):
        return (
            f"{self.employee.get_full_name() or self.employee.username} "
            f"{self.start_date}–{self.end_date} ({self.kind})"
        )

    @property
    def is_single_day(self):
        return self.start_date == self.end_date

    def clean(self):
        from .time_off_utils import validate_time_off_entry

        if self.end_date and self.start_date and self.end_date < self.start_date:
            raise ValidationError({
                'end_date': 'End date must be on or after start date.',
            })
        errors = validate_time_off_entry(self)
        if errors:
            raise ValidationError(errors)


class CollaborationRate(models.Model):
    """Percentage rates for project-based employees based on team size"""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    employee = models.ForeignKey(
        User, 
        on_delete=models.CASCADE, 
        related_name='collaboration_rates'
    )
    member_count = models.PositiveIntegerField(
        help_text="Number of team members (1=solo, 2=two members, etc.)"
    )
    percentage = models.DecimalField(
        max_digits=5, 
        decimal_places=2,
        help_text="Percentage rate for this team size"
    )
    
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    class Meta:
        db_table = 'collaboration_rates'
        unique_together = ['employee', 'member_count']
        ordering = ['employee', 'member_count']
    
    def __str__(self):
        return f"{self.employee.username} - {self.member_count} members: {self.percentage}%"


class TimeEntry(models.Model):
    """Time clock entries for hourly employees"""
    STATUS_CHOICES = [
        ('checked_in', 'Checked In'),
        ('checked_out', 'Checked Out'),
    ]
    
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    account = models.ForeignKey(
        'accounts.GHLAuthCredentials',
        on_delete=models.CASCADE,
        related_name='time_entries',
        null=True,
        blank=True,
        help_text='GHL subaccount where this clock entry was created.',
    )
    employee = models.ForeignKey(User, on_delete=models.CASCADE, related_name='time_entries')
    
    check_in_time = models.DateTimeField()
    check_out_time = models.DateTimeField(null=True, blank=True)
    total_hours = models.DecimalField(
        max_digits=5, 
        decimal_places=2,
        null=True,
        blank=True,
        help_text="Calculated automatically on check-out"
    )
    notes = models.TextField(blank=True, null=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='checked_in')
    
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    class Meta:
        db_table = 'time_entries'
        ordering = ['-check_in_time']
    
    def __str__(self):
        return f"{self.employee.username} - {self.check_in_time.strftime('%Y-%m-%d %H:%M')}"
    
    def save(self, *args, **kwargs):
        if self.account_id is None and self.employee_id:
            self.account_id = getattr(self.employee, 'account_id', None)
        super().save(*args, **kwargs)
    
    def calculate_hours(self):
        """Calculate total hours worked"""
        if self.check_out_time and self.check_in_time:
            delta = self.check_out_time - self.check_in_time
            hours = Decimal(str(delta.total_seconds() / 3600))
            return hours.quantize(Decimal('0.01'))
        return None


class Payout(models.Model):
    """All payouts for employees (hourly, project, bonuses)"""
    PAYOUT_TYPE_CHOICES = [
        ('hourly', 'Hourly'),
        ('project', 'Project'),
        ('bonus_first_time', 'First Time Bonus'),
        ('bonus_quoted_by', 'Quoted By Bonus'),
        ('tip', 'Tip'),
    ]
    
    SOURCE_CHOICES = [
        ('auto', 'Auto'),
        ('manual', 'Manual'),
    ]
    
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    account = models.ForeignKey(
        'accounts.GHLAuthCredentials',
        on_delete=models.CASCADE,
        related_name='payouts',
        null=True,
        blank=True,
        help_text='GHL subaccount this payout belongs to.',
    )
    employee = models.ForeignKey(User, on_delete=models.CASCADE, related_name='payouts')
    payout_type = models.CharField(max_length=20, choices=PAYOUT_TYPE_CHOICES)
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    
    # For hourly payouts
    time_entry = models.ForeignKey(
        TimeEntry, 
        on_delete=models.SET_NULL, 
        null=True, 
        blank=True,
        related_name='payouts'
    )
    
    # For project payouts
    job = models.ForeignKey(
        'jobtracker_app.Job', 
        on_delete=models.SET_NULL, 
        null=True, 
        blank=True,
        related_name='payouts'
    )
    project_value = models.DecimalField(
        max_digits=12, 
        decimal_places=2, 
        null=True, 
        blank=True
    )
    rate_percentage = models.DecimalField(
        max_digits=5, 
        decimal_places=2, 
        null=True, 
        blank=True
    )
    
    # For manual calculator entries
    project_title = models.CharField(max_length=255, blank=True, null=True)
    notes = models.TextField(blank=True, null=True)
    
    # Source of payout (auto or manual)
    source = models.CharField(max_length=10, choices=SOURCE_CHOICES, default='auto', blank=True, null=True)
    
    created_at = models.DateTimeField(auto_now_add=True)
    
    class Meta:
        db_table = 'payouts'
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['employee', '-created_at']),
            models.Index(fields=['job', 'employee']),  # For duplicate prevention
        ]
    
    def __str__(self):
        return f"{self.employee.username} - {self.payout_type} - ${self.amount}"

    def save(self, *args, **kwargs):
        if self.account_id is None:
            if self.time_entry_id:
                te_account_id = (
                    self.time_entry.account_id
                    if getattr(self, 'time_entry', None) is not None
                    else None
                )
                if te_account_id is None:
                    from payroll_app.models import TimeEntry
                    te_account_id = (
                        TimeEntry.objects.filter(pk=self.time_entry_id)
                        .values_list('account_id', flat=True)
                        .first()
                    )
                self.account_id = te_account_id
            elif self.job_id:
                job_account_id = (
                    self.job.account_id
                    if getattr(self, 'job', None) is not None
                    else None
                )
                if job_account_id is None:
                    from jobtracker_app.models import Job
                    job_account_id = (
                        Job.objects.filter(pk=self.job_id)
                        .values_list('account_id', flat=True)
                        .first()
                    )
                self.account_id = job_account_id
            elif self.employee_id:
                self.account_id = getattr(self.employee, 'account_id', None)
        super().save(*args, **kwargs)


class PayrollSettings(models.Model):
    """Singleton model for payroll bonus settings (one per account)."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    account = models.ForeignKey(
        'accounts.GHLAuthCredentials',
        on_delete=models.CASCADE,
        related_name='payroll_settings',
        null=True,
        blank=True,
        help_text='GHL account these settings belong to (for multi-account onboarding)',
    )
    first_time_bonus_percentage = models.DecimalField(
        max_digits=5, 
        decimal_places=2, 
        default=Decimal('15.00'),
        help_text="Bonus for the quoted-by employee on first-time projects"
    )
    quoted_by_bonus_percentage = models.DecimalField(
        max_digits=5, 
        decimal_places=2, 
        default=Decimal('2.00'),
        help_text="Bonus for the quoted-by employee on regular (non-first-time) projects"
    )
    
    updated_at = models.DateTimeField(auto_now=True)
    
    class Meta:
        db_table = 'payroll_settings'
        verbose_name = 'Payroll Settings'
        verbose_name_plural = 'Payroll Settings'
    
    def __str__(self):
        return "Payroll Settings"
    
    def save(self, *args, **kwargs):
        # Ensure only one settings record exists per account
        if self.account_id is not None:
            existing = PayrollSettings.objects.filter(account_id=self.account_id).first()
            if existing and existing.pk != self.pk:
                self.pk = existing.pk
        super().save(*args, **kwargs)
    
    @classmethod
    def get_settings(cls, account=None):
        """Get or create the singleton settings instance for the given account."""
        if account is None:
            obj = cls.objects.filter(account__isnull=True).first()
            if not obj:
                obj = cls.objects.create()
            return obj
        obj = cls.objects.filter(account=account).first()
        if not obj:
            obj = cls.objects.create(account=account)
        return obj
