# serializers.py
from rest_framework import serializers
from django.contrib.auth import authenticate
from django.db.models import Max
from decimal import Decimal
from .models import (
    User, Location, Service, Package, Feature, PackageFeature,
    Question, QuestionOption, QuestionPricing, OptionPricing,
    Order, OrderQuestionAnswer,ServiceSettings, QuestionResponse, SubQuestion, SubQuestionPricing, SubQuestionResponse,
    OptionResponse,GlobalBasePrice
)


class UserSerializer(serializers.ModelSerializer):
    """Serializer for User model"""
    password = serializers.CharField(write_only=True, required=False, allow_blank=False, style={'input_type': 'password'})
    class Meta:
        model = User
        fields = [
            'id', 'username', 'email', 'first_name', 'ghl_user_id', 'last_name', 'role',
            'is_admin', 'is_superuser', 'payroll_can_view_team_data',
            'can_access_service_management_tool',
            'can_access_location_management_tool',
            'can_access_house_size_management_tool',
            'created_at', 'password', 'is_active',
        ]
        read_only_fields = ['id', 'created_at', 'is_admin', 'is_superuser', 'ghl_user_id']

    def create(self, validated_data):
        password = validated_data.pop('password', None)
        user = User(**validated_data)
        if not password:
            raise serializers.ValidationError({'password': 'Password is required'})
        user.set_password(password)
        user.save()
        return user

    def update(self, instance, validated_data):
        password = validated_data.pop('password', None)
        for attr, value in validated_data.items():
            setattr(instance, attr, value)
        if password:
            instance.set_password(password)
        instance.save()
        return instance


class LoginSerializer(serializers.Serializer):
    """Login serializer for admin authentication"""
    username = serializers.CharField()
    password = serializers.CharField(write_only=True)

    def validate(self, data):
        username = data.get('username')
        password = data.get('password')

        if username and password:
            user = authenticate(username=username, password=password)
            if user:
                if not user.is_admin:
                    raise serializers.ValidationError("Only admins can access this interface.")
                if not user.is_active:
                    raise serializers.ValidationError("User account is disabled.")
                data['user'] = user
            else:
                raise serializers.ValidationError("Invalid credentials.")
        else:
            raise serializers.ValidationError("Must include username and password.")
        
        return data


class LocationSerializer(serializers.ModelSerializer):
    """Serializer for Location model"""
    created_by_name = serializers.CharField(source='created_by.username', read_only=True)

    class Meta:
        model = Location
        fields = [
            'id', 'name', 'address', 'latitude', 'longitude', 
            'trip_surcharge', 'google_place_id', 'is_active',
            'created_at', 'updated_at', 'created_by', 'created_by_name'
        ]
        read_only_fields = ['id', 'created_at', 'updated_at', 'created_by']

    def create(self, validated_data):
        # Set created_by from request user
        request = self.context.get('request')
        if request and hasattr(request, 'user'):
            validated_data['created_by'] = request.user
        return super().create(validated_data)


class PackageSerializer(serializers.ModelSerializer):
    """Serializer for Package model"""
    service_name = serializers.CharField(source='service.name', read_only=True)
    features = serializers.SerializerMethodField()
    base_price = serializers.DecimalField(
        max_digits=10, 
        decimal_places=2, 
        min_value=0,  # <-- allow zero
    )

    class Meta:
        model = Package
        fields = [
            'id', 'service', 'service_name', 'name', 'base_price', 
            'order', 'is_active', 'created_at', 'updated_at', 'features'
        ]
        read_only_fields = ['id', 'created_at', 'updated_at']

    def get_features(self, obj):
        """Get features included in the package (ordered by order field)"""
        package_features = PackageFeature.objects.filter(package=obj).order_by('order', 'created_at')
        return [
            {
                'id': pf.id,
                'feature': pf.feature.id,
                'name': pf.feature.name,
                'description': pf.feature.description,
                'is_included': pf.is_included,
                'order': pf.order
            }
            for pf in package_features
        ]

    
    def create(self, validated_data):
        package = super().create(validated_data)

        # Automatically link to all existing features under the same service
        features = Feature.objects.filter(service=package.service, is_active=True)
        package_features = [
            PackageFeature(package=package, feature=feature, is_included=False, order=idx)
            for idx, feature in enumerate(features)
        ]
        PackageFeature.objects.bulk_create(package_features)
        return package


class BulkSubQuestionPricingSerializer(serializers.Serializer):
    """Serializer for bulk updating sub-question pricing rules"""
    sub_question_id = serializers.UUIDField()
    pricing_rules = serializers.ListField(
        child=serializers.DictField(), 
        allow_empty=False
    )


class BulkQuestionOrderItemSerializer(serializers.Serializer):
    """Serializer for a single question order update. Accepts question_id or id."""
    question_id = serializers.UUIDField(required=False)
    id = serializers.UUIDField(required=False)
    service_id = serializers.UUIDField()
    order = serializers.IntegerField(min_value=0)

    def validate(self, data):
        qid = data.get('question_id') or data.get('id')
        if not qid:
            raise serializers.ValidationError(
                'Either question_id or id is required for each item'
            )
        data['question_id'] = qid
        data.pop('id', None)  # Normalize to question_id only
        return data


class BulkQuestionOrderSerializer(serializers.Serializer):
    """Serializer for bulk reordering service questions. Accepts {"questions": [...]} or raw array."""
    questions = serializers.ListField(
        child=BulkQuestionOrderItemSerializer(),
        allow_empty=False,
        required=False
    )

    def to_internal_value(self, data):
        # Accept raw array: [{"question_id": "...", "service_id": "...", "order": 0}]
        if isinstance(data, list):
            data = {'questions': data}
        return super().to_internal_value(data)

    def validate(self, data):
        if not data.get('questions'):
            raise serializers.ValidationError({'questions': 'This field is required and cannot be empty.'})
        return data


class OptionResponseSerializer(serializers.ModelSerializer):
    """Serializer for customer option responses"""
    option_text = serializers.CharField(source='option.option_text', read_only=True)
    
    class Meta:
        model = OptionResponse
        fields = ['id', 'option', 'option_text', 'quantity', 'created_at']
        read_only_fields = ['id', 'created_at']


class SubQuestionResponseSerializer(serializers.ModelSerializer):
    """Serializer for customer sub-question responses"""
    sub_question_text = serializers.CharField(source='sub_question.sub_question_text', read_only=True)
    
    class Meta:
        model = SubQuestionResponse
        fields = ['id', 'sub_question', 'sub_question_text', 'answer', 'created_at']
        read_only_fields = ['id', 'created_at']


class QuestionResponseSerializer(serializers.ModelSerializer):
    """Serializer for customer question responses"""
    question_text = serializers.CharField(source='question.question_text', read_only=True)
    question_type = serializers.CharField(source='question.question_type', read_only=True)
    option_responses = OptionResponseSerializer(many=True, read_only=True)
    sub_question_responses = SubQuestionResponseSerializer(many=True, read_only=True)
    
    class Meta:
        model = QuestionResponse
        fields = [
            'id', 'question', 'question_text', 'question_type',
            'yes_no_answer', 'text_answer', 'option_responses', 
            'sub_question_responses', 'created_at'
        ]
        read_only_fields = ['id', 'created_at']


class PricingCalculationSerializer(serializers.Serializer):
    """Serializer for pricing calculation requests"""
    service_id = serializers.UUIDField()
    package_id = serializers.UUIDField()
    responses = serializers.ListField(child=serializers.DictField())

    def validate_responses(self, value):
        """Validate response format"""
        for response in value:
            if 'question_id' not in response:
                raise serializers.ValidationError("Each response must have a question_id")
        return value

class FeatureSerializer(serializers.ModelSerializer):
    """Serializer for Feature model"""
    service_name = serializers.CharField(source='service.name', read_only=True)

    class Meta:
        model = Feature
        fields = [
            'id', 'service', 'service_name', 'name', 'description',
            'is_active', 'order', 'created_at', 'updated_at'
        ]
        read_only_fields = ['id', 'created_at', 'updated_at']

    def create(self, validated_data):
        # Set order to max+1 if not provided
        if 'order' not in validated_data or validated_data.get('order') is None:
            service = validated_data.get('service')
            max_order = Feature.objects.filter(service=service).aggregate(
                m=Max('order')
            )['m'] or 0
            validated_data['order'] = max_order + 1
        feature = super().create(validated_data)

        # Automatically link to all existing packages under the same service
        packages = Package.objects.filter(service=feature.service, is_active=True)
        package_features = []
        for package in packages:
            max_order = PackageFeature.objects.filter(package=package).aggregate(
                m=Max('order')
            )['m'] or 0
            package_features.append(
                PackageFeature(package=package, feature=feature, is_included=False, order=max_order + 1)
            )
        PackageFeature.objects.bulk_create(package_features)
        return feature


class PackageFeatureSerializer(serializers.ModelSerializer):
    """Serializer for PackageFeature model"""
    package_name = serializers.CharField(source='package.name', read_only=True)
    feature_name = serializers.CharField(source='feature.name', read_only=True)

    class Meta:
        model = PackageFeature
        fields = [
            'id', 'package', 'package_name', 'feature', 'feature_name',
            'is_included', 'order', 'created_at'
        ]
        read_only_fields = ['id', 'created_at']


class QuestionOptionSerializer(serializers.ModelSerializer):
    """Serializer for QuestionOption model"""
    pricing_rules = serializers.SerializerMethodField(read_only=True)
    
    class Meta:
        model = QuestionOption
        fields = [
            'id', 'option_text', 'order', 'is_active', 'created_at', 'question',
            'allow_quantity', 'max_quantity', 'pricing_rules'
        ]
        extra_kwargs = {
            'question': {'required': False}
        }
        read_only_fields = ['id', 'created_at']

    def get_pricing_rules(self, obj):
        return OptionPricingSerializer(obj.pricing_rules, many=True).data
    
class SubQuestionPricingSerializer(serializers.ModelSerializer):
    """Serializer for SubQuestionPricing model"""
    package_name = serializers.CharField(source='package.name', read_only=True)
    sub_question_text = serializers.CharField(source='sub_question.sub_question_text', read_only=True)

    class Meta:
        model = SubQuestionPricing
        fields = [
            'id', 'sub_question', 'sub_question_text', 'package', 'package_name',
            'yes_pricing_type', 'yes_value', 'created_at', 'updated_at'
        ]
        read_only_fields = ['id', 'created_at', 'updated_at']


class SubQuestionSerializer(serializers.ModelSerializer):
    """Serializer for SubQuestion model"""
    pricing_rules = serializers.SerializerMethodField(read_only=True)
    
    class Meta:
        model = SubQuestion
        fields = [
            'id', 'parent_question', 'sub_question_text', 'order', 
            'is_active', 'created_at', 'pricing_rules'
        ]
        extra_kwargs = {
            'parent_question': {'required': False}
        }
        read_only_fields = ['id', 'created_at']

    def get_pricing_rules(self, obj):
        return SubQuestionPricingSerializer(obj.pricing_rules, many=True).data




class OptionPricingSerializer(serializers.ModelSerializer):
    """Serializer for OptionPricing model"""
    package_name = serializers.CharField(source='package.name', read_only=True)
    option_text = serializers.CharField(source='option.option_text', read_only=True)

    class Meta:
        model = OptionPricing
        fields = [
            'id', 'option', 'option_text', 'package', 'package_name',
            'pricing_type', 'value', 'created_at', 'updated_at'
        ]
        read_only_fields = ['id', 'created_at', 'updated_at']



class QuestionPricingSerializer(serializers.ModelSerializer):
    """Serializer for QuestionPricing model"""
    package_name = serializers.CharField(source='package.name', read_only=True)
    question_text = serializers.CharField(source='question.question_text', read_only=True)

    class Meta:
        model = QuestionPricing
        fields = [
            'id', 'question', 'question_text', 'package', 'package_name',
            'yes_pricing_type', 'yes_value', 'created_at', 'updated_at'
        ]
        read_only_fields = ['id', 'created_at', 'updated_at']


class QuestionSerializer(serializers.ModelSerializer):
    """Serializer for Question model"""
    service_name = serializers.CharField(source='service.name', read_only=True)
    parent_question_text = serializers.CharField(source='parent_question.question_text', read_only=True)
    condition_option_text = serializers.CharField(source='condition_option.option_text', read_only=True)
    
    options = QuestionOptionSerializer(many=True, read_only=True)
    sub_questions = SubQuestionSerializer(many=True, read_only=True)
    child_questions = serializers.SerializerMethodField(read_only=True)
    pricing_rules = serializers.SerializerMethodField(read_only=True)

    class Meta:
        model = Question
        fields = [
            'id', 'service', 'service_name', 'parent_question', 'parent_question_text',
            'condition_answer', 'condition_option', 'condition_option_text',
            'question_text', 'question_type', 'order', 'is_active', 
            'created_at', 'updated_at', 'options', 'sub_questions', 
            'child_questions', 'pricing_rules', 'is_conditional', 'is_parent'
        ]
        read_only_fields = ['id', 'created_at', 'updated_at', 'is_conditional', 'is_parent']

    def get_child_questions(self, obj):
        """Get child questions recursively"""
        child_questions = obj.child_questions.filter(is_active=True).order_by('order')
        return QuestionSerializer(child_questions, many=True, context=self.context).data

    def get_pricing_rules(self, obj):
        if obj.question_type in ['yes_no', 'conditional']:
            return QuestionPricingSerializer(obj.pricing_rules, many=True).data
        elif obj.question_type in ['describe', 'quantity']:
            all_option_pricing = OptionPricing.objects.filter(option__in=obj.options.all())
            return OptionPricingSerializer(all_option_pricing, many=True).data
        elif obj.question_type == 'multiple_yes_no':
            all_sub_question_pricing = SubQuestionPricing.objects.filter(sub_question__in=obj.sub_questions.all())
            return SubQuestionPricingSerializer(all_sub_question_pricing, many=True).data
        return []


class ServiceSettingsSerializer(serializers.ModelSerializer):
    class Meta:
        model = ServiceSettings
        fields = [
            'id',
            'general_disclaimer',
            'bid_in_person_disclaimer',
            'apply_area_minimum',
            'apply_house_size_minimum',
            'apply_trip_charge_to_bid',
            'enable_dollar_minimum',
        ]



class ServiceSerializer(serializers.ModelSerializer):
    """Serializer for Service model"""
    packages = PackageSerializer(many=True, read_only=True)  # Assuming you have this
    features = FeatureSerializer(many=True, read_only=True)  # Assuming you have this
    questions = serializers.SerializerMethodField(read_only=True)
    created_by_name = serializers.CharField(source='created_by.username', read_only=True)
    settings = ServiceSettingsSerializer(read_only=True)

    class Meta:
        model = Service
        fields = [
            'id', 'name', 'description', 'price', 'hours', 'is_active', 'order',
            'created_at', 'updated_at', 'created_by', 'created_by_name',
            'questions' ,'packages', 'features','settings'
        ]
        read_only_fields = ['id', 'created_at', 'updated_at', 'created_by']

    def get_questions(self, obj):
        """Get only root questions (non-conditional ones)"""
        root_questions = obj.questions.filter(
            parent_question__isnull=True
        ).order_by('order')
        return QuestionSerializer(root_questions, many=True, context=self.context).data

    def create(self, validated_data):
        request = self.context.get('request')
        if request and hasattr(request, 'user'):
            validated_data['created_by'] = request.user
        return super().create(validated_data)



class ServiceListSerializer(serializers.ModelSerializer):
    """Lightweight serializer for Service list view"""
    # packages_count = serializers.IntegerField(source='packages.count', read_only=True)
    # features_count = serializers.IntegerField(source='features.count', read_only=True)
    questions_count = serializers.IntegerField(source='questions.count', read_only=True)
    created_by_name = serializers.CharField(source='created_by.username', read_only=True)

    class Meta:
        model = Service
        fields = [
            'id', 'name', 'description', 'price', 'hours', 'is_active', 'order',
            'created_at', 'updated_at', 'created_by_name',
            'questions_count'  # 'packages_count', 'features_count', 
        ]


class ServiceBasicSerializer(serializers.ModelSerializer):
    """Minimal serializer for basic service details"""
    class Meta:
        model = Service
        fields = [
            'id', 'name', 'description', 'price', 'hours', 'is_active', 'order'
        ]


# Nested serializers for complex operations
class QuestionCreateSerializer(serializers.ModelSerializer):
    """Serializer for creating questions with nested data"""
    options = QuestionOptionSerializer(many=True, required=False)
    sub_questions = SubQuestionSerializer(many=True, required=False)
    
    class Meta:
        model = Question
        fields = [
            'service', 'parent_question', 'condition_answer', 'condition_option',
            'question_text', 'question_type', 'order', 'is_active', 
            'options', 'sub_questions',"id",
        ]

    def validate(self, data):
        """Validate question data based on type"""
        question_type = data.get('question_type')
        options = data.get('options', [])
        sub_questions = data.get('sub_questions', [])
        parent_question = data.get('parent_question')
        condition_answer = data.get('condition_answer')

        # Validate conditional questions
        if parent_question and not condition_answer:
            raise serializers.ValidationError(
                "Conditional questions must have a condition_answer"
            )

        # Validate question type requirements
        if question_type in ['describe', 'quantity'] and not options:
            raise serializers.ValidationError(
                f"{question_type} questions must have options"
            )
        
        if question_type == 'multiple_yes_no' and not sub_questions:
            raise serializers.ValidationError(
                "multiple_yes_no questions must have sub_questions"
            )

        return data

    def create(self, validated_data):
        options_data = validated_data.pop('options', [])
        sub_questions_data = validated_data.pop('sub_questions', [])
        
        question = Question.objects.create(**validated_data)
        
        # Create options if provided
        for option_data in options_data:
            QuestionOption.objects.create(question=question, **option_data)
        
        # Create sub-questions if provided
        for sub_question_data in sub_questions_data:
            SubQuestion.objects.create(parent_question=question, **sub_question_data)
            
        return question

    def update(self, instance, validated_data):
        options_data = validated_data.pop('options', [])
        sub_questions_data = validated_data.pop('sub_questions', [])
        
        # Update question fields
        for attr, value in validated_data.items():
            setattr(instance, attr, value)
        instance.save()
        
        # Handle options update (simple approach - recreate all)
        if options_data:
            instance.options.all().delete()
            for option_data in options_data:
                QuestionOption.objects.create(question=instance, **option_data)
        
        # Handle sub-questions update (simple approach - recreate all)
        if sub_questions_data:
            instance.sub_questions.all().delete()
            for sub_question_data in sub_questions_data:
                SubQuestion.objects.create(parent_question=instance, **sub_question_data)
        
        return instance



class PackageWithFeaturesSerializer(serializers.ModelSerializer):
    """Serializer for Package with included features"""
    features = serializers.SerializerMethodField()

    class Meta:
        model = Package
        fields = [
            'id', 'service', 'name', 'base_price', 'order', 
            'is_active', 'created_at', 'updated_at', 'features'
        ]
        read_only_fields = ['id', 'created_at', 'updated_at']

    def get_features(self, obj):
        """Get included features for the package (ordered by order field)"""
        package_features = PackageFeature.objects.filter(
            package=obj, is_included=True
        ).order_by('order', 'created_at')
        return [
            {
                'id': pf.id,
                'feature_id': pf.feature.id,
                'name': pf.feature.name,
                'description': pf.feature.description,
                'is_included': pf.is_included,
                'order': pf.order
            }
            for pf in package_features
        ]


# Future serializers for user side
class OrderQuestionAnswerSerializer(serializers.ModelSerializer):
    """Serializer for OrderQuestionAnswer model"""
    question_text = serializers.CharField(source='question.question_text', read_only=True)
    question_type = serializers.CharField(source='question.question_type', read_only=True)
    selected_option_text = serializers.CharField(source='selected_option.option_text', read_only=True)

    class Meta:
        model = OrderQuestionAnswer
        fields = [
            'id', 'question', 'question_text', 'question_type',
            'yes_no_answer', 'selected_option', 'selected_option_text',
            'price_adjustment', 'created_at'
        ]
        read_only_fields = ['id', 'created_at']


class OrderSerializer(serializers.ModelSerializer):
    """Serializer for Order model"""
    service_name = serializers.CharField(source='service.name', read_only=True)
    package_name = serializers.CharField(source='package.name', read_only=True)
    location_name = serializers.CharField(source='location.name', read_only=True)
    question_answers = OrderQuestionAnswerSerializer(many=True, read_only=True)

    class Meta:
        model = Order
        fields = [
            'id', 'service', 'service_name', 'package', 'package_name',
            'location', 'location_name', 'base_price', 'trip_surcharge',
            'question_adjustments', 'total_price', 'status',
            'created_at', 'updated_at', 'question_answers'
        ]
        read_only_fields = ['id', 'created_at', 'updated_at']


# Utility serializers for complex operations
class BulkPricingUpdateSerializer(serializers.Serializer):
    """Serializer for bulk updating pricing rules"""
    question_id = serializers.UUIDField()
    pricing_rules = serializers.ListField(
        child=serializers.DictField(), 
        allow_empty=False
    )

    def validate_pricing_rules(self, value):
        """Validate pricing rules structure"""
        for rule in value:
            required_fields = ['package_id', 'pricing_type', 'value']
            for field in required_fields:
                if field not in rule:
                    raise serializers.ValidationError(f"Each pricing rule must have a {field}")
        return value

class ServiceAnalyticsSerializer(serializers.Serializer):
    """Serializer for service analytics data"""
    service_id = serializers.UUIDField()
    service_name = serializers.CharField()
    total_packages = serializers.IntegerField()
    total_features = serializers.IntegerField()
    total_questions = serializers.IntegerField()
    average_package_price = serializers.DecimalField(max_digits=10, decimal_places=2)
    created_at = serializers.DateTimeField()




from .models import GlobalPackageTemplate, GlobalSizePackage, ServicePackageSizeMapping

class GlobalPackageTemplateSerializer(serializers.ModelSerializer):
    class Meta:
        model = GlobalPackageTemplate
        fields = ['label', 'price', 'order']


class GlobalSizePackageSerializer(serializers.ModelSerializer):
    template_prices = GlobalPackageTemplateSerializer(many=True)

    class Meta:
        model = GlobalSizePackage
        fields = ['id', 'min_sqft', 'max_sqft', 'order', 'template_prices']

    def create(self, validated_data):
        from .models import Service, ServicePackageSizeMapping

        templates = validated_data.pop('template_prices', [])
        global_size = GlobalSizePackage.objects.create(**validated_data)

        # Create template prices
        for template in templates:
            GlobalPackageTemplate.objects.create(global_size=global_size, **template)

        # 🧠 Auto-map to all services' packages by order
        all_services = Service.objects.prefetch_related('packages').filter(is_active=True)

        for service in all_services:
            service_packages = list(service.packages.filter(is_active=True).order_by('order'))
            sorted_templates = sorted(global_size.template_prices.all(), key=lambda t: t.order)

            for idx, template in enumerate(sorted_templates):
                if idx < len(service_packages):
                    service_package = service_packages[idx]
                    # Avoid duplicates
                    ServicePackageSizeMapping.objects.get_or_create(
                        service_package=service_package,
                        global_size=global_size,
                        defaults={'price': template.price}
                    )

        return global_size
    
    def update(self, instance, validated_data):
        templates_data = validated_data.pop('template_prices', [])
        
        # Update basic fields
        for attr, value in validated_data.items():
            setattr(instance, attr, value)
        instance.save()

        # Update template_prices
        existing_templates = {t.order: t for t in instance.template_prices.all()}
        for template_data in templates_data:
            order = template_data.get('order')
            if order in existing_templates:
                # Update existing
                template_obj = existing_templates[order]
                for attr, value in template_data.items():
                    setattr(template_obj, attr, value)
                template_obj.save()
            else:
                # Create new
                GlobalPackageTemplate.objects.create(global_size=instance, **template_data)

        return instance

    def to_representation(self, instance):
        representation = super().to_representation(instance)
        representation['template_prices'] = GlobalPackageTemplateSerializer(
            instance.template_prices.all().order_by('order'), many=True
        ).data
        return representation
    


class ServicePackageSizeMappingSerializer(serializers.ModelSerializer):
    service_package_name = serializers.CharField(source='service_package.name', read_only=True)
    global_size_range = serializers.SerializerMethodField()

    class Meta:
        model = ServicePackageSizeMapping
        fields = ['id', 'service_package', 'service_package_name', 'global_size', 'global_size_range', 'price']

    def get_global_size_range(self, obj):
        return f"{obj.global_size.min_sqft} – {obj.global_size.max_sqft} sqft"
    


class GlobalBasePriceSerializer(serializers.ModelSerializer):
    class Meta:
        model = GlobalBasePrice
        fields = ["id", "base_price", "updated_at"]
        read_only_fields = ["id", "updated_at"]