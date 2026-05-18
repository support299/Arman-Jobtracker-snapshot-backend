# user_serializers.py - Serializers for user-side functionality
from rest_framework import serializers
from decimal import Decimal
from service_app.models import (
    Service, Package, Feature, PackageFeature, Location, 
    Question, QuestionOption, SubQuestion, GlobalSizePackage,
    ServicePackageSizeMapping, QuestionPricing, OptionPricing, SubQuestionPricing, User
)
from .models import (
    CustomerSubmission, CustomerServiceSelection, CustomerQuestionResponse,
    CustomerOptionResponse, CustomerSubQuestionResponse, CustomerPackageQuote,CustomService, QuoteSchedule, CustomerSubmissionImage
)

from accounts.models import Address, Contact, GHLAuthCredentials
from accounts.models import Location as GHLLocation

from service_app.serializers import ServiceSettingsSerializer



class CustomServiceSerializer(serializers.ModelSerializer):
    class Meta:
        model = CustomService
        fields = ['id', 'purchase', 'product_name', 'description', 'price', 'is_active']

class AddressSerializer(serializers.ModelSerializer):
    class Meta:
        model = Address
        fields = '__all__'


class ContactSerializer(serializers.ModelSerializer):
    class Meta:
        model = Contact
        fields = '__all__'


class LocationPublicSerializer(serializers.ModelSerializer):
    """Public serializer for locations"""
    class Meta:
        model = Location
        fields = ['id', 'name', 'address', 'trip_surcharge','latitude','longitude']

class ServicePublicSerializer(serializers.ModelSerializer):
    packages_count = serializers.SerializerMethodField()
    service_settings = ServiceSettingsSerializer(read_only=True, source='settings')
    
    class Meta:
        model = Service
        fields = ['id', 'name', 'description', 'packages_count', 'service_settings']
    
    # class Meta:
    #     model = Service
    #     fields = ['id', 'name', 'description', 'packages_count','service_settings']
    
    def get_packages_count(self, obj):
        return obj.packages.filter(is_active=True).count()

class PackagePublicSerializer(serializers.ModelSerializer):
    """Public serializer for packages"""
    class Meta:
        model = Package
        fields = ['id', 'name', 'base_price', 'order']

class FeaturePublicSerializer(serializers.ModelSerializer):
    """Public serializer for features"""
    class Meta:
        model = Feature
        fields = ['id', 'name', 'description']

class QuestionOptionPublicSerializer(serializers.ModelSerializer):
    """Public serializer for question options"""
    class Meta:
        model = QuestionOption
        fields = ['id', 'option_text', 'order', 'allow_quantity', 'max_quantity']

class SubQuestionPublicSerializer(serializers.ModelSerializer):
    """Public serializer for sub-questions"""
    class Meta:
        model = SubQuestion
        fields = ['id', 'sub_question_text', 'order']

class QuestionPublicSerializer(serializers.ModelSerializer):
    """Public serializer for questions"""
    options = QuestionOptionPublicSerializer(many=True, read_only=True)
    sub_questions = SubQuestionPublicSerializer(many=True, read_only=True)
    child_questions = serializers.SerializerMethodField()
    
    class Meta:
        model = Question
        fields = [
            'id', 'question_text', 'question_type', 'order',
            'parent_question', 'condition_answer', 'condition_option',
            'options', 'sub_questions', 'child_questions'
        ]
    
    def get_child_questions(self, obj):
        child_questions = obj.child_questions.filter(is_active=True).order_by('order')
        return QuestionPublicSerializer(child_questions, many=True, context=self.context).data

class GlobalSizePackagePublicSerializer(serializers.ModelSerializer):
    """Public serializer for global size packages"""
    class Meta:
        model = GlobalSizePackage
        fields = ['id', 'min_sqft', 'max_sqft']

# Customer submission serializers
class CustomerSubmissionCreateSerializer(serializers.ModelSerializer):
    """Serializer for creating customer submissions. Optional location_id applies that location's trip_surcharge to the quote."""
    contact = serializers.PrimaryKeyRelatedField(queryset=Contact.objects.all())
    address = serializers.PrimaryKeyRelatedField(queryset=Address.objects.all(), required=False, allow_null=True)
    quoted_by = serializers.PrimaryKeyRelatedField(queryset=User.objects.all(), write_only=True, required=False, allow_null=True)
    first_time = serializers.BooleanField(write_only=True)
    location = serializers.PrimaryKeyRelatedField(
        queryset=Location.objects.filter(is_active=True),
        required=False,
        allow_null=True,
        write_only=True,
        help_text='Optional location ID; when provided, the location\'s trip_surcharge is applied to the quote.',
    )
    class Meta:
        model = CustomerSubmission
        fields = [
            'contact', 'address', 'house_sqft', 'quoted_by', 'first_time', 'location'
        ]

    def _get_location_queryset(self):
        """Scope location queryset to request account when available."""
        request = self.context.get('request')
        account = getattr(request, 'account', None) if request else None
        if account:
            return Location.objects.filter(is_active=True, account=account)
        return Location.objects.filter(is_active=True)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['location'].queryset = self._get_location_queryset()

    def create(self, validated_data):
        from django.utils import timezone
        from datetime import timedelta

        quoted_by_user = validated_data.pop('quoted_by', None)
        first_time = validated_data.pop('first_time')
        location = validated_data.pop('location', None)
        # Avoid duplicate 'account' (can be in validated_data when passed via serializer.save(account=...))
        account = validated_data.pop('account', None)
        if account is None:
            request = self.context.get('request')
            account = getattr(request, 'account', None) if request else None

        # Create submission with quoted_by user and account
        submission = CustomerSubmission.objects.create(
            **validated_data,
            quoted_by=quoted_by_user,
            account=account,
            location=location,
        )
        submission.expires_at = timezone.now() + timedelta(days=30)

        # Apply location trip_surcharge when location is provided and has a surcharge
        if location and location.trip_surcharge and location.trip_surcharge > Decimal('0.00'):
            submission.total_surcharges = location.trip_surcharge
            submission.quote_surcharge_applicable = True
        submission.save()

        # Store quoted_by as string in QuoteSchedule for backward compatibility
        quoted_by_str = None
        if quoted_by_user:
            # Try email first, then username, then ID
            quoted_by_str = getattr(quoted_by_user, 'email', None) or getattr(quoted_by_user, 'username', None) or str(quoted_by_user.id)

        QuoteSchedule.objects.create(
            submission=submission,
            quoted_by=quoted_by_str or '',
            first_time=first_time
        )

        return submission

class CustomerServiceSelectionSerializer(serializers.ModelSerializer):
    """Serializer for service selections"""
    service_name = serializers.CharField(source='service.name', read_only=True)
    
    class Meta:
        model = CustomerServiceSelection
        fields = [
            'id', 'service', 'service_name', 'base_price_total',
            'question_adjustments', 'surcharge_applicable', 'surcharge_amount'
        ]
        read_only_fields = ['id', 'base_price_total', 'question_adjustments', 'surcharge_amount']

class CustomerOptionResponseSerializer(serializers.ModelSerializer):
    """Serializer for option responses"""
    option_text = serializers.CharField(source='option.option_text', read_only=True)
    
    class Meta:
        model = CustomerOptionResponse
        fields = ['id', 'option', 'option_text', 'quantity', 'price_adjustment']
        read_only_fields = ['id', 'price_adjustment']

class CustomerSubQuestionResponseSerializer(serializers.ModelSerializer):
    """Serializer for sub-question responses"""
    sub_question_text = serializers.CharField(source='sub_question.sub_question_text', read_only=True)
    
    class Meta:
        model = CustomerSubQuestionResponse
        fields = ['id', 'sub_question', 'sub_question_text', 'answer', 'price_adjustment']
        read_only_fields = ['id', 'price_adjustment']

class CustomerQuestionResponseSerializer(serializers.ModelSerializer):
    """Serializer for question responses"""
    question_text = serializers.CharField(source='question.question_text', read_only=True)
    question_type = serializers.CharField(source='question.question_type', read_only=True)
    option_responses = CustomerOptionResponseSerializer(many=True, read_only=True)
    sub_question_responses = CustomerSubQuestionResponseSerializer(many=True, read_only=True)
    
    class Meta:
        model = CustomerQuestionResponse
        fields = [
            'id', 'question', 'question_text', 'question_type',
            'yes_no_answer', 'text_answer', 'option_responses',
            'sub_question_responses', 'price_adjustment'
        ]
        read_only_fields = ['id', 'price_adjustment']

class ServiceQuestionResponseSerializer(serializers.Serializer):
    """Serializer for submitting service question responses"""
    service_id = serializers.UUIDField()
    responses = serializers.ListField(child=serializers.DictField())

class CustomerPackageQuoteSerializer(serializers.ModelSerializer):
    """Serializer for package quotes"""
    package_name = serializers.CharField(source='package.name', read_only=True)
    package_description = serializers.CharField(source='package.description', read_only=True, default='')
    service_name = serializers.CharField(source='service_selection.service.name', read_only=True)
    included_features_details = serializers.SerializerMethodField()
    excluded_features_details = serializers.SerializerMethodField()
    
    class Meta:
        model = CustomerPackageQuote
        fields = [
            'id', 'package', 'package_name', 'package_description', 'service_name',
            'base_price', 'sqft_price', 'question_adjustments',
            'surcharge_amount', 'total_price', 'is_selected',
            'included_features', 'excluded_features',
            'included_features_details', 'excluded_features_details'
        ]
    
    def get_included_features_details(self, obj):
        if not obj.included_features:
            return []
        features = Feature.objects.filter(id__in=obj.included_features)
        return FeaturePublicSerializer(features, many=True).data
    
    def get_excluded_features_details(self, obj):
        if not obj.excluded_features:
            return []
        features = Feature.objects.filter(id__in=obj.excluded_features)
        return FeaturePublicSerializer(features, many=True).data


class QuoteScheduleSerializer(serializers.ModelSerializer):
    class Meta:
        model = QuoteSchedule
        fields = "__all__"


class CustomerSubmissionImageSerializer(serializers.ModelSerializer):
    """Serializer for customer submission images (stored in GHL only; image field not persisted to S3)."""
    image = serializers.ImageField(required=False, allow_null=True)
    image_url = serializers.SerializerMethodField()
    uploaded_by_name = serializers.SerializerMethodField()
    submission_id = serializers.UUIDField(source='submission.id', read_only=True)

    class Meta:
        model = CustomerSubmissionImage
        fields = [
            'id', 'submission', 'submission_id', 'image', 'image_url', 'caption',
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


class CustomerSubmissionDetailSerializer(serializers.ModelSerializer):
    """Detailed serializer for customer submissions"""
    service_selections = serializers.SerializerMethodField()
    contact = ContactSerializer(read_only=True)
    custom_products = CustomServiceSerializer(many=True, read_only=True)
    address = AddressSerializer(read_only=True)
    quote_schedule = QuoteScheduleSerializer(read_only=True)
    quoted_by_details = serializers.SerializerMethodField()
    images = CustomerSubmissionImageSerializer(many=True, read_only=True)

    class Meta:
        model = CustomerSubmission
        fields = [
            'id',
            'house_sqft',
            'status', 'total_base_price', 'total_adjustments',
            'total_surcharges', 'final_total', 'created_at','quote_surcharge_applicable',
            'expires_at', 'service_selections','additional_data','contact','address','custom_products','custom_service_total','quote_schedule',
            'quoted_by', 'quoted_by_details', 'images'
        ]
    
    def get_service_selections(self, obj):
        selections = obj.customerserviceselection_set.all().prefetch_related(
            'package_quotes__package',
            'question_responses__option_responses',
            'question_responses__sub_question_responses'
        )
        return CustomerServiceSelectionDetailSerializer(selections, many=True).data
    
    def get_quoted_by_details(self, obj):
        """Return quoted_by user details"""
        if obj.quoted_by:
            return {
                'id': obj.quoted_by.id,
                'username': obj.quoted_by.username,
                'email': obj.quoted_by.email,
                'first_name': obj.quoted_by.first_name,
                'last_name': obj.quoted_by.last_name,
                'full_name': obj.quoted_by.get_full_name() or obj.quoted_by.username,
            }
        return None
    
    def get_fields(self):
        fields = super().get_fields()
        request = self.context.get('request')
        # if request and request.method in ['PATCH', 'PUT']:
        #     fields['customer_address'].read_only = True
        return fields


class CustomerServiceSelectionDetailSerializer(serializers.ModelSerializer):
    """Detailed serializer for service selections"""
    service_details = ServicePublicSerializer(source='service', read_only=True)
    selected_package_details = PackagePublicSerializer(source='selected_package', read_only=True)
    package_quotes = serializers.SerializerMethodField()
    question_responses = CustomerQuestionResponseSerializer(many=True, read_only=True)
    
    class Meta:
        model = CustomerServiceSelection
        fields = [
            'id', 'service', 'service_details', 'selected_package', 'selected_package_details',
            'question_adjustments', 'surcharge_applicable', 'surcharge_amount',
            'final_base_price', 'final_sqft_price', 'final_total_price',
            'package_quotes', 'question_responses'
        ]
    
    def get_package_quotes(self, obj):
        # Only return selected quote if packages are selected, otherwise all quotes
        if obj.selected_package:
            quotes = obj.package_quotes.filter(is_selected=True)
        else:
            quotes = obj.package_quotes.all().order_by('package__order')
        return CustomerPackageQuoteSerializer(quotes, many=True).data
    
    

# Utility serializers
class PricingCalculationRequestSerializer(serializers.Serializer):
    """Serializer for pricing calculation requests"""
    submission_id = serializers.UUIDField()
    service_responses = ServiceQuestionResponseSerializer(many=True)

class ConditionalQuestionRequestSerializer(serializers.Serializer):
    """Serializer for conditional question requests"""
    parent_question_id = serializers.UUIDField()
    answer = serializers.CharField(required=False, allow_blank=True)
    option_id = serializers.UUIDField(required=False, allow_null=True)


class PackageSelectionSerializer(serializers.Serializer):
    """Serializer for package selection"""
    service_selection_id = serializers.UUIDField()
    package_id = serializers.UUIDField()




class ConditionalQuestionResponseSerializer(serializers.Serializer):
    """Enhanced serializer for question responses including conditional logic"""
    question_id = serializers.UUIDField()
    question_type = serializers.CharField()
    
    # For conditional questions
    parent_question_id = serializers.UUIDField(required=False, allow_null=True)
    condition_type = serializers.CharField(required=False, allow_blank=True)
    condition_value = serializers.CharField(required=False, allow_blank=True)
    
    # Response data based on question type
    yes_no_answer = serializers.BooleanField(required=False, allow_null=True)
    text_answer = serializers.CharField(required=False, allow_blank=True)
    
    # For option-based questions
    selected_options = serializers.ListField(
        child=serializers.DictField(),
        required=False,
        allow_empty=True
    )
    
    # For multiple_yes_no questions
    sub_question_answers = serializers.ListField(
        child=serializers.DictField(),
        required=False,
        allow_empty=True
    )
    
    def validate(self, data):
        """Validate response data based on question type"""
        question_type = data.get('question_type')
        
        if question_type == 'yes_no':
            if data.get('yes_no_answer') is None:
                raise serializers.ValidationError("yes_no_answer is required for yes_no questions")
        
        elif question_type in ['describe', 'quantity']:
            if not data.get('selected_options'):
                raise serializers.ValidationError("selected_options is required for describe/quantity questions")
        
        elif question_type == 'multiple_yes_no':
            if not data.get('sub_question_answers'):
                raise serializers.ValidationError("sub_question_answers is required for multiple_yes_no questions")
        
        # Validate conditional question requirements
        if data.get('parent_question_id'):
            if not data.get('condition_type') or not data.get('condition_value'):
                raise serializers.ValidationError(
                    "condition_type and condition_value are required for conditional questions"
                )
        
        return data

class ServiceResponseSubmissionSerializer(serializers.Serializer):
    """Serializer for the complete service response submission"""
    responses = ConditionalQuestionResponseSerializer(many=True)
    
    def validate_responses(self, value):
        """Validate the responses list"""
        if not value:
            raise serializers.ValidationError("At least one response is required")
        
        # Check for duplicate question responses
        question_ids = [r['question_id'] for r in value]
        if len(question_ids) != len(set(question_ids)):
            raise serializers.ValidationError("Duplicate question responses found")
        
        return value
    



class SelectedPackageSerializer(serializers.Serializer):
    """Serializer for selected package information"""
    service_selection_id = serializers.UUIDField()
    package_id = serializers.UUIDField()
    package_name = serializers.CharField(read_only=True)
    total_price = serializers.DecimalField(max_digits=10, decimal_places=2, read_only=True)

class SubmitFinalQuoteSerializer(serializers.Serializer):
    """Serializer for final quote submission"""
    customer_confirmation = serializers.BooleanField(default=True)
    selected_packages = SelectedPackageSerializer(many=True, required=False)
    additional_notes = serializers.CharField(max_length=1000, required=False, allow_blank=True)
    signature = serializers.CharField(required=False, allow_blank=True)
    preferred_contact_method = serializers.ChoiceField(
        choices=[('email', 'Email'), ('phone', 'Phone'), ('both', 'Both')],
        default='email'
    )
    preferred_start_date = serializers.DateField(required=False, allow_null=True)
    terms_accepted = serializers.BooleanField(default=True)
    marketing_consent = serializers.BooleanField(default=False)
    
    def validate_customer_confirmation(self, value):
        if not value:
            raise serializers.ValidationError("Customer confirmation is required")
        return value
    
    def validate_terms_accepted(self, value):
        if not value:
            raise serializers.ValidationError("Terms and conditions must be accepted")
        return value






class ServiceListSerializer(serializers.ModelSerializer):
    """Simplified serializer for listing services"""
    class Meta:
        model = Service
        fields = ['id', 'name', 'description', 'order']


class CustomServiceSerializer(serializers.ModelSerializer):
    """Serializer for custom services"""
    class Meta:
        model = CustomService
        fields = ['id', 'product_name', 'description', 'price','purchase', 'is_active']


class QuoteScheduleUpdateSerializer(serializers.ModelSerializer):
    class Meta:
        model = QuoteSchedule
        fields = ['first_time', 'quoted_by', 'scheduled_date', 'notes', 'is_submitted']


class JobRescheduleQuoteCreateSerializer(serializers.Serializer):
    scheduled_date = serializers.CharField(required=True, help_text='ISO 8601 datetime for the new booking.')
    notes = serializers.CharField(required=False, allow_blank=True, default='')
    quoted_by = serializers.CharField(required=False, allow_blank=True, default='')


class RescheduleConvertToJobSerializer(serializers.Serializer):
    scheduled_date = serializers.CharField(
        required=False,
        allow_blank=True,
        help_text='Optional: override scheduled time before conversion (same as booking).',
    )


class GHLAccountPublicSerializer(serializers.ModelSerializer):
    """Public GHL account info for quote flows (no OAuth tokens)."""

    account_name = serializers.CharField(source='company_name', read_only=True)
    logo_url = serializers.URLField(source='company_logo_url', read_only=True)
    website = serializers.SerializerMethodField()
    domain = serializers.SerializerMethodField()
    location_name = serializers.SerializerMethodField()

    class Meta:
        model = GHLAuthCredentials
        fields = [
            'id',
            'location_id',
            'company_id',
            'account_name',
            'logo_url',
            'timezone',
            'user_type',
            'is_active',
            'website',
            'domain',
            'location_name',
            'created_at',
            'updated_at',
        ]
        read_only_fields = fields

    def _ghl_location(self, obj):
        cache = self.context.setdefault('_ghl_location_cache', {})
        loc_id = (obj.location_id or '').strip()
        if not loc_id:
            return None
        if loc_id not in cache:
            cache[loc_id] = GHLLocation.objects.filter(pk=loc_id).first()
        return cache[loc_id]

    def get_website(self, obj):
        loc = self._ghl_location(obj)
        return loc.website if loc else None

    def get_domain(self, obj):
        loc = self._ghl_location(obj)
        return loc.domain if loc else None

    def get_location_name(self, obj):
        loc = self._ghl_location(obj)
        return loc.name if loc else None