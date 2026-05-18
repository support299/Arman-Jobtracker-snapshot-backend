# user_views.py - Views for user-side functionality
from rest_framework import generics, status, permissions
from rest_framework.decorators import api_view, permission_classes
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework.permissions import AllowAny
from rest_framework.exceptions import PermissionDenied, NotFound, ValidationError
from django.shortcuts import get_object_or_404
from django.db import transaction
from django.db.models import Q, Prefetch
from decimal import Decimal
from datetime import timedelta
from django.utils import timezone
from accounts.permissions import AccountScopedPermission
from accounts.mixins import AccountScopedQuerysetMixin
from .account_scope_utils import get_submission_for_account, get_job_for_account
from .reschedule_utils import clone_submission_for_reschedule
from service_app.account_scope_utils import get_service_for_account
from service_app.models import ServiceSettings
from service_app.models import (
    Service, Package, Feature, PackageFeature, Location,
    Question, QuestionOption, SubQuestion, GlobalSizePackage,
    ServicePackageSizeMapping, QuestionPricing, OptionPricing, SubQuestionPricing, GlobalBasePrice
)
from .models import (
    CustomerSubmission, CustomerServiceSelection, CustomerQuestionResponse,
    CustomerOptionResponse, CustomerSubQuestionResponse, CustomerPackageQuote,CustomService, QuoteSchedule, CustomerSubmissionImage
)
from .serializers import (
    LocationPublicSerializer, ServicePublicSerializer, PackagePublicSerializer,
    QuestionPublicSerializer, GlobalSizePackagePublicSerializer,
    CustomerSubmissionCreateSerializer, CustomerSubmissionDetailSerializer,AddressSerializer,
    ServiceQuestionResponseSerializer, PricingCalculationRequestSerializer,SubmitFinalQuoteSerializer,ContactSerializer,
    ConditionalQuestionRequestSerializer, CustomerPackageQuoteSerializer,ConditionalQuestionResponseSerializer,ServiceResponseSubmissionSerializer,QuoteScheduleUpdateSerializer, CustomerSubmissionImageSerializer,
    JobRescheduleQuoteCreateSerializer, RescheduleConvertToJobSerializer,
    GHLAccountPublicSerializer,
)
from service_app.serializers import GlobalBasePriceSerializer, UserSerializer
from service_app.models import User
from payroll_app.models import EmployeeProfile
from jobtracker_app.models import Job
from jobtracker_app.serializers import JobSerializer

from quote_app.helpers import create_or_update_ghl_contact
from accounts.utils import (
    get_ghl_media_storage_for_location,
    upload_file_to_ghl_media,
    delete_ghl_media,
    compress_image_for_upload,
)
from rest_framework.generics import ListAPIView
import requests
from accounts.models import GHLAuthCredentials, GHLCustomField, Contact, Address, Calendar
from accounts.account_scope import DEFAULT_LOCATION_ID

from rest_framework import viewsets
from .serializers import CustomServiceSerializer


from rest_framework.pagination import PageNumberPagination

import json
import re
import uuid
from django.http import JsonResponse
from django.utils.dateparse import parse_datetime
# Must match GHL calendar name and jobtracker_app lookups (typo is intentional in GHL).
RECURRING_SERVICE_CALENDAR_NAME = "Reccuring Service Calendar"

class ContactPagination(PageNumberPagination):
    page_size = 20  # items per page
    page_size_query_param = 'page_size'  # allow client to override with ?page_size=50
    max_page_size = 100


class ContactSearchView(AccountScopedQuerysetMixin, ListAPIView):
    queryset = Contact.objects.all()
    serializer_class = ContactSerializer
    pagination_class = ContactPagination
    permission_classes = [AccountScopedPermission, AllowAny]
    account_lookup = "account"

    def get_queryset(self):
        query = self.request.query_params.get('search', '').strip()
        qs = super().get_queryset()
        if query:
            keywords = query.split()
            q_object = Q()
            for keyword in keywords:
                q_object |= Q(first_name__icontains=keyword)
                q_object |= Q(last_name__icontains=keyword)
                q_object |= Q(email__icontains=keyword)
                q_object |= Q(phone__icontains=keyword)
                q_object |= Q(country__icontains=keyword)
            qs = qs.filter(q_object)

        return qs.order_by('-date_added')
    


class AddressByContactView(APIView):
    permission_classes = [AccountScopedPermission, AllowAny]

    def get(self, request, contact_id):
        if contact_id is None:
            return Response({'error': 'contact_id is required'}, status=status.HTTP_400_BAD_REQUEST)
        account = getattr(request, 'account', None)
        # URL uses <int:contact_id> so contact_id is Django pk; support both pk and GHL contact_id (string)
        if isinstance(contact_id, int):
            contact = get_object_or_404(Contact, pk=contact_id, account=account)
        else:
            contact = get_object_or_404(Contact, contact_id=contact_id, account=account)
        addresses = Address.objects.filter(contact=contact)
        serializer = AddressSerializer(addresses, many=True)
        return Response(serializer.data, status=status.HTTP_200_OK)


class AccountInfoView(APIView):
    """
    Public account branding/settings for quote flows.

    Requires location_id (query param, body, or X-Location-Id header). Unauthenticated.
    Returns GHL account metadata only — never access/refresh tokens.
    """
    permission_classes = [AccountScopedPermission, AllowAny]

    def get(self, request):
        account = getattr(request, 'account', None)
        if not account:
            return Response(
                {
                    'error': (
                        'Account could not be determined. Provide location_id in query, '
                        'body, or X-Location-Id header.'
                    ),
                },
                status=status.HTTP_404_NOT_FOUND,
            )
        return Response(GHLAccountPublicSerializer(account).data)


# Step 1: Get initial data (locations, services, size ranges)
class InitialDataView(APIView):
    """Get initial data for the quote form (scoped to account via location_id when unauthenticated)."""
    permission_classes = [AccountScopedPermission, AllowAny]

    def get(self, request):
        account = getattr(request, 'account', None)
        locations = Location.objects.filter(is_active=True, account=account).order_by('name')
        services = Service.objects.filter(is_active=True, account=account).order_by('order', 'name')
        size_ranges = GlobalSizePackage.objects.filter(account=account).order_by('order', 'min_sqft')
        
        # Project-based employees for this account only (active, exclude Django superusers)
        project_employees = EmployeeProfile.objects.filter(
            account=account,
            pay_scale_type='project',
            status='active',
            user__isnull=False,
            user__is_active=True,
            user__is_superuser=False,
            user__account=account,
        ).select_related('user').order_by('user__first_name', 'user__last_name', 'user__username')

        project_users = [emp.user for emp in project_employees]
        
        return Response({
            'locations': LocationPublicSerializer(locations, many=True).data,
            'services': ServicePublicSerializer(services, many=True).data,
            'size_ranges': GlobalSizePackagePublicSerializer(size_ranges, many=True).data,
            'project_employees': UserSerializer(project_users, many=True).data
        })

# Step 2: Create customer submission
class CustomerSubmissionCreateView(generics.CreateAPIView):
    """Create a new customer submission (scoped to account via location_id when unauthenticated)."""
    queryset = CustomerSubmission.objects.all()
    serializer_class = CustomerSubmissionCreateSerializer
    permission_classes = [AccountScopedPermission, AllowAny]

    def create(self, request, *args, **kwargs):
        account = getattr(request, 'account', None)
        if not account and DEFAULT_LOCATION_ID:
            account = GHLAuthCredentials.objects.filter(location_id=DEFAULT_LOCATION_ID).first()
            if account:
                request.account = account
        if not account:
            return Response(
                {'error': 'Account could not be determined. Provide location_id in query, body, or X-Location-Id header when unauthenticated.'},
                status=status.HTTP_400_BAD_REQUEST
            )
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        self.perform_create(serializer)
        submission = serializer.instance
        return Response({
            'submission_id': submission.id,
            'message': 'Customer information saved successfully'
        }, status=status.HTTP_201_CREATED)

    def perform_create(self, serializer):
        serializer.save(account=getattr(self.request, 'account', None))

# Step 3: Add services to submission
class AddServicesToSubmissionView(APIView):
    """Add selected services to customer submission (scoped to account)."""
    permission_classes = [AccountScopedPermission, AllowAny]

    def post(self, request, submission_id):
        account = getattr(request, 'account', None)
        submission = get_submission_for_account(submission_id, account)
        service_ids = request.data.get('service_ids', [])
        
        # if not service_ids:
        #     return Response({'error': 'No services selected'}, status=status.HTTP_400_BAD_REQUEST)
        
        try:
            with transaction.atomic():
                # Clear existing selections
                # submission.customerserviceselection_set.all().delete()
                CustomerServiceSelection.objects.filter(
                    submission=submission
                ).exclude(service_id__in=service_ids).delete()
                
                # Add new selections
                for service_id in service_ids:
                    service = get_object_or_404(Service, id=service_id, is_active=True, account=account)
                    CustomerServiceSelection.objects.get_or_create(
                        submission=submission,
                        service=service
                    )
                
                return Response({'message': 'Services added successfully'})
        
        except Exception as e:
            return Response({'error': str(e)}, status=status.HTTP_400_BAD_REQUEST)

# Step 4: Get questions for a specific service
class ServiceQuestionsView(APIView):
    """Get questions for a specific service (scoped to account)."""
    permission_classes = [AccountScopedPermission, AllowAny]

    def get(self, request, service_id):
        account = getattr(request, 'account', None)
        service = get_service_for_account(service_id, account)
        if not service.is_active:
            raise NotFound("Service not found.")
        
        # Get root questions (no parent)
        root_questions = Question.objects.filter(
            service=service,
            is_active=True,
            parent_question__isnull=True
        ).prefetch_related(
            'options',
            'sub_questions',
            'child_questions__options',
            'child_questions__sub_questions'
        ).order_by('order')
        
        serializer = QuestionPublicSerializer(root_questions, many=True, context={'request': request})
        
        return Response({
            'service': {
                'id': service.id,
                'name': service.name,
                'description': service.description
            },
            'questions': serializer.data
        })

# Step 5: Get conditional questions
class ConditionalQuestionsView(APIView):
    """Get conditional questions based on parent answer (scoped to account)."""
    permission_classes = [AccountScopedPermission, AllowAny]

    def post(self, request):
        serializer = ConditionalQuestionRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        parent_question_id = serializer.validated_data['parent_question_id']
        answer = serializer.validated_data.get('answer')
        option_id = serializer.validated_data.get('option_id')
        account = getattr(request, 'account', None)
        parent_question = get_object_or_404(Question, id=parent_question_id, service__account=account)
        
        # Build filter for conditional questions
        filter_kwargs = {
            'parent_question': parent_question,
            'is_active': True
        }
        
        if answer:
            filter_kwargs['condition_answer'] = answer
        if option_id:
            filter_kwargs['condition_option_id'] = option_id
        
        conditional_questions = Question.objects.filter(**filter_kwargs).prefetch_related(
            'options',
            'sub_questions'
        ).order_by('order')
        
        questions_serializer = QuestionPublicSerializer(conditional_questions, many=True, context={'request': request})
        
        return Response({
            'parent_question_id': parent_question_id,
            'conditional_questions': questions_serializer.data
        })

# Step 6: Submit service responses and calculate pricing
class SubmitServiceResponsesView(APIView):
    """Submit responses for a service including conditional questions (scoped to account)."""
    permission_classes = [AccountScopedPermission, AllowAny]
    
    def post(self, request, submission_id, service_id):
        account = getattr(request, 'account', None)
        submission = get_submission_for_account(submission_id, account)
        service_selection = get_object_or_404(
            CustomerServiceSelection, 
            submission=submission,
            service_id=service_id
        )
        
        responses = request.data.get('responses', [])
        
        try:
            with transaction.atomic():
                # Validate conditional question logic first
                validation_result = self._validate_conditional_responses(responses, service_id)
                if not validation_result['valid']:
                    return Response({
                        'error': 'Invalid conditional question responses',
                        'details': validation_result['errors']
                    }, status=status.HTTP_400_BAD_REQUEST)
                
                # Clear existing responses
                service_selection.question_responses.all().delete()
                
                # Process responses in dependency order (parents first, then children)
                ordered_responses = self._order_responses_by_dependency(responses)
                
                total_adjustment = Decimal('0.00')
                
                for response_data in ordered_responses:
                    question_id = response_data['question_id']
                    question = get_object_or_404(Question, id=question_id)
                    
                    # Create question response
                    question_response = CustomerQuestionResponse.objects.create(
                        service_selection=service_selection,
                        question=question,
                        yes_no_answer=response_data.get('yes_no_answer'),
                        text_answer=response_data.get('text_answer', '')
                    )
                    
                    # Calculate pricing adjustment
                    question_adjustment = self._calculate_question_adjustment(
                        question, response_data, question_response, service_selection
                    )

                    print("question_adjustment:",question_adjustment)
                    
                    question_response.price_adjustment = question_adjustment
                    question_response.save()
                    
                    total_adjustment += question_adjustment
                
                # Update service selection totals
                service_selection.question_adjustments = total_adjustment
                service_selection.save()
                # Generate package quotes for ALL packages (per-package surcharge when apply_trip_charge_to_bid)
                self._generate_all_package_quotes(service_selection, submission)

                # Check if all services have responses
                all_services_completed = self._check_all_services_completed(submission)
                all_services_completed = True
                if all_services_completed:
                    submission.status = 'responses_completed'
                    submission.save()

                create_or_update_ghl_contact(submission)

                return Response({
                    'message': 'Responses submitted successfully',
                    'all_services_completed': all_services_completed,
                    'total_questions_answered': len(ordered_responses),
                    'conditional_questions_answered': len([r for r in responses if r.get('parent_question_id')])
                })
        
        except Exception as e:
            return Response({'error': str(e)}, status=status.HTTP_400_BAD_REQUEST)
    
    def _validate_conditional_responses(self, responses, service_id):
        """Validate that conditional questions are only answered when conditions are met"""
        validation_errors = []
        
        # Create lookup maps
        responses_by_question = {r['question_id']: r for r in responses}
        
        for response in responses:
            question_id = response['question_id']
            
            # Skip validation for non-conditional questions
            if not response.get('parent_question_id'):
                continue
                
            try:
                question = Question.objects.get(id=question_id)
                parent_question_id = response['parent_question_id']
                
                # Check if parent question was answered
                if parent_question_id not in responses_by_question:
                    validation_errors.append(
                        f"Conditional question {question_id} answered but parent {parent_question_id} not found"
                    )
                    continue
                
                parent_response = responses_by_question[parent_question_id]
                parent_question = Question.objects.get(id=parent_question_id)
                
                # Validate condition based on type
                condition_met = self._check_condition_met(
                    parent_question, parent_response, question, response
                )
                
                if not condition_met:
                    validation_errors.append(
                        f"Conditional question {question_id} answered but condition not met"
                    )
                    
            except Question.DoesNotExist:
                validation_errors.append(f"Question {question_id} not found")
        
        return {
            'valid': len(validation_errors) == 0,
            'errors': validation_errors
        }
    
    def _check_condition_met(self, parent_question, parent_response, conditional_question, conditional_response):
        """Check if the condition for a conditional question is met"""
        
        # For yes/no parent questions
        if parent_question.question_type == 'yes_no':
            expected_answer = conditional_question.condition_answer
            actual_answer = 'yes' if parent_response.get('yes_no_answer') else 'no'
            return expected_answer == actual_answer
        
        # For option-based parent questions (describe/quantity)
        elif parent_question.question_type in ['describe', 'quantity']:
            expected_option_id = str(conditional_question.condition_option_id) if conditional_question.condition_option_id else None
            selected_options = parent_response.get('selected_options', [])
            selected_option_ids = [str(opt['option_id']) for opt in selected_options]
            
            return expected_option_id in selected_option_ids
        
        # For multiple_yes_no parent questions
        elif parent_question.question_type == 'multiple_yes_no':
            # This would need custom logic based on your requirements
            # For now, assume condition is met if any sub-question is answered yes
            sub_answers = parent_response.get('sub_question_answers', [])
            return any(sub['answer'] for sub in sub_answers)
        
        return False
    
    def _order_responses_by_dependency(self, responses):
        """Order responses so parent questions are processed before conditional questions"""
        parent_responses = []
        conditional_responses = []
        
        for response in responses:
            if response.get('parent_question_id'):
                conditional_responses.append(response)
            else:
                parent_responses.append(response)
        
        # Sort conditional responses by their parent order
        conditional_responses.sort(key=lambda x: x.get('parent_question_id', ''))
        
        return parent_responses + conditional_responses
    
    def _calculate_question_adjustment(self, question, response_data, question_response, service_selection):
        """FIXED: Calculate price adjustment - don't average across packages for quantity questions"""
        
        print(f"\n=== FIXED: Processing question: {question.question_text} ===")
        print(f"Question type: {question.question_type}")
        print(f"Response data: {response_data}")
        
        # Get all packages for this service
        packages = Package.objects.filter(service=question.service, is_active=True)
        print(f"Found {packages.count()} packages for service: {question.service.name}")
        
        # For quantity questions, we don't calculate a single adjustment
        # Instead, we store the responses and calculate per-package in _calculate_package_specific_adjustments
        total_adjustment = Decimal('0.00')  # This will be 0 for quantity questions
        
        if question.question_type == 'yes_no':
            if response_data.get('yes_no_answer') is True:
                package_adjustments = []
                for package in packages:
                    pricing = QuestionPricing.objects.filter(
                        question=question, package=package
                    ).first()
                    if pricing and pricing.yes_pricing_type != 'ignore':
                        if pricing.yes_pricing_type in ('upcharge_percent_of_total', 'discount_percent_of_total'):
                            continue  # applied per-package in _calculate_package_specific_adjustments
                        package_adjustments.append(pricing.yes_value)
                        print(f"Yes/No adjustment for {package.name}: {pricing.yes_value}")
                if package_adjustments:
                    total_adjustment = sum(package_adjustments) / len(package_adjustments)
        
        elif question.question_type in ['describe', 'quantity']:
            selected_options = response_data.get('selected_options', [])
            print(f"Selected options: {selected_options}")
            
            for option_data in selected_options:
                option_id = option_data['option_id']
                quantity = option_data.get('quantity', 1)
                
                print(f"\nProcessing option {option_id} with quantity {quantity}")
                
                option = get_object_or_404(QuestionOption, id=option_id)
                print(f"Option text: {option.option_text}")
                
                # Create option response - store the quantity for later package-specific calculations
                option_response = CustomerOptionResponse.objects.create(
                    question_response=question_response,
                    option=option,
                    quantity=quantity
                )
                print(f"Created option response with quantity: {option_response.quantity}")
                
                # For quantity questions, don't calculate adjustment here
                # It will be calculated per-package in _calculate_package_specific_adjustments
                if question.question_type == 'quantity':
                    print(f"Quantity question - adjustment will be calculated per package")
                    option_response.price_adjustment = Decimal('0.00')  # Store 0 for now
                    option_response.save()
                    # Don't add to total_adjustment
                
                # For describe questions, calculate average as before (skip percent_of_total)
                elif question.question_type == 'describe':
                    package_adjustments = []
                    for package in packages:
                        pricing = OptionPricing.objects.filter(
                            option=option, package=package
                        ).first()
                        if pricing and pricing.pricing_type not in ('ignore', 'upcharge_percent_of_total', 'discount_percent_of_total'):
                            if pricing.pricing_type == 'per_quantity':
                                package_adjustment = pricing.value * quantity
                            else:
                                package_adjustment = pricing.value
                            package_adjustments.append(package_adjustment)
                    if package_adjustments:
                        option_adjustment = sum(package_adjustments) / len(package_adjustments)
                        option_response.price_adjustment = option_adjustment
                        option_response.save()
                        total_adjustment += option_adjustment
        
        elif question.question_type == 'multiple_yes_no':
            sub_question_answers = response_data.get('sub_question_answers', [])
            for sub_answer in sub_question_answers:
                if sub_answer.get('answer') is True:
                    sub_question_id = sub_answer['sub_question_id']
                    sub_question = get_object_or_404(SubQuestion, id=sub_question_id)
                    
                    # Create sub-question response
                    sub_response = CustomerSubQuestionResponse.objects.create(
                        question_response=question_response,
                        sub_question=sub_question,
                        answer=True
                    )
                    
                    # Calculate sub-question pricing (average across packages; skip percent_of_total)
                    sub_adjustment = Decimal('0.00')
                    for package in packages:
                        pricing = SubQuestionPricing.objects.filter(
                            sub_question=sub_question, package=package
                        ).first()
                        if pricing and pricing.yes_pricing_type not in ('ignore', 'upcharge_percent_of_total', 'discount_percent_of_total'):
                            if pricing.yes_pricing_type == 'discount_percent':
                                sub_adjustment -= pricing.yes_value
                            else:
                                sub_adjustment += pricing.yes_value
                            print(f"Sub-question adjustment for {package.name}: {pricing.yes_value}")
                    if packages.count() > 0:
                        sub_adjustment = sub_adjustment / packages.count()
                    
                    sub_response.price_adjustment = sub_adjustment
                    sub_response.save()
                    total_adjustment += sub_adjustment
        
        print(f"=== Final question adjustment (for averaging): {total_adjustment} ===\n")
        return total_adjustment

    
    def _generate_package_quotes(self, service_selection, submission):
        """Generate package quotes for the service"""
        service = service_selection.service
        packages = Package.objects.filter(service=service, is_active=True)
        
        # Get square footage pricing
        sqft_mappings = ServicePackageSizeMapping.objects.filter(
            service_package__service=service,
            global_size__min_sqft__lte=submission.house_sqft,
            global_size__max_sqft__gte=submission.house_sqft
        ).select_related('service_package', 'global_size')
        
        # Create mapping dict for quick lookup
        sqft_pricing = {mapping.service_package_id: mapping.price for mapping in sqft_mappings}
        
        # Check if location surcharge applies
        surcharge_amount = Decimal('0.00')
        if submission.location and hasattr(service, 'settings'):
            settings = service.settings
            if settings.apply_trip_charge_to_bid:
                surcharge_amount = submission.location.trip_surcharge
                service_selection.surcharge_applicable = True
                service_selection.surcharge_amount = surcharge_amount
                service_selection.save()
        
        # Generate quotes for each package
        for package in packages:
            base_price = package.base_price
            sqft_price = sqft_pricing.get(package.id, Decimal('0.00'))
            question_adjustments = service_selection.question_adjustments
            
            total_price = base_price + sqft_price + question_adjustments + surcharge_amount
            
            # Get package features
            package_features = PackageFeature.objects.filter(package=package).select_related('feature')
            
            # Convert UUIDs to strings here
            included_features = [str(pf.feature.id) for pf in package_features if pf.is_included]
            excluded_features = [str(pf.feature.id) for pf in package_features if not pf.is_included]
            
            CustomerPackageQuote.objects.update_or_create(
                service_selection=service_selection,
                package=package,
                defaults={
                    'base_price': base_price,
                    'sqft_price': sqft_price,
                    'question_adjustments': question_adjustments,
                    'surcharge_amount': surcharge_amount,
                    'total_price': total_price,
                    'included_features': included_features,
                    'excluded_features': excluded_features
                }
            )


    def _generate_all_package_quotes(self, service_selection, submission):
        """Generate quotes for ALL packages in the service"""
        service = service_selection.service
        packages = Package.objects.filter(service=service, is_active=True)
        
        # Get square footage pricing
        sqft_mappings = ServicePackageSizeMapping.objects.filter(
            service_package__service=service
        ).filter(
            Q(global_size__min_sqft__lte=submission.house_sqft) &
            (Q(global_size__max_sqft__gte=submission.house_sqft) | Q(global_size__max_sqft__isnull=True))
        ).select_related('service_package', 'global_size')
        
        # Create mapping dict for quick lookup
        sqft_pricing = {mapping.service_package_id: mapping.price for mapping in sqft_mappings}
        
        # Check if location surcharge applies
        surcharge_amount = Decimal('0.00')
        surcharge_applied = False
        surcharge_amount_applied = Decimal('0.00')
        if submission.location and hasattr(service, 'settings'):
            try:
                settings = service.settings
                if settings.apply_trip_charge_to_bid:
                    surcharge_amount = submission.location.trip_surcharge or Decimal('0.00')
                    surcharge_amount_applied = surcharge_amount
                    service_selection.surcharge_applicable = surcharge_amount > Decimal('0.00')
                    service_selection.surcharge_amount = surcharge_amount
                    surcharge_applied = service_selection.surcharge_applicable
                    service_selection.save(update_fields=['surcharge_applicable', 'surcharge_amount'])
            except ServiceSettings.DoesNotExist:
                # Service doesn't have settings, no surcharge
                pass
        
        # Clear existing quotes for this service
        service_selection.package_quotes.all().delete()
        
        # Generate quotes for each package
        for package in packages:
            base_price = package.base_price
            sqft_price = sqft_pricing.get(package.id, Decimal('0.00'))
            
            # Calculate package-specific question adjustments (two-pass: fixed then % of total)
            question_adjustments = self._calculate_package_specific_adjustments(
                service_selection, package,
                base_price=base_price,
                sqft_price=sqft_price,
                surcharge_amount=surcharge_amount,
            )
            
            total_price = base_price + sqft_price + question_adjustments + surcharge_amount
            
            # Get package features
            package_features = PackageFeature.objects.filter(package=package).select_related('feature')
            included_features = [str(pf.feature.id) for pf in package_features if pf.is_included]
            excluded_features = [str(pf.feature.id) for pf in package_features if not pf.is_included]
            
            CustomerPackageQuote.objects.create(
                service_selection=service_selection,
                package=package,
                base_price=base_price,
                sqft_price=sqft_price,
                question_adjustments=question_adjustments,
                surcharge_amount=surcharge_amount,
                total_price=total_price,
                included_features=included_features,
                excluded_features=excluded_features,
                is_selected=False  # Initially not selected
            )
        return surcharge_applied,surcharge_amount_applied


    def _calculate_package_specific_adjustments(self, service_selection, package, base_price, sqft_price, surcharge_amount):
        """Calculate question adjustments per package. Two-pass: (1) fixed adjustments, (2) % of package subtotal."""
        PERCENT_OF_TOTAL = ('upcharge_percent_of_total', 'discount_percent_of_total')
        fixed_sum = Decimal('0.00')
        percent_entries = []  # list of (sign, value): adjustment = subtotal * sign * value / 100

        for question_response in service_selection.question_responses.all():
            question = question_response.question
            question_fixed = Decimal('0.00')

            if question.question_type == 'yes_no':
                if question_response.yes_no_answer is True:
                    pricing = QuestionPricing.objects.filter(
                        question=question, package=package
                    ).first()
                    if pricing and pricing.yes_pricing_type != 'ignore':
                        if pricing.yes_pricing_type in PERCENT_OF_TOTAL:
                            sign = 1 if pricing.yes_pricing_type == 'upcharge_percent_of_total' else -1
                            percent_entries.append((sign, pricing.yes_value))
                        elif pricing.yes_pricing_type == 'upcharge_percent':
                            question_fixed += pricing.yes_value
                        elif pricing.yes_pricing_type == 'discount_percent':
                            question_fixed -= pricing.yes_value
                        elif pricing.yes_pricing_type == 'fixed_price':
                            question_fixed += pricing.yes_value

            elif question.question_type in ['describe', 'quantity']:
                for option_response in question_response.option_responses.all():
                    pricing = OptionPricing.objects.filter(
                        option=option_response.option, package=package
                    ).first()
                    if not pricing or pricing.pricing_type == 'ignore':
                        continue
                    if pricing.pricing_type in PERCENT_OF_TOTAL:
                        sign = 1 if pricing.pricing_type == 'upcharge_percent_of_total' else -1
                        percent_entries.append((sign, pricing.value))
                        continue
                    if question.question_type == 'quantity':
                        if pricing.pricing_type == 'discount_percent':
                            question_fixed -= pricing.value * option_response.quantity
                        elif pricing.pricing_type == 'upcharge_percent':
                            question_fixed += pricing.value * option_response.quantity
                        elif pricing.pricing_type == 'per_quantity':
                            question_fixed += pricing.value * option_response.quantity
                        elif pricing.pricing_type == 'fixed_price':
                            question_fixed += pricing.value * option_response.quantity
                    elif question.question_type == 'describe':
                        if pricing.pricing_type == 'per_quantity':
                            question_fixed += pricing.value * option_response.quantity
                        elif pricing.pricing_type == 'upcharge_percent':
                            question_fixed += pricing.value
                        elif pricing.pricing_type == 'discount_percent':
                            question_fixed -= pricing.value
                        elif pricing.pricing_type == 'fixed_price':
                            question_fixed += pricing.value

            elif question.question_type == 'multiple_yes_no':
                for sub_response in question_response.sub_question_responses.all():
                    if sub_response.answer is not True:
                        continue
                    pricing = SubQuestionPricing.objects.filter(
                        sub_question=sub_response.sub_question, package=package
                    ).first()
                    if not pricing or pricing.yes_pricing_type == 'ignore':
                        continue
                    if pricing.yes_pricing_type in PERCENT_OF_TOTAL:
                        sign = 1 if pricing.yes_pricing_type == 'upcharge_percent_of_total' else -1
                        percent_entries.append((sign, pricing.yes_value))
                    elif pricing.yes_pricing_type == 'upcharge_percent':
                        question_fixed += pricing.yes_value
                    elif pricing.yes_pricing_type == 'discount_percent':
                        question_fixed -= pricing.yes_value
                    elif pricing.yes_pricing_type == 'fixed_price':
                        question_fixed += pricing.yes_value

            fixed_sum += question_fixed

        # Subtotal = base + sqft + surcharge + all fixed adjustments
        subtotal = base_price + sqft_price + surcharge_amount + fixed_sum
        percent_sum = Decimal('0.00')
        for sign, value in percent_entries:
            percent_sum += subtotal * (Decimal(sign) * value / Decimal('100'))

        return fixed_sum + percent_sum


    def _is_conditional_question_condition_met(self, question_response, service_selection):
        """Check if a conditional question's condition is met"""
        question = question_response.question
        
        if not question.parent_question:
            return True  # Not a conditional question
        
        # Find the parent question response
        parent_response = service_selection.question_responses.filter(
            question=question.parent_question
        ).first()
        
        if not parent_response:
            return False  # Parent not answered
        
        # Check condition based on parent question type
        if question.parent_question.question_type == 'yes_no':
            expected_answer = question.condition_answer
            actual_answer = 'yes' if parent_response.yes_no_answer else 'no'
            return expected_answer == actual_answer
        
        elif question.parent_question.question_type in ['describe', 'quantity']:
            if question.condition_option:
                # Check if the specific option was selected
                selected_options = parent_response.option_responses.all()
                selected_option_ids = [opt.option.id for opt in selected_options]
                return question.condition_option.id in selected_option_ids
        
        return False

    def _check_all_services_completed(self, submission):
        """Check if all selected services have responses"""
        service_selections = submission.customerserviceselection_set.all()
        
        for selection in service_selections:
            # Check if this service has any question responses
            if not selection.question_responses.exists():
                return False
            
            # Get all root questions (non-conditional) for this service
            root_questions = Question.objects.filter(
                service=selection.service,
                is_active=True,
                parent_question__isnull=True
            )
            
            # Check if all root questions have responses
            answered_question_ids = set(
                selection.question_responses.values_list('question_id', flat=True)
            )
            
            for root_question in root_questions:
                if root_question.id not in answered_question_ids:
                    return False
                
                # Check conditional questions if they should be answered
                conditional_questions = Question.objects.filter(
                    parent_question=root_question,
                    is_active=True
                )
                
                for conditional_question in conditional_questions:
                    # Check if condition is met
                    root_response = selection.question_responses.filter(
                        question=root_question
                    ).first()
                    
                    if self._should_conditional_question_be_answered(
                        conditional_question, root_response
                    ):
                        if conditional_question.id not in answered_question_ids:
                            return False
        
        return True

    def _should_conditional_question_be_answered(self, conditional_question, parent_response):
        """Check if a conditional question should be answered based on parent response"""
        if not parent_response:
            return False
        
        parent_question = conditional_question.parent_question
        
        # For yes/no parent questions
        if parent_question.question_type == 'yes_no':
            expected_answer = conditional_question.condition_answer
            actual_answer = 'yes' if parent_response.yes_no_answer else 'no'
            return expected_answer == actual_answer
        
        # For option-based parent questions
        elif parent_question.question_type in ['describe', 'quantity']:
            if conditional_question.condition_option:
                selected_options = parent_response.option_responses.all()
                selected_option_ids = [opt.option.id for opt in selected_options]
                return conditional_question.condition_option.id in selected_option_ids
        
        # For multiple_yes_no parent questions
        elif parent_question.question_type == 'multiple_yes_no':
            # This would depend on your specific business logic
            # For example, show conditional if any sub-question is answered yes
            sub_responses = parent_response.sub_question_responses.all()
            return any(sub.answer for sub in sub_responses)
        
        return False


class SubmitCustomServiceResponsesView(APIView):
    """Submit responses for a service including conditional questions (scoped to account)."""
    permission_classes = [AccountScopedPermission, AllowAny]

    def post(self, request, submission_id):
        account = getattr(request, 'account', None)
        submission = get_submission_for_account(submission_id, account)
        try:
            with transaction.atomic():
                # Update submission status
                submission.status = 'responses_completed'
                submission.save(update_fields=["status"])

                # Sync with GHL contact
                create_or_update_ghl_contact(submission)

            return Response(
                {"detail": "Responses submitted successfully."},
                status=status.HTTP_200_OK
            )

        except Exception as e:
            return Response(
                {"detail": f"An error occurred: {str(e)}"},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )


# Step 7: Get submission details with quotes
class SubmissionDetailView(generics.RetrieveUpdateAPIView):
    """Get detailed submission with all quotes.

    GET/HEAD/OPTIONS: resolve by submission id only; account comes from the submission
    (no location_id / request account required). Mutations still require account context.
    """
    queryset = CustomerSubmission.objects.all()
    serializer_class = CustomerSubmissionDetailSerializer
    permission_classes = [AccountScopedPermission, AllowAny]
    lookup_field = 'id'

    def get_permissions(self):
        if self.request.method in ('GET', 'HEAD', 'OPTIONS'):
            return [AllowAny()]
        return [AccountScopedPermission(), AllowAny()]

    def get_object(self):
        submission_id = self.kwargs['id']
        qs = CustomerSubmission.objects.prefetch_related(
            'customerserviceselection_set__service',
            'customerserviceselection_set__package_quotes__package',
            'customerserviceselection_set__question_responses__question',
            'customerserviceselection_set__question_responses__option_responses__option',
            'customerserviceselection_set__question_responses__sub_question_responses__sub_question',
            'images',
        )
        submission = get_object_or_404(qs, pk=submission_id)
        account = submission.account
        if account is not None:
            self.request.account = account
        return submission

# Update additional_data for submission
class UpdateSubmissionAdditionalDataView(APIView):
    """
    Update additional_data field for a customer submission.
    PATCH /api/quote/<submission_id>/additional-data/
    Body: {"additional_data": {...}}
    """
    permission_classes = [AccountScopedPermission, AllowAny]

    def patch(self, request, submission_id):
        account = getattr(request, 'account', None)
        submission = get_submission_for_account(submission_id, account)
        # Get additional_data from request
        additional_data = request.data.get('additional_data')
        if additional_data is None:
            return Response(
                {'detail': 'additional_data field is required.'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        # Validate that additional_data is a dict/JSON object
        if not isinstance(additional_data, dict):
            return Response(
                {'detail': 'additional_data must be a JSON object.'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        # Update additional_data
        # If submission already has additional_data, merge with existing data
        if submission.additional_data:
            # Merge existing data with new data (new data takes precedence)
            updated_data = {**submission.additional_data, **additional_data}
            submission.additional_data = updated_data
        else:
            submission.additional_data = additional_data
        
        submission.save(update_fields=['additional_data', 'updated_at'])
        
        # Return updated submission
        serializer = CustomerSubmissionDetailSerializer(submission, context={'request': request})
        return Response(serializer.data, status=status.HTTP_200_OK)

# Step 8: Submit final quote
class SubmitFinalQuoteView(APIView):
    """Submit the final quote (scoped to account)."""
    permission_classes = [AccountScopedPermission, AllowAny]

    def post(self, request, submission_id):
        account = getattr(request, 'account', None)
        submission = get_submission_for_account(submission_id, account)
        
        # Check if packages are already selected (from Step 8)
        print("submission status: ", submission.status)
        if submission.status == 'packages_selected':
            # Packages already selected, just need final confirmation
            serializer = SubmitFinalQuoteSerializer(data=request.data)
        elif submission.status in ['responses_completed','draft']:
            # Need to select packages first, then submit
            serializer = SubmitFinalQuoteSerializer(data=request.data)
            # if not request.data.get('selected_packages'):
            #     return Response({
            #         'error': 'Please select packages first or use the package selection endpoint'
            #     }, status=status.HTTP_400_BAD_REQUEST)
        else:
            return Response({
                'error': f'Invalid submission status: {submission.status}. Complete all steps first.'
            }, status=status.HTTP_400_BAD_REQUEST)
        
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
        
        try:
            with transaction.atomic():
                # If packages are provided in payload, update selections
                if serializer.validated_data.get('selected_packages',''):
                    self._update_package_selections(submission, serializer.validated_data['selected_packages'])
                
                # Update submission with additional information
                submission.status = 'submitted'
                
                # Store additional submission details
                additional_data = {
                    'additional_notes': serializer.validated_data.get('additional_notes', ''),
                    'preferred_contact_method': serializer.validated_data.get('preferred_contact_method', 'email'),
                    'preferred_start_date': (
                        serializer.validated_data.get('preferred_start_date').isoformat()
                        if serializer.validated_data.get('preferred_start_date') else None
                    ),                    
                    'marketing_consent': serializer.validated_data.get('marketing_consent', False),
                    'signature': serializer.validated_data.get('signature', ""),
                    'submitted_at': timezone.now().isoformat()
                }


                
                # You might want to store this in a separate field or model
                # For now, we'll add it to a JSON field if you have one
                submission.additional_data = additional_data
                submission.save()
                
                # Calculate final totals if not already done
                # if submission.final_total == Decimal('0.00'):
                if True:
                    self._calculate_final_totals(submission)
                    
                
                # Here you might want to:
                # 1. Send confirmation email to customer
                # 2. Notify admin/sales team
                # 3. Create order record
                # 4. Generate PDF quote
                create_or_update_ghl_contact(submission, is_submit=True)
                
                return Response({
                    'message': 'Quote submitted successfully',
                    'submission_id': submission.id,
                    'final_total': submission.final_total,
                    'quote_url': f'/quote/{submission.id}/',
                    'status': submission.status,
                    'submitted_at': timezone.now().isoformat()
                })
        
        except Exception as e:
            return Response({'error': str(e)}, status=status.HTTP_400_BAD_REQUEST)
    
    def _update_package_selections(self, submission, selected_packages):
        """Update package selections if provided in payload"""
        for package_data in selected_packages:
            service_selection = get_object_or_404(
                CustomerServiceSelection,
                id=package_data['service_selection_id'],
                submission=submission
            )
            
            package = get_object_or_404(Package, id=package_data['package_id'])
            
            # Update service selection
            service_selection.selected_package = package
            
            # Get the quote for this package
            quote = get_object_or_404(
                CustomerPackageQuote,
                service_selection=service_selection,
                package=package
            )
            
            service_selection.final_base_price = quote.base_price + quote.sqft_price
            service_selection.final_sqft_price = quote.sqft_price
            service_selection.final_total_price = quote.total_price
            service_selection.save()
            
            # Mark this quote as selected
            service_selection.package_quotes.update(is_selected=False)
            quote.is_selected = True
            quote.save()
        
        # Update submission status
        submission.status = 'packages_selected'
        submission.save()

    def _effective_trip_surcharge_for_submission(self, submission):
        """
        Trip fee used at submission level when selected package rows have no surcharge
        (e.g. apply_trip_charge_to_bid is false). Uses explicit submission.location when set;
        otherwise, if the account has exactly one active location with a trip charge, use that
        so totals match create-submission behavior when the client omits location FK.
        """
        if getattr(submission, 'location_id', None):
            loc = submission.location
            if loc and loc.trip_surcharge and loc.trip_surcharge > Decimal('0.00'):
                return Decimal(loc.trip_surcharge)
        account_id = getattr(submission, 'account_id', None)
        if not account_id:
            return Decimal('0.00')
        loc_qs = Location.objects.filter(
            account_id=account_id,
            is_active=True,
        ).exclude(trip_surcharge__lte=Decimal('0.00'))
        if loc_qs.count() == 1:
            return Decimal(loc_qs.first().trip_surcharge)
        return Decimal('0.00')

    def _calculate_final_totals(self, submission):
        """Calculate final totals for the submission"""
        service_selections = submission.customerserviceselection_set.filter(
            selected_package__isnull=False
        )
        
        total_base_price = Decimal('0.00')
        total_adjustments = Decimal('0.00')
        package_surcharge_total = Decimal('0.00')

        for selection in service_selections:
            selected_quote = selection.package_quotes.filter(is_selected=True).first()
            if selected_quote:
                total_base_price += selected_quote.base_price + selected_quote.sqft_price
                total_adjustments += selected_quote.question_adjustments
                package_surcharge_total += selected_quote.surcharge_amount

        # Package rows include trip only when apply_trip_charge_to_bid is true. Otherwise use one
        # submission-level trip (explicit location, or single trip-bearing location for the account).
        # Trip is not written onto per-service rows so one-service quotes match multi-service: line
        # totals are base + sqft + question adjustments only; total_surcharges + final_total on the
        # submission carry the trip once.
        if package_surcharge_total > Decimal('0.00'):
            total_surcharges = package_surcharge_total
        else:
            trip = self._effective_trip_surcharge_for_submission(submission)
            if trip > Decimal('0.00'):
                total_surcharges = trip
            else:
                total_surcharges = Decimal('0.00')

        submission.quote_surcharge_applicable = total_surcharges > Decimal('0.00')

        final_total = total_base_price + total_adjustments + total_surcharges + submission.custom_service_total
        
        submission.total_base_price = total_base_price
        submission.total_adjustments = total_adjustments
        submission.total_surcharges = total_surcharges

        # Trip lives on the submission only (not on each line). Sync selections and selected quotes
        # so they never carry a stale trip from older logic or mismatched saves.
        if package_surcharge_total == Decimal('0.00') and total_surcharges > Decimal('0.00'):
            for selection in service_selections:
                selected_quote = selection.package_quotes.filter(is_selected=True).first()
                if not selected_quote:
                    continue
                line_total = (
                    selected_quote.base_price
                    + selected_quote.sqft_price
                    + selected_quote.question_adjustments
                )
                selected_quote.surcharge_amount = Decimal('0.00')
                selected_quote.total_price = line_total
                selected_quote.save()
                selection.surcharge_amount = Decimal('0.00')
                selection.surcharge_applicable = False
                selection.final_total_price = line_total
                selection.save()

        global_settings = GlobalBasePrice.objects.first()
        if global_settings:
            base_price = global_settings.base_price
        else:
            base_price = 0  # fallback if not configured

        # apply minimum price rule
        if final_total < base_price:
            final_total = base_price

        # save submission
        submission.final_total = final_total
        submission.save()

# Reject quote view
class RejectQuoteView(APIView):
    """Reject a submitted quote (scoped to account)."""
    permission_classes = [AccountScopedPermission, AllowAny]

    def post(self, request, submission_id):
        account = getattr(request, 'account', None)
        submission = get_submission_for_account(submission_id, account)
        
        # Check if quote can be rejected (must be in submitted or accepted status)
        if submission.status in ['submitted', 'accepted']:
            return Response({
                'error': f'Quote cannot be rejected. Current status: {submission.status}. Only quotes with status "submitted" or "accepted" can be rejected.'
            }, status=status.HTTP_400_BAD_REQUEST)
        
        try:
            with transaction.atomic():
                # Collect rejection data
                rejected_at = timezone.now()
                rejection_reason = request.data.get('rejection_reason', '')
                rejection_notes = request.data.get('rejection_notes', '')
                rejected_by = request.data.get('rejected_by', '')
                
                # Update submission status to rejected
                submission.status = 'rejected'
                
                # Store rejection details in additional_data
                rejection_data = submission.additional_data or {}
                rejection_data['rejected_at'] = rejected_at.isoformat()
                rejection_data['rejection_reason'] = rejection_reason
                rejection_data['rejection_notes'] = rejection_notes
                rejection_data['rejected_by'] = rejected_by
                
                submission.additional_data = rejection_data
                submission.save()
                
                # Update GHL custom fields and add "quote rejected" tag
                try:
                    if submission.contact:
                        contact = submission.contact
                        location_id = contact.location_id
                        ghl_contact_id = contact.contact_id
                        
                        if location_id and ghl_contact_id:
                            # Get credentials for this location
                            credentials = GHLAuthCredentials.objects.filter(location_id=location_id).first()
                            
                            if credentials:
                                headers = {
                                    'Authorization': f'Bearer {credentials.access_token}',
                                    'Content-Type': 'application/json',
                                    'Version': '2021-07-28',
                                    'Accept': 'application/json'
                                }
                                
                                # Get contact from GHL to retrieve current tags
                                search_url = f'https://services.leadconnectorhq.com/contacts/{ghl_contact_id}'
                                search_response = requests.get(search_url, headers=headers)
                                
                                if search_response.status_code == 200:
                                    contact_data = search_response.json()
                                    current_tags = contact_data.get("tags", [])
                                    
                                    # Add "quote rejected" tag if not present (case-insensitive check)
                                    tag_exists = any(tag.lower() == "quote rejected" for tag in current_tags)
                                    
                                    if not tag_exists:
                                        current_tags.append("quote rejected")
                                    
                                    # Prepare custom fields payload
                                    custom_fields = []
                                    
                                    # Get Decline Date custom field
                                    try:
                                        decline_date_field = GHLCustomField.objects.get(
                                            account=credentials,
                                            field_name='Decline Date',
                                            is_active=True
                                        )
                                        decline_date_field.refresh_from_db()
                                        
                                        if decline_date_field.ghl_field_id and decline_date_field.ghl_field_id != 'ghl_field_id' and len(decline_date_field.ghl_field_id) >= 5:
                                            # Format date as string (YYYY-MM-DD or ISO format)
                                            decline_date_value = rejected_at.strftime('%Y-%m-%d')
                                            custom_fields.append({
                                                "id": str(decline_date_field.ghl_field_id),
                                                "field_value": decline_date_value
                                            })
                                            print(f"✅ [QUOTE REJECTED] Added Decline Date field: {decline_date_value}")
                                    except GHLCustomField.DoesNotExist:
                                        print(f"⚠️ [QUOTE REJECTED] 'Decline Date' custom field not found")
                                    except Exception as e:
                                        print(f"❌ [QUOTE REJECTED] Error getting Decline Date field: {str(e)}")
                                    
                                    # Get Decline Notes custom field
                                    try:
                                        decline_notes_field = GHLCustomField.objects.get(
                                            account=credentials,
                                            field_name='Decline Notes',
                                            is_active=True
                                        )
                                        decline_notes_field.refresh_from_db()
                                        
                                        if decline_notes_field.ghl_field_id and decline_notes_field.ghl_field_id != 'ghl_field_id' and len(decline_notes_field.ghl_field_id) >= 5:
                                            custom_fields.append({
                                                "id": str(decline_notes_field.ghl_field_id),
                                                "field_value": rejection_notes
                                            })
                                            print(f"✅ [QUOTE REJECTED] Added Decline Notes field: {rejection_notes}")
                                    except GHLCustomField.DoesNotExist:
                                        print(f"⚠️ [QUOTE REJECTED] 'Decline Notes' custom field not found")
                                    except Exception as e:
                                        print(f"❌ [QUOTE REJECTED] Error getting Decline Notes field: {str(e)}")
                                    
                                    # Get Decline Reason custom field
                                    try:
                                        decline_reason_field = GHLCustomField.objects.get(
                                            account=credentials,
                                            field_name='Decline Reason',
                                            is_active=True
                                        )
                                        decline_reason_field.refresh_from_db()
                                        
                                        if decline_reason_field.ghl_field_id and decline_reason_field.ghl_field_id != 'ghl_field_id' and len(decline_reason_field.ghl_field_id) >= 5:
                                            custom_fields.append({
                                                "id": str(decline_reason_field.ghl_field_id),
                                                "field_value": rejection_reason
                                            })
                                            print(f"✅ [QUOTE REJECTED] Added Decline Reason field: {rejection_reason}")
                                    except GHLCustomField.DoesNotExist:
                                        print(f"⚠️ [QUOTE REJECTED] 'Decline Reason' custom field not found")
                                    except Exception as e:
                                        print(f"❌ [QUOTE REJECTED] Error getting Decline Reason field: {str(e)}")
                                    
                                    # Update contact with tags and custom fields
                                    update_url = f'https://services.leadconnectorhq.com/contacts/{ghl_contact_id}'
                                    update_payload = {}
                                    
                                    if not tag_exists:
                                        update_payload["tags"] = current_tags
                                    
                                    if custom_fields:
                                        update_payload["customFields"] = custom_fields
                                    
                                    if update_payload:
                                        update_response = requests.put(update_url, headers=headers, json=update_payload)
                                        
                                        if update_response.status_code in [200, 201]:
                                            if not tag_exists:
                                                print(f"✅ [QUOTE REJECTED] Successfully added 'quote rejected' tag to contact {ghl_contact_id}")
                                            if custom_fields:
                                                print(f"✅ [QUOTE REJECTED] Successfully updated GHL custom fields for contact {ghl_contact_id}")
                                        else:
                                            print(f"❌ [QUOTE REJECTED] Failed to update contact: {update_response.status_code} - {update_response.text}")
                                    else:
                                        if tag_exists:
                                            print(f"ℹ️ [QUOTE REJECTED] Contact {ghl_contact_id} already has 'quote rejected' tag and no custom fields to update")
                                else:
                                    print(f"⚠️ [QUOTE REJECTED] Failed to fetch contact from GHL: {search_response.status_code} - {search_response.text}")
                            else:
                                print(f"⚠️ [QUOTE REJECTED] No credentials found for location_id: {location_id}")
                        else:
                            print(f"⚠️ [QUOTE REJECTED] Missing location_id or ghl_contact_id for contact")
                    else:
                        print(f"⚠️ [QUOTE REJECTED] Submission has no contact linked")
                except Exception as e:
                    print(f"❌ [QUOTE REJECTED] Error updating GHL contact: {str(e)}")
                    import traceback
                    traceback.print_exc()
                    # Don't fail the request if GHL update fails
                
                return Response({
                    'message': 'Quote rejected successfully',
                    'submission_id': submission.id,
                    'status': submission.status,
                    'rejected_at': timezone.now().isoformat()
                }, status=status.HTTP_200_OK)
        
        except Exception as e:
            return Response({'error': str(e)}, status=status.HTTP_400_BAD_REQUEST)

# Utility views
class SubmissionStatusView(APIView):
    """Check submission status (scoped to account)."""
    permission_classes = [AccountScopedPermission, AllowAny]

    def get(self, request, submission_id):
        account = getattr(request, 'account', None)
        submission = get_submission_for_account(submission_id, account)
        
        # Check if expired
        if submission.expires_at and submission.expires_at < timezone.now():
            submission.status = 'expired'
            submission.save()
        
        return Response({
            'id': submission.id,
            'status': submission.status,
            'expires_at': submission.expires_at,
            'created_at': submission.created_at
        })

class ServicePackagesView(APIView):
    """Get packages for a specific service (scoped to account)."""
    permission_classes = [AccountScopedPermission, AllowAny]

    def get(self, request, service_id):
        account = getattr(request, 'account', None)
        service = get_service_for_account(service_id, account)
        if not service.is_active:
            raise NotFound("Service not found.")
        packages = Package.objects.filter(service=service, is_active=True).order_by('order')
        
        return Response({
            'service': ServicePublicSerializer(service).data,
            'packages': PackagePublicSerializer(packages, many=True).data
        })



class CustomServiceViewSet(AccountScopedQuerysetMixin, viewsets.ModelViewSet):
    """CRUD for CustomService (scoped to account via purchase.submission)."""
    queryset = CustomService.objects.all()
    serializer_class = CustomServiceSerializer
    permission_classes = [AccountScopedPermission, AllowAny]
    account_lookup = "purchase__account"

    def get_queryset(self):
        queryset = super().get_queryset()
        purchase_id = self.request.query_params.get("purchase")
        if purchase_id:
            queryset = queryset.filter(purchase_id=purchase_id)
        return queryset
    

from quote_app.serializers import ServiceListSerializer,CustomServiceSerializer

class ServiceAndCustomServiceListView(APIView):
    """List services and custom services (scoped to account)."""
    permission_classes = [AccountScopedPermission, AllowAny]

    def get(self, request, *args, **kwargs):
        account = getattr(request, 'account', None)
        submission_id = request.query_params.get("submission_id")
        services = Service.objects.filter(is_active=True, account=account).order_by("order")
        services_data = ServiceListSerializer(services, many=True).data
        custom_services_data = []
        if submission_id:
            custom_services = CustomService.objects.filter(
                purchase_id=submission_id,
                purchase__account=account
            )
            custom_services_data = CustomServiceSerializer(custom_services, many=True).data
        return Response({
            "services": services_data,
            "custom_services": custom_services_data
        })
    


class QuoteScheduleUpdateView(generics.UpdateAPIView):
    """Update a QuoteSchedule record by submission ID (scoped to account)."""
    serializer_class = QuoteScheduleUpdateSerializer
    permission_classes = [AccountScopedPermission, AllowAny]

    def get_object(self):
        submission_id = self.kwargs.get('submission_id')
        account = getattr(self.request, 'account', None)
        submission = get_submission_for_account(submission_id, account)
        obj = get_object_or_404(QuoteSchedule, submission=submission)
        return obj

    def update(self, request, *args, **kwargs):
        partial = kwargs.pop('partial', False)
        instance = self.get_object()
        serializer = self.get_serializer(instance, data=request.data, partial=partial)
        serializer.is_valid(raise_exception=True)
        self.perform_update(serializer)

        return Response(serializer.data, status=status.HTTP_200_OK)
    


from django.utils.decorators import method_decorator
from django.views.decorators.csrf import csrf_exempt
@method_decorator(csrf_exempt, name='dispatch')
class ScheduleCalendarAppointmentView(APIView):
    """
    Webhook to update QuoteSchedule from calendar booking payload.
    """
    permission_classes = [AllowAny]

    def post(self, request, *args, **kwargs):
        try:
            data = request.data

            # Extract submission_id from quotelink
            print("calendar appointment data:", data)
            appointment_id = data.get("calendar", {}).get("appointmentId")
            quotelink = data.get("customData", {}).get("quotelink")
            if not quotelink:
                return JsonResponse({"error": "quotelink missing"}, status=400)

            # submission_id is the last UUID in the link
            match = re.search(r"/quote/details/([a-f0-9\-]+)/?", quotelink)
            if not match:
                return JsonResponse({"error": "Invalid quotelink format"}, status=400)
            submission_id = match.group(1)

            # Extract start time
            start_time = data.get("calendar", {}).get("startTime")
            if not start_time:
                return JsonResponse({"error": "startTime missing"}, status=400)

            scheduled_date = parse_datetime(start_time)
            if not scheduled_date:
                return JsonResponse({"error": "Invalid datetime format"}, status=400)

            # Find QuoteSchedule
            submission = get_object_or_404(CustomerSubmission, id=submission_id)
            quote_schedule = get_object_or_404(QuoteSchedule, submission=submission)

            # Update scheduled date
            quote_schedule.scheduled_date = scheduled_date
            quote_schedule.appointment_id = appointment_id
            quote_schedule.is_submitted = True
            quote_schedule.save(update_fields=["scheduled_date", "appointment_id", "is_submitted"])

            return JsonResponse({
                "status": "success",
                "message": f"QuoteSchedule updated for submission {submission_id}",
                "scheduled_date": scheduled_date.isoformat()
            }, status=200)

        except Exception as e:
            return JsonResponse({"error": str(e)}, status=500)
        

@method_decorator(csrf_exempt, name='dispatch')
class BookQuoteScheduleView(APIView):
    """
    Book a quote schedule directly from custom calendar UI.
    """
    permission_classes = [AllowAny]

    def post(self, request, submission_id, *args, **kwargs):
        try:
            data = request.data or {}

            start_time = data.get("scheduled_date") or data.get("start_time")
            if not start_time:
                return JsonResponse(
                    {"error": "scheduled_date or start_time is required"},
                    status=400
                )

            scheduled_date = parse_datetime(start_time)
            if not scheduled_date:
                return JsonResponse({"error": "Invalid datetime format"}, status=400)

            duration = data.get("duration")
            if duration in [None, ""]:
                return JsonResponse({"error": "duration is required"}, status=400)

            try:
                duration = int(duration)
            except (TypeError, ValueError):
                return JsonResponse({"error": "duration must be an integer"}, status=400)

            if duration <= 0:
                return JsonResponse({"error": "duration must be greater than 0"}, status=400)

            appointment_id = data.get("appointment_id") or f"local_{uuid.uuid4()}"

            submission = get_object_or_404(CustomerSubmission, id=submission_id)
            quote_schedule, _ = QuoteSchedule.objects.get_or_create(
                submission=submission,
                defaults={
                    "quoted_by": (
                        getattr(submission.quoted_by, "email", None)
                        or getattr(submission.quoted_by, "username", None)
                        or ""
                    )
                }
            )

            quote_schedule.scheduled_date = scheduled_date
            quote_schedule.appointment_id = str(appointment_id)
            quote_schedule.is_submitted = True
            quote_schedule.save(update_fields=["scheduled_date", "appointment_id", "is_submitted"])

            additional_data = submission.additional_data or {}
            additional_data.update({
                "booking_duration_minutes": duration,
                "booking_source": "custom_calendar",
                "booking_submitted_at": timezone.now().isoformat(),
            })
            submission.additional_data = additional_data
            submission.save(update_fields=["additional_data", "updated_at"])

            return JsonResponse({
                "status": "success",
                "message": f"QuoteSchedule updated for submission {submission_id}",
                "scheduled_date": scheduled_date.isoformat(),
                "duration": duration,
                "appointment_id": str(appointment_id),
            }, status=200)

        except Exception as e:
            return JsonResponse({"error": str(e)}, status=500)


class CalendarFreeSlotsView(APIView):
    """
    Proxy endpoint to fetch free slots from GHL calendar API.
    """
    permission_classes = [AccountScopedPermission, AllowAny]

    def get(self, request, *args, **kwargs):
        try:
            account = getattr(request, "account", None)
            if not account:
                return Response(
                    {"error": "Account could not be determined."},
                    status=status.HTTP_400_BAD_REQUEST
                )

            calendar_id = request.query_params.get("calendarId")
            if not calendar_id:
                cal = (
                    Calendar.objects.filter(account=account, name=RECURRING_SERVICE_CALENDAR_NAME)
                    .values_list("ghl_calendar_id", flat=True)
                    .first()
                )
                calendar_id = cal or ""
            if not calendar_id:
                return Response(
                    {
                        "error": (
                            "calendarId is required, or sync calendars from GHL and ensure "
                            f"'{RECURRING_SERVICE_CALENDAR_NAME}' exists for this account."
                        )
                    },
                    status=status.HTTP_400_BAD_REQUEST,
                )

            start_date = request.query_params.get("startDate")
            end_date = request.query_params.get("endDate")
            if not start_date or not end_date:
                return Response(
                    {"error": "startDate and endDate are required query params."},
                    status=status.HTTP_400_BAD_REQUEST
                )

            version = request.query_params.get("version", "2021-07-28")
            timezone_param = request.query_params.get("timezone")
            user_id = request.query_params.get("userId")

            ghl_url = f"https://services.leadconnectorhq.com/calendars/{calendar_id}/free-slots"
            query_params = {
                "startDate": start_date,
                "endDate": end_date,
            }
            if timezone_param:
                query_params["timezone"] = timezone_param
            if user_id:
                query_params["userId"] = user_id

            headers = {
                "Authorization": f"Bearer {account.access_token}",
                "Version": version,
                "Accept": "application/json",
            }

            ghl_response = requests.get(ghl_url, headers=headers, params=query_params, timeout=30)
            content_type = ghl_response.headers.get("Content-Type", "")
            response_data = ghl_response.json() if "application/json" in content_type else {
                "error": "Non-JSON response from GHL",
                "raw_response": ghl_response.text[:1000],
            }

            if ghl_response.status_code >= 400:
                return Response(
                    {
                        "error": "Failed to fetch free slots from GHL",
                        "ghl_status_code": ghl_response.status_code,
                        "details": response_data,
                    },
                    status=ghl_response.status_code
                )

            # Return GHL payload as-is so frontend can consume it directly.
            return Response(response_data, status=status.HTTP_200_OK)

        except requests.RequestException as e:
            return Response(
                {"error": f"Network error while fetching free slots: {str(e)}"},
                status=status.HTTP_502_BAD_GATEWAY
            )
        except ValueError:
            return Response(
                {"error": "Invalid JSON received from GHL free slots API."},
                status=status.HTTP_502_BAD_GATEWAY
            )
        except Exception as e:
            return Response({"error": str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)




from django.db import models

class RemoveServiceFromSubmissionView(APIView):
    """Remove a selected service from a submission (scoped to account)."""
    permission_classes = [AccountScopedPermission, AllowAny]

    def delete(self, request, submission_id, service_id):
        account = getattr(request, 'account', None)
        submission = get_submission_for_account(submission_id, account)

        try:
            with transaction.atomic():
                # Find the service selection to remove
                service_selection = get_object_or_404(
                    CustomerServiceSelection,
                    submission=submission,
                    service_id=service_id
                )

                # Delete it (will cascade delete responses, quotes, etc.)
                service_selection.delete()

                # Optional: Recalculate submission totals
                submission.total_adjustments = submission.customerserviceselection_set.aggregate(
                    total=models.Sum('question_adjustments')
                )['total'] or Decimal('0.00')

                submission.total_surcharges = submission.customerserviceselection_set.aggregate(
                    total=models.Sum('surcharge_amount')
                )['total'] or Decimal('0.00')

                submission.final_total = (
                    submission.total_base_price +
                    submission.total_adjustments +
                    submission.total_surcharges +
                    (submission.custom_service_total or Decimal('0.00'))
                )
                submission.save()

                return Response({
                    "message": f"Service {service_id} removed from submission {submission_id}",
                    "submission_id": str(submission.id),
                    "remaining_services": submission.customerserviceselection_set.count(),
                    "final_total": str(submission.final_total)
                }, status=status.HTTP_200_OK)

        except Exception as e:
            return Response(
                {"error": str(e)},
                status=status.HTTP_400_BAD_REQUEST
            )
        


class GlobalSettingsView(APIView):
    """
    GET → Retrieve global base price for current account
    PUT → Update global base price for current account
    """
    permission_classes = [AccountScopedPermission, AllowAny]

    def get(self, request):
        account = getattr(request, 'account', None)
        settings, _ = GlobalBasePrice.objects.get_or_create(
            account=account,
            defaults={'base_price': 0}
        )
        serializer = GlobalBasePriceSerializer(settings)
        return Response(serializer.data)

    def put(self, request):
        account = getattr(request, 'account', None)
        settings, _ = GlobalBasePrice.objects.get_or_create(
            account=account,
            defaults={'base_price': 0}
        )
        serializer = GlobalBasePriceSerializer(settings, data=request.data, partial=True)
        if serializer.is_valid():
            serializer.save()
            return Response(serializer.data)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


class CustomerSubmissionImageViewSet(AccountScopedQuerysetMixin, viewsets.ModelViewSet):
    """ViewSet for managing customer submission images (scoped to account)."""
    queryset = CustomerSubmissionImage.objects.all()
    serializer_class = CustomerSubmissionImageSerializer
    permission_classes = [AccountScopedPermission, permissions.AllowAny]
    account_lookup = "submission__account"

    def get_queryset(self):
        qs = super().get_queryset().select_related('submission', 'uploaded_by')
        submission_id = self.request.query_params.get('submission_id')
        if submission_id:
            try:
                submission_uuid = uuid.UUID(submission_id)
                qs = qs.filter(submission_id=submission_uuid)
            except (ValueError, TypeError):
                return CustomerSubmissionImage.objects.none()
        return qs.order_by('-created_at')

    def get_serializer_context(self):
        """Add request to serializer context for building absolute URLs"""
        context = super().get_serializer_context()
        context['request'] = self.request
        return context

    def perform_create(self, serializer):
        """Validate submission, upload image to GHL only (not S3), then save record with ghl_file_id/ghl_file_url."""
        submission_id = self.request.data.get('submission')
        if not submission_id:
            raise ValidationError({'submission': 'submission field is required'})

        uploaded_file = self.request.FILES.get('image')
        if not uploaded_file:
            raise ValidationError({'image': 'image file is required'})

        account = getattr(self.request, 'account', None)
        submission = get_submission_for_account(submission_id, account)
        user = self.request.user if self.request.user.is_authenticated else None
        location_id = None
        if submission.contact:
            location_id = getattr(submission.contact, 'location_id', None)
        if not location_id:
            creds = GHLAuthCredentials.objects.first()
            location_id = creds.location_id if creds else None
        if not location_id:
            raise ValidationError({'detail': 'No GHL location available for media upload.'})

        credentials, media_storage = get_ghl_media_storage_for_location(location_id, storage_name='Submission Images')
        if not media_storage:
            credentials, media_storage = get_ghl_media_storage_for_location(location_id)
        if not credentials or not media_storage:
            raise ValidationError({'detail': 'GHL media storage not configured for this location.'})

        # Allowed types: PNG, JPG, JPEG, GIF, WEBP (matches UI and GHL support)
        allowed_extensions = {'png', 'jpg', 'jpeg', 'gif', 'webp'}
        original_name = getattr(uploaded_file, 'name', '') or ''
        ext = original_name.lower().split('.')[-1] if '.' in original_name else ''
        if ext not in allowed_extensions:
            raise ValidationError({
                'image': f'Only PNG, JPG, GIF, and WEBP images are supported. Got: {ext or "unknown"}.'
            })
        # Sanitize filename: strip path, limit length, keep safe chars
        name = self.request.data.get('caption') or original_name or 'submission-image'
        if isinstance(name, str):
            name = name.split('/')[-1].split('\\')[-1].strip()
            name = re.sub(r'[^\w\s\-\.]', '_', name)[:200] or 'submission-image'
        if not name.lower().endswith(f'.{ext}'):
            name = f"{name.rsplit('.', 1)[0] if '.' in name else name}.{ext}"
        # Compress large images for faster upload; otherwise pass file directly (no BytesIO copy)
        uploaded_file.seek(0)
        file_to_upload, content_type, upload_filename = compress_image_for_upload(uploaded_file, name)
        if file_to_upload is not None:
            upload_name = upload_filename
            upload_ct = content_type
        else:
            uploaded_file.seek(0)
            file_to_upload = uploaded_file
            upload_name = name
            upload_ct = getattr(uploaded_file, 'content_type', None)
        result, error_message = upload_file_to_ghl_media(
            credentials.access_token,
            location_id,
            media_storage.ghl_id,
            upload_name,
            file_to_upload,
            file_content_type=upload_ct,
            filename_override=upload_name,
        )
        if not result:
            raise ValidationError({
                'detail': error_message or 'Failed to upload image to GHL media.'
            })

        serializer.save(
            submission=submission,
            uploaded_by=user,
            image=None,
            ghl_file_id=result.get('fileId'),
            ghl_file_url=result.get('url'),
        )

    def perform_destroy(self, instance):
        """Delete from GHL media if we have ghl_file_id, then delete local record."""
        if instance.ghl_file_id:
            location_id = None
            if instance.submission.contact:
                location_id = getattr(instance.submission.contact, 'location_id', None)
            if not location_id:
                creds = GHLAuthCredentials.objects.first()
                location_id = creds.location_id if creds else None
            if location_id:
                try:
                    creds = GHLAuthCredentials.objects.filter(location_id=location_id).first()
                    if creds:
                        delete_ghl_media(creds.access_token, instance.ghl_file_id, location_id)
                except Exception:
                    pass
        instance.delete()


def _apply_reschedule_pending_job_prefetch(queryset):
    """Prefetch for JobSerializer on reschedule-pending job lists and detail."""
    return queryset.select_related(
        'submission',
        'submission__quote_schedule',
        'submission__contact',
        'submission__address',
        'submission__quoted_by',
        'contact',
        'address',
        'quoted_by',
        'account',
    ).prefetch_related(
        'items__service',
        'assignments__user',
        'images',
        'schedule_occurrences',
    )


class JobRescheduleQuoteCreateView(APIView):
    """Clone the job's quote: new accepted submission + new job with status reschedule_pending."""

    permission_classes = [AccountScopedPermission, AllowAny]

    def post(self, request, job_id, *args, **kwargs):
        account = getattr(request, 'account', None)
        job = get_job_for_account(job_id, account)
        if job.status != 'completed':
            return Response(
                {'error': 'Only completed jobs can request a repeat job.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        if not job.submission_id:
            return Response(
                {'error': 'This job has no linked quote submission to copy.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        serializer = JobRescheduleQuoteCreateSerializer(data=request.data or {})
        serializer.is_valid(raise_exception=True)

        raw_dt = serializer.validated_data['scheduled_date']
        scheduled_date = parse_datetime(raw_dt)
        if not scheduled_date:
            return Response(
                {'error': 'Invalid scheduled_date. Use ISO 8601 format.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        notes = serializer.validated_data.get('notes') or ''
        quoted_by_str = serializer.validated_data.get('quoted_by') or ''
        source = job.submission
        if not quoted_by_str and source and source.quoted_by:
            qb = source.quoted_by
            quoted_by_str = getattr(qb, 'email', None) or getattr(qb, 'username', None) or ''

        _new_sub, new_job = clone_submission_for_reschedule(
            source,
            scheduled_at=scheduled_date,
            job=job,
            notes=notes,
            quoted_by_str=quoted_by_str,
        )
        detail = _apply_reschedule_pending_job_prefetch(Job.objects.filter(pk=new_job.pk)).first()
        data = JobSerializer(detail, context={'request': request}).data
        return Response(data, status=status.HTTP_201_CREATED)


class ReschedulePendingJobListView(AccountScopedQuerysetMixin, ListAPIView):
    """List jobs awaiting reschedule confirmation (Job.status = reschedule_pending)."""

    queryset = Job.objects.all()
    serializer_class = JobSerializer
    permission_classes = [AccountScopedPermission, AllowAny]
    pagination_class = ContactPagination
    account_lookup = 'account'

    def get_queryset(self):
        qs = super().get_queryset().filter(status='reschedule_pending')
        return _apply_reschedule_pending_job_prefetch(qs).order_by('-created_at')


class RescheduleConvertToJobView(APIView):
    """Submit the QuoteSchedule for a reschedule job (same signal path as booking → to_convert)."""

    permission_classes = [AccountScopedPermission, AllowAny]

    def post(self, request, job_id, *args, **kwargs):
        account = getattr(request, 'account', None)
        job = get_job_for_account(job_id, account)

        if job.status != 'reschedule_pending':
            return Response(
                {'error': 'Only reschedule_pending jobs can be converted.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        submission = job.submission
        if not submission:
            return Response(
                {'error': 'Job has no linked submission.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        serializer = RescheduleConvertToJobSerializer(data=request.data or {})
        serializer.is_valid(raise_exception=True)

        quote_schedule = get_object_or_404(QuoteSchedule, submission=submission)

        scheduled_override = serializer.validated_data.get('scheduled_date')
        override_clean = (scheduled_override or '').strip() if scheduled_override else ''
        if override_clean:
            dt = parse_datetime(override_clean)
            if not dt:
                return Response(
                    {'error': 'Invalid scheduled_date.'},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            quote_schedule.scheduled_date = dt

        quote_schedule.is_submitted = True
        update_fields = ['is_submitted']
        if override_clean:
            update_fields.append('scheduled_date')
        quote_schedule.save(update_fields=update_fields)

        job.refresh_from_db()
        detail = _apply_reschedule_pending_job_prefetch(Job.objects.filter(pk=job.pk)).first()
        return Response(
            {
                'detail': 'Reschedule confirmed; job moved to needs conversion like an accepted quote.',
                'job_id': str(job.id),
                'job_status': job.status,
                'job': JobSerializer(detail, context={'request': request}).data,
            },
            status=status.HTTP_200_OK,
        )
