# user_models.py - Updated with package selection
from django.db import models
from decimal import Decimal, ROUND_HALF_UP
import uuid
from service_app.models import Service, Package, Location, Question, QuestionOption, SubQuestion, User
from accounts.models import Contact, Address
from django.db.models import Sum




class CustomerSubmission(models.Model):
    """Main customer submission model"""
    STATUS_CHOICES = [
        ('draft', 'Draft'),
        ('responses_completed', 'Responses Completed'),
        ('packages_selected', 'Packages Selected'),
        ('submitted', 'Submitted'),
        ('accepted', 'Accepted'),
        ('rejected', 'Rejected'),
        ('expired', 'Expired'),
    ]
    
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    
    account = models.ForeignKey(
        'accounts.GHLAuthCredentials',
        on_delete=models.CASCADE,
        related_name='customer_submissions',
        null=True,
        blank=True,
        help_text='GHL account this submission belongs to (for multi-account onboarding)',
    )
    
    # Customer Information
    # customer_name = models.CharField(max_length=255)
    # customer_email = models.EmailField()
    # customer_phone = models.CharField(max_length=20)
    # ghl_contact_id = models.CharField(null=True, blank=True, max_length=255)
    contact = models.ForeignKey(Contact, on_delete=models.CASCADE, null=True, blank=True)
    address = models.ForeignKey(Address, on_delete=models.SET_NULL, null=True, blank=True)
    
    # House Information
    house_sqft = models.PositiveIntegerField()
    location = models.ForeignKey(
        Location,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='customer_submissions',
        help_text='Service location; when set, trip_surcharge from this location is applied to the quote.',
    )

    # Submission Details
    selected_services = models.ManyToManyField(Service, through='CustomerServiceSelection')
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='draft')
    
    # User who created/quoted this submission
    quoted_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='quoted_submissions')

    # Persisted snapshot of the original proposal (technician save before client edits)
    is_persisted_snapshot = models.BooleanField(
        default=False,
        help_text='When True, this submission is an immutable copy of the original proposal.',
        null=True,
        blank=True,
    )
    source_submission = models.OneToOneField(
        'self',
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name='persisted_snapshot',
        help_text='Working submission this snapshot was copied from.',
    )
    
    # Pricing Summary (calculated after package selection)
    total_base_price = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('0.00'))
    total_adjustments = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('0.00'))
    total_surcharges = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('0.00'))
    quote_surcharge_applicable = models.BooleanField(default=False)
    custom_service_total=models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('0.00'), null=True, blank=True)
    final_total = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('0.00'))
    additional_data = models.JSONField(default=dict, null=True,blank=True)
    
    # Timestamps
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    expires_at = models.DateTimeField(null=True, blank=True)
    
    class Meta:
        db_table = 'customer_submissions'
        ordering = ['-created_at']

    def save(self, *args, **kwargs):
        """Ensure final_total and custom_service_total are always rounded"""
        if self.custom_service_total is not None:
            print(f"[SAVE] Before rounding custom_service_total: {self.custom_service_total}")
            self.custom_service_total = Decimal(self.custom_service_total).quantize(
                Decimal("1"), rounding=ROUND_HALF_UP
            )
            print(f"[SAVE] After rounding custom_service_total: {self.custom_service_total}")

        if self.final_total is not None:
            print(f"[SAVE] Before rounding final_total: {self.final_total}")
            self.final_total = Decimal(self.final_total).quantize(
                Decimal("1"), rounding=ROUND_HALF_UP
            )
            print(f"[SAVE] After rounding final_total: {self.final_total}")

        super().save(*args, **kwargs)
    #     print("[SAVE] Instance saved successfully\n")


 
    def calculate_final_total(self):
        """Recalculate final_total from base components + only active custom services."""
        # Sum only active custom services
        custom_services_total = self.custom_products.filter(is_active=True).aggregate(
            total=Sum('price')
        )['total'] or 0

        # Ensure Decimal type
        custom_services_total = Decimal(str(custom_services_total))
        self.custom_service_total = custom_services_total

    #     # Use stored submission-level components (these should be kept up-to-date elsewhere)
    #     base_total = (self.total_base_price or Decimal('0.00')) \
    #                 + (self.total_adjustments or Decimal('0.00')) \
    #                 + (self.total_surcharges or Decimal('0.00'))

    #     # Compute final total fresh (do not rely on self.final_total being correct)
    #     final_total = base_total + self.custom_service_total

    #     # Optionally apply global minimum base price if you want:
    #     # global_settings = GlobalBasePrice.objects.first()
    #     # if global_settings:
    #     #     min_price = Decimal(str(global_settings.base_price))
    #     #     if final_total < min_price:
    #     #         final_total = min_price

    #     # Round to nearest whole number (no cents)
    #     final_total = final_total.quantize(Decimal("1"), rounding=ROUND_HALF_UP)

    #     # Assign and save
    #     self.final_total = final_total
        self.save(update_fields=['custom_service_total'])





class QuoteSchedule(models.Model):
    """
    Holds information about a scheduled quote booking for a customer submission.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    
    # One-to-one relationship with CustomerSubmission
    submission = models.OneToOneField(
        CustomerSubmission, 
        on_delete=models.CASCADE, 
        related_name='quote_schedule'
    )

    # Provided fields
    first_time = models.BooleanField(default=True)
    quoted_by = models.CharField(max_length=255)
    scheduled_date = models.DateTimeField(null=True, blank=True)
    is_submitted = models.BooleanField(default=False)

    # Additional useful fields
    notes = models.TextField(blank=True, null=True, help_text="Any internal notes about the booking.")
    created_at = models.DateTimeField(auto_now_add=True)
    appointment_id = models.CharField(max_length=255, null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    def __str__(self):
        return f"Booking for {self.submission.id} on {self.scheduled_date.strftime('%Y-%m-%d')}"
    


class CustomService(models.Model):
    purchase = models.ForeignKey(CustomerSubmission, on_delete=models.CASCADE, related_name='custom_products')
    product_name = models.CharField(max_length=200)
    description = models.TextField(blank=True, null=True)
    is_active = models.BooleanField(default=True)
    price = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('0.00'))




class CustomerServiceSelection(models.Model):
    """Through model for customer service selections"""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    submission = models.ForeignKey(CustomerSubmission, on_delete=models.CASCADE)
    service = models.ForeignKey(Service, on_delete=models.CASCADE)
    
    # Selected package (set after user chooses)
    selected_package = models.ForeignKey(Package, on_delete=models.SET_NULL, null=True, blank=True)
    
    # Service-level pricing summary
    question_adjustments = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal('0.00'))
    surcharge_applicable = models.BooleanField(default=False)
    surcharge_amount = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal('0.00'))
    
    # Final pricing (calculated after package selection)
    final_base_price = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal('0.00'))
    final_sqft_price = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal('0.00'))
    final_total_price = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal('0.00'))
    
    created_at = models.DateTimeField(auto_now_add=True)
    
    class Meta:
        db_table = 'customer_service_selections'
        unique_together = ['submission', 'service']
        ordering = ['service__order', 'service__name']



    def save(self, *args, **kwargs):
        if self.final_total_price is not None:
            # Round to nearest integer before saving
            self.final_total_price = self.final_total_price.quantize(Decimal("1"), rounding=ROUND_HALF_UP)
        super().save(*args, **kwargs)

# Keep all other models the same...
class CustomerQuestionResponse(models.Model):
    """Customer responses to questions"""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    service_selection = models.ForeignKey(CustomerServiceSelection, related_name='question_responses', on_delete=models.CASCADE)
    question = models.ForeignKey(Question, on_delete=models.CASCADE)
    
    # Response data based on question type
    yes_no_answer = models.BooleanField(null=True, blank=True)
    text_answer = models.TextField(null=True, blank=True)
    
    # Pricing impact
    price_adjustment = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal('0.00'))
    
    created_at = models.DateTimeField(auto_now_add=True)
    
    class Meta:
        db_table = 'customer_question_responses'
        unique_together = ['service_selection', 'question']

class CustomerOptionResponse(models.Model):
    """Customer responses to question options"""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    question_response = models.ForeignKey(CustomerQuestionResponse, related_name='option_responses', on_delete=models.CASCADE)
    option = models.ForeignKey(QuestionOption, on_delete=models.CASCADE)
    quantity = models.PositiveIntegerField(default=1)
    
    # Pricing impact
    price_adjustment = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal('0.00'))
    
    created_at = models.DateTimeField(auto_now_add=True)
    
    class Meta:
        db_table = 'customer_option_responses'

class CustomerSubQuestionResponse(models.Model):
    """Customer responses to sub-questions"""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    question_response = models.ForeignKey(CustomerQuestionResponse, related_name='sub_question_responses', on_delete=models.CASCADE)
    sub_question = models.ForeignKey(SubQuestion, on_delete=models.CASCADE)
    answer = models.BooleanField()
    
    # Pricing impact
    price_adjustment = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal('0.00'))
    
    created_at = models.DateTimeField(auto_now_add=True)
    
    class Meta:
        db_table = 'customer_sub_question_responses'

class CustomerPackageQuote(models.Model):
    """Package quotes for customer (all available packages)"""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    service_selection = models.ForeignKey(CustomerServiceSelection, related_name='package_quotes', on_delete=models.CASCADE)
    package = models.ForeignKey(Package, on_delete=models.CASCADE)
    
    # Pricing breakdown
    base_price = models.DecimalField(max_digits=10, decimal_places=2)
    # service_base_price = models.DecimalField(max_digits=10, decimal_places=2,default=Decimal('0.00'), null=True, blank=True)
    sqft_price = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal('0.00'))
    question_adjustments = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal('0.00'))
    surcharge_amount = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal('0.00'))
    total_price = models.DecimalField(max_digits=10, decimal_places=2)
    
    # Features breakdown
    included_features = models.JSONField(default=list)  # List of included feature IDs
    excluded_features = models.JSONField(default=list)  # List of excluded feature IDs
    
    # Selection status
    is_selected = models.BooleanField(default=False)
    
    created_at = models.DateTimeField(auto_now_add=True)
    
    class Meta:
        db_table = 'customer_package_quotes'
        unique_together = ['service_selection', 'package']


    def save(self, *args, **kwargs):
        if self.total_price is not None:
            # Round to nearest integer
            self.total_price = self.total_price.quantize(Decimal("1"), rounding=ROUND_HALF_UP)
        super().save(*args, **kwargs)


class CustomerSubmissionImage(models.Model):
    """Model to store images uploaded for customer submissions"""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    submission = models.ForeignKey(
        CustomerSubmission, 
        on_delete=models.CASCADE, 
        related_name='images'
    )
    image = models.ImageField(upload_to='submission_images/%Y/%m/%d/', blank=True, null=True, help_text="Not used when storing in GHL only")
    caption = models.CharField(
        max_length=255, 
        blank=True, 
        null=True, 
        help_text="Optional caption for the image"
    )
    uploaded_by = models.ForeignKey(
        User, 
        on_delete=models.SET_NULL, 
        null=True, 
        blank=True, 
        related_name='uploaded_submission_images'
    )
    ghl_file_id = models.CharField(max_length=255, blank=True, null=True, help_text="GHL media document ID after upload")
    ghl_file_url = models.URLField(max_length=500, blank=True, null=True, help_text="GHL media file URL after upload")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']
        verbose_name = 'Customer Submission Image'
        verbose_name_plural = 'Customer Submission Images'
        db_table = 'customer_submission_images'

    def __str__(self):
        return f"Image for submission {self.submission.id} - {self.created_at}"
