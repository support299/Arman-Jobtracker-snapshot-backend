# models.py
from django.db import models
from django.contrib.auth.models import AbstractUser
from django.core.validators import MinValueValidator, MaxValueValidator
from decimal import Decimal
import uuid


class User(AbstractUser):
    """Extended User model for admin authentication. Belongs to one GHL account (GHLAuthCredentials)."""
    ROLE_MANAGER = 'manager'
    ROLE_SUPERVISOR = 'supervisor'
    ROLE_WORKER = 'worker'

    ROLE_CHOICES = [
        (ROLE_MANAGER, 'Manager'),
        (ROLE_SUPERVISOR, 'Supervisor'),
        (ROLE_WORKER, 'Worker'),
    ]
    # id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    # Parent account (one GHL onboarding = one account; one account can have many users)
    account = models.ForeignKey(
        'accounts.GHLAuthCredentials',
        on_delete=models.CASCADE,
        related_name='users',
        null=True,
        blank=True,
        help_text='GHL account this user belongs to (for multi-app onboarding)',
    )
    ghl_user_id = models.CharField(max_length=255, unique=True, null=True, blank=True)
    is_admin = models.BooleanField(default=False)
    payroll_can_view_team_data = models.BooleanField(
        default=True,
        help_text=(
            'When True (and user is admin: manager/supervisor), payroll reads see all account '
            'team data for payouts list, time-entries today, and active-session. '
            "When False, those endpoints only return the user's own data (worker scope)."
        ),
    )
    can_access_service_management_tool = models.BooleanField(
        default=False,
        help_text='Grants access to the service management tool for this user.',
    )
    can_access_location_management_tool = models.BooleanField(
        default=False,
        help_text='Grants access to the location management tool for this user.',
    )
    can_access_house_size_management_tool = models.BooleanField(
        default=False,
        help_text='Grants access to the house size management tool for this user.',
    )
    role = models.CharField(max_length=20, choices=ROLE_CHOICES, default=ROLE_WORKER)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'auth_user'

    def save(self, *args, **kwargs):
        # Auto-sync is_admin from role: Managers and Supervisors are admins; others are regular users
        self.is_admin = self.role in [self.ROLE_MANAGER, self.ROLE_SUPERVISOR]
        super().save(*args, **kwargs)


class Location(models.Model):
    """Location model with Google Places API integration"""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    account = models.ForeignKey(
        'accounts.GHLAuthCredentials',
        on_delete=models.CASCADE,
        related_name='locations',
        null=True,
        blank=True,
        help_text='GHL account this location belongs to (for multi-account onboarding)',
    )
    name = models.CharField(max_length=255)
    address = models.TextField()
    latitude = models.DecimalField(max_digits=20, decimal_places=16)
    longitude = models.DecimalField(max_digits=21, decimal_places=16)
    trip_surcharge = models.DecimalField(
        max_digits=10, 
        decimal_places=2, 
        default=Decimal('0.00'),
        validators=[MinValueValidator(Decimal('0.00'))]
    )
    google_place_id = models.CharField(max_length=255, unique=True, null=True, blank=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True)

    class Meta:
        db_table = 'locations'
        ordering = ['name']

    def __str__(self):
        return f"{self.name} - {self.address}"




class GlobalBasePrice(models.Model):
    account = models.ForeignKey(
        'accounts.GHLAuthCredentials',
        on_delete=models.CASCADE,
        related_name='global_base_prices',
        null=True,
        blank=True,
        help_text='GHL account this base price belongs to (for multi-account onboarding)',
    )
    base_price = models.DecimalField(
        max_digits=10, 
        decimal_places=2, 
        default=0.00,
        help_text="Global base price applied across the system."
    )

    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"Base Price: {self.base_price}"

class Service(models.Model):
    """Main service model"""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    account = models.ForeignKey(
        'accounts.GHLAuthCredentials',
        on_delete=models.CASCADE,
        related_name='services',
        null=True,
        blank=True,
        help_text='GHL account this service belongs to (for multi-account onboarding)',
    )
    name = models.CharField(max_length=255)
    description = models.TextField()
    # base_price=models.DecimalField(max_digits=10,decimal_places=2,default=Decimal('0.00'))
    price = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal('0.00'))
    hours = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal('0.00'))
    is_active = models.BooleanField(default=True)
    order = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True)

    class Meta:
        db_table = 'services'
        ordering = ['order', 'name']


    def __str__(self):
        return self.name
    
class ServiceSettings(models.Model):
    service = models.OneToOneField('Service', on_delete=models.CASCADE, related_name='settings')

    # Disclaimers
    general_disclaimer = models.TextField(blank=True, null=True)
    bid_in_person_disclaimer = models.TextField(blank=True, null=True)

    # Boolean settings (based on the UI)
    apply_area_minimum = models.BooleanField(default=False)
    apply_house_size_minimum = models.BooleanField(default=False)
    apply_trip_charge_to_bid = models.BooleanField(default=False)
    enable_dollar_minimum = models.BooleanField(default=False)

    def __str__(self):
        return f"Settings for {self.service.name}"


class Package(models.Model):
    """Package model with one-to-many relationship to Service"""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    service = models.ForeignKey(Service, related_name='packages', on_delete=models.CASCADE)
    name = models.CharField(max_length=255)
    base_price = models.DecimalField(
        max_digits=10, 
        decimal_places=2,
        validators=[MinValueValidator(Decimal('0.00'))]
    )
    order = models.PositiveIntegerField(default=0)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'packages'
        ordering = ['order', 'name']
        unique_together = ['service', 'name']

    def __str__(self):
        return f"{self.service.name} - {self.name}"


class Feature(models.Model):
    """Feature model with many-to-many relationship to Package"""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    service = models.ForeignKey(Service, related_name='features', on_delete=models.CASCADE)
    name = models.CharField(max_length=255)
    description = models.TextField(blank=True)
    is_active = models.BooleanField(default=True)
    order = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'features'
        unique_together = ['service', 'name']
        ordering = ['order', 'name']

    def __str__(self):
        return f"{self.service.name} - {self.name}"


class PackageFeature(models.Model):
    """Through model for Package-Feature relationship with additional logic"""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    package = models.ForeignKey(Package, related_name='package_features', on_delete=models.CASCADE)
    feature = models.ForeignKey(Feature, related_name='package_features', on_delete=models.CASCADE)
    is_included = models.BooleanField(default=True)
    order = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'package_features'
        unique_together = ['package', 'feature']
        ordering = ['order', 'created_at']

    def __str__(self):
        return f"{self.package.name} - {self.feature.name}"


class Question(models.Model):
    """Base question model for dynamic question builder"""
    
    QUESTION_TYPES = [
        ('yes_no', 'Yes/No'),
        ('describe', 'Describe (Multiple Options)'),  # Renamed from 'options'
        ('multiple_yes_no', 'Multiple Yes/No Sub-Questions'),
        ('conditional', 'Conditional Questions'),
        ('quantity', 'How Many (Quantity Selection)'),
    ]
    
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    service = models.ForeignKey(Service, related_name='questions', on_delete=models.CASCADE)
    parent_question = models.ForeignKey('self', related_name='child_questions', 
                                      on_delete=models.CASCADE, null=True, blank=True)
    
    # Conditional logic fields
    condition_answer = models.CharField(max_length=20, null=True, blank=True, 
                                      help_text="Answer value that triggers this conditional question (e.g., 'yes', 'no', or option ID)")
    condition_option = models.ForeignKey('QuestionOption', related_name='conditional_questions', 
                                       on_delete=models.CASCADE, null=True, blank=True,
                                       help_text="Option that triggers this conditional question")
    
    question_text = models.TextField()
    question_type = models.CharField(max_length=20, choices=QUESTION_TYPES)
    order = models.PositiveIntegerField(default=0)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'questions'
        ordering = ['order', 'created_at']

    def __str__(self):
        return f"{self.service.name} - {self.question_text[:50]}..."

    @property
    def is_conditional(self):
        """Check if this is a conditional question"""
        return self.parent_question is not None

    @property
    def is_parent(self):
        """Check if this question has child questions"""
        return self.child_questions.exists()


class QuestionOption(models.Model):
    """Options for describe/quantity type questions"""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    question = models.ForeignKey(Question, related_name='options', on_delete=models.CASCADE)
    option_text = models.CharField(max_length=255)
    order = models.PositiveIntegerField(default=0)
    is_active = models.BooleanField(default=True)
    
    # For quantity questions
    allow_quantity = models.BooleanField(default=False, 
                                       help_text="Allow quantity input for this option")
    max_quantity = models.PositiveIntegerField( 
                                             help_text="Maximum allowed quantity", null=True , blank=True)
    
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'question_options'
        ordering = ['order', 'option_text']

    def __str__(self):
        return f"{self.question.question_text[:30]}... - {self.option_text}"
    


class SubQuestion(models.Model):
    """Sub-questions for multiple_yes_no type questions"""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    parent_question = models.ForeignKey(Question, related_name='sub_questions', on_delete=models.CASCADE)
    sub_question_text = models.TextField()
    order = models.PositiveIntegerField(default=0)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'sub_questions'
        ordering = ['order', 'sub_question_text']

    def __str__(self):
        return f"{self.parent_question.question_text[:30]}... - {self.sub_question_text[:30]}..."
    



class QuestionPricing(models.Model):
    """Pricing rules for questions per package"""
    
    PRICING_TYPES = [
        ('upcharge_percent', 'Fixed Upcharge Amount'),
        ('discount_percent', 'Fixed Discount Amount'),
        ('upcharge_percent_of_total', 'Upcharge % of Package Total'),
        ('discount_percent_of_total', 'Discount % of Package Total'),
        ('fixed_price', 'Fixed Price'),
        ('ignore', 'Ignore'),
    ]
    # Types that are applied as percentage of package subtotal (base + sqft + surcharge + fixed adjustments)
    PERCENT_OF_TOTAL_TYPES = ('upcharge_percent_of_total', 'discount_percent_of_total')
    
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    question = models.ForeignKey(Question, related_name='pricing_rules', on_delete=models.CASCADE)
    package = models.ForeignKey('Package', related_name='question_pricing', on_delete=models.CASCADE)
    
    # For Yes/No questions - pricing when answer is "Yes"
    yes_pricing_type = models.CharField(max_length=30, choices=PRICING_TYPES, default='ignore')
    yes_value = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        default=Decimal('0.00'),
        help_text="Fixed amount (e.g. 12.00) or percentage for percent-of-total (e.g. 10 for 10%)",
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'question_pricing'
        unique_together = ['question', 'package']

    def __str__(self):
        return f"{self.package.name} - {self.question.question_text[:30]}..."


class SubQuestionPricing(models.Model):
    """Pricing rules for sub-questions per package"""
    
    PRICING_TYPES = [
        ('upcharge_percent', 'Fixed Upcharge Amount'),
        ('discount_percent', 'Fixed Discount Amount'),
        ('upcharge_percent_of_total', 'Upcharge % of Package Total'),
        ('discount_percent_of_total', 'Discount % of Package Total'),
        ('fixed_price', 'Fixed Price'),
        ('ignore', 'Ignore'),
    ]
    PERCENT_OF_TOTAL_TYPES = ('upcharge_percent_of_total', 'discount_percent_of_total')
    
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    sub_question = models.ForeignKey(SubQuestion, related_name='pricing_rules', on_delete=models.CASCADE)
    package = models.ForeignKey('Package', related_name='sub_question_pricing', on_delete=models.CASCADE)
    
    yes_pricing_type = models.CharField(max_length=30, choices=PRICING_TYPES, default='ignore')
    yes_value = models.DecimalField(
        max_digits=10, 
        decimal_places=2, 
        default=Decimal('0.00'),
        help_text="Fixed amount to add/subtract for 'Yes' answer"
    )
    
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'sub_question_pricing'
        unique_together = ['sub_question', 'package']

    def __str__(self):
        return f"{self.package.name} - {self.sub_question.sub_question_text[:30]}..."


class OptionPricing(models.Model):
    """Pricing rules for question options per package"""
    
    PRICING_TYPES = [
        ('upcharge_percent', 'Fixed Upcharge Amount'),
        ('discount_percent', 'Fixed Discount Amount'),
        ('upcharge_percent_of_total', 'Upcharge % of Package Total'),
        ('discount_percent_of_total', 'Discount % of Package Total'),
        ('fixed_price', 'Fixed Price'),
        ('per_quantity', 'Price Per Quantity'),  # New for quantity questions
        ('ignore', 'Ignore'),
    ]
    PERCENT_OF_TOTAL_TYPES = ('upcharge_percent_of_total', 'discount_percent_of_total')
    
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    option = models.ForeignKey(QuestionOption, related_name='pricing_rules', on_delete=models.CASCADE)
    package = models.ForeignKey('Package', related_name='option_pricing', on_delete=models.CASCADE)
    
    pricing_type = models.CharField(max_length=30, choices=PRICING_TYPES, default='ignore')
    value = models.DecimalField(
        max_digits=10, 
        decimal_places=2, 
        default=Decimal('0.00'),
        help_text="Fixed amount to add/subtract (e.g., 12.00 for $12) or price per quantity"
    )
    
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'option_pricing'
        unique_together = ['option', 'package']

    def __str__(self):
        return f"{self.package.name} - {self.option.option_text}"



class QuestionResponse(models.Model):
    """Store customer responses to questions"""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    question = models.ForeignKey(Question, on_delete=models.CASCADE)
    # You can add user/session reference here
    
    # For different question types
    yes_no_answer = models.BooleanField(null=True, blank=True)
    text_answer = models.TextField(null=True, blank=True)
    
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'question_responses'


class OptionResponse(models.Model):
    """Store customer responses to options"""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    question_response = models.ForeignKey(QuestionResponse, related_name='option_responses', on_delete=models.CASCADE)
    option = models.ForeignKey(QuestionOption, on_delete=models.CASCADE)
    quantity = models.PositiveIntegerField(default=1)
    
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'option_responses'


class SubQuestionResponse(models.Model):
    """Store customer responses to sub-questions"""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    question_response = models.ForeignKey(QuestionResponse, related_name='sub_question_responses', on_delete=models.CASCADE)
    sub_question = models.ForeignKey(SubQuestion, on_delete=models.CASCADE)
    answer = models.BooleanField()
    
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'sub_question_responses'

# Future-ready models for orders/invoices (when user side is built)
class Order(models.Model):
    """Order model for future user side implementation"""
    
    STATUS_CHOICES = [
        ('pending', 'Pending'),
        ('confirmed', 'Confirmed'),
        ('in_progress', 'In Progress'),
        ('completed', 'Completed'),
        ('cancelled', 'Cancelled'),
    ]
    
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    # user = models.ForeignKey(User, related_name='orders', on_delete=models.CASCADE)  # Future
    service = models.ForeignKey(Service, on_delete=models.PROTECT, related_name='orders')
    package = models.ForeignKey(Package, on_delete=models.PROTECT, related_name='orders')
    location = models.ForeignKey(Location, on_delete=models.PROTECT, null=True, blank=True)
    
    base_price = models.DecimalField(max_digits=10, decimal_places=2)
    trip_surcharge = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal('0.00'))
    question_adjustments = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal('0.00'))
    total_price = models.DecimalField(max_digits=10, decimal_places=2)
    
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending')
    
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'orders'
        ordering = ['-created_at']

    def __str__(self):
        return f"Order {self.id} - {self.service.name}"


class OrderQuestionAnswer(models.Model):
    """Store user answers to questions for each order"""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    order = models.ForeignKey(Order, related_name='question_answers', on_delete=models.CASCADE)
    question = models.ForeignKey(Question, on_delete=models.PROTECT)
    
    # For Yes/No questions
    yes_no_answer = models.BooleanField(null=True, blank=True)
    
    # For Option questions
    selected_option = models.ForeignKey(QuestionOption, on_delete=models.PROTECT, null=True, blank=True)
    
    # Price impact from this answer
    price_adjustment = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal('0.00'))
    
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'order_question_answers'
        unique_together = ['order', 'question']

    def __str__(self):
        return f"Order {self.order.id} - {self.question.question_text[:30]}..."
    



# models.py
class GlobalSizePackage(models.Model):
    """Defines a size range globally applicable to all services"""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    account = models.ForeignKey(
        'accounts.GHLAuthCredentials',
        on_delete=models.CASCADE,
        related_name='global_size_packages',
        null=True,
        blank=True,
        help_text='GHL account this size package belongs to (for multi-account onboarding)',
    )
    min_sqft = models.PositiveIntegerField()
    max_sqft = models.PositiveIntegerField(null=True, blank=True, default=100000000)
    order = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['order', 'min_sqft']

    def __str__(self):
        return f"{self.min_sqft} – {self.max_sqft} sqft"
    

class GlobalPackageTemplate(models.Model):
    """Defines prices per package type for a global size range"""
    global_size = models.ForeignKey(GlobalSizePackage, related_name='template_prices', on_delete=models.CASCADE)
    label = models.CharField(max_length=255)  # Example: Package 1, Package 2
    price = models.DecimalField(max_digits=10, decimal_places=2)
    order = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ['order']
        unique_together = ['global_size', 'label']

    def __str__(self):
        return f"{self.label} @ {self.global_size}"
    

class ServicePackageSizeMapping(models.Model):
    """Actual price mapping for service-level packages against size range"""
    service_package = models.ForeignKey(Package, related_name='size_pricings', on_delete=models.CASCADE)
    global_size = models.ForeignKey(GlobalSizePackage, on_delete=models.CASCADE)
    price = models.DecimalField(max_digits=10, decimal_places=2)

    class Meta:
        unique_together = ['service_package', 'global_size']
        ordering = ['global_size__order']

    def __str__(self):
        return f"{self.service_package} ({self.global_size}) - ₹{self.price}"


class Appointment(models.Model):
    """Appointment model for GHL appointments"""
    
    APPOINTMENT_STATUS_CHOICES = [
        ('new', 'New'),  # Maps to 'unconfirmed' in UI
        ('confirmed', 'Confirmed'),
        ('cancelled', 'Cancelled'),
        ('showed', 'Showed'),
        ('noshow', 'No Show'),
        ('invalid', 'Invalid'),
    ]
    
    ESTIMATE_STATUS_CHOICES = [
        ('confirmed', 'Confirmed'),
        ('on_my_way', 'On My Way'),
        ('in_progress', 'In Progress'),
        ('quoted', 'Quoted'),
        ('canceled', 'Canceled'),
        ('accepted', 'Accepted'),
        ('declined', 'Declined'),
        ('expired', 'Expired'),
    ]
    
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    account = models.ForeignKey(
        'accounts.GHLAuthCredentials',
        on_delete=models.CASCADE,
        related_name='appointments',
        null=True,
        blank=True,
        help_text='GHL account this appointment belongs to (for multi-account onboarding)',
    )
    ghl_appointment_id = models.CharField(max_length=255, unique=True, db_index=True)
    location_id = models.CharField(max_length=255, db_index=True)
    
    # Basic appointment info
    title = models.CharField(max_length=255, blank=True, null=True)
    address = models.URLField(max_length=500, blank=True, null=True, help_text="Meeting URL or address")
    calendar = models.ForeignKey(
        'accounts.Calendar',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='appointments',
        help_text="GHL calendar this appointment belongs to"
    )
    appointment_status = models.CharField(max_length=50, choices=APPOINTMENT_STATUS_CHOICES, blank=True, null=True)
    estimate_status = models.CharField(max_length=50, choices=ESTIMATE_STATUS_CHOICES, blank=True, null=True, help_text="Status for estimate appointments (FREE On-Site Estimate calendar)")
    source = models.CharField(max_length=100, blank=True, null=True)
    notes = models.TextField(blank=True, null=True)
    
    # Relationships
    contact = models.ForeignKey(
        'accounts.Contact',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='appointments',
        to_field='contact_id'
    )
    ghl_contact_id = models.CharField(max_length=255, blank=True, null=True, help_text="GHL contact ID")
    group_id = models.CharField(max_length=255, blank=True, null=True)
    
    # User assignments - using ghl_user_id to map
    assigned_user = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='assigned_appointments',
        to_field='ghl_user_id'
    )
    ghl_assigned_user_id = models.CharField(max_length=255, blank=True, null=True, help_text="GHL assigned user ID")
    
    # Many-to-many for users array
    users = models.ManyToManyField(
        User,
        related_name='appointments',
        blank=True
    )
    users_ghl_ids = models.JSONField(
        default=list,
        blank=True,
        help_text="Store GHL user IDs from users array"
    )
    
    # Backend creation tracking
    created_from_backend = models.BooleanField(
        default=False,
        help_text="True if appointment was created from our backend, False if from GHL"
    )
    
    # Job relationship - one job can create one appointment per assigned technician
    job = models.ForeignKey(
        'jobtracker_app.Job',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='appointment',
        help_text="Related job if appointment was created from a job"
    )
    
    # Timestamps
    start_time = models.DateTimeField(blank=True, null=True)
    end_time = models.DateTimeField(blank=True, null=True)
    date_added = models.DateTimeField(blank=True, null=True)
    date_updated = models.DateTimeField(blank=True, null=True)
    
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    class Meta:
        db_table = 'appointments'
        ordering = ['-start_time', '-created_at']
        indexes = [
            models.Index(fields=['ghl_appointment_id']),
            models.Index(fields=['location_id']),
            models.Index(fields=['start_time']),
        ]
    
    def __str__(self):
        return f"{self.title or 'Appointment'} - {self.ghl_appointment_id}"