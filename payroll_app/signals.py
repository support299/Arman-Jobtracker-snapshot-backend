from django.db.models.signals import pre_save, post_save
from django.dispatch import receiver
from django.core.exceptions import ValidationError
from decimal import Decimal
from jobtracker_app.models import Job
from service_app.models import User
from .models import Payout, PayrollSettings, EmployeeProfile, CollaborationRate
from .utils import is_first_time_bonus_eligible


@receiver(pre_save, sender=Job)
def prevent_job_status_change_after_completion(sender, instance, **kwargs):
    """Prevent changing status once job is marked as completed"""
    if instance.pk:  # Only for updates
        try:
            old_instance = Job.objects.get(pk=instance.pk)
            # If job was already completed, prevent status change
            if old_instance.status == 'completed' and instance.status != 'completed':
                raise ValidationError(
                    "Cannot change status of a completed job. "
                    "Once a job is completed, its status cannot be modified."
                )
        except Job.DoesNotExist:
            pass


@receiver(pre_save, sender=Job)
def create_project_payouts_on_completion(sender, instance, **kwargs):
    """Create payouts when job status changes to completed"""
    if instance.pk:  # Only for updates
        try:
            old_instance = Job.objects.get(pk=instance.pk)
            # Check if status changed to 'completed'
            if old_instance.status != 'completed' and instance.status == 'completed':
                # Check if completion was already processed (prevent duplicate calls)
                if old_instance.completion_processed:
                    print(f"⚠️ Job {instance.id} completion was already processed. Skipping payout creation.")
                    return
                
                # Additional check: if payouts already exist, skip (prevent duplicate calls)
                if Payout.objects.filter(job=instance).exists():
                    print(f"⚠️ Job {instance.id} payouts already exist. Skipping payout creation.")
                    return
                
                _create_project_payouts(instance)
        except Job.DoesNotExist:
            pass


def _create_project_payouts(job):
    """
    Create payouts for all assigned employees and quoted_by person.
    
    Logic:
    1. For each assigned employee (project-based only):
       - Get their collaboration rate for the team size
       - Create a 'project' payout based on their individual rate
    2. For the quoted_by person:
       - Create a bonus payout (first_time or quoted_by bonus)
       - one_time jobs always get first_time rate; recurring jobs get first_time
         rate only on the first completed occurrence in the series
       - This is separate from assignee payouts, so if quoted_by is also
         an assignee, they get BOTH payouts
    """
    # Get assigned employees (when job has account, only include users in that account)
    assignments = job.assignments.all().select_related('user')
    job_account_id = getattr(job, 'account_id', None)
    if job_account_id:
        assignments = [a for a in assignments if getattr(a.user, 'account_id', None) == job_account_id]
    else:
        assignments = list(assignments)

    if not assignments:
        return  # No employees assigned (or none in job's account), skip payout creation
    
    # Get project value
    project_value = job.total_price or Decimal('0.00')
    
    if project_value <= 0:
        return  # No value, skip payout creation
    
    # Get payroll settings for the job's account (so bonus % etc. are account-specific)
    settings = PayrollSettings.get_settings(account=getattr(job, 'account', None))
    
    # Determine if quoted-by gets first-time bonus % (one_time, or first completed in series)
    is_first_time = is_first_time_bonus_eligible(job)
    
    # Get number of assigned employees (in job's account)
    employee_count = len(assignments)
    
    # Step 1: Create payouts for each assigned employee (based on their collaboration rates)
    for assignment in assignments:
        employee = assignment.user
        
        # Check if employee has profile and is project-based
        try:
            profile = employee.employee_profile
            if profile.pay_scale_type != 'project':
                continue  # Skip hourly employees (they don't get project payouts)
        except EmployeeProfile.DoesNotExist:
            continue  # Skip employees without profile
        
        # Check if payout already exists (duplicate prevention)
        existing_payout = Payout.objects.filter(
            job=job,
            employee=employee,
            payout_type='project'
        ).first()
        
        if existing_payout:
            continue  # Payout already exists, skip
        
        # Get collaboration rate for this team size
        try:
            collaboration_rate = CollaborationRate.objects.get(
                employee=employee,
                member_count=employee_count
            )
            rate_percentage = collaboration_rate.percentage
        except CollaborationRate.DoesNotExist:
            # If no rate found for this team size, skip this employee
            continue
        
        # Calculate payout amount based on employee's individual rate
        # Each employee gets their own percentage of the project value
        amount = (project_value * rate_percentage) / Decimal('100')
        amount = amount.quantize(Decimal('0.01'))
        
        # Create project payout for this assignee
        Payout.objects.create(
            account=job.account,
            employee=employee,
            payout_type='project',
            amount=amount,
            job=job,
            project_value=project_value,
            rate_percentage=rate_percentage,
            notes=f"Automated project payout for assignee: {job.title or job.id} (Rate: {rate_percentage}% for {employee_count} members)"
        )
    
    # Step 2: Create bonus payout for quoted_by person (separate from assignee payouts)
    # When job has account, only create if quoted_by is in the same account
    if job.quoted_by:
        quoted_by_employee = job.quoted_by
        if job_account_id and getattr(quoted_by_employee, 'account_id', None) != job_account_id:
            quoted_by_employee = None  # Skip bonus for cross-account quoted_by
    else:
        quoted_by_employee = None
    if quoted_by_employee:
        # Determine bonus type
        bonus_type = 'bonus_first_time' if is_first_time else 'bonus_quoted_by'
        
        # Check if bonus payout already exists (duplicate prevention)
        existing_bonus = Payout.objects.filter(
            job=job,
            employee=quoted_by_employee,
            payout_type=bonus_type
        ).first()
        
        if not existing_bonus:
            # Get bonus percentage from settings
            if is_first_time:
                bonus_percentage = settings.first_time_bonus_percentage
            else:
                bonus_percentage = settings.quoted_by_bonus_percentage
            
            # Calculate bonus amount
            bonus_amount = (project_value * bonus_percentage) / Decimal('100')
            bonus_amount = bonus_amount.quantize(Decimal('0.01'))
            
            # Create bonus payout for quoted_by person
            # Note: If quoted_by is also an assignee, they will have TWO payouts:
            # 1. Their assignee payout (created above)
            # 2. This bonus payout
            Payout.objects.create(
                account=job.account,
                employee=quoted_by_employee,
                payout_type=bonus_type,
                amount=bonus_amount,
                job=job,
                project_value=project_value,
                rate_percentage=bonus_percentage,
                notes=f"Automated {bonus_type} bonus for quoted_by: {job.title or job.id}"
            )


@receiver(post_save, sender=User)
def create_employee_profile(sender, instance, created, **kwargs):
    """Automatically create EmployeeProfile when a User is created"""
    if created:
        from payroll_app.utils import ensure_employee_profile_for_user

        ensure_employee_profile_for_user(instance, account=getattr(instance, "account", None))

