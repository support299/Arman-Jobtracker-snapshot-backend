# views.py
from rest_framework import generics, viewsets, status, permissions
from rest_framework.decorators import action, api_view, permission_classes
from rest_framework.response import Response
from rest_framework.authtoken.models import Token
from rest_framework.views import APIView
from rest_framework.exceptions import ValidationError
from django.contrib.auth import authenticate
from django.db.models import Count, Avg, Prefetch
from django.db import transaction, models
from django.shortcuts import get_object_or_404
from django.utils import timezone
from decimal import Decimal
import requests
import os
from urllib.parse import quote, urlparse

from django.conf import settings as django_settings
from .models import Service, ServiceSettings
from .serializers import ServiceSettingsSerializer
from accounts.permissions import AccountScopedPermission
from accounts.mixins import AccountScopedQuerysetMixin
from accounts.utils import try_link_user_ghl_id_from_email
from .account_scope_utils import get_service_for_account, get_location_for_account

from rest_framework.permissions import IsAuthenticated

from .models import (
    User, Location, Service, Package, Feature, PackageFeature,
    Question, QuestionOption, QuestionPricing, OptionPricing,
    Order, OrderQuestionAnswer,SubQuestionPricing,SubQuestion,QuestionResponse, GlobalBasePrice
)
from jobtracker_app.models import JobAssignment
from .serializers import (
    UserSerializer, LoginSerializer, LocationSerializer, ServiceSerializer,
    ServiceListSerializer, ServiceBasicSerializer, PackageSerializer, FeatureSerializer,
    PackageFeatureSerializer, QuestionSerializer, QuestionCreateSerializer,
    QuestionOptionSerializer, QuestionPricingSerializer, OptionPricingSerializer,
    PackageWithFeaturesSerializer, BulkPricingUpdateSerializer,
    ServiceAnalyticsSerializer, SubQuestionPricingSerializer,BulkSubQuestionPricingSerializer,QuestionResponseSerializer,
    PricingCalculationSerializer, SubQuestionSerializer,GlobalBasePriceSerializer,
    BulkQuestionOrderSerializer, BulkQuestionOrderItemSerializer
)   



from rest_framework_simplejwt.views import TokenObtainPairView, TokenRefreshView
from rest_framework_simplejwt.tokens import RefreshToken
from rest_framework.permissions import IsAdminUser, AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView


class IsAdminPermission(permissions.BasePermission):
    """Custom permission to only allow admins to access views"""
    
    def has_permission(self, request, view):
        return request.user and request.user.is_authenticated and request.user.is_admin
    


class AdminTokenObtainPairView(TokenObtainPairView):
    permission_classes = [AllowAny]
    
    def post(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        try:
            serializer.is_valid(raise_exception=True)
        except Exception as e:
            return Response({'detail': str(e)}, status=status.HTTP_400_BAD_REQUEST)
        
        # Get the user from validated data
        user = serializer.user
        
        # Get tokens
        refresh = RefreshToken.for_user(user)
        access_token = refresh.access_token
        
        # Serialize user data
        user_data = UserSerializer(user).data
        
        # Check if user has employee profile
        employee_profile = None
        try:
            from payroll_app.models import EmployeeProfile
            from payroll_app.serializers import EmployeeProfileSerializer
            profile = user.employee_profile
            employee_profile = EmployeeProfileSerializer(profile).data
        except (AttributeError, ImportError):
            pass
        except Exception:
            # Handle DoesNotExist or other exceptions
            pass
        
        # Build response with tokens and user data
        response_data = {
            'access': str(access_token),
            'refresh': str(refresh),
            'user': user_data
        }
        
        if employee_profile:
            response_data['employee_profile'] = employee_profile
        
        return Response(response_data, status=status.HTTP_200_OK)

class AdminTokenRefreshView(TokenRefreshView):
    permission_classes = [AllowAny]

# Public user JWT login/refresh (no admin restriction)
class UserTokenObtainPairView(TokenObtainPairView):
    permission_classes = [AllowAny]
    
    def post(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        try:
            serializer.is_valid(raise_exception=True)
        except Exception as e:
            return Response({'detail': str(e)}, status=status.HTTP_400_BAD_REQUEST)
        
        # Get the user from validated data
        user = serializer.user
        
        # Get tokens
        refresh = RefreshToken.for_user(user)
        access_token = refresh.access_token
        
        # Serialize user data
        user_data = UserSerializer(user).data
        
        # Check if user has employee profile
        employee_profile = None
        try:
            from payroll_app.models import EmployeeProfile
            from payroll_app.serializers import EmployeeProfileSerializer
            profile = user.employee_profile
            employee_profile = EmployeeProfileSerializer(profile).data
        except (AttributeError, ImportError):
            pass
        except Exception:
            # Handle DoesNotExist or other exceptions
            pass
        
        # Build response with tokens and user data
        response_data = {
            'access': str(access_token),
            'refresh': str(refresh),
            'user': user_data
        }
        
        if employee_profile:
            response_data['employee_profile'] = employee_profile
        
        return Response(response_data, status=status.HTTP_200_OK)

class UserTokenRefreshView(TokenRefreshView):
    permission_classes = [AllowAny]

class AdminLogoutView(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        try:
            refresh_token = request.data["refresh"]
            token = RefreshToken(refresh_token)
            token.blacklist()  # Requires Blacklist app enabled
            return Response({"detail": "Successfully logged out."})
        except Exception as e:
            return Response({"detail": "Invalid token or already logged out."}, status=400)



# Authentication Views
class AdminLoginView(APIView):
    """Admin login view"""
    permission_classes = []

    def post(self, request):
        print("request: ", request.data)
        serializer = LoginSerializer(data=request.data)
        if serializer.is_valid():
            user = serializer.validated_data['user']
            token, created = Token.objects.get_or_create(user=user)
            return Response({
                'token': token.key,
                'user': UserSerializer(user).data,
                'message': 'Login successful'
            })
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


# class AdminLogoutView(APIView):
#     """Admin logout view"""
#     permission_classes = [IsAuthenticated]

#     def post(self, request):
#         try:
#             request.user.auth_token.delete()
#             return Response({'message': 'Logout successful'})
#         except:
#             return Response({'message': 'Logout successful'})


# Location Views
class LocationListCreateView(AccountScopedQuerysetMixin, generics.ListCreateAPIView):
    """List all locations and create new ones (scoped to current account)."""
    queryset = Location.objects.filter(is_active=True)
    serializer_class = LocationSerializer
    permission_classes = [AccountScopedPermission, IsAdminPermission]
    account_lookup = "account"

    def get_queryset(self):
        queryset = super().get_queryset()
        search = self.request.query_params.get('search', None)
        if search:
            queryset = queryset.filter(
                models.Q(name__icontains=search) |
                models.Q(address__icontains=search)
            )
        return queryset.order_by('name')

    def perform_create(self, serializer):
        serializer.save(account=getattr(self.request, 'account', None))


class LocationDetailView(AccountScopedQuerysetMixin, generics.RetrieveUpdateDestroyAPIView):
    """Retrieve, update or delete a location (scoped to current account)."""
    queryset = Location.objects.all()
    serializer_class = LocationSerializer
    permission_classes = [AccountScopedPermission, IsAdminPermission]
    account_lookup = "account"

    def perform_destroy(self, instance):
        # Soft delete
        instance.is_active = False
        instance.save()


class LocationManagementViewSet(AccountScopedQuerysetMixin, viewsets.ModelViewSet):
    """
    Account-scoped location CRUD for the location management tool.
    Only users with is_admin=True may access (same as LocationListCreateView / LocationDetailView).

    POST .../onboard/ returns the GHL chooselocation OAuth URL; redirect_uri is built from the
    request frontend origin (Origin or Referer) plus settings.GHL_LOCATION_CONNECT_REDIRECT_PATH.
    """
    serializer_class = LocationSerializer
    permission_classes = [AccountScopedPermission, IsAdminPermission]
    account_lookup = "account"
    queryset = Location.objects.all()

    @staticmethod
    def _frontend_base_url(request):
        origin = (request.headers.get("Origin") or "").strip()
        if origin:
            return origin.rstrip("/")
        referer = request.headers.get("Referer") or ""
        if referer:
            parsed = urlparse(referer)
            if parsed.scheme and parsed.netloc:
                return f"{parsed.scheme}://{parsed.netloc}".rstrip("/")
        return ""

    @action(detail=False, methods=["post"], url_path="onboard")
    def onboard(self, request):
        base = self._frontend_base_url(request)
        if not base:
            return Response(
                {
                    "detail": (
                        "Could not determine frontend URL. Send an Origin header, "
                        "or a Referer header, when requesting the connect URL."
                    )
                },
                status=status.HTTP_400_BAD_REQUEST,
            )
        path = getattr(django_settings, "GHL_LOCATION_CONNECT_REDIRECT_PATH", "") or "/"
        if not path.startswith("/"):
            path = "/" + path
        redirect_uri = f"{base}{path}"

        client_id = getattr(django_settings, "GHL_CLIENT_ID", "") or ""
        scope = getattr(django_settings, "GHL_OAUTH_SCOPE", "") or ""
        if not client_id or not scope:
            return Response(
                {
                    "detail": (
                        "GHL OAuth is not configured: set GHL_CLIENT_ID and SCOPE in the environment."
                    )
                },
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )

        q_redirect = quote(redirect_uri, safe="")
        q_scope = quote(scope, safe="")
        auth_url = (
            "https://marketplace.gohighlevel.com/oauth/chooselocation?response_type=code&"
            f"redirect_uri={q_redirect}&"
            f"client_id={quote(client_id, safe='')}&"
            f"scope={q_scope}"
        )
        return Response({"auth_url": auth_url})

    def get_queryset(self):
        queryset = super().get_queryset()
        if self.action == "list":
            queryset = queryset.filter(is_active=True)
            search = self.request.query_params.get("search", None)
            if search:
                queryset = queryset.filter(
                    models.Q(name__icontains=search)
                    | models.Q(address__icontains=search)
                )
        return queryset.order_by("name")

    def perform_destroy(self, instance):
        instance.is_active = False
        instance.save()

    def perform_create(self, serializer):
        serializer.save(account=getattr(self.request, "account", None))


# Service Views
class ServiceListCreateView(AccountScopedQuerysetMixin, generics.ListCreateAPIView):
    """List all services and create new ones (scoped to current account)."""
    queryset = Service.objects.all()
    permission_classes = [AccountScopedPermission, IsAuthenticated]
    account_lookup = "account"

    def get_queryset(self):
        queryset = super().get_queryset().prefetch_related(
            'questions__options',
            'questions__sub_questions',
            'questions__child_questions'
        )
        search = self.request.query_params.get('search', None)
        if search:
            queryset = queryset.filter(name__icontains=search)
        return queryset.order_by('order', 'name')

    def get_serializer_class(self):
        if self.request.method == 'GET':
            return ServiceListSerializer
        return ServiceSerializer

    def perform_create(self, serializer):
        serializer.save(account=getattr(self.request, 'account', None))

    def get_queryset(self):
        queryset = super().get_queryset().prefetch_related(
            'questions__options',
            'questions__sub_questions',
            'questions__child_questions'
        )
        search = self.request.query_params.get('search', None)
        if search:
            queryset = queryset.filter(name__icontains=search)
        return queryset.order_by('order', 'name')


class ServiceBasicListView(AccountScopedQuerysetMixin, generics.ListAPIView):
    """Simple endpoint to list all services with basic details only (scoped to account via location_id for unauthenticated)."""
    queryset = Service.objects.filter(is_active=True).order_by('order', 'name')
    serializer_class = ServiceBasicSerializer
    permission_classes = [AccountScopedPermission, AllowAny]
    account_lookup = "account"

    def get_queryset(self):
        queryset = super().get_queryset()
        is_active = self.request.query_params.get('is_active', None)
        if is_active is not None:
            is_active_bool = is_active.lower() == 'true'
            queryset = queryset.filter(is_active=is_active_bool)
        return queryset

class ServiceDetailView(AccountScopedQuerysetMixin, generics.RetrieveUpdateDestroyAPIView):
    """Retrieve, update or delete a service (scoped to current account)."""
    queryset = Service.objects.prefetch_related(
        'questions__options__pricing_rules',
        'questions__sub_questions__pricing_rules',
        'questions__pricing_rules',
        'questions__child_questions'
    )
    serializer_class = ServiceSerializer
    permission_classes = [AccountScopedPermission, IsAdminPermission]
    account_lookup = "account"

    # def perform_destroy(self, instance):
    #     # Soft delete
    #     instance.delete
    #     instance.is_active = False
    #     instance.save()



# Package Views
class PackageListCreateView(AccountScopedQuerysetMixin, generics.ListCreateAPIView):
    """List all packages and create new ones (scoped to account via service)."""
    queryset = Package.objects.filter(is_active=True).select_related('service')
    serializer_class = PackageSerializer
    permission_classes = [AccountScopedPermission, IsAdminPermission]
    account_lookup = "service__account"

    def get_queryset(self):
        queryset = super().get_queryset()
        service_id = self.request.query_params.get('service', None)
        if service_id:
            queryset = queryset.filter(service_id=service_id)
        return queryset.order_by('service__name', 'order', 'name')

    def perform_create(self, serializer):
        service = serializer.validated_data.get('service')
        account = getattr(self.request, 'account', None)
        if account and service and service.account_id != account.id:
            raise ValidationError({"service": "Service does not belong to your account."})
        serializer.save()


class PackageDetailView(AccountScopedQuerysetMixin, generics.RetrieveUpdateDestroyAPIView):
    """Retrieve, update or delete a package (scoped to account via service)."""
    queryset = Package.objects.all()
    serializer_class = PackageSerializer
    permission_classes = [AccountScopedPermission, IsAdminPermission]
    account_lookup = "service__account"

    # def perform_destroy(self, instance):
    #     # Soft delete
    #     # instance.is_active = True
    #     instance.delete()


class PackageWithFeaturesView(AccountScopedQuerysetMixin, generics.RetrieveAPIView):
    """Get package with its features (scoped to account via service)."""
    queryset = Package.objects.prefetch_related('package_features__feature')
    serializer_class = PackageWithFeaturesSerializer
    permission_classes = [AccountScopedPermission, IsAdminPermission]
    account_lookup = "service__account"


# Feature Views
class FeatureListCreateView(AccountScopedQuerysetMixin, generics.ListCreateAPIView):
    """List all features and create new ones (scoped to account via service)."""
    queryset = Feature.objects.filter(is_active=True).select_related('service')
    serializer_class = FeatureSerializer
    permission_classes = [AccountScopedPermission, IsAdminPermission]
    account_lookup = "service__account"

    def get_queryset(self):
        queryset = super().get_queryset()
        service_id = self.request.query_params.get('service', None)
        if service_id:
            queryset = queryset.filter(service_id=service_id)
        return queryset.order_by('service__name', 'order', 'name')

    def perform_create(self, serializer):
        service = serializer.validated_data.get('service')
        account = getattr(self.request, 'account', None)
        if account and service and service.account_id != account.id:
            raise ValidationError({"service": "Service does not belong to your account."})
        serializer.save()


class FeatureDetailView(AccountScopedQuerysetMixin, generics.RetrieveUpdateDestroyAPIView):
    """Retrieve, update or delete a feature (scoped to account via service)."""
    queryset = Feature.objects.all()
    serializer_class = FeatureSerializer
    permission_classes = [AccountScopedPermission, IsAdminPermission]
    account_lookup = "service__account"

    # def perform_destroy(self, instance):
    #     # Soft delete
    #     instance.is_active = False
    #     instance.save()


# Package-Feature Views
class PackageFeatureListCreateView(AccountScopedQuerysetMixin, generics.ListCreateAPIView):
    """List and create package-feature relationships (scoped via package.service.account)."""
    queryset = PackageFeature.objects.select_related('package', 'feature')
    serializer_class = PackageFeatureSerializer
    permission_classes = [AccountScopedPermission, IsAdminPermission]
    account_lookup = "package__service__account"

    def get_queryset(self):
        queryset = super().get_queryset()
        package_id = self.request.query_params.get('package', None)
        if package_id:
            queryset = queryset.filter(package_id=package_id)
        return queryset

    def perform_create(self, serializer):
        package = serializer.validated_data.get('package')
        account = getattr(self.request, 'account', None)
        if account and package and getattr(package.service, 'account_id', None) != account.id:
            raise ValidationError({"package": "Package does not belong to your account."})
        serializer.save()


class PackageFeatureDetailView(AccountScopedQuerysetMixin, generics.RetrieveUpdateDestroyAPIView):
    """Retrieve, update or delete a package-feature relationship."""
    queryset = PackageFeature.objects.all()
    serializer_class = PackageFeatureSerializer
    permission_classes = [AccountScopedPermission, IsAdminPermission]
    account_lookup = "package__service__account"


# Question Views
class QuestionListCreateView(AccountScopedQuerysetMixin, generics.ListCreateAPIView):
    """List all questions and create new ones (scoped to account via service)."""
    queryset = Question.objects.filter(is_active=True)
    permission_classes = [AccountScopedPermission, IsAdminPermission]
    account_lookup = "service__account"

    def get_queryset(self):
        queryset = super().get_queryset().select_related(
            'service', 'parent_question', 'condition_option'
        ).prefetch_related(
            'options__pricing_rules',
            'sub_questions__pricing_rules',
            'pricing_rules__package',
            'child_questions'
        )
        service_id = self.request.query_params.get('service', None)
        question_type = self.request.query_params.get('type', None)
        parent_only = self.request.query_params.get('parent_only', 'false').lower() == 'true'
        if service_id:
            queryset = queryset.filter(service_id=service_id)
        if question_type:
            queryset = queryset.filter(question_type=question_type)
        if parent_only:
            queryset = queryset.filter(parent_question__isnull=True)
        return queryset.order_by('service__name', 'order', 'created_at')

    def get_serializer_class(self):
        if self.request.method == 'POST':
            return QuestionCreateSerializer
        return QuestionSerializer

    def perform_create(self, serializer):
        service = serializer.validated_data.get('service')
        account = getattr(self.request, 'account', None)
        if account and service and service.account_id != account.id:
            raise ValidationError({"service": "Service does not belong to your account."})
        serializer.save()

class QuestionDetailView(AccountScopedQuerysetMixin, generics.RetrieveUpdateDestroyAPIView):
    """Retrieve, update or delete a question (scoped to account via service)."""
    queryset = Question.objects.prefetch_related(
        'options__pricing_rules',
        'sub_questions__pricing_rules',
        'pricing_rules',
        'child_questions'
    )
    permission_classes = [AccountScopedPermission, IsAdminPermission]
    account_lookup = "service__account"

    def get_serializer_class(self):
        if self.request.method in ['PUT', 'PATCH']:
            return QuestionCreateSerializer
        return QuestionSerializer

    # def perform_destroy(self, instance):
    #     # Soft delete
    #     instance.is_active = False
    #     instance.save()

# Question Option Views
class QuestionOptionListCreateView(AccountScopedQuerysetMixin, generics.ListCreateAPIView):
    """List and create question options (scoped via question.service.account)."""
    queryset = QuestionOption.objects.filter(is_active=True)
    serializer_class = QuestionOptionSerializer
    permission_classes = [AccountScopedPermission, IsAdminPermission]
    account_lookup = "question__service__account"

    def get_queryset(self):
        queryset = super().get_queryset().prefetch_related('pricing_rules')
        question_id = self.request.query_params.get('question', None)
        if question_id:
            queryset = queryset.filter(question_id=question_id)
        return queryset.order_by('order', 'option_text')

    def perform_create(self, serializer):
        question = serializer.validated_data.get('question')
        account = getattr(self.request, 'account', None)
        if account and question and getattr(question.service, 'account_id', None) != account.id:
            raise ValidationError({"question": "Question does not belong to your account."})
        serializer.save()


class QuestionOptionDetailView(AccountScopedQuerysetMixin, generics.RetrieveUpdateDestroyAPIView):
    """Retrieve, update or delete a question option."""
    queryset = QuestionOption.objects.prefetch_related('pricing_rules')
    serializer_class = QuestionOptionSerializer
    permission_classes = [AccountScopedPermission, IsAdminPermission]
    account_lookup = "question__service__account"

    # def perform_destroy(self, instance):
    #     # Soft delete
    #     instance.is_active = False
    #     instance.save()

# Pricing Views
class QuestionPricingListCreateView(AccountScopedQuerysetMixin, generics.ListCreateAPIView):
    """List and create question pricing rules (scoped via question.service.account)."""
    queryset = QuestionPricing.objects.select_related('question', 'package')
    serializer_class = QuestionPricingSerializer
    permission_classes = [AccountScopedPermission, IsAdminPermission]
    account_lookup = "question__service__account"

    def get_queryset(self):
        queryset = super().get_queryset()
        question_id = self.request.query_params.get('question', None)
        package_id = self.request.query_params.get('package', None)
        if question_id:
            queryset = queryset.filter(question_id=question_id)
        if package_id:
            queryset = queryset.filter(package_id=package_id)
        return queryset


class QuestionPricingDetailView(AccountScopedQuerysetMixin, generics.RetrieveUpdateDestroyAPIView):
    """Retrieve, update or delete a question pricing rule."""
    queryset = QuestionPricing.objects.all()
    serializer_class = QuestionPricingSerializer
    permission_classes = [AccountScopedPermission, IsAdminPermission]
    account_lookup = "question__service__account"



class SubQuestionPricingListCreateView(AccountScopedQuerysetMixin, generics.ListCreateAPIView):
    """List and create sub-question pricing rules (scoped via sub_question.parent_question.service.account)."""
    queryset = SubQuestionPricing.objects.select_related('sub_question', 'package')
    serializer_class = SubQuestionPricingSerializer
    permission_classes = [AccountScopedPermission, IsAdminPermission]
    account_lookup = "sub_question__parent_question__service__account"

    def get_queryset(self):
        queryset = super().get_queryset()
        sub_question_id = self.request.query_params.get('sub_question', None)
        package_id = self.request.query_params.get('package', None)
        if sub_question_id:
            queryset = queryset.filter(sub_question_id=sub_question_id)
        if package_id:
            queryset = queryset.filter(package_id=package_id)
        return queryset


class SubQuestionPricingDetailView(AccountScopedQuerysetMixin, generics.RetrieveUpdateDestroyAPIView):
    """Retrieve, update or delete a sub-question pricing rule."""
    queryset = SubQuestionPricing.objects.all()
    serializer_class = SubQuestionPricingSerializer
    permission_classes = [AccountScopedPermission, IsAdminPermission]
    account_lookup = "sub_question__parent_question__service__account"


class SubQuestionDetailView(AccountScopedQuerysetMixin, generics.RetrieveUpdateDestroyAPIView):
    """Retrieve, update or delete a sub-question (scoped via parent_question.service.account)."""
    queryset = SubQuestion.objects.prefetch_related('pricing_rules')
    serializer_class = SubQuestionSerializer
    permission_classes = [AccountScopedPermission, IsAdminPermission]
    account_lookup = "parent_question__service__account"

    def perform_destroy(self, instance):
        instance.is_active = False
        instance.save()


class OptionPricingListCreateView(AccountScopedQuerysetMixin, generics.ListCreateAPIView):
    """List and create option pricing rules (scoped via option.question.service.account)."""
    queryset = OptionPricing.objects.select_related('option', 'package')
    serializer_class = OptionPricingSerializer
    permission_classes = [AccountScopedPermission, IsAdminPermission]
    account_lookup = "option__question__service__account"

    def get_queryset(self):
        queryset = super().get_queryset()
        option_id = self.request.query_params.get('option', None)
        package_id = self.request.query_params.get('package', None)
        if option_id:
            queryset = queryset.filter(option_id=option_id)
        if package_id:
            queryset = queryset.filter(package_id=package_id)
        return queryset


class OptionPricingDetailView(AccountScopedQuerysetMixin, generics.RetrieveUpdateDestroyAPIView):
    """Retrieve, update or delete an option pricing rule."""
    queryset = OptionPricing.objects.all()
    serializer_class = OptionPricingSerializer
    permission_classes = [AccountScopedPermission, IsAdminPermission]
    account_lookup = "option__question__service__account"



class SubQuestionListCreateView(AccountScopedQuerysetMixin, generics.ListCreateAPIView):
    """List and create sub-questions (scoped via parent_question.service.account)."""
    queryset = SubQuestion.objects.filter(is_active=True)
    serializer_class = SubQuestionSerializer
    permission_classes = [AccountScopedPermission, IsAdminPermission]
    account_lookup = "parent_question__service__account"

    def get_queryset(self):
        queryset = super().get_queryset().prefetch_related('pricing_rules')
        parent_question_id = self.request.query_params.get('parent_question', None)
        if parent_question_id:
            queryset = queryset.filter(parent_question_id=parent_question_id)
        return queryset.order_by('order', 'sub_question_text')

    def perform_create(self, serializer):
        parent_question = serializer.validated_data.get('parent_question')
        account = getattr(self.request, 'account', None)
        if account and parent_question and getattr(parent_question.service, 'account_id', None) != account.id:
            raise ValidationError({"parent_question": "Question does not belong to your account."})
        serializer.save()


# Bulk Operations Views
class BulkQuestionOrderView(APIView):
    """PATCH endpoint to bulk reorder service questions. Supports all questions (root and conditional)."""
    permission_classes = [AccountScopedPermission, IsAdminPermission]

    def patch(self, request):
        import json
        data = request.data
        
        serializer = BulkQuestionOrderItemSerializer(data=data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        account = getattr(request, 'account', None)
        if not account:
            return Response(
                {'error': 'Account scope required'},
                status=status.HTTP_403_FORBIDDEN
            )

        try:
            with transaction.atomic():
                question_id = serializer.validated_data['question_id']
                service_id = serializer.validated_data['service_id']
                order = serializer.validated_data['order']

                question = get_object_or_404(
                    Question,
                    id=question_id,
                    service_id=service_id,
                    service__account=account
                )
                question.order = order
                question.save()

            return Response({
                'message': 'Question order updated successfully',
            })
        except Exception as e:
            return Response({'error': str(e)}, status=status.HTTP_400_BAD_REQUEST)


class BulkQuestionPricingView(APIView):
    """Bulk update question pricing rules for all packages (scoped to account)."""
    permission_classes = [AccountScopedPermission, IsAdminPermission]

    def post(self, request):
        serializer = BulkPricingUpdateSerializer(data=request.data)
        if serializer.is_valid():
            question_id = serializer.validated_data['question_id']
            pricing_rules = serializer.validated_data['pricing_rules']
            account = getattr(request, 'account', None)
            try:
                with transaction.atomic():
                    question = get_object_or_404(Question, id=question_id, service__account=account)
                    
                    # Update or create pricing rules
                    for rule in pricing_rules:
                        package_id = rule['package_id']
                        pricing_type = rule['pricing_type']
                        value = Decimal(str(rule['value']))
                        
                        # package = get_object_or_404(Package, id=package_id)
                        
                        pricing, created = QuestionPricing.objects.get_or_create(
                            question=question,
                            package_id=package_id,
                            defaults={
                                'yes_pricing_type': pricing_type,
                                'yes_value': value
                            }
                        )
                        
                        if not created:
                            pricing.yes_pricing_type = pricing_type
                            pricing.yes_value = value
                            pricing.save()

                return Response({'message': 'Question pricing rules updated successfully'})
                
            except Exception as e:
                return Response({'error': str(e)}, status=status.HTTP_400_BAD_REQUEST)
        
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

class BulkSubQuestionPricingView(APIView):
    """Bulk update sub-question pricing rules for all packages (scoped to account)."""
    permission_classes = [AccountScopedPermission, IsAdminPermission]

    def post(self, request):
        serializer = BulkSubQuestionPricingSerializer(data=request.data)
        if serializer.is_valid():
            sub_question_id = serializer.validated_data['sub_question_id']
            pricing_rules = serializer.validated_data['pricing_rules']
            account = getattr(request, 'account', None)
            try:
                with transaction.atomic():
                    sub_question = get_object_or_404(SubQuestion, id=sub_question_id, parent_question__service__account=account)
                    
                    for rule in pricing_rules:
                        package_id = rule['package_id']
                        pricing_type = rule['pricing_type']
                        value = Decimal(str(rule['value']))
                        
                        pricing, created = SubQuestionPricing.objects.get_or_create(
                            sub_question=sub_question,
                            package_id=package_id,
                            defaults={
                                'yes_pricing_type': pricing_type,
                                'yes_value': value
                            }
                        )
                        
                        if not created:
                            pricing.yes_pricing_type = pricing_type
                            pricing.yes_value = value
                            pricing.save()

                return Response({'message': 'Sub-question pricing rules updated successfully'})
                
            except Exception as e:
                return Response({'error': str(e)}, status=status.HTTP_400_BAD_REQUEST)
        
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


class BulkOptionPricingView(APIView):
    """Bulk update option pricing rules for all packages (scoped to account)."""
    permission_classes = [AccountScopedPermission, IsAdminPermission]

    def post(self, request):
        option_id = request.data.get('option_id')
        pricing_rules = request.data.get('pricing_rules', [])
        if not option_id or not pricing_rules:
            return Response({'error': 'option_id and pricing_rules are required'},
                            status=status.HTTP_400_BAD_REQUEST)
        account = getattr(request, 'account', None)
        try:
            with transaction.atomic():
                option = get_object_or_404(QuestionOption, id=option_id, question__service__account=account)
                
                for rule in pricing_rules:
                    package_id = rule['package_id']
                    pricing_type = rule['pricing_type']
                    value = Decimal(str(rule['value']))
                    
                    pricing, created = OptionPricing.objects.get_or_create(
                        option=option,
                        package_id=package_id,
                        defaults={
                            'pricing_type': pricing_type,
                            'value': value
                        }
                    )
                    
                    if not created:
                        pricing.pricing_type = pricing_type
                        pricing.value = value
                        pricing.save()

            return Response({'message': 'Option pricing rules updated successfully'})
            
        except Exception as e:
            return Response({'error': str(e)}, status=status.HTTP_400_BAD_REQUEST)


class QuestionTreeView(APIView):
    """Get the complete question tree for a service (scoped to account)."""
    permission_classes = [AccountScopedPermission, IsAuthenticated]

    def get(self, request, service_id):
        try:
            account = getattr(request, 'account', None)
            service = get_service_for_account(service_id, account)
            if not service.is_active:
                from rest_framework.exceptions import NotFound
                raise NotFound("Service not found.")
            
            # Get root questions (no parent)
            root_questions = Question.objects.filter(
                service=service,
                is_active=True,
                parent_question__isnull=True
            ).prefetch_related(
                'options__pricing_rules',
                'sub_questions__pricing_rules',
                'pricing_rules',
                'child_questions__options',
                'child_questions__sub_questions',
                'child_questions__pricing_rules'
            ).order_by('order')
            
            serializer = QuestionSerializer(root_questions, many=True, context={'request': request})
            
            return Response({
                'service': {
                    'id': service.id,
                    'name': service.name,
                    'description': service.description
                },
                'questions': serializer.data
            })
            
        except Exception as e:
            return Response({'error': str(e)}, status=status.HTTP_400_BAD_REQUEST)


class ConditionalQuestionsView(APIView):
    """Get conditional questions based on parent question and answer (scoped to account)."""
    permission_classes = [AccountScopedPermission, IsAuthenticated]

    def get(self, request, parent_question_id):
        answer = request.query_params.get('answer')
        option_id = request.query_params.get('option_id')
        if not answer and not option_id:
            return Response({'error': 'Either answer or option_id is required'},
                            status=status.HTTP_400_BAD_REQUEST)
        try:
            account = getattr(request, 'account', None)
            parent_question = get_object_or_404(
                Question,
                id=parent_question_id,
                service__account=account
            )
            
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
                'options__pricing_rules',
                'sub_questions__pricing_rules',
                'pricing_rules'
            ).order_by('order')
            
            serializer = QuestionSerializer(conditional_questions, many=True, context={'request': request})
            
            return Response({
                'parent_question_id': parent_question_id,
                'condition': {
                    'answer': answer,
                    'option_id': option_id
                },
                'conditional_questions': serializer.data
            })
            
        except Exception as e:
            return Response({'error': str(e)}, status=status.HTTP_400_BAD_REQUEST)
        

class QuestionResponseListCreateView(AccountScopedQuerysetMixin, generics.ListCreateAPIView):
    """List and create question responses (scoped via question.service.account)."""
    queryset = QuestionResponse.objects.prefetch_related(
        'option_responses__option',
        'sub_question_responses__sub_question'
    )
    serializer_class = QuestionResponseSerializer
    permission_classes = [AccountScopedPermission, IsAuthenticated]
    account_lookup = "question__service__account"

    def get_queryset(self):
        queryset = super().get_queryset()
        question_id = self.request.query_params.get('question', None)
        if question_id:
            queryset = queryset.filter(question_id=question_id)
        return queryset.order_by('-created_at')




# Analytics Views
class ServiceAnalyticsView(APIView):
    """Get analytics data for services (scoped to account)."""
    permission_classes = [AccountScopedPermission, IsAdminPermission]

    def get(self, request):
        account = getattr(request, 'account', None)
        services = Service.objects.filter(is_active=True, account=account).annotate(
            total_packages=Count('packages', filter=models.Q(packages__is_active=True)),
            total_features=Count('features', filter=models.Q(features__is_active=True)),
            total_questions=Count('questions', filter=models.Q(questions__is_active=True)),
            average_package_price=Avg('packages__base_price', filter=models.Q(packages__is_active=True))
        ).order_by('order', 'name')

        analytics_data = []
        for service in services:
            analytics_data.append({
                'service_id': service.id,
                'service_name': service.name,
                'total_packages': service.total_packages or 0,
                'total_features': service.total_features or 0,
                'total_questions': service.total_questions or 0,
                'average_package_price': service.average_package_price or Decimal('0.00'),
                'created_at': service.created_at
            })

        serializer = ServiceAnalyticsSerializer(analytics_data, many=True)
        return Response(serializer.data)


# Utility Views
class PricingCalculatorView(APIView):
    """Calculate pricing based on question responses (scoped to account)."""
    permission_classes = [AccountScopedPermission, IsAuthenticated]

    def post(self, request):
        serializer = PricingCalculationSerializer(data=request.data)
        if serializer.is_valid():
            service_id = serializer.validated_data['service_id']
            package_id = serializer.validated_data['package_id']
            responses = serializer.validated_data['responses']
            account = getattr(request, 'account', None)
            try:
                service = get_object_or_404(Service, id=service_id, account=account)
                total_adjustment = Decimal('0.00')
                breakdown = []
                for response in responses:
                    question_id = response['question_id']
                    question = get_object_or_404(Question, id=question_id, service__account=account)
                    
                    question_adjustment = Decimal('0.00')
                    question_breakdown = {
                        'question_id': question_id,
                        'question_text': question.question_text,
                        'question_type': question.question_type,
                        'adjustments': []
                    }

                    if question.question_type == 'yes_no':
                        if response.get('yes_no_answer') is True:
                            pricing = QuestionPricing.objects.filter(
                                question=question, package_id=package_id
                            ).first()
                            if pricing and pricing.yes_pricing_type != 'ignore':
                                question_adjustment += pricing.yes_value
                                question_breakdown['adjustments'].append({
                                    'type': 'yes_answer',
                                    'pricing_type': pricing.yes_pricing_type,
                                    'value': pricing.yes_value
                                })

                    elif question.question_type in ['describe', 'quantity']:
                        selected_options = response.get('selected_options', [])
                        for option_data in selected_options:
                            option_id = option_data['option_id']
                            quantity = option_data.get('quantity', 1)
                            
                            pricing = OptionPricing.objects.filter(
                                option_id=option_id, package_id=package_id
                            ).first()
                            
                            if pricing and pricing.pricing_type != 'ignore':
                                if pricing.pricing_type == 'per_quantity':
                                    adjustment = pricing.value * quantity
                                else:
                                    adjustment = pricing.value
                                    
                                question_adjustment += adjustment
                                question_breakdown['adjustments'].append({
                                    'type': 'option_selection',
                                    'option_id': option_id,
                                    'quantity': quantity,
                                    'pricing_type': pricing.pricing_type,
                                    'value': adjustment
                                })

                    elif question.question_type == 'multiple_yes_no':
                        sub_question_answers = response.get('sub_question_answers', [])
                        for sub_answer in sub_question_answers:
                            if sub_answer.get('answer') is True:
                                sub_question_id = sub_answer['sub_question_id']
                                pricing = SubQuestionPricing.objects.filter(
                                    sub_question_id=sub_question_id, package_id=package_id
                                ).first()
                                
                                if pricing and pricing.yes_pricing_type != 'ignore':
                                    question_adjustment += pricing.yes_value
                                    question_breakdown['adjustments'].append({
                                        'type': 'sub_question_yes',
                                        'sub_question_id': sub_question_id,
                                        'pricing_type': pricing.yes_pricing_type,
                                        'value': pricing.yes_value
                                    })

                    total_adjustment += question_adjustment
                    question_breakdown['total_adjustment'] = question_adjustment
                    breakdown.append(question_breakdown)

                return Response({
                    'service_id': service_id,
                    'package_id': package_id,
                    'total_adjustment': total_adjustment,
                    'breakdown': breakdown
                })
                
            except Exception as e:
                return Response({'error': str(e)}, status=status.HTTP_400_BAD_REQUEST)
        
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        


class ServiceSettingsView(APIView):
    permission_classes = [AccountScopedPermission, IsAdminPermission]

    def get(self, request, service_id):
        account = getattr(request, 'account', None)
        service = get_service_for_account(service_id, account)
        try:
            settings = service.settings
            serializer = ServiceSettingsSerializer(settings)
            return Response(serializer.data)
        except ServiceSettings.DoesNotExist:
            return Response({"detail": "Settings not found."}, status=status.HTTP_404_NOT_FOUND)
    def post(self, request, service_id):
        account = getattr(request, 'account', None)
        service = get_service_for_account(service_id, account)
        serializer = ServiceSettingsSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        settings, created = ServiceSettings.objects.update_or_create(
            service=service,
            defaults=serializer.validated_data
        )

        return Response(
            ServiceSettingsSerializer(settings).data,
            status=status.HTTP_201_CREATED if created else status.HTTP_200_OK
        )

    def put(self, request, service_id):
        account = getattr(request, 'account', None)
        service = get_service_for_account(service_id, account)
        settings = get_object_or_404(ServiceSettings, service=service)

        serializer = ServiceSettingsSerializer(settings, data=request.data, partial=True)
        if serializer.is_valid():
            serializer.save()
            return Response(serializer.data)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


# Who am I (current user profile)
class MeView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        return Response(UserSerializer(request.user).data)
    



from .serializers import GlobalSizePackageSerializer, ServicePackageSizeMappingSerializer
from .models import GlobalSizePackage, ServicePackageSizeMapping

class GlobalSizePackageListCreateView(generics.ListCreateAPIView):
    """
    GET  /api/global-sizes/ → List all global size packages with templates
    POST /api/global-sizes/ → Create a global size package with template prices
    """
    serializer_class = GlobalSizePackageSerializer
    queryset = GlobalSizePackage.objects.all().prefetch_related('template_prices')

class AutoMapGlobalToServicePackages(APIView):
    """
    POST /api/services/{service_id}/auto-map-packages/
    Automatically map global pricing templates to service-level packages
    by order.
    """
    def post(self, request, service_id):
        try:
            service = Service.objects.prefetch_related('packages').get(id=service_id)
        except Service.DoesNotExist:
            return Response({'detail': 'Service not found'}, status=404)

        global_sizes = GlobalSizePackage.objects.prefetch_related('template_prices').order_by('order')
        service_packages = list(service.packages.filter(is_active=True).order_by('order'))

        if not service_packages:
            return Response({'detail': 'No service-level packages found.'}, status=400)

        created_mappings = []
        for global_size in global_sizes:
            templates = list(global_size.template_prices.order_by('order'))
            for idx, template in enumerate(templates):
                if idx < len(service_packages):
                    service_package = service_packages[idx]
                    mapping, created = ServicePackageSizeMapping.objects.get_or_create(
                        service_package=service_package,
                        global_size=global_size,
                        defaults={'price': template.price}
                    )
                    if created:
                        created_mappings.append(mapping)

        return Response(ServicePackageSizeMappingSerializer(created_mappings, many=True).data, status=201)
    

from rest_framework.generics import ListAPIView

class ServiceMappedSizesAPIView(ListAPIView):
    """
    GET /api/services/{service_id}/mapped-sizes/

    Retrieve all size mappings with prices for a given service.
    """
    serializer_class = ServicePackageSizeMappingSerializer

    def get_queryset(self):
        service_id = self.kwargs['service_id']
        return ServicePackageSizeMapping.objects.filter(
            service_package__service_id=service_id
        ).select_related('global_size', 'service_package')
    


class GlobalSizePackageDetailView(generics.RetrieveUpdateDestroyAPIView):
    serializer_class = GlobalSizePackageSerializer
    queryset = GlobalSizePackage.objects.all().prefetch_related('template_prices')
    lookup_field = 'id'





class GlobalSettingsView(APIView):
    """
    GET → Retrieve global base price for current account
    PUT → Update global base price for current account
    """
    permission_classes = [AccountScopedPermission, IsAuthenticated]

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


# ============================
# User CRUD (Admin managed, scoped to account)
# ============================
class UserListCreateView(AccountScopedQuerysetMixin, generics.ListCreateAPIView):
    queryset = User.objects.all().order_by('-date_joined')
    serializer_class = UserSerializer
    permission_classes = [AccountScopedPermission, IsAdminPermission]
    account_lookup = "account"

    def get_queryset(self):
        qs = super().get_queryset()
        qs = qs.filter(is_superuser=False)
        search = self.request.query_params.get('search')
        role = self.request.query_params.get('role')
        if search:
            qs = qs.filter(
                models.Q(username__icontains=search) |
                models.Q(email__icontains=search) |
                models.Q(first_name__icontains=search) |
                models.Q(last_name__icontains=search)
            )
        if role:
            qs = qs.filter(role=role)
        return qs

    def perform_create(self, serializer):
        account = getattr(self.request, "account", None)
        user = serializer.save(account=account)
        try_link_user_ghl_id_from_email(user, account)


class UserDetailView(AccountScopedQuerysetMixin, generics.RetrieveUpdateDestroyAPIView):
    queryset = User.objects.all()
    serializer_class = UserSerializer
    account_lookup = "account"

    def get_queryset(self):
        qs = super().get_queryset()
        return qs.filter(is_superuser=False)

    def get_permissions(self):
        base = [AccountScopedPermission()]
        if self.request.method in ['PUT', 'PATCH', 'DELETE', 'POST']:
            return base + [IsAdminPermission()]
        return base + [permissions.IsAuthenticated()]

    def get_object(self):
        obj = super().get_object()
        user = self.request.user
        if self.request.method == 'GET':
            if user.is_authenticated and (user.is_admin or user.id == obj.id):
                return obj
            raise permissions.PermissionDenied('Not allowed')
        return obj


class UserFutureJobUnassignView(APIView):
    """
    Admin endpoint to remove a technician from all future jobs in the same account.
    Past jobs are not modified.
    """
    permission_classes = [AccountScopedPermission, IsAdminPermission]

    def post(self, request, pk):
        account = getattr(request, 'account', None)
        if not account:
            return Response({'detail': 'Account scope required.'}, status=status.HTTP_403_FORBIDDEN)

        technician = get_object_or_404(
            User.objects.filter(account=account, is_superuser=False),
            pk=pk,
        )

        now = timezone.now()
        assignments_qs = JobAssignment.objects.filter(
            user=technician,
            job__account=account,
            job__scheduled_at__gt=now,
        )

        distinct_jobs_count = assignments_qs.values('job_id').distinct().count()
        removed_assignments_count, _ = assignments_qs.delete()

        return Response({
            'detail': 'Technician unassigned from future jobs successfully.',
            'user_id': technician.id,
            'future_jobs_affected': distinct_jobs_count,
            'assignments_removed': removed_assignments_count,
            'cutoff': now.isoformat(),
        }, status=status.HTTP_200_OK)