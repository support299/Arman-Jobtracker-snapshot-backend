# urls.py
from django.urls import path
from . import views

# Complete Admin API URLs
urlpatterns = [
    # ============================================================================
    # AUTHENTICATION ENDPOINTS
    # ============================================================================
    path('auth/login/', views.AdminTokenObtainPairView.as_view(), name='admin-login'),
    path('auth/logout/', views.AdminLogoutView.as_view(), name='admin-logout'),
    path('auth/refresh/', views.AdminTokenRefreshView.as_view(), name='token_refresh'),
    # User (non-admin) auth
    path('auth/user/login/', views.UserTokenObtainPairView.as_view(), name='user-login'),
    path('auth/user/refresh/', views.UserTokenRefreshView.as_view(), name='user-token-refresh'),
    path('auth/me/', views.MeView.as_view(), name='auth-me'),

    # ============================================================================
    # LOCATION MANAGEMENT (if you have these)
    # ============================================================================
    path('locations/', views.LocationListCreateView.as_view(), name='location-list-create'),
    path('locations/<uuid:pk>/', views.LocationDetailView.as_view(), name='location-detail'),
    
    # ============================================================================
    # SERVICE MANAGEMENT
    # ============================================================================
    path('services/', views.ServiceListCreateView.as_view(), name='service-list-create'),
    path('services/basic/', views.ServiceBasicListView.as_view(), name='service-basic-list'),
    path('services/<uuid:pk>/', views.ServiceDetailView.as_view(), name='service-detail'),
    path('services/<uuid:service_id>/settings/', views.ServiceSettingsView.as_view(), name='service-settings'),
    path('services/<uuid:service_id>/question-tree/', views.QuestionTreeView.as_view(), name='service-question-tree'),
    path('services/analytics/', views.ServiceAnalyticsView.as_view(), name='service-analytics'),
    
    # ============================================================================
    # PACKAGE MANAGEMENT (if you have these)
    # ============================================================================
    path('packages/', views.PackageListCreateView.as_view(), name='package-list-create'),
    path('packages/<uuid:pk>/', views.PackageDetailView.as_view(), name='package-detail'),
    path('packages/<uuid:pk>/features/', views.PackageWithFeaturesView.as_view(), name='package-features'),
    
    # ============================================================================
    # FEATURE MANAGEMENT (if you have these)
    # ============================================================================
    path('features/', views.FeatureListCreateView.as_view(), name='feature-list-create'),
    path('features/<uuid:pk>/', views.FeatureDetailView.as_view(), name='feature-detail'),
    
    # ============================================================================
    # PACKAGE-FEATURE RELATIONSHIPS (if you have these)
    # ============================================================================
    path('package-features/', views.PackageFeatureListCreateView.as_view(), name='package-feature-list-create'),
    path('package-features/<uuid:pk>/', views.PackageFeatureDetailView.as_view(), name='package-feature-detail'),
    
    # ============================================================================
    # QUESTION MANAGEMENT (Core Feature)
    # ============================================================================
    
    # Main Questions
    path('questions/', views.QuestionListCreateView.as_view(), name='question-list-create'),
    path('questions/<uuid:pk>/', views.QuestionDetailView.as_view(), name='question-detail'),
    path('questions/<uuid:parent_question_id>/conditional/', views.ConditionalQuestionsView.as_view(), name='conditional-questions'),
    
    # Question Options (for describe/quantity type questions)
    path('question-options/', views.QuestionOptionListCreateView.as_view(), name='question-option-list-create'),
    path('question-options/<uuid:pk>/', views.QuestionOptionDetailView.as_view(), name='question-option-detail'),
    
    # Sub-Questions (for multiple_yes_no type questions)
    path('sub-questions/', views.SubQuestionListCreateView.as_view(), name='sub-question-list-create'),
    path('sub-questions/<uuid:pk>/', views.SubQuestionDetailView.as_view(), name='sub-question-detail'),
    
    # ============================================================================
    # PRICING MANAGEMENT
    # ============================================================================
    
    # Question Pricing (for yes_no/conditional questions)
    path('question-pricing/', views.QuestionPricingListCreateView.as_view(), name='question-pricing-list-create'),
    path('question-pricing/<uuid:pk>/', views.QuestionPricingDetailView.as_view(), name='question-pricing-detail'),
    
    # Sub-Question Pricing (for multiple_yes_no sub-questions)
    path('sub-question-pricing/', views.SubQuestionPricingListCreateView.as_view(), name='sub-question-pricing-list-create'),
    path('sub-question-pricing/<uuid:pk>/', views.SubQuestionPricingDetailView.as_view(), name='sub-question-pricing-detail'),
    
    # Option Pricing (for describe/quantity question options)
    path('option-pricing/', views.OptionPricingListCreateView.as_view(), name='option-pricing-list-create'),
    path('option-pricing/<uuid:pk>/', views.OptionPricingDetailView.as_view(), name='option-pricing-detail'),
    
    # ============================================================================
    # BULK OPERATIONS
    # ============================================================================
    path('questions/reorder/', views.BulkQuestionOrderView.as_view(), name='questions-reorder'),
    path('questions/bulk-pricing/', views.BulkQuestionPricingView.as_view(), name='bulk-question-pricing'),
    path('sub-questions/bulk-pricing/', views.BulkSubQuestionPricingView.as_view(), name='bulk-sub-question-pricing'),
    path('options/bulk-pricing/', views.BulkOptionPricingView.as_view(), name='bulk-option-pricing'),
    
    # ============================================================================
    # CUSTOMER RESPONSES & INTERACTIONS
    # ============================================================================
    path('question-responses/', views.QuestionResponseListCreateView.as_view(), name='question-response-list-create'),
    # path('question-responses/<uuid:pk>/', views.QuestionResponseDetailView.as_view(), name='question-response-detail'),
    
    # ============================================================================
    # UTILITY ENDPOINTS
    # ============================================================================
    path('pricing/calculate/', views.PricingCalculatorView.as_view(), name='pricing-calculator'),
    # path('questions/validate-structure/', views.QuestionStructureValidatorView.as_view(), name='validate-question-structure'),


    path('global-sizes/', views.GlobalSizePackageListCreateView.as_view(), name='global-size-create'),
    path('services/<uuid:service_id>/auto-map-packages/', views.AutoMapGlobalToServicePackages.as_view(), name='auto-map-packages'),
    path('services/<uuid:service_id>/mapped-sizes/', views.ServiceMappedSizesAPIView.as_view(), name='mapped-sizes'),
    path('global-sizes/<str:id>/', views.GlobalSizePackageDetailView.as_view(), name='global-size-detail'),

    path("global-base-price/", views.GlobalSettingsView.as_view(), name="global-settings"),

    # ============================
    # USER MANAGEMENT
    # ============================
    path('users/', views.UserListCreateView.as_view(), name='user-list-create'),
    path('users/<int:pk>/', views.UserDetailView.as_view(), name='user-detail'),
    path('users/<int:pk>/unassign-future-jobs/', views.UserFutureJobUnassignView.as_view(), name='user-unassign-future-jobs'),

]