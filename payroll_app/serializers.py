from rest_framework import serializers
from decimal import Decimal
import pytz
from django.utils import timezone as django_timezone
from service_app.models import User
from .access import payroll_can_manage_time_off
from .models import (
    EmployeeProfile,
    CollaborationRate,
    TimeEntry,
    Payout,
    PayrollSettings,
    EmployeeTimeOff,
)
from .utils import get_user_timezone, convert_utc_to_user_timezone
from .time_off_utils import validate_time_off_entry, equivalent_days


class CollaborationRateSerializer(serializers.ModelSerializer):
    class Meta:
        model = CollaborationRate
        fields = ['id', 'member_count', 'percentage', 'created_at', 'updated_at']
        read_only_fields = ['id', 'created_at', 'updated_at']


class CollaborationRateCreateSerializer(serializers.ModelSerializer):
    """Serializer for creating/updating collaboration rates"""
    class Meta:
        model = CollaborationRate
        fields = ['id', 'member_count', 'percentage']
        read_only_fields = ['id']
    
    def validate_member_count(self, value):
        """Validate member_count is between 1 and 10"""
        if value < 1 or value > 10:
            raise serializers.ValidationError("member_count must be between 1 and 10")
        return value
    
    def validate_percentage(self, value):
        """Validate percentage is between 0 and 100"""
        if value < 0 or value > 100:
            raise serializers.ValidationError("percentage must be between 0 and 100")
        return value


class EmployeeProfileSerializer(serializers.ModelSerializer):
    user = serializers.PrimaryKeyRelatedField(
        queryset=User.objects.all(),
        write_only=True,
        required=False
    )
    collaboration_rates = CollaborationRateCreateSerializer(many=True, required=False, write_only=True)
    collaboration_rates_read = serializers.SerializerMethodField(read_only=True)
    user_id = serializers.IntegerField(source='user.id', read_only=True)
    username = serializers.CharField(source='user.username', read_only=True)
    email = serializers.EmailField(source='user.email', read_only=True)
    first_name = serializers.CharField(source='user.first_name', read_only=True)
    last_name = serializers.CharField(source='user.last_name', read_only=True)
    full_name = serializers.SerializerMethodField()
    is_active = serializers.BooleanField(source='user.is_active',read_only=True)

    class Meta:
        model = EmployeeProfile
        fields = [
            'id', 'user', 'user_id', 'username', 'email', 'first_name', 'last_name', 'full_name',
            'phone', 'address', 'date_of_birth', 'hire_date', 'emergency_contact_name', 
            'emergency_contact_number', 'department', 'position', 'timezone',
            'pay_scale_type', 'hourly_rate', 'is_administrator', 'status','is_active',
            'collaboration_rates', 'collaboration_rates_read', 'created_at', 'updated_at'
        ]
        read_only_fields = ['id', 'created_at', 'updated_at']
    
    def get_full_name(self, obj):
        return obj.user.get_full_name() or obj.user.username
    
    def get_collaboration_rates_read(self, obj):
        """Read-only field for collaboration rates"""
        rates_qs = getattr(obj.user, 'collaboration_rates', None)
        if hasattr(rates_qs, 'all'):
            queryset = rates_qs.all()
        else:
            queryset = CollaborationRate.objects.filter(employee=obj.user)
        return CollaborationRateSerializer(
            queryset.order_by('member_count'),
            many=True
        ).data
    
    def validate(self, data):
        if not self.instance and not data.get('user'):
            raise serializers.ValidationError({
                'user': 'This field is required.'
            })
        if data.get('pay_scale_type') == 'hourly' and not data.get('hourly_rate'):
            raise serializers.ValidationError({
                'hourly_rate': 'Hourly rate is required for hourly employees'
            })
        return data
    
    def create(self, validated_data):
        """Create employee profile and collaboration rates"""
        collaboration_rates_data = validated_data.pop('collaboration_rates', [])
        employee_profile = EmployeeProfile.objects.create(**validated_data)
        
        # Sync User.is_active with EmployeeProfile.status
        user = employee_profile.user
        if employee_profile.status == 'inactive':
            user.is_active = False
        elif employee_profile.status == 'active':
            user.is_active = True
        user.save()
        
        # Create collaboration rates if provided
        if collaboration_rates_data:
            for rate_data in collaboration_rates_data:
                CollaborationRate.objects.create(
                    employee=employee_profile.user,
                    member_count=rate_data['member_count'],
                    percentage=rate_data['percentage']
                )
        
        return employee_profile
    
    def update(self, instance, validated_data):
        """Update employee profile and collaboration rates"""
        collaboration_rates_data = validated_data.pop('collaboration_rates', None)
        
        # Track if status is being changed
        status_changed = 'status' in validated_data
        old_status = instance.status
        new_status = validated_data.get('status') if status_changed else None
        
        # Update employee profile fields
        for attr, value in validated_data.items():
            setattr(instance, attr, value)
        instance.save()
        
        # Sync User.is_active with EmployeeProfile.status
        if status_changed and new_status != old_status:
            user = instance.user
            if new_status == 'inactive':
                user.is_active = False
            elif new_status == 'active':
                user.is_active = True
            user.save()
        
        # Update collaboration rates if provided
        if collaboration_rates_data is not None:
            # Delete existing rates
            CollaborationRate.objects.filter(employee=instance.user).delete()
            
            # Create new rates
            for rate_data in collaboration_rates_data:
                CollaborationRate.objects.create(
                    employee=instance.user,
                    member_count=rate_data['member_count'],
                    percentage=rate_data['percentage']
                )
        
        return instance
    
    def to_representation(self, instance):
        """Override to include collaboration_rates in response"""
        representation = super().to_representation(instance)
        # Use the read-only field for response
        representation['collaboration_rates'] = representation.pop('collaboration_rates_read', [])
        return representation


class CollaborationRateCreateSerializer(serializers.ModelSerializer):
    """Serializer for creating/updating collaboration rates"""
    class Meta:
        model = CollaborationRate
        fields = ['id', 'member_count', 'percentage']
        read_only_fields = ['id']
    
    def validate_member_count(self, value):
        """Validate member_count is between 1 and 10"""
        if value < 1 or value > 10:
            raise serializers.ValidationError("member_count must be between 1 and 10")
        return value
    
    def validate_percentage(self, value):
        """Validate percentage is between 0 and 100"""
        if value < 0 or value > 100:
            raise serializers.ValidationError("percentage must be between 0 and 100")
        return value


class AvailableEmployeeSerializer(serializers.ModelSerializer):
    """Minimal user shape for who is not on time off in a date range."""
    full_name = serializers.SerializerMethodField()

    class Meta:
        model = User
        fields = ['id', 'username', 'email', 'first_name', 'last_name', 'full_name']

    def get_full_name(self, obj):
        return obj.get_full_name() or obj.username


class EmployeeTimeOffSerializer(serializers.ModelSerializer):
    employee_name = serializers.CharField(source='employee.get_full_name', read_only=True)
    employee_email = serializers.EmailField(source='employee.email', read_only=True)
    equivalent_days = serializers.SerializerMethodField(read_only=True)
    is_single_day = serializers.SerializerMethodField(read_only=True)

    class Meta:
        model = EmployeeTimeOff
        fields = [
            'id',
            'employee',
            'employee_name',
            'employee_email',
            'start_date',
            'end_date',
            'kind',
            'coverage',
            'start_day_coverage',
            'end_day_coverage',
            'start_time',
            'end_time',
            'end_start_time',
            'end_end_time',
            'equivalent_days',
            'is_single_day',
            'notes',
            'created_at',
            'updated_at',
        ]
        read_only_fields = ['id', 'created_at', 'updated_at']

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        request = self.context.get('request')
        if request and request.user.is_authenticated and not payroll_can_manage_time_off(
            request.user
        ):
            self.fields['employee'].read_only = True

    def get_equivalent_days(self, obj):
        return str(equivalent_days(obj))

    def get_is_single_day(self, obj):
        return obj.is_single_day

    def _build_validation_instance(self, data):
        """Merge payload with instance for cross-field validation."""
        class _Holder:
            pass

        h = _Holder()
        inst = self.instance
        for field in (
            'start_date', 'end_date', 'coverage', 'start_day_coverage',
            'end_day_coverage', 'start_time', 'end_time', 'end_start_time',
            'end_end_time',
        ):
            if field in data:
                setattr(h, field, data[field])
            elif inst is not None:
                setattr(h, field, getattr(inst, field))
            else:
                setattr(h, field, None)

        if h.start_date is None or h.end_date is None:
            h.is_single_day = False
        else:
            h.is_single_day = h.start_date == h.end_date

        defaults = {
            'coverage': 'full_day',
            'start_day_coverage': 'full_day',
            'end_day_coverage': 'full_day',
        }
        for key, default in defaults.items():
            if getattr(h, key) in (None, ''):
                setattr(h, key, default)

        return h

    def validate(self, data):
        start = data.get('start_date')
        end = data.get('end_date')
        if self.instance:
            if start is None:
                start = self.instance.start_date
            if end is None:
                end = self.instance.end_date
        if start and end and end < start:
            raise serializers.ValidationError({
                'end_date': 'End date must be on or after start date.',
            })

        holder = self._build_validation_instance(data)
        errors = validate_time_off_entry(holder)
        if errors:
            raise serializers.ValidationError(errors)
        return data


class TimeEntrySerializer(serializers.ModelSerializer):
    employee_name = serializers.CharField(source='employee.get_full_name', read_only=True)
    employee_email = serializers.EmailField(source='employee.email', read_only=True)
    employee_timezone = serializers.SerializerMethodField()
    check_in_time_local = serializers.SerializerMethodField()
    check_out_time_local = serializers.SerializerMethodField()
    
    class Meta:
        model = TimeEntry
        fields = [
            'id', 'employee', 'employee_name', 'employee_email', 'employee_timezone',
            'check_in_time', 'check_in_time_local', 'check_out_time', 'check_out_time_local',
            'total_hours', 'notes', 'status', 'created_at', 'updated_at'
        ]
        read_only_fields = ['id', 'status', 'created_at', 'updated_at']
    
    def get_employee_timezone(self, obj):
        """Get employee's timezone"""
        return get_user_timezone(obj.employee)
    
    def get_check_in_time_local(self, obj):
        """Convert check_in_time to employee's local timezone"""
        if not obj.check_in_time:
            return None
        return self._convert_to_user_timezone(obj.check_in_time, obj.employee)
    
    def get_check_out_time_local(self, obj):
        """Convert check_out_time to employee's local timezone"""
        if not obj.check_out_time:
            return None
        return self._convert_to_user_timezone(obj.check_out_time, obj.employee)
    
    def _convert_to_user_timezone(self, utc_datetime, user):
        """Convert UTC datetime to user's timezone"""
        try:
            local_time = convert_utc_to_user_timezone(utc_datetime, user)
            return local_time.isoformat()
        except Exception:
            # Fallback to UTC if timezone conversion fails
            if django_timezone.is_naive(utc_datetime):
                utc_datetime = django_timezone.make_aware(utc_datetime, pytz.UTC)
            return utc_datetime.isoformat()
    
    def validate(self, data):
        if 'check_out_time' in data and 'check_in_time' in data:
            # Ensure both datetimes are timezone-aware
            check_in = data.get('check_in_time')
            check_out = data.get('check_out_time')
            
            if check_in and check_out:
                # Convert to UTC if they're naive or in different timezone
                if django_timezone.is_naive(check_in):
                    check_in = django_timezone.make_aware(check_in, pytz.UTC)
                else:
                    check_in = check_in.astimezone(pytz.UTC)
                
                if django_timezone.is_naive(check_out):
                    check_out = django_timezone.make_aware(check_out, pytz.UTC)
                else:
                    check_out = check_out.astimezone(pytz.UTC)
                
                if check_out <= check_in:
                    raise serializers.ValidationError({
                        'check_out_time': 'Check-out time must be after check-in time'
                    })
        return data
    
    def create(self, validated_data):
        """Create time entry ensuring all times are in UTC"""
        # Ensure check_in_time is in UTC
        if 'check_in_time' in validated_data:
            check_in = validated_data['check_in_time']
            if django_timezone.is_naive(check_in):
                validated_data['check_in_time'] = django_timezone.make_aware(check_in, pytz.UTC)
            else:
                validated_data['check_in_time'] = check_in.astimezone(pytz.UTC)
        
        # Ensure check_out_time is in UTC
        if 'check_out_time' in validated_data:
            check_out = validated_data['check_out_time']
            if django_timezone.is_naive(check_out):
                validated_data['check_out_time'] = django_timezone.make_aware(check_out, pytz.UTC)
            else:
                validated_data['check_out_time'] = check_out.astimezone(pytz.UTC)
        
        return super().create(validated_data)
    
    def update(self, instance, validated_data):
        """Update time entry ensuring all times are in UTC"""
        # Ensure check_in_time is in UTC
        if 'check_in_time' in validated_data:
            check_in = validated_data['check_in_time']
            if django_timezone.is_naive(check_in):
                validated_data['check_in_time'] = django_timezone.make_aware(check_in, pytz.UTC)
            else:
                validated_data['check_in_time'] = check_in.astimezone(pytz.UTC)
        
        # Ensure check_out_time is in UTC
        if 'check_out_time' in validated_data:
            check_out = validated_data['check_out_time']
            if django_timezone.is_naive(check_out):
                validated_data['check_out_time'] = django_timezone.make_aware(check_out, pytz.UTC)
            else:
                validated_data['check_out_time'] = check_out.astimezone(pytz.UTC)
        
        # If total_hours is explicitly provided, use it
        # Otherwise, if check_in_time or check_out_time changed, recalculate
        total_hours_provided = 'total_hours' in validated_data
        times_changed = 'check_in_time' in validated_data or 'check_out_time' in validated_data
        
        # Update the instance
        updated_instance = super().update(instance, validated_data)
        
        # If times changed but total_hours wasn't explicitly provided, recalculate
        if times_changed and not total_hours_provided:
            # Refresh from DB to get updated times
            updated_instance.refresh_from_db()
            calculated_hours = updated_instance.calculate_hours()
            if calculated_hours is not None:
                updated_instance.total_hours = calculated_hours
                updated_instance.save(update_fields=['total_hours'])
        
        return updated_instance


class PayoutSerializer(serializers.ModelSerializer):
    employee_name = serializers.CharField(source='employee.get_full_name', read_only=True)
    employee_email = serializers.EmailField(source='employee.email', read_only=True)
    job_title = serializers.CharField(source='job.title', read_only=True)
    time_entry_details = serializers.SerializerMethodField()
    
    class Meta:
        model = Payout
        fields = [
            'id', 'employee', 'employee_name', 'employee_email',
            'payout_type', 'amount', 'time_entry', 'time_entry_details', 'job', 'job_title',
            'project_value', 'rate_percentage', 'project_title', 'notes',
            'created_at'
        ]
        read_only_fields = ['id', 'created_at']
    
    def get_time_entry_details(self, obj):
        """Include TimeEntry details for hourly payouts"""
        if obj.payout_type == 'hourly' and obj.time_entry:
            return TimeEntrySerializer(obj.time_entry).data
        return None


class PayrollSettingsSerializer(serializers.ModelSerializer):
    class Meta:
        model = PayrollSettings
        fields = [
            'id', 'first_time_bonus_percentage', 'quoted_by_bonus_percentage', 'updated_at'
        ]
        read_only_fields = ['id', 'updated_at']

