from django.db import transaction
from rest_framework import serializers
from .models import Job, JobServiceItem, JobAssignment, JobOccurrence, JobImage
from datetime import datetime, timedelta
import calendar
from service_app.models import User, Service, Appointment
from accounts.models import Contact, Address


class JobServiceItemSerializer(serializers.ModelSerializer):
    service_name = serializers.CharField(source='service.name', read_only=True)

    class Meta:
        model = JobServiceItem
        fields = ['id', 'service', 'service_name', 'custom_name', 'price', 'duration_hours']
        read_only_fields = ['id']


class JobAssignmentSerializer(serializers.ModelSerializer):
    user_email = serializers.EmailField(source='user.email', read_only=True)
    user_name = serializers.CharField(source='user.username', read_only=True)
    first_name = serializers.CharField(source='user.first_name', read_only=True)
    last_name = serializers.CharField(source='user.last_name', read_only=True)

    class Meta:
        model = JobAssignment
        fields = ['id', 'user', 'user_email', 'user_name', 'first_name', 'last_name', 'role']
        read_only_fields = ['id']


class JobOccurrenceSerializer(serializers.ModelSerializer):
    class Meta:
        model = JobOccurrence
        fields = ['id', 'scheduled_at', 'sequence']
        read_only_fields = ['id']


class OccurrenceEventSerializer(serializers.ModelSerializer):
    job_id = serializers.UUIDField(source='job.id')
    title = serializers.CharField(source='job.title')
    status = serializers.CharField(source='job.status')
    priority = serializers.CharField(source='job.priority')
    duration_hours = serializers.DecimalField(source='job.duration_hours', max_digits=5, decimal_places=2)

    class Meta:
        model = JobOccurrence
        fields = [
            'id', 'job_id', 'title', 'scheduled_at', 'sequence',
            'status', 'priority', 'duration_hours'
        ]


class CalendarEventSerializer(serializers.ModelSerializer):
    """Serializer for calendar view - works with Job model directly (supports both one-time and recurring series instances)"""
    job_id = serializers.UUIDField(source='id')
    company_name = serializers.SerializerMethodField()
    assigned_user_ids = serializers.SerializerMethodField()
    job_address = serializers.SerializerMethodField()

    class Meta:
        model = Job
        fields = [
            'job_id', 'title', 'scheduled_at', 'status', 'priority',
            'duration_hours', 'total_price', 'total_surcharge', 'customer_name', 'company_name',
            'series_id', 'series_sequence', 'job_type', 'assigned_user_ids', 'job_address'
        ]

    def get_job_address(self, obj):
        """Return job address from Address FK or customer_address."""
        if obj.address:
            return obj.address.get_full_address() or None
        return obj.customer_address or None

    def get_company_name(self, obj):
        if obj.contact:
            return obj.contact.company_name or None
        return None

    def get_assigned_user_ids(self, obj):
        """Return list of assigned user primary keys (integer) for this job."""
        ids = []
        for assignment in (obj.assignments.all() if hasattr(obj, 'assignments') else []):
            user = getattr(assignment, 'user', None)
            if user:
                ids.append(user.pk)
        return ids


class AppointmentCalendarSerializer(serializers.ModelSerializer):
    """Serializer for appointment calendar view"""
    appointment_id = serializers.UUIDField(source='id')
    assigned_user_name = serializers.SerializerMethodField()
    assigned_user_ids = serializers.SerializerMethodField()
    contact_name = serializers.SerializerMethodField()
    contact_company_name = serializers.SerializerMethodField()
    users_count = serializers.SerializerMethodField()
    calendar = serializers.SerializerMethodField()

    class Meta:
        model = Appointment
        fields = [
            'appointment_id', 'title', 'start_time', 'end_time',
            'appointment_status', 'assigned_user_name', 'assigned_user_ids', 'contact_name',
            'contact_company_name', 'address', 'notes', 'source', 'users_count', 'calendar',
            'ghl_contact_id'
        ]

    def get_assigned_user_name(self, obj):
        if obj.assigned_user:
            return obj.assigned_user.get_full_name() or obj.assigned_user.username
        return None

    def get_assigned_user_ids(self, obj):
        """Return list of user primary keys (integer) for this appointment: assigned_user + users."""
        ids = []
        seen = set()
        if obj.assigned_user:
            u = obj.assigned_user
            if u.pk not in seen:
                seen.add(u.pk)
                ids.append(u.pk)
        users_mgr = getattr(obj, 'users', None)
        if users_mgr is not None:
            for u in users_mgr.all():
                if u.pk not in seen:
                    seen.add(u.pk)
                    ids.append(u.pk)
        return ids

    def get_contact_name(self, obj):
        if obj.contact:
            return f"{obj.contact.first_name or ''} {obj.contact.last_name or ''}".strip() or None
        return None

    def get_users_count(self, obj):
        return obj.users.count()
    
    def get_contact_company_name(self, obj):
        if obj.contact:
            return obj.contact.company_name or None
        return None
    
    def get_calendar(self, obj):
        """Return full calendar information"""
        if obj.calendar:
            return {
                'id': obj.calendar.ghl_calendar_id,
                'name': obj.calendar.name,
                'description': obj.calendar.description,
                'widget_type': obj.calendar.widget_type,
                'calendar_type': obj.calendar.calendar_type,
                'widget_slug': obj.calendar.widget_slug,
                'group_id': obj.calendar.group_id
            }
        return None


class AppointmentSerializer(serializers.ModelSerializer):
    """Full serializer for CRUD operations on appointments"""
    appointment_id = serializers.UUIDField(source='id', read_only=True)
    assigned_user_id = serializers.UUIDField(source='assigned_user.id', read_only=True)
    assigned_user_name = serializers.SerializerMethodField()
    assigned_user_email = serializers.EmailField(source='assigned_user.email', read_only=True)
    assigned_user_uuid = serializers.UUIDField(
        write_only=True,
        required=False,
        help_text="UUID of the user to assign as the primary assigned user (users can view appointments they're assigned to)"
    )
    contact_id = serializers.CharField(source='contact.contact_id', read_only=True)
    contact_name = serializers.SerializerMethodField()
    contact_email = serializers.EmailField(source='contact.email', read_only=True)
    contact_phone = serializers.SerializerMethodField()
    contact_full_address = serializers.SerializerMethodField()
    calendar_id = serializers.SerializerMethodField()
    calendar = serializers.SerializerMethodField()
    users = serializers.SerializerMethodField()
    users_list = serializers.ListField(
        child=serializers.UUIDField(),
        write_only=True,
        required=False,
        help_text="List of user UUIDs to assign to this appointment (users can view appointments they're assigned to)"
    )

    class Meta:
        model = Appointment
        fields = [
            'appointment_id', 'ghl_appointment_id', 'location_id', 'title',
            'address', 'calendar_id', 'calendar', 'appointment_status', 'estimate_status', 'source', 'notes',
            'start_time', 'end_time', 'date_added', 'date_updated',
            'ghl_contact_id', 'group_id',
            'assigned_user_id', 'assigned_user_name', 'assigned_user_email', 'assigned_user_uuid',
            'ghl_assigned_user_id',
            'contact_id', 'contact_name', 'contact_email', 'contact_phone', 'contact_full_address',
            'users', 'users_list', 'users_ghl_ids',
            'created_at', 'updated_at'
        ]
        read_only_fields = [
            'appointment_id', 'ghl_appointment_id', 'date_added', 'date_updated',
            'created_at', 'updated_at'
        ]

    def get_assigned_user_name(self, obj):
        if obj.assigned_user:
            return obj.assigned_user.get_full_name() or obj.assigned_user.username
        return None

    def get_contact_name(self, obj):
        if obj.contact:
            return f"{obj.contact.first_name or ''} {obj.contact.last_name or ''}".strip() or None
        return None

    def get_contact_phone(self, obj):
        if obj.contact:
            return obj.contact.phone or None
        return None

    def get_contact_full_address(self, obj):
        """Return the full address string from the contact's first address (by order)."""
        if obj.contact:
            first_address = obj.contact.contact_location.order_by('order').first()
            if first_address:
                return first_address.get_full_address() or None
        return None

    def get_calendar_id(self, obj):
        """Return the GHL calendar ID from the calendar ForeignKey"""
        if obj.calendar:
            return obj.calendar.ghl_calendar_id
        return None

    def get_calendar(self, obj):
        """Return full calendar information"""
        if obj.calendar:
            return {
                'id': obj.calendar.ghl_calendar_id,
                'name': obj.calendar.name,
                'description': obj.calendar.description,
                'widget_type': obj.calendar.widget_type,
                'calendar_type': obj.calendar.calendar_type,
                'widget_slug': obj.calendar.widget_slug,
                'group_id': obj.calendar.group_id
            }
        return None

    def get_users(self, obj):
        """Return list of user details for the appointment"""
        return [
            {
                'id': str(user.id),
                'email': user.email,
                'name': user.get_full_name() or user.username,
                'ghl_user_id': user.ghl_user_id
            }
            for user in obj.users.all()
        ]

    def validate_appointment_status(self, value):
        """Validate appointment status"""
        valid_statuses = [choice[0] for choice in Appointment.APPOINTMENT_STATUS_CHOICES]
        if value and value not in valid_statuses:
            raise serializers.ValidationError(
                f"Invalid status. Must be one of: {', '.join(valid_statuses)}"
            )
        return value

    def validate(self, data):
        """Validate appointment data"""
        start_time = data.get('start_time')
        end_time = data.get('end_time')
        
        if start_time and end_time and start_time >= end_time:
            raise serializers.ValidationError({
                'end_time': 'End time must be after start time'
            })
        
        return data

    def create(self, validated_data):
        """Create a new appointment"""
        users_list = validated_data.pop('users_list', [])
        assigned_user_uuid = validated_data.pop('assigned_user_uuid', None)
        
        # Handle assigned_user if provided
        if assigned_user_uuid:
            try:
                assigned_user = User.objects.get(id=assigned_user_uuid)
                validated_data['assigned_user'] = assigned_user
                # Also set ghl_assigned_user_id if available
                if assigned_user.ghl_user_id:
                    validated_data['ghl_assigned_user_id'] = assigned_user.ghl_user_id
            except User.DoesNotExist:
                raise serializers.ValidationError({
                    'assigned_user_uuid': f'User with ID {assigned_user_uuid} does not exist'
                })
        
        # Generate ghl_appointment_id if not provided (for local appointments)
        if 'ghl_appointment_id' not in validated_data or not validated_data.get('ghl_appointment_id'):
            import uuid
            validated_data['ghl_appointment_id'] = f"local_{uuid.uuid4()}"
        
        appointment = Appointment.objects.create(**validated_data)
        
        # Handle users assignment (many-to-many)
        if users_list:
            users = User.objects.filter(id__in=users_list)
            appointment.users.set(users)
        
        return appointment

    def update(self, instance, validated_data):
        """Update an existing appointment"""
        users_list = validated_data.pop('users_list', None)
        assigned_user_uuid = validated_data.pop('assigned_user_uuid', None)
        
        # Handle assigned_user if provided
        if assigned_user_uuid is not None:
            if assigned_user_uuid:
                try:
                    assigned_user = User.objects.get(id=assigned_user_uuid)
                    instance.assigned_user = assigned_user
                    # Also set ghl_assigned_user_id if available
                    if assigned_user.ghl_user_id:
                        instance.ghl_assigned_user_id = assigned_user.ghl_user_id
                except User.DoesNotExist:
                    raise serializers.ValidationError({
                        'assigned_user_uuid': f'User with ID {assigned_user_uuid} does not exist'
                    })
            else:
                # Clear assigned_user if None is passed
                instance.assigned_user = None
                instance.ghl_assigned_user_id = None
        
        # Update other fields
        for attr, value in validated_data.items():
            setattr(instance, attr, value)
        instance.save()
        
        # Handle users assignment (many-to-many)
        if users_list is not None:
            users = User.objects.filter(id__in=users_list)
            instance.users.set(users)
        
        return instance
        
class JobImageSerializer(serializers.ModelSerializer):
    """Serializer for job images (stored in GHL only; image field not persisted to S3)."""
    image = serializers.ImageField(required=False, allow_null=True)
    image_url = serializers.SerializerMethodField()
    uploaded_by_name = serializers.SerializerMethodField()
    job_title = serializers.CharField(source='job.title', read_only=True)

    class Meta:
        model = JobImage
        fields = [
            'id', 'job', 'job_title', 'image', 'image_url', 'caption',
            'ghl_file_id', 'ghl_file_url',
            'uploaded_by', 'uploaded_by_name', 'created_at', 'updated_at'
        ]
        read_only_fields = ['id', 'uploaded_by', 'created_at', 'updated_at', 'ghl_file_id', 'ghl_file_url']

    def get_image_url(self, obj):
        """Return GHL URL when stored in GHL only, else local/S3 URL."""
        if obj.ghl_file_url:
            return obj.ghl_file_url
        if obj.image:
            request = self.context.get('request')
            if request:
                return request.build_absolute_uri(obj.image.url)
            return obj.image.url
        return None

    def get_uploaded_by_name(self, obj):
        """Return the name of the user who uploaded the image"""
        if obj.uploaded_by:
            return obj.uploaded_by.get_full_name() or obj.uploaded_by.username
        return None

class JobSerializer(serializers.ModelSerializer):
    items = JobServiceItemSerializer(many=True, required=False)
    assignments = JobAssignmentSerializer(many=True, required=False)
    occurrence_count = serializers.IntegerField(source='occurrences', read_only=True)
    occurrence_events = JobOccurrenceSerializer(many=True, read_only=True, source='schedule_occurrences')
    series_id = serializers.UUIDField(read_only=True)
    series_sequence = serializers.IntegerField(read_only=True)
    quoted_by_name = serializers.SerializerMethodField()
    slot_reserved_info = serializers.SerializerMethodField()
    account_timezone = serializers.SerializerMethodField()
    images = JobImageSerializer(many=True, read_only=True)
    contact_details = serializers.SerializerMethodField()
    address_details = serializers.SerializerMethodField()

    # Write-only fields for linking to Contact and Address models
    contact_id = serializers.IntegerField(
        write_only=True,
        required=False,
        allow_null=True,
        help_text="ID of Contact model (optional - if provided, customer info will be auto-populated)"
    )
    address_id = serializers.IntegerField(
        write_only=True,
        required=False,
        allow_null=True,
        help_text="ID of Address model (optional - if provided, customer_address will be auto-populated)"
    )

    class Meta:
        model = Job
        fields = [
            'id', 'submission', 'title', 'description', 'priority', 'duration_hours', 'scheduled_at',
            'total_price', 'total_surcharge', 'discount_type', 'discount_value', 'revised_total',
            'contact', 'address', 'contact_id', 'address_id', 'contact_details', 'address_details',  # New fields
            'customer_name', 'customer_phone', 'customer_email', 'customer_address', 'ghl_contact_id',
            'quoted_by', 'quoted_by_name', 'created_by', 'created_by_email',
            'job_type', 'repeat_every', 'repeat_unit', 'occurrences', 'day_of_week',
            'status', 'notes', 'payment_method', 'items', 'assignments',
            'occurrence_count', 'occurrence_events', 'series_id', 'series_sequence',
            'invoice_url', 'slot_reserved_info', 'account_timezone', 'images', 'created_at', 'updated_at'
        ]
        read_only_fields = [
            'id', 'contact', 'address', 'revised_total', 'account_timezone', 'created_at', 'updated_at',
        ]

    def get_contact_details(self, obj):
        """Return contact details if contact is linked"""
        if obj.contact:
            return {
                'id': obj.contact.id,
                'contact_id': obj.contact.contact_id,
                'first_name': obj.contact.first_name,
                'last_name': obj.contact.last_name,
                'company_name': obj.contact.company_name,
                'email': obj.contact.email,
                'phone': obj.contact.phone,
            }
        return None

    def get_address_details(self, obj):
        """Return address details if address is linked"""
        if obj.address:
            return {
                'id': obj.address.id,
                'address_id': obj.address.address_id,
                'name': obj.address.name,
                'street_address': obj.address.street_address,
                'city': obj.address.city,
                'state': obj.address.state,
                'postal_code': obj.address.postal_code,
                'full_address': obj.address.get_full_address(),
            }
        return None

    def get_quoted_by_name(self, obj):
        """Return the quoted_by user's full name or username"""
        if obj.quoted_by:
            full_name = obj.quoted_by.get_full_name()
            return full_name if full_name else obj.quoted_by.username
        return None

    def get_account_timezone(self, obj):
        """GHL location timezone for this job (not employee payroll timezone)."""
        from accounts.timezone_utils import DEFAULT_ACCOUNT_TIMEZONE

        acc = getattr(obj, 'account', None)
        if acc is not None and getattr(acc, 'timezone', None):
            tz = (acc.timezone or '').strip()
            if tz:
                return tz
        return DEFAULT_ACCOUNT_TIMEZONE

    def get_slot_reserved_info(self, obj):
        """
        Check if there's a matching appointment for this job (manual check, same logic as
        job_appointment_utils.get_slot_reserved_info_for_job). Only computed when fetching
        a single job (GET /api/job/jobs/{id}/) to avoid heavy DB work and N+1 on list endpoints.
        """
        if not self.context.get('include_slot_reserved_info'):
            return None
        from .job_appointment_utils import get_slot_reserved_info_for_job
        return get_slot_reserved_info_for_job(obj)

    def create(self, validated_data):
        items_data = validated_data.pop('items', [])
        assignments_data = validated_data.pop('assignments', [])
        
        # Set account from request context (user's account) so job is scoped correctly
        request = self.context.get('request')
        account = getattr(request, 'account', None) if request else None
        if account and not validated_data.get('account'):
            validated_data['account'] = account
        
        # Handle contact_id and address_id (write-only fields)
        contact_id = validated_data.pop('contact_id', None)
        address_id = validated_data.pop('address_id', None)
        
        # If contact_id is provided, get the contact and populate customer fields
        if contact_id:
            try:
                contact = Contact.objects.get(id=contact_id)
                validated_data['contact'] = contact
                
                # Populate customer fields from contact if not already provided
                if not validated_data.get('customer_name'):
                    # Combine first_name and last_name
                    name_parts = [contact.first_name, contact.last_name]
                    validated_data['customer_name'] = ' '.join(filter(None, name_parts)) or None
                
                if not validated_data.get('customer_phone'):
                    validated_data['customer_phone'] = contact.phone
                
                if not validated_data.get('customer_email'):
                    validated_data['customer_email'] = contact.email
                
                if not validated_data.get('ghl_contact_id'):
                    validated_data['ghl_contact_id'] = contact.contact_id
            except Contact.DoesNotExist:
                raise serializers.ValidationError({
                    'contact_id': f'Contact with id {contact_id} does not exist.'
                })
        
        # If address_id is provided, get the address and populate customer_address
        if address_id:
            try:
                address = Address.objects.get(id=address_id)
                validated_data['address'] = address
                
                # Populate customer_address from address if not already provided
                if not validated_data.get('customer_address'):
                    validated_data['customer_address'] = address.get_full_address()
            except Address.DoesNotExist:
                raise serializers.ValidationError({
                    'address_id': f'Address with id {address_id} does not exist.'
                })
        
        # Validate that if address is provided, it belongs to the contact (if contact is also provided)
        if validated_data.get('address') and validated_data.get('contact'):
            if validated_data['address'].contact != validated_data['contact']:
                raise serializers.ValidationError({
                    'address_id': 'The provided address does not belong to the provided contact.'
                })
        
        job = Job.objects.create(**validated_data)
        # Fallback: if still no account, set from submission when job came from a quote
        if job.account_id is None and job.submission_id:
            job.account_id = getattr(job.submission, 'account_id', None)
            if job.account_id:
                job.save(update_fields=['account'])

        for item in items_data:
            JobServiceItem.objects.create(job=job, **item)
        for assign in assignments_data:
            JobAssignment.objects.create(job=job, **assign)
        self._rebuild_occurrences(job)
        return job

    def update(self, instance, validated_data):
        items_data = validated_data.pop('items', None)
        assignments_data = validated_data.pop('assignments', None)
        
        # Handle contact_id and address_id (write-only fields) from initial_data
        contact_id = self.initial_data.get('contact_id')
        address_id = self.initial_data.get('address_id')
        
        # If contact_id is provided and not empty, get the contact and populate customer fields
        if contact_id:
            try:
                contact = Contact.objects.get(id=contact_id)
                validated_data['contact'] = contact
                
                # Override customer fields from contact
                name_parts = [contact.first_name, contact.last_name]
                validated_data['customer_name'] = ' '.join(filter(None, name_parts)) or None
                validated_data['customer_phone'] = contact.phone
                validated_data['customer_email'] = contact.email
                validated_data['ghl_contact_id'] = contact.contact_id
            except Contact.DoesNotExist:
                raise serializers.ValidationError({
                    'contact_id': f'Contact with id {contact_id} does not exist.'
                })
        
        # If address_id is provided and not empty, get the address and populate customer_address
        if address_id:
            try:
                address = Address.objects.get(id=address_id)
                validated_data['address'] = address
                
                # Override customer_address from address
                validated_data['customer_address'] = address.get_full_address()
            except Address.DoesNotExist:
                raise serializers.ValidationError({
                    'address_id': f'Address with id {address_id} does not exist.'
                })
        
        # Validate that if address is provided, it belongs to the contact (if contact is also provided)
        if validated_data.get('address') and validated_data.get('contact'):
            if validated_data['address'].contact != validated_data['contact']:
                raise serializers.ValidationError({
                    'address_id': 'The provided address does not belong to the provided contact.'
                })

        appointment_sync_fields = frozenset({
            'scheduled_at', 'duration_hours', 'title', 'customer_address',
        })
        will_sync_linked_appointment = (
            Appointment.objects.filter(job_id=instance.id).exists()
            and bool(appointment_sync_fields & set(validated_data.keys()))
        )

        for attr, value in validated_data.items():
            setattr(instance, attr, value)

        scheduling_fields = [
            'job_type', 'repeat_every', 'repeat_unit', 'occurrences', 'day_of_week', 'scheduled_at',
        ]
        need_occurrence_rebuild = any(f in self.initial_data for f in scheduling_fields)

        def _persist_related_and_occurrences():
            if items_data is not None:
                instance.items.all().delete()
                for item in items_data:
                    JobServiceItem.objects.create(job=instance, **item)
            if assignments_data is not None:
                instance.assignments.all().delete()
                for assign in assignments_data:
                    JobAssignment.objects.create(job=instance, **assign)
            if need_occurrence_rebuild:
                self._rebuild_occurrences(instance)

        if will_sync_linked_appointment:
            from .ghl_appointment_sync import sync_linked_appointment_from_job

            with transaction.atomic():
                instance._skip_linked_appointment_sync = True
                instance.save()
                _persist_related_and_occurrences()
                ok, err = sync_linked_appointment_from_job(instance)
                if not ok:
                    raise serializers.ValidationError(
                        err or 'Could not update calendar appointment in GoHighLevel.'
                    )
        else:
            instance.save()
            _persist_related_and_occurrences()

        return instance

    def validate(self, data):
        repeat_unit = data.get('repeat_unit')
        day_of_week = data.get('day_of_week')
        
        # If repeat_unit is 'week', day_of_week should be provided
        if repeat_unit == 'week' and day_of_week is None:
            raise serializers.ValidationError({
                'day_of_week': 'day_of_week is required when repeat_unit is "week"'
            })
        
        # If repeat_unit is not 'week', day_of_week should be None
        if repeat_unit and repeat_unit != 'week' and day_of_week is not None:
            raise serializers.ValidationError({
                'day_of_week': 'day_of_week should only be provided when repeat_unit is "week"'
            })
        
        # Prevent status changes after completion
        if self.instance and self.instance.status == 'completed':
            new_status = data.get('status')
            if new_status and new_status != 'completed':
                raise serializers.ValidationError({
                    'status': 'Cannot change status of a completed job. '
                             'Once a job is completed, its status cannot be modified.'
                })
        
        return data

    # ===== recurrence helpers =====
    def _rebuild_occurrences(self, job: Job):
        JobOccurrence.objects.filter(job=job).delete()
        if not job.scheduled_at:
            return
        if job.job_type == 'one_time':
            JobOccurrence.objects.create(job=job, scheduled_at=job.scheduled_at, sequence=1)
            return
        if job.job_type != 'recurring':
            return
        if not job.repeat_every or not job.repeat_unit or not job.occurrences:
            return
        dates = JobSerializer._build_occurrence_datetimes(
            job.scheduled_at, 
            job.repeat_every, 
            job.repeat_unit, 
            job.occurrences,
            day_of_week=job.day_of_week
        )
        for idx, dt in enumerate(dates, start=1):
            JobOccurrence.objects.create(job=job, scheduled_at=dt, sequence=idx)

    @staticmethod
    def _build_occurrence_datetimes(start_dt, repeat_every, repeat_unit, occurrences, day_of_week=None):
        result = []
        current = start_dt
        
        for i in range(occurrences):
            if i == 0:
                # For the first occurrence, if it's weekly and day_of_week is specified,
                # adjust to the correct day of week
                if repeat_unit == 'week' and day_of_week is not None:
                    # Get the current weekday (Python's weekday() is 0=Monday, 6=Sunday)
                    current_weekday = current.weekday()  # 0=Monday, 6=Sunday
                    days_to_add = (day_of_week - current_weekday) % 7
                    if days_to_add > 0:
                        current = current + timedelta(days=days_to_add)
                result.append(current)
                continue
                
            if repeat_unit == 'day':
                current = current + timedelta(days=repeat_every)
            elif repeat_unit == 'week':
                # For weekly, add the number of weeks
                current = current + timedelta(weeks=repeat_every)
                # Ensure it's on the correct day of week
                if day_of_week is not None:
                    current_weekday = current.weekday()
                    days_to_add = (day_of_week - current_weekday) % 7
                    if days_to_add > 0:
                        current = current + timedelta(days=days_to_add)
            elif repeat_unit in ['month', 'quarter', 'semi_annual', 'year']:
                months_to_add = repeat_every
                if repeat_unit == 'quarter':
                    months_to_add = 3 * repeat_every
                elif repeat_unit == 'semi_annual':
                    months_to_add = 6 * repeat_every
                elif repeat_unit == 'year':
                    months_to_add = 12 * repeat_every
                current = JobSerializer._add_months(current, months_to_add)
            else:
                current = current + timedelta(days=repeat_every)
            result.append(current)
        return result

    @staticmethod
    def _add_months(dt, months):
        month = dt.month - 1 + months
        year = dt.year + month // 12
        month = month % 12 + 1
        day = min(dt.day, calendar.monthrange(year, month)[1])
        return dt.replace(year=year, month=month, day=day)


class JobSeriesCreateSerializer(serializers.Serializer):
    # base job fields
    title = serializers.CharField()
    description = serializers.CharField(required=False, allow_blank=True)
    priority = serializers.ChoiceField(choices=['low', 'medium', 'high'], default='low')
    duration_hours = serializers.DecimalField(max_digits=5, decimal_places=2)
    scheduled_at = serializers.DateTimeField()
    total_price = serializers.DecimalField(max_digits=12, decimal_places=2)
    # Contact and Address fields (optional - if provided, customer info will be auto-populated)
    contact_id = serializers.IntegerField(required=False, allow_null=True, help_text="ID of Contact model (optional - if provided, customer info will be auto-populated)")
    address_id = serializers.IntegerField(required=False, allow_null=True, help_text="ID of Address model (optional - if provided, customer_address will be auto-populated)")
    # Customer info (can be manually provided or auto-populated from contact/address)
    customer_name = serializers.CharField(required=False, allow_blank=True)
    customer_phone = serializers.CharField(required=False, allow_blank=True)
    customer_email = serializers.EmailField(required=False, allow_blank=True)
    customer_address = serializers.CharField(required=False, allow_blank=True)
    ghl_contact_id = serializers.CharField(required=False, allow_blank=True)
    # Accept either UUID string or omit. We'll map to quoted_by_id in create
    quoted_by = serializers.CharField(required=False, allow_blank=True, allow_null=True)
    notes = serializers.CharField(required=False, allow_blank=True)
    # recurrence
    repeat_every = serializers.IntegerField(min_value=1)
    repeat_unit = serializers.ChoiceField(choices=['day', 'week', 'month', 'quarter', 'semi_annual', 'year'])
    occurrences = serializers.IntegerField(min_value=1)
    day_of_week = serializers.IntegerField(min_value=0, max_value=6, required=False, allow_null=True)
    # nested
    items = JobServiceItemSerializer(many=True, required=False)
    assignments = JobAssignmentSerializer(many=True, required=False)

    def create(self, validated):
        from uuid import uuid4
        base_dt = validated.pop('scheduled_at')
        repeat_every = validated.pop('repeat_every')
        repeat_unit = validated.pop('repeat_unit')
        count = validated.pop('occurrences')
        day_of_week = validated.pop('day_of_week', None)
        items = validated.pop('items', [])
        assigns = validated.pop('assignments', [])
        quoted_by_raw = validated.pop('quoted_by', None)
        
        # Handle contact_id and address_id (for auto-populating customer fields)
        contact_id = validated.pop('contact_id', None)
        address_id = validated.pop('address_id', None)
        
        # Prepare base job data with contact/address info
        job_data = validated.copy()
        
        # If contact_id is provided, get the contact and populate customer fields
        contact_obj = None
        if contact_id:
            try:
                contact_obj = Contact.objects.get(id=contact_id)
                job_data['contact'] = contact_obj
                
                # Populate customer fields from contact if not already provided
                if not job_data.get('customer_name'):
                    # Combine first_name and last_name
                    name_parts = [contact_obj.first_name, contact_obj.last_name]
                    job_data['customer_name'] = ' '.join(filter(None, name_parts)) or None
                
                if not job_data.get('customer_phone'):
                    job_data['customer_phone'] = contact_obj.phone
                
                if not job_data.get('customer_email'):
                    job_data['customer_email'] = contact_obj.email
                
                if not job_data.get('ghl_contact_id'):
                    job_data['ghl_contact_id'] = contact_obj.contact_id
            except Contact.DoesNotExist:
                raise serializers.ValidationError({
                    'contact_id': f'Contact with id {contact_id} does not exist.'
                })
        
        # If address_id is provided, get the address and populate customer_address
        address_obj = None
        if address_id:
            try:
                address_obj = Address.objects.get(id=address_id)
                job_data['address'] = address_obj
                
                # Populate customer_address from address if not already provided
                if not job_data.get('customer_address'):
                    job_data['customer_address'] = address_obj.get_full_address()
            except Address.DoesNotExist:
                raise serializers.ValidationError({
                    'address_id': f'Address with id {address_id} does not exist.'
                })
        
        # Validate that if address is provided, it belongs to the contact (if contact is also provided)
        if address_obj and contact_obj:
            if address_obj.contact != contact_obj:
                raise serializers.ValidationError({
                    'address_id': 'The provided address does not belong to the provided contact.'
                })

        request = self.context.get('request')
        creator = request.user if request and request.user.is_authenticated else None
        account = getattr(request, 'account', None) if request else None

        # build dates using the existing helper, passing day_of_week
        dates = JobSerializer._build_occurrence_datetimes(
            base_dt, repeat_every, repeat_unit, count, day_of_week=day_of_week
        )
        series = uuid4()
        created_ids = []

        for idx, dt in enumerate(dates, start=1):
            # normalize quoted_by: accept uuid/email/username
            qb_id = None
            if quoted_by_raw:
                qb_id = self._resolve_user_id(quoted_by_raw)
            
            # Create job with all the data (including contact, address, account)
            job = Job.objects.create(
                **job_data,
                scheduled_at=dt,
                job_type='recurring',
                repeat_every=repeat_every,
                repeat_unit=repeat_unit,
                occurrences=count,
                day_of_week=day_of_week,
                status='pending',
                created_by=creator,
                created_by_email=getattr(creator, 'email', None),
                series_id=series,
                series_sequence=idx,
                account=account,
                **({ 'quoted_by_id': qb_id } if qb_id else {})
            )
            for it in items:
                # Accept either service UUID or a service name, or a pure custom item
                service_ref = it.get('service')
                service_id = None
                if service_ref:
                    ref_str = str(service_ref)
                    # naive UUID check
                    if len(ref_str) == 36 and ref_str.count('-') == 4:
                        service_id = ref_str
                    else:
                        svc = Service.objects.filter(name=ref_str).first()
                        if svc:
                            service_id = str(svc.id)

                JobServiceItem.objects.create(
                    job=job,
                    service_id=service_id,
                    custom_name=it.get('custom_name'),
                    price=it.get('price', '0'),
                    duration_hours=it.get('duration_hours', '0'),
                )
            for a in assigns:
                user_ref = a.get('user')
                user_id = self._resolve_user_id(user_ref) if user_ref is not None else None
                JobAssignment.objects.create(
                    job=job,
                    user_id=user_id,
                    role=a.get('role')
                )
            created_ids.append(str(job.id))

        return {'series_id': str(series), 'job_ids': created_ids}

    def validate(self, data):
        repeat_unit = data.get('repeat_unit')
        day_of_week = data.get('day_of_week')
        
        # If repeat_unit is 'week', day_of_week should be provided
        if repeat_unit == 'week' and day_of_week is None:
            raise serializers.ValidationError({
                'day_of_week': 'day_of_week is required when repeat_unit is "week"'
            })
        
        # If repeat_unit is not 'week', day_of_week should be None
        if repeat_unit and repeat_unit != 'week' and day_of_week is not None:
            raise serializers.ValidationError({
                'day_of_week': 'day_of_week should only be provided when repeat_unit is "week"'
            })
        
        return data

    def _resolve_user_id(self, ref):
        if ref is None:
            return None
        ref_str = str(ref).strip()
        # UUID-like
        if len(ref_str) == 36 and ref_str.count('-') == 4:
            return ref_str
        # Email
        if '@' in ref_str:
            u = User.objects.filter(email=ref_str).only('id').first()
            return str(u.id) if u else None
        # Username
        u = User.objects.filter(username=ref_str).only('id').first()
        return str(u.id) if u else None


class JobConvertToSeriesSerializer(serializers.Serializer):
    title = serializers.CharField(required=False, allow_blank=True, allow_null=True)
    description = serializers.CharField(required=False, allow_blank=True, allow_null=True)
    priority = serializers.ChoiceField(choices=['low', 'medium', 'high'], required=False)
    duration_hours = serializers.DecimalField(max_digits=5, decimal_places=2, required=False)
    scheduled_at = serializers.DateTimeField(required=False)
    total_price = serializers.DecimalField(max_digits=12, decimal_places=2, required=False)
    total_surcharge = serializers.DecimalField(max_digits=12, decimal_places=2, required=False)
    discount_type = serializers.ChoiceField(
        choices=[Job.DISCOUNT_TYPE_AMOUNT, Job.DISCOUNT_TYPE_PERCENTAGE],
        required=False,
        allow_blank=True,
        allow_null=True,
    )
    discount_value = serializers.DecimalField(max_digits=12, decimal_places=2, required=False, allow_null=True)
    contact_id = serializers.IntegerField(required=False, allow_null=True)
    address_id = serializers.IntegerField(required=False, allow_null=True)
    customer_name = serializers.CharField(required=False, allow_blank=True, allow_null=True)
    customer_phone = serializers.CharField(required=False, allow_blank=True, allow_null=True)
    customer_email = serializers.EmailField(required=False, allow_blank=True, allow_null=True)
    customer_address = serializers.CharField(required=False, allow_blank=True, allow_null=True)
    ghl_contact_id = serializers.CharField(required=False, allow_blank=True, allow_null=True)
    quoted_by = serializers.CharField(required=False, allow_blank=True, allow_null=True)
    notes = serializers.CharField(required=False, allow_blank=True, allow_null=True)
    payment_method = serializers.ChoiceField(
        choices=[choice[0] for choice in Job.PAYMENT_METHOD_CHOICES],
        required=False,
        allow_blank=True,
        allow_null=True,
    )
    repeat_every = serializers.IntegerField(min_value=1)
    repeat_unit = serializers.ChoiceField(choices=['day', 'week', 'month', 'quarter', 'semi_annual', 'year'])
    occurrences = serializers.IntegerField(min_value=1)
    day_of_week = serializers.IntegerField(min_value=0, max_value=6, required=False, allow_null=True)
    items = JobServiceItemSerializer(many=True, required=False)
    assignments = JobAssignmentSerializer(many=True, required=False)

    def validate(self, data):
        repeat_unit = data.get('repeat_unit')
        day_of_week = data.get('day_of_week')

        if repeat_unit == 'week' and day_of_week is None:
            raise serializers.ValidationError({
                'day_of_week': 'day_of_week is required when repeat_unit is "week"'
            })

        if repeat_unit and repeat_unit != 'week' and day_of_week is not None:
            raise serializers.ValidationError({
                'day_of_week': 'day_of_week should only be provided when repeat_unit is "week"'
            })

        return data

    def save(self, **kwargs):
        job = kwargs.get('job')
        if job is None:
            raise serializers.ValidationError({'job': 'A source job is required.'})

        request = self.context.get('request')
        account = getattr(request, 'account', None) if request else None
        creator = request.user if request and request.user.is_authenticated else None

        repeat_every = self.validated_data['repeat_every']
        repeat_unit = self.validated_data['repeat_unit']
        count = self.validated_data['occurrences']
        day_of_week = self.validated_data.get('day_of_week')
        base_dt = self.validated_data.get('scheduled_at') or job.scheduled_at
        items_data = self.validated_data.get('items')
        assignments_data = self.validated_data.get('assignments')
        quoted_by_raw = self.validated_data.get('quoted_by', None)

        if not base_dt:
            raise serializers.ValidationError({
                'scheduled_at': 'scheduled_at is required when the source job has no scheduled date.'
            })

        dates = JobSerializer._build_occurrence_datetimes(
            base_dt, repeat_every, repeat_unit, count, day_of_week=day_of_week
        )

        from uuid import uuid4

        with transaction.atomic():
            source_job = Job.objects.select_for_update().get(pk=job.pk)
            if source_job.status not in ['to_convert', 'reschedule_pending']:
                raise serializers.ValidationError({
                    'status': 'Only jobs with status "to_convert" or "reschedule_pending" can be converted to a recurring series.'
                })

            contact = self._resolve_contact()
            address = self._resolve_address()
            if contact and address and address.contact_id != contact.id:
                raise serializers.ValidationError({
                    'address_id': 'The provided address does not belong to the provided contact.'
                })

            series = uuid4()
            self._apply_job_updates(source_job, contact=contact, address=address)
            source_job.scheduled_at = dates[0]
            source_job.job_type = 'recurring'
            source_job.repeat_every = repeat_every
            source_job.repeat_unit = repeat_unit
            source_job.occurrences = count
            source_job.day_of_week = day_of_week
            source_job.status = 'pending'
            source_job.series_id = series
            source_job.series_sequence = 1
            if account and not source_job.account_id:
                source_job.account = account
            if creator and not source_job.created_by_id:
                source_job.created_by = creator
            if creator and not source_job.created_by_email:
                source_job.created_by_email = getattr(creator, 'email', None)
            if 'quoted_by' in self.validated_data:
                source_job.quoted_by_id = self._resolve_user_id_or_error(quoted_by_raw)

            source_job.save()
            JobOccurrence.objects.filter(job=source_job).delete()

            if items_data is not None:
                source_job.items.all().delete()
                for item in items_data:
                    JobServiceItem.objects.create(job=source_job, **item)
            if assignments_data is not None:
                source_job.assignments.all().delete()
                for assignment in assignments_data:
                    JobAssignment.objects.create(job=source_job, **assignment)

            source_items = list(
                JobServiceItem.objects.filter(job=source_job).select_related('service')
            )
            source_assignments = list(
                JobAssignment.objects.filter(job=source_job).select_related('user')
            )

            created_ids = [str(source_job.id)]
            job_account = source_job.account or account
            created_by = source_job.created_by or creator
            created_by_email = source_job.created_by_email or getattr(creator, 'email', None)

            for idx, dt in enumerate(dates[1:], start=2):
                new_job = Job.objects.create(
                    submission=source_job.submission,
                    title=source_job.title,
                    description=source_job.description,
                    priority=source_job.priority,
                    duration_hours=source_job.duration_hours,
                    scheduled_at=dt,
                    total_price=source_job.total_price,
                    total_surcharge=source_job.total_surcharge,
                    contact=source_job.contact,
                    address=source_job.address,
                    customer_name=source_job.customer_name,
                    customer_phone=source_job.customer_phone,
                    customer_email=source_job.customer_email,
                    customer_address=source_job.customer_address,
                    ghl_contact_id=source_job.ghl_contact_id,
                    quoted_by=source_job.quoted_by,
                    created_by=created_by,
                    created_by_email=created_by_email,
                    job_type='recurring',
                    repeat_every=repeat_every,
                    repeat_unit=repeat_unit,
                    occurrences=count,
                    day_of_week=day_of_week,
                    status='pending',
                    notes=source_job.notes,
                    payment_method=source_job.payment_method,
                    discount_type=source_job.discount_type,
                    discount_value=source_job.discount_value,
                    invoice_url=source_job.invoice_url,
                    series_id=series,
                    series_sequence=idx,
                    account=job_account,
                )

                for item in source_items:
                    JobServiceItem.objects.create(
                        job=new_job,
                        service=item.service,
                        custom_name=item.custom_name,
                        price=item.price,
                        duration_hours=item.duration_hours,
                    )
                for assignment in source_assignments:
                    JobAssignment.objects.create(
                        job=new_job,
                        user=assignment.user,
                        role=assignment.role,
                    )

                created_ids.append(str(new_job.id))

        return {
            'series_id': str(series),
            'job_ids': created_ids,
            'converted_job_id': str(source_job.id),
        }

    def _resolve_contact(self):
        if 'contact_id' not in self.validated_data:
            return None
        contact_id = self.validated_data.get('contact_id')
        if contact_id is None:
            return None
        try:
            return Contact.objects.get(id=contact_id)
        except Contact.DoesNotExist:
            raise serializers.ValidationError({
                'contact_id': f'Contact with id {contact_id} does not exist.'
            })

    def _resolve_address(self):
        if 'address_id' not in self.validated_data:
            return None
        address_id = self.validated_data.get('address_id')
        if address_id is None:
            return None
        try:
            return Address.objects.get(id=address_id)
        except Address.DoesNotExist:
            raise serializers.ValidationError({
                'address_id': f'Address with id {address_id} does not exist.'
            })

    def _apply_job_updates(self, source_job, contact=None, address=None):
        scalar_fields = [
            'title', 'description', 'priority', 'duration_hours', 'total_price',
            'total_surcharge', 'discount_type', 'discount_value', 'customer_name',
            'customer_phone', 'customer_email', 'customer_address', 'ghl_contact_id',
            'notes', 'payment_method',
        ]
        for field in scalar_fields:
            if field in self.validated_data:
                setattr(source_job, field, self.validated_data[field])

        if 'contact_id' in self.validated_data:
            source_job.contact = contact
            if contact:
                name_parts = [contact.first_name, contact.last_name]
                if 'customer_name' not in self.validated_data:
                    source_job.customer_name = ' '.join(filter(None, name_parts)) or None
                if 'customer_phone' not in self.validated_data:
                    source_job.customer_phone = contact.phone
                if 'customer_email' not in self.validated_data:
                    source_job.customer_email = contact.email
                if 'ghl_contact_id' not in self.validated_data:
                    source_job.ghl_contact_id = contact.contact_id

        if 'address_id' in self.validated_data:
            source_job.address = address
            if address and 'customer_address' not in self.validated_data:
                source_job.customer_address = address.get_full_address()

    def _resolve_user_id_or_error(self, ref):
        if ref in (None, ''):
            return None

        ref_str = str(ref).strip()
        if not ref_str:
            return None

        if ref_str.isdigit():
            user_id = int(ref_str)
            if User.objects.filter(id=user_id).exists():
                return user_id

        if len(ref_str) == 36 and ref_str.count('-') == 4:
            try:
                if User.objects.filter(id=ref_str).exists():
                    return ref_str
            except (TypeError, ValueError):
                pass

        if '@' in ref_str:
            user = User.objects.filter(email=ref_str).only('id').first()
        else:
            user = User.objects.filter(username=ref_str).only('id').first()

        if user:
            return user.id

        raise serializers.ValidationError({
            'quoted_by': f'User "{ref_str}" could not be found.'
        })


class LocationSummarySerializer(serializers.Serializer):
    """Serializer for location summary card data"""
    address = serializers.CharField()
    job_count = serializers.IntegerField()
    customer_names = serializers.ListField(child=serializers.CharField())
    status_counts = serializers.DictField()
    total_price = serializers.DecimalField(max_digits=12, decimal_places=2)
    total_hours = serializers.DecimalField(max_digits=8, decimal_places=2)
    next_scheduled = serializers.DateTimeField(allow_null=True)
    service_names = serializers.ListField(child=serializers.CharField())
    job_ids = serializers.ListField(child=serializers.UUIDField())


