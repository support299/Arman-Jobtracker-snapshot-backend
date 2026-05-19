from rest_framework import viewsets, status
from rest_framework.decorators import action
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from django.db.models import Q, Sum, Count
from django.db.models.functions import TruncDate, TruncWeek, TruncMonth
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from datetime import datetime, timedelta, time, date
import pytz

from django_filters import rest_framework as filters
from django.contrib.auth import get_user_model

from accounts.permissions import AccountScopedPermission
from accounts.mixins import AccountScopedQuerysetMixin
from accounts.timezone_utils import get_pytz_for_request
from jobtracker_app.models import Job
from jobtracker_app.views import resolve_user_identifier
from accounts.models import Contact
from quote_app.models import CustomerSubmission

from .models import Invoice, InvoiceItem
from .serializers import InvoiceSerializer, InvoiceDetailSerializer, InvoiceItemSerializer
from .services.invoice_sync import sync_invoices


class InvoiceFilter(filters.FilterSet):
    """Filter class for Invoice model"""
    
    search = filters.CharFilter(method='filter_search')
    # Extended choices to include calculated statuses (due, overdue)
    status_choices = list(Invoice.STATUS_CHOICES) + [('due', 'Due'), ('overdue', 'Overdue')]
    status = filters.MultipleChoiceFilter(method='filter_status', choices=status_choices)
    
    issue_date_from = filters.DateTimeFilter(field_name='issue_date', lookup_expr='gte')
    issue_date_to = filters.DateTimeFilter(field_name='issue_date', lookup_expr='lte')
    due_date_from = filters.DateTimeFilter(field_name='due_date', lookup_expr='gte')
    due_date_to = filters.DateTimeFilter(field_name='due_date', lookup_expr='lte')
    created_date_from = filters.DateTimeFilter(field_name='created_at', lookup_expr='gte')
    created_date_to = filters.DateTimeFilter(field_name='created_at', lookup_expr='lte')
    
    total_min = filters.NumberFilter(field_name='total', lookup_expr='gte')
    total_max = filters.NumberFilter(field_name='total', lookup_expr='lte')
    amount_due_min = filters.NumberFilter(field_name='amount_due', lookup_expr='gte')
    amount_due_max = filters.NumberFilter(field_name='amount_due', lookup_expr='lte')
    
    contact_id = filters.CharFilter(field_name='contact_id')
    contact_email = filters.CharFilter(field_name='contact_email', lookup_expr='icontains')
    contact_name = filters.CharFilter(field_name='contact_name', lookup_expr='icontains')
    
    location_id = filters.CharFilter(field_name='location_id')
    company_id = filters.CharFilter(field_name='company_id')
    
    is_overdue = filters.BooleanFilter(method='filter_overdue')
    is_paid = filters.BooleanFilter(method='filter_paid')
    has_balance = filters.BooleanFilter(method='filter_has_balance')
    
    class Meta:
        model = Invoice
        fields = ['status', 'location_id', 'company_id', 'contact_id', 'invoice_number', 'currency']
    
    def filter_search(self, queryset, name, value):
        return queryset.filter(
            Q(invoice_number__icontains=value) |
            Q(name__icontains=value) |
            Q(contact_name__icontains=value) |
            Q(contact_email__icontains=value) |
            Q(contact_phone__icontains=value)
        )
    
    def filter_status(self, queryset, name, value):
        """
        Custom status filter that handles both database statuses and calculated statuses (due, overdue).
        """
        from django.utils import timezone
        now = timezone.now()
        
        if not value:
            return queryset
        
        # Handle multiple status values
        status_filters = Q()
        has_due = False
        has_overdue = False
        regular_statuses = []
        
        for status_val in value:
            if status_val == 'due':
                has_due = True
            elif status_val == 'overdue':
                has_overdue = True
            else:
                regular_statuses.append(status_val)
        
        # Build the combined filter
        if regular_statuses:
            status_filters |= Q(status__in=regular_statuses)
        
        if has_due:
            # Due: invoices with due_date >= today, amount_due > 0, status not paid/void
            status_filters |= Q(
                due_date__gte=now,
                amount_due__gt=0,
            ) & ~Q(status__in=['paid', 'void'])
        
        if has_overdue:
            # Overdue: invoices with due_date < today, amount_due > 0, status not paid/void
            status_filters |= Q(
                due_date__lt=now,
                amount_due__gt=0,
            ) & ~Q(status__in=['paid', 'void'])
        
        return queryset.filter(status_filters)
    
    def filter_overdue(self, queryset, name, value):
        from django.utils import timezone
        if value:
            return queryset.filter(
                due_date__lt=timezone.now(),
                amount_due__gt=0
            ).exclude(status__in=['paid', 'void'])
        return queryset.exclude(due_date__lt=timezone.now(), amount_due__gt=0)
    
    def filter_paid(self, queryset, name, value):
        if value:
            return queryset.filter(status='paid', amount_due=0)
        return queryset.exclude(status='paid')
    
    def filter_has_balance(self, queryset, name, value):
        if value:
            return queryset.filter(amount_due__gt=0)
        return queryset.filter(amount_due=0)


class InvoiceViewSet(AccountScopedQuerysetMixin, viewsets.ModelViewSet):
    """ViewSet for Invoice model (scoped to current account)."""
    queryset = Invoice.objects.all().prefetch_related('items')
    serializer_class = InvoiceSerializer
    permission_classes = [AccountScopedPermission, IsAuthenticated]
    account_lookup = "account"
    filterset_class = InvoiceFilter
    ordering_fields = ['created_at', 'updated_at', 'issue_date', 'due_date', 'total', 'amount_due', 'invoice_number', 'status']
    ordering = ['-created_at']
    search_fields = ['invoice_number', 'contact_name', 'contact_email']
    
    def get_serializer_class(self):
        if self.action == 'retrieve':
            return InvoiceDetailSerializer
        return InvoiceSerializer
    
    @action(detail=False, methods=['post'])
    def sync(self, request):
        """Sync invoices from GHL API (uses request.account or location_id in body)."""
        location_id = request.data.get('location_id')
        invoice_id = request.data.get('invoice_id')
        account = getattr(request, 'account', None)
        
        if not location_id and account:
            location_id = getattr(account, 'location_id', None)
        if not location_id:
            return Response({'error': 'location_id is required (or authenticate with an account)'}, status=status.HTTP_400_BAD_REQUEST)
        
        try:
            result = sync_invoices(location_id, invoice_id)
            
            if invoice_id:
                if result:
                    serializer = self.get_serializer(result)
                    return Response({'message': 'Invoice synced successfully', 'invoice': serializer.data})
                else:
                    return Response({'error': 'Failed to sync invoice'}, status=status.HTTP_400_BAD_REQUEST)
            else:
                return Response({'message': 'Invoices synced successfully', 'statistics': result})
        
        except ValueError as e:
            return Response({'error': str(e)}, status=status.HTTP_400_BAD_REQUEST)
        except Exception as e:
            return Response({'error': f'Sync failed: {str(e)}'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
    
    @action(detail=False, methods=['get'])
    def statistics(self, request):
        """Get invoice statistics (scoped to current account)."""
        queryset = self.filter_queryset(self.get_queryset())
        
        location_id = request.query_params.get('location_id')
        if location_id:
            queryset = queryset.filter(location_id=location_id)
        
        date_from = request.query_params.get('date_from')
        if date_from:
            queryset = queryset.filter(created_at__gte=parse_datetime(date_from))
        
        date_to = request.query_params.get('date_to')
        if date_to:
            queryset = queryset.filter(created_at__lte=parse_datetime(date_to))
        
        stats = queryset.aggregate(
            total_invoices=Count('id'),
            total_amount=Sum('total'),
            total_paid=Sum('amount_paid'),
            total_due=Sum('amount_due')
        )
        
        status_breakdown = {}
        for choice_value, choice_label in Invoice.STATUS_CHOICES:
            count = queryset.filter(status=choice_value).count()
            status_breakdown[choice_value] = {'count': count, 'label': choice_label}
        
        from django.utils import timezone
        overdue_count = queryset.filter(
            due_date__lt=timezone.now(),
            amount_due__gt=0
        ).exclude(status__in=['paid', 'void']).count()
        
        return Response({
            'statistics': stats,
            'status_breakdown': status_breakdown,
            'overdue_count': overdue_count
        })


    @action(detail=False, methods=['get'])
    def analytics(self, request):
        """
        Comprehensive invoice analytics endpoint.
        Returns summarized and trend data (daily/weekly/monthly).
        """
        queryset = self.filter_queryset(self.get_queryset())

        central_tz = get_pytz_for_request(request)

        # === Query Params ===
        start_date = request.query_params.get("start_date")
        end_date = request.query_params.get("end_date")
        granularity = request.query_params.get("granularity", "daily")  # daily | weekly | monthly
        location_id = request.query_params.get("location_id")

        if location_id:
            queryset = queryset.filter(location_id=location_id)

        if start_date:
            start_date = parse_datetime(start_date)
            queryset = queryset.filter(created_at__gte=start_date)
        if end_date:
            end_date = parse_datetime(end_date)
            queryset = queryset.filter(created_at__lte=end_date)
        else:
            end_date = timezone.now().astimezone(central_tz)

        # === Base Stats ===
        total_invoices = queryset.count()
        total_amount = queryset.aggregate(Sum("total"))["total__sum"] or 0
        total_paid = queryset.aggregate(Sum("amount_paid"))["amount_paid__sum"] or 0
        total_due = queryset.aggregate(Sum("amount_due"))["amount_due__sum"] or 0

        overdue_qs = queryset.filter(
            # due_date__lt=timezone.now().astimezone(central_tz),
            amount_due__gt=0,
            status__in=['overdue']
        )
        # .exclude(status__in=["paid", "void"])
        overdue_count = overdue_qs.count()
        overdue_total = overdue_qs.aggregate(Sum("amount_due"))["amount_due__sum"] or 0

        # === Paid vs Unpaid vs Payment Processing ===
        paid_count = queryset.filter(status__in=["paid"]).count()
        payment_processing_count = queryset.filter(status="payment_processing").count()
        payment_processing_total = queryset.filter(status="payment_processing").aggregate(Sum("total"))["total__sum"] or 0
        unpaid_count = queryset.exclude(status__in=["paid", "payment_processing"]).count()

        paid_total = queryset.filter(status__in=["paid"]).aggregate(Sum("total"))["total__sum"] or 0
        unpaid_total = queryset.exclude(status__in=["paid", "payment_processing"]).aggregate(Sum("total"))["total__sum"] or 0

        # === Status Distribution ===
        status_distribution = {}
        now = timezone.now().astimezone(central_tz)
        
        # Calculate Due and Overdue dynamically based on due_date
        # Due: invoices with due_date >= today and amount_due > 0, status not paid/void
        # Include all statuses except 'paid' and 'void' (e.g., sent, partially_paid, payment_processing, draft)
        due_queryset = queryset.filter(
            due_date__gte=now,
            amount_due__gt=0,
            status__in=['sent']
        ).exclude(status__in=['paid', 'void', 'overdue', 'partially_paid', 'partial','payment_processing','draft'])
        due_count = due_queryset.count()
        due_total = due_queryset.aggregate(Sum("amount_due"))["amount_due__sum"] or 0
        status_distribution["due"] = {
            "label": "Due",
            "count": due_count,
            "total": float(due_total),
        }
        
        # Overdue: invoices with due_date < today and amount_due > 0, status not paid/void
        # Include all statuses except 'paid' and 'void'
        overdue_queryset = queryset.filter(
            due_date__lt=now,
            amount_due__gt=0,
            status__in=['sent','overdue']
        )
        # .exclude(status__in=['paid', 'void', 'partially_paid', 'partial'])
        overdue_count = overdue_queryset.count()
        overdue_total = overdue_queryset.aggregate(Sum("amount_due"))["amount_due__sum"] or 0
        status_distribution["overdue"] = {
            "label": "Overdue",
            "count": overdue_count,
            "total": overdue_total,
        }
        
        # Keep other statuses from STATUS_CHOICES (excluding 'overdue' since we calculate it dynamically)
        for value, label in Invoice.STATUS_CHOICES:
            if value != 'overdue':  # Skip 'overdue' as we calculate it dynamically
                count = queryset.filter(status=value).count()
                amount = queryset.filter(status=value).aggregate(Sum("total"))["total__sum"] or 0
                status_distribution[value] = {
                    "label": label,
                    "count": count,
                    "total": amount,
                }

        # === Grouping by Time (Trends) ===
        if granularity == "weekly":
            date_trunc = TruncWeek("created_at")
        elif granularity == "monthly":
            date_trunc = TruncMonth("created_at")
        else:
            date_trunc = TruncDate("created_at")

        trends = (
            queryset.annotate(period=date_trunc)
            .values("period")
            .annotate(
                total_invoices=Count("id"),
                total_amount=Sum("total"),
                total_paid=Sum("amount_paid"),
                total_due=Sum("amount_due"),
                paid_count=Count("id", filter=Q(status="paid")),
                payment_processing_count=Count("id", filter=Q(status="payment_processing")),
                payment_processing_total=Sum("total", filter=Q(status="payment_processing")),
                unpaid_count=Count("id", filter=~Q(status__in=["paid", "payment_processing"])),
                unpaid_total=Sum("total", filter=~Q(status__in=["paid", "payment_processing"])),
            )
            .order_by("period")
        )
        # Serialize trends for JSON: period as ISO string, decimals as float
        trends_data = []
        for row in trends:
            period = row["period"]
            period_str = period.isoformat() if hasattr(period, "isoformat") else str(period)
            trends_data.append({
                "period": period_str,
                "total_invoices": row["total_invoices"],
                "total_amount": float(row["total_amount"] or 0),
                "total_paid": float(row["total_paid"] or 0),
                "total_due": float(row["total_due"] or 0),
                "paid_count": row["paid_count"],
                "payment_processing_count": row["payment_processing_count"],
                "payment_processing_total": float(row["payment_processing_total"] or 0),
                "unpaid_count": row["unpaid_count"],
                "unpaid_total": float(row["unpaid_total"] or 0),
            })

        # === Top Customers (by total invoiced) ===
        top_customers = (
            queryset.values("contact_name", "contact_email")
            .annotate(
                total_invoiced=Sum("total"),
                invoices_count=Count("id"),
                total_paid=Sum("amount_paid"),
            )
            .order_by("-total_invoiced")[:5]
        )

        # === Response ===
        return Response({
            "summary": {
                "total_invoices": total_invoices,
                "total_amount": float(total_amount),
                "total_paid": float(total_paid),
                "total_due": float(total_due),
                "overdue_count": overdue_count,
                "overdue_total": float(overdue_total),
                "payment_processing_count": payment_processing_count,
                "payment_processing_total": float(payment_processing_total),
            },
            "paid_unpaid_overview": {
                "paid": {"count": paid_count, "total": float(paid_total)},
                "payment_processing": {"count": payment_processing_count, "total": float(payment_processing_total)},
                "unpaid": {"count": unpaid_count, "total": float(unpaid_total)},
            },
            "status_distribution": status_distribution,
            "trends": trends_data,
            "top_customers": list(top_customers),
        })
    
    @action(detail=True, methods=['get'])
    def items(self, request, pk=None):
        """Get all items for a specific invoice"""
        invoice = self.get_object()
        items = invoice.items.all()
        serializer = InvoiceItemSerializer(items, many=True)
        return Response(serializer.data)
    
    @action(detail=False, methods=['get'])
    def lead_funnel_report(self, request):
        """
        Lead Funnel Report endpoint (scoped to current account).
        Returns comprehensive metrics for the sales funnel including leads, estimates, and jobs.
        Supports date range filter via start_date and end_date (default: current year).
        Optional assignee_ids: comma-separated user ids, UUIDs, or emails (same as calendar / sales_forecasting).
        When omitted, metrics include all assignees.
        """
        account = getattr(request, 'account', None)
        if not account:
            return Response({'error': 'Account context is required.'}, status=status.HTTP_403_FORBIDDEN)
        
        location_id = request.query_params.get('location_id')
        # Optional location filter within account
        location_filter = {}
        if location_id:
            location_filter['location_id'] = location_id

        User = get_user_model()
        assignee_ids_param = request.query_params.get('assignee_ids')
        assignee_user_ids = []
        if assignee_ids_param:
            for assignee in (a.strip() for a in assignee_ids_param.split(',') if a.strip()):
                uid = resolve_user_identifier(assignee)
                if uid is not None:
                    assignee_user_ids.append(uid)
        if assignee_user_ids:
            valid_ids = set(
                User.objects.filter(account=account).exclude(is_superuser=True).values_list('id', flat=True)
            )
            assignee_user_ids = [uid for uid in assignee_user_ids if uid in valid_ids]
        
        now = timezone.now()
        # Date range filter: default current year (01-01 to 31-12)
        start_date_param = request.query_params.get('start_date')
        end_date_param = request.query_params.get('end_date')
        try:
            if start_date_param:
                start_dt = timezone.make_aware(
                    datetime.strptime(start_date_param[:10], '%Y-%m-%d'),
                    timezone.get_current_timezone()
                )
            else:
                start_dt = timezone.make_aware(datetime(now.year, 1, 1), timezone.get_current_timezone())
            if end_date_param:
                end_dt = timezone.make_aware(
                    datetime.strptime(end_date_param[:10], '%Y-%m-%d') + timedelta(days=1),
                    timezone.get_current_timezone()
                ) - timedelta(microseconds=1)
            else:
                end_dt = timezone.make_aware(datetime(now.year, 12, 31, 23, 59, 59, 999999), timezone.get_current_timezone())
        except (ValueError, TypeError):
            start_dt = timezone.make_aware(datetime(now.year, 1, 1), timezone.get_current_timezone())
            end_dt = timezone.make_aware(datetime(now.year, 12, 31, 23, 59, 59, 999999), timezone.get_current_timezone())

        filter_description = 'Date range filter applied to all counts (default: current year)'
        if assignee_user_ids:
            filter_description += '; assignee_ids filter applied to leads, estimates, and jobs'
        
        # 1. New Lead Count (contacts created in date range) — scoped to account
        contacts_qs = Contact.objects.filter(account=account)
        if location_filter:
            contacts_qs = contacts_qs.filter(**location_filter)
        if assignee_user_ids:
            sc_for_leads = CustomerSubmission.objects.filter(
                account=account,
                created_at__gte=start_dt,
                created_at__lte=end_dt,
            )
            if location_filter:
                sc_for_leads = sc_for_leads.filter(contact__location_id=location_filter['location_id'])
            sc_for_leads = sc_for_leads.filter(
                Q(quoted_by_id__in=assignee_user_ids)
                | Q(jobs__assignments__user_id__in=assignee_user_ids, jobs__account=account)
            )
            jobs_for_leads = Job.objects.filter(
                account=account,
                created_at__gte=start_dt,
                created_at__lte=end_dt,
                assignments__user_id__in=assignee_user_ids,
            )
            if location_filter:
                jobs_for_leads = jobs_for_leads.filter(contact__location_id=location_filter['location_id'])
            contact_ids = set(sc_for_leads.values_list('contact_id', flat=True)) | set(
                jobs_for_leads.values_list('contact_id', flat=True)
            )
            contact_ids.discard(None)
            new_leads_count = contacts_qs.filter(
                date_added__gte=start_dt,
                date_added__lte=end_dt,
                pk__in=contact_ids,
            ).count()
        else:
            new_leads_count = contacts_qs.filter(
                date_added__gte=start_dt,
                date_added__lte=end_dt
            ).count()
        
        # 2–5. Estimates — scoped to account; assignee = quoted_by OR any linked job assignment
        open_statuses = ['draft', 'responses_completed', 'packages_selected']
        submissions_qs = CustomerSubmission.objects.filter(account=account)
        if location_filter:
            submissions_qs = submissions_qs.filter(contact__location_id=location_filter['location_id'])
        submissions_qs = submissions_qs.filter(created_at__gte=start_dt, created_at__lte=end_dt)
        if assignee_user_ids:
            submissions_qs = submissions_qs.filter(
                Q(quoted_by_id__in=assignee_user_ids)
                | Q(jobs__assignments__user_id__in=assignee_user_ids, jobs__account=account)
            )
        submissions_scoped = CustomerSubmission.objects.filter(id__in=submissions_qs.values('id').distinct())
        open_estimate_count = submissions_scoped.filter(status__in=open_statuses).count()
        
        # 3. Rejected Estimate (status: rejected)
        rejected_estimate_count = submissions_scoped.filter(status='rejected').count()
        rejected_estimates = submissions_scoped.filter(status='rejected')
        rejected_estimate_total_value = rejected_estimates.aggregate(Sum('final_total'))['final_total__sum'] or 0
        
        # 4. Accepted Estimate (quote status = submitted — when customer accepts/signs)
        accepted_estimate_count = submissions_scoped.filter(status='submitted').count()
        accepted_estimates = submissions_scoped.filter(status='submitted')
        accepted_estimate_total_value = accepted_estimates.aggregate(Sum('final_total'))['final_total__sum'] or 0
        
        # 5. Scheduled Quotes (submissions with status: accepted, within date range)
        scheduled_quotes_qs = submissions_scoped.filter(status='accepted')
        scheduled_quotes_count = scheduled_quotes_qs.count()
        scheduled_quotes_total_value = scheduled_quotes_qs.aggregate(Sum('final_total'))['final_total__sum'] or 0
        
        # Jobs queryset — scoped to account (Job.account), optional location via contact
        jobs_qs = Job.objects.filter(account=account, created_at__gte=start_dt, created_at__lte=end_dt)
        if location_filter:
            jobs_qs = jobs_qs.filter(contact__location_id=location_filter['location_id'])
        if assignee_user_ids:
            jobs_qs = jobs_qs.filter(assignments__user_id__in=assignee_user_ids)
        jobs_scoped = Job.objects.filter(id__in=jobs_qs.values('id').distinct())
        
        # 6. Estimate to Convert (jobs with status: to_convert)
        estimate_to_convert_jobs_qs = jobs_scoped.filter(status='to_convert')
        estimate_to_convert_count = estimate_to_convert_jobs_qs.count()
        estimate_to_convert_total_value = estimate_to_convert_jobs_qs.aggregate(Sum('total_price'))['total_price__sum'] or 0
        
        # 7. Scheduled Jobs (pending, confirmed, on_the_way, service_due) — upcoming jobs from now to end_date
        scheduled_job_statuses = ['pending', 'confirmed', 'on_the_way', 'service_due']
        scheduled_jobs_qs = Job.objects.filter(
            account=account,
            status__in=scheduled_job_statuses,
            scheduled_at__isnull=False,
            scheduled_at__gte=now,
            scheduled_at__lte=end_dt
        )
        if location_filter:
            scheduled_jobs_qs = scheduled_jobs_qs.filter(contact__location_id=location_filter['location_id'])
        if assignee_user_ids:
            scheduled_jobs_qs = scheduled_jobs_qs.filter(assignments__user_id__in=assignee_user_ids)
        scheduled_jobs_scoped = Job.objects.filter(id__in=scheduled_jobs_qs.values('id').distinct())
        scheduled_job_count = scheduled_jobs_scoped.count()
        scheduled_job_total_value = scheduled_jobs_scoped.aggregate(Sum('total_price'))['total_price__sum'] or 0
        
        # 8. In Progress Jobs
        in_progress_jobs_qs = jobs_scoped.filter(status='in_progress')
        in_progress_job_count = in_progress_jobs_qs.count()
        in_progress_job_total_value = in_progress_jobs_qs.aggregate(Sum('total_price'))['total_price__sum'] or 0
        
        # 9. Cancelled Jobs
        cancelled_jobs_qs = jobs_scoped.filter(status='cancelled')
        cancelled_job_count = cancelled_jobs_qs.count()
        cancelled_job_total_value = cancelled_jobs_qs.aggregate(Sum('total_price'))['total_price__sum'] or 0
        
        # 10. Closed Jobs (status: completed)
        closed_jobs_qs = jobs_scoped.filter(status='completed')
        closed_job_count = closed_jobs_qs.count()
        closed_job_total_value = closed_jobs_qs.aggregate(Sum('total_price'))['total_price__sum'] or 0
        
        # Summary metrics (pipeline: open estimates + scheduled jobs; no amount on open estimates)
        pipeline_value = float(scheduled_job_total_value)
        # Acceptance rate = (Accepted Estimates (submitted) + Scheduled Quotes) / all estimates in range
        # Denominator: open + rejected + accepted (submitted) + scheduled = all estimates we're showing
        total_estimates = open_estimate_count + rejected_estimate_count + accepted_estimate_count + scheduled_quotes_count
        accepted_count = accepted_estimate_count + scheduled_quotes_count
        acceptance_rate = (accepted_count / total_estimates * 100) if total_estimates > 0 else 0
        rejection_rate = (rejected_estimate_count / total_estimates * 100) if total_estimates > 0 else 0
        
        return Response({
            'report_period': {
                'start_date': start_dt.date().isoformat(),
                'end_date': end_dt.date().isoformat(),
                'filter_description': filter_description,
                'assignee_user_ids': assignee_user_ids if assignee_user_ids else None,
            },
            'lead_funnel': {
                'new_leads': {
                    'count': new_leads_count,
                    'label': 'New Leads'
                },
                'open_estimates': {
                    'count': open_estimate_count,
                    'label': 'Open Estimates',
                    'statuses': open_statuses
                },
                'rejected_estimates': {
                    'count': rejected_estimate_count,
                    'total_value': float(rejected_estimate_total_value),
                    'label': 'Rejected Estimates'
                },
                'accepted_estimates': {
                    'count': accepted_estimate_count,
                    'total_value': float(accepted_estimate_total_value),
                    'label': 'Accepted Estimates (Submitted)',
                    'status': 'submitted'
                },
                'scheduled_quotes': {
                    'count': scheduled_quotes_count,
                    'total_value': float(scheduled_quotes_total_value),
                    'label': 'Scheduled Quotes (Accepted)',
                    'status': 'accepted'
                },
                'estimate_to_convert': {
                    'count': estimate_to_convert_count,
                    'total_value': float(estimate_to_convert_total_value),
                    'label': 'Estimate to Convert'
                },
                'scheduled_jobs': {
                    'count': scheduled_job_count,
                    'total_value': float(scheduled_job_total_value),
                    'label': 'Scheduled Jobs (Upcoming through end date)',
                    'statuses': scheduled_job_statuses,
                    'scheduled_at_range': {'from': now.isoformat(), 'to': end_dt.isoformat()}
                },
                'in_progress_jobs': {
                    'count': in_progress_job_count,
                    'total_value': float(in_progress_job_total_value),
                    'label': 'In Progress Jobs'
                },
                'cancelled_jobs': {
                    'count': cancelled_job_count,
                    'total_value': float(cancelled_job_total_value),
                    'label': 'Cancelled Jobs'
                },
                'closed_jobs': {
                    'count': closed_job_count,
                    'total_value': float(closed_job_total_value),
                    'label': 'Closed/Completed Jobs'
                }
            },
            'summary_metrics': {
                'pipeline_value': pipeline_value,
                'total_pipeline_items': open_estimate_count + scheduled_job_count,
                'acceptance_rate_percent': round(acceptance_rate, 2),
                'rejection_rate_percent': round(rejection_rate, 2),
                'total_revenue_closed_jobs': float(closed_job_total_value)
            }
        })
    
    @action(detail=False, methods=['get'])
    def sales_forecasting(self, request):
        """
        Sales Forecasting endpoint (Job-based).
        Timeline: Previous 3 months (Actual) + Next 6 months (Forecast).

        Forecast is fixed once a month begins:
        - baseline_scheduled_revenue = jobs scheduled in that calendar month with qualifying statuses
          that existed before the first day of that month (created_at < month start). This approximates
          the schedule as-of month open; jobs created later are \"bonus\" and do not change the forecast.
        - historical_average = mean completed revenue for the same calendar month in the prior 5 years
          (target year excluded, so the target month is not blended with its own actuals).
        - forecast = historical_average + baseline_scheduled_revenue (locked after month start).

        Months not yet started use the same formula with all currently scheduled jobs in that month as a
        provisional forecast until the month locks.

        Actual = all completed job revenue in the month (includes jobs added mid-month). Variance = actual - forecast.
        Data source: Job table only (no Invoice).
        Optional assignee_ids: same as calendar filter. Scoped to request.account.
        """
        account = getattr(request, 'account', None)
        if not account:
            return Response({'error': 'Account context is required.'}, status=status.HTTP_403_FORBIDDEN)
        location_id = request.query_params.get('location_id')
        location_filter = {}
        if location_id:
            location_filter['location_id'] = location_id

        # Optional assignee filter (same as calendar occurrences); exclude superusers
        User = get_user_model()
        assignee_ids_param = request.query_params.get('assignee_ids')
        assignee_user_ids = []
        if assignee_ids_param:
            for assignee in (a.strip() for a in assignee_ids_param.split(',') if a.strip()):
                uid = resolve_user_identifier(assignee)
                if uid is not None:
                    assignee_user_ids.append(uid)
        if assignee_user_ids:
            valid_ids = set(User.objects.filter(account=account).exclude(is_superuser=True).values_list('id', flat=True))
            assignee_user_ids = [uid for uid in assignee_user_ids if uid in valid_ids]

        now = timezone.now()
        current_year, current_month = now.year, now.month

        def jobs_base():
            qs = Job.objects.filter(account=account)
            if location_filter:
                qs = qs.filter(contact__location_id=location_filter['location_id'])
            if assignee_user_ids:
                qs = qs.filter(assignments__user_id__in=assignee_user_ids).distinct()
            return qs

        def first_day_tz(y, m):
            return timezone.make_aware(datetime(y, m, 1), timezone.get_current_timezone())

        def last_day_tz(y, m):
            if m == 12:
                next_first = datetime(y + 1, 1, 1)
            else:
                next_first = datetime(y, m + 1, 1)
            return timezone.make_aware(next_first, timezone.get_current_timezone()) - timedelta(microseconds=1)

        # Historical: average of same month completed revenue over the 5 calendar years before the target year.
        # The target year is excluded so the month's forecast is not mixed with that year's same-month actuals.
        YEARS_BACK = 5

        def historical_average_for_year_month(target_year, month_num):
            years_included = [yy for yy in range(target_year - YEARS_BACK, target_year) if yy >= 2000]
            if not years_included:
                return 0.0
            totals = []
            for yy in years_included:
                start = first_day_tz(yy, month_num)
                end = last_day_tz(yy, month_num)
                agg = (
                    jobs_base()
                    .filter(status='completed', scheduled_at__isnull=False, scheduled_at__gte=start, scheduled_at__lte=end)
                    .aggregate(s=Sum('total_price'))
                )
                val = agg['s']
                totals.append(float(val) if val is not None else 0.0)
            return sum(totals) / len(years_included)

        # Scheduled revenue for (year, month): only jobs scheduled for that month with status in:
        # pending, confirmed, on_the_way, completed, in_progress, service_due. Exclude cancelled and to_convert.
        SCHEDULED_STATUSES = ['pending', 'confirmed', 'on_the_way', 'completed', 'in_progress', 'service_due']

        def scheduled_aggregate_for_month(year, month, created_before=None):
            start = first_day_tz(year, month)
            end = last_day_tz(year, month)
            qs = jobs_base().filter(
                status__in=SCHEDULED_STATUSES,
                scheduled_at__isnull=False,
                scheduled_at__gte=start,
                scheduled_at__lte=end,
            )
            if created_before is not None:
                qs = qs.filter(created_at__lt=created_before)
            agg = qs.aggregate(s=Sum('total_price'), c=Count('id'))
            return float(agg['s'] or 0), int(agg['c'] or 0)

        # Actual revenue for (year, month): completed jobs whose scheduled_at falls in that month (same as calendar)
        def actual_aggregate_for_month(year, month):
            start = first_day_tz(year, month)
            end = last_day_tz(year, month)
            agg = (
                jobs_base()
                .filter(status='completed', scheduled_at__isnull=False, scheduled_at__gte=start, scheduled_at__lte=end)
                .aggregate(s=Sum('total_price'), c=Count('id'))
            )
            return float(agg['s'] or 0), int(agg['c'] or 0)

        MONTH_NAMES = ['', 'January', 'February', 'March', 'April', 'May', 'June',
                       'July', 'August', 'September', 'October', 'November', 'December']

        # Build timeline: previous 3 months (actual) + next 6 months (forecast)
        months_list = []
        # Previous 3 months
        for i in range(3, 0, -1):
            m = current_month - i
            y = current_year
            while m <= 0:
                m += 12
                y -= 1
            months_list.append(('actual', y, m))
        # Next 6 months (including current month as first “forecast” month)
        for i in range(6):
            m = current_month + i
            y = current_year
            while m > 12:
                m -= 12
                y += 1
            months_list.append(('forecast', y, m))

        result_months = []
        for kind, y, m in months_list:
            month_start = first_day_tz(y, m)
            hist_avg = historical_average_for_year_month(y, m)
            sched_total, sched_count_total = scheduled_aggregate_for_month(y, m, created_before=None)

            if month_start <= now:
                baseline_sched, baseline_count = scheduled_aggregate_for_month(y, m, created_before=month_start)
                forecast_locked = True
            else:
                baseline_sched, baseline_count = sched_total, sched_count_total
                forecast_locked = False

            forecast_val = hist_avg + baseline_sched
            additional_sched = max(0.0, sched_total - baseline_sched) if forecast_locked else 0.0
            additional_sched_count = max(0, sched_count_total - baseline_count) if forecast_locked else 0

            has_actual_period = y < current_year or (y == current_year and m <= current_month)
            if has_actual_period:
                actual_val, actual_job_count = actual_aggregate_for_month(y, m)
            else:
                actual_val, actual_job_count = None, None

            if actual_val is not None:
                variance = actual_val - forecast_val
                variance_percent = round((variance / forecast_val) * 100, 2) if forecast_val else None
                vs_hist = actual_val - hist_avg
            else:
                variance = None
                variance_percent = None
                vs_hist = None

            month_label = f"{MONTH_NAMES[m]} {y}"
            result_months.append({
                'type': kind,
                'year': y,
                'month': m,
                'month_label': month_label,
                'forecast_is_locked': forecast_locked,
                'historical_average': round(hist_avg, 2),
                'baseline_scheduled_revenue': round(baseline_sched, 2),
                'baseline_scheduled_job_count': baseline_count,
                'scheduled_revenue': round(sched_total, 2),
                'scheduled_revenue_total': round(sched_total, 2),
                'scheduled_job_count_total': sched_count_total,
                'additional_scheduled_revenue': round(additional_sched, 2),
                'additional_scheduled_job_count': additional_sched_count,
                'forecast': round(forecast_val, 2),
                'actual': round(actual_val, 2) if actual_val is not None else None,
                'actual_job_count': actual_job_count,
                'variance': round(variance, 2) if variance is not None else None,
                'variance_percent': variance_percent,
                'actual_vs_historical_average': round(vs_hist, 2) if vs_hist is not None else None,
            })

        return Response({
            'forecast_generated_at': now.isoformat(),
            'data_source': 'Job table only',
            'forecast_formula': (
                'Locked forecast (month has started) = historical_average + baseline_scheduled_revenue. '
                'historical_average = mean completed revenue for that calendar month over the 5 years before '
                'the target year. baseline_scheduled_revenue = sum of total_price for jobs with scheduled_at in '
                'that month, status in pending/confirmed/on_the_way/completed/in_progress/service_due, and '
                'created_at strictly before the first instant of that month (schedule as of month open). '
                'Jobs created on or after month start add to scheduled_revenue_total and actual when completed, '
                'but do not change forecast. Future months not yet started use provisional forecast = '
                'historical_average + all scheduled jobs in that month until the month begins.'
            ),
            'timeline': {
                'previous_3_months_actual': [r for r in result_months if r['type'] == 'actual'],
                'next_6_months_forecast': [r for r in result_months if r['type'] == 'forecast'],
            },
            'months': result_months,
        })


class TechnicianWorkloadHeatmapView(APIView):
    """
    Returns a 7-day (configurable) workload heatmap per technician (scoped to current account).
    The response includes the ordered date headers plus per-technician
    aggregates (job counts, total value, and load intensity classification).
    """
    permission_classes = [AccountScopedPermission, IsAuthenticated]
    DEFAULT_STATUSES = [
        status for status, _ in Job.STATUS_CHOICES
        if status not in ('to_convert', 'reschedule_pending')
    ]
    LOAD_THRESHOLDS = (
        (0, 'none'),
        (2, 'light'),     # 1-2 jobs
        (4, 'moderate'),  # 3-4 jobs
        (float('inf'), 'heavy'),  # 5+
    )

    def get(self, request):
        tz = timezone.get_current_timezone()
        start_dt = self._resolve_start_datetime(request.query_params.get('start_date'), tz)
        days = self._resolve_days(request.query_params.get('days'))
        end_dt = start_dt + timedelta(days=days)

        statuses = self._parse_csv(request.query_params.get('statuses')) or self.DEFAULT_STATUSES
        job_types = self._parse_csv(request.query_params.get('job_types'))
        technician_filter = self._parse_id_list(
            request.query_params.get('technicians') or request.query_params.get('technician')
        )
        sort_by = request.query_params.get('sort_by', 'total_value')
        order = request.query_params.get('order', 'desc').lower()
        view_mode = request.query_params.get('view', 'heatmap')

        account = getattr(request, 'account', None)
        if not account:
            return Response({'error': 'Account context is required.'}, status=status.HTTP_403_FORBIDDEN)

        jobs = Job.objects.filter(
            account=account,
            scheduled_at__isnull=False,
            scheduled_at__gte=start_dt,
            scheduled_at__lt=end_dt,
        ).prefetch_related('assignments__user')

        if statuses:
            jobs = jobs.filter(status__in=statuses)
        if job_types:
            jobs = jobs.filter(job_type__in=job_types)
        if technician_filter:
            jobs = jobs.filter(assignments__user_id__in=technician_filter).distinct()

        date_headers = [
            {
                "date": (start_dt + timedelta(days=i)).date().isoformat(),
                "label": (start_dt + timedelta(days=i)).strftime("%b %d"),
            }
            for i in range(days)
        ]

        technician_map = {}
        available_technicians = {}

        for job in jobs:
            scheduled_local = timezone.localtime(job.scheduled_at, tz)
            date_key = scheduled_local.date().isoformat()
            job_value = float(job.total_price or 0)

            for assignment in job.assignments.all():
                user = assignment.user
                if not user:
                    continue
                if getattr(user, 'is_superuser', False):
                    continue
                if technician_filter and user.id not in technician_filter:
                    continue

                tech_id = str(user.id)
                technician_record = technician_map.setdefault(tech_id, {
                    "technician_id": tech_id,
                    "technician_name": user.get_full_name() or user.username or user.email,
                    "technician_email": user.email,
                    "total_jobs": 0,
                    "total_value": 0.0,
                    "days": {},
                })

                day_bucket = technician_record["days"].setdefault(date_key, {
                    "job_count": 0,
                    "total_value": 0.0,
                })
                day_bucket["job_count"] += 1
                day_bucket["total_value"] += job_value
                technician_record["total_jobs"] += 1
                technician_record["total_value"] += job_value

                if user.id not in available_technicians:
                    available_technicians[user.id] = {
                        "id": tech_id,
                        "name": technician_record["technician_name"],
                    }

        technicians_payload = []
        for record in technician_map.values():
            days_payload = []
            for header in date_headers:
                day_data = record["days"].get(header["date"], {"job_count": 0, "total_value": 0.0})
                load_level = self._determine_load(day_data["job_count"])
                days_payload.append({
                    "date": header["date"],
                    "label": header["label"],
                    "job_count": day_data["job_count"],
                    "total_value": round(day_data["total_value"], 2),
                    "load_level": load_level,
                })

            technicians_payload.append({
                "technician_id": record["technician_id"],
                "technician_name": record["technician_name"],
                "technician_email": record["technician_email"],
                "total_jobs": record["total_jobs"],
                "total_value": round(record["total_value"], 2),
                "days": days_payload,
            })

        reverse = order != 'asc'
        sort_key = {
            'total_jobs': lambda item: item['total_jobs'],
            'name': lambda item: item['technician_name'].lower(),
            'technician_name': lambda item: item['technician_name'].lower(),
            'total_value': lambda item: item['total_value'],
        }.get(sort_by, lambda item: item['total_value'])
        technicians_payload.sort(key=sort_key, reverse=reverse)

        summary = {
            "total_jobs": sum(t["total_jobs"] for t in technicians_payload),
            "total_value": round(sum(t["total_value"] for t in technicians_payload), 2),
        }

        response = {
            "range": {
                "start_date": date_headers[0]["date"] if date_headers else None,
                "end_date": date_headers[-1]["date"] if date_headers else None,
                "days": days,
                "headers": date_headers,
            },
            "filters_applied": {
                "statuses": statuses,
                "job_types": job_types or [],
                "technicians": [str(tid) for tid in technician_filter] if technician_filter else [],
                "sort_by": sort_by,
                "order": order,
                "view": view_mode,
            },
            "legend": [
                {"label": "No jobs", "value": "none"},
                {"label": "Light (1-2)", "value": "light"},
                {"label": "Moderate (3-4)", "value": "moderate"},
                {"label": "Heavy (5+)", "value": "heavy"},
            ],
            "summary": summary,
            "technicians": technicians_payload,
            "available_filters": {
                "job_types": [
                    {"value": value, "label": label}
                    for value, label in Job.JOB_TYPE_CHOICES
                ],
                "statuses": [
                    {"value": value, "label": label}
                    for value, label in Job.STATUS_CHOICES
                ],
                "technicians": list(available_technicians.values()),
                "sort_by": [
                    {"value": "total_value", "label": "Total Amount"},
                    {"value": "total_jobs", "label": "Total Jobs"},
                    {"value": "technician_name", "label": "Technician Name"},
                ],
                "order": [
                    {"value": "asc", "label": "Low to High"},
                    {"value": "desc", "label": "High to Low"},
                ],
            },
        }

        return Response(response)

    @staticmethod
    def _parse_csv(raw_value):
        if not raw_value:
            return []
        return [part.strip() for part in raw_value.split(',') if part.strip()]

    @staticmethod
    def _parse_id_list(raw_value):
        if not raw_value:
            return []
        ids = []
        for part in raw_value.split(','):
            part = part.strip()
            if not part:
                continue
            try:
                ids.append(int(part))
            except ValueError:
                continue
        return ids

    @staticmethod
    def _resolve_days(raw_days):
        try:
            value = int(raw_days)
        except (TypeError, ValueError):
            value = 7
        return min(max(value, 1), 31)

    @staticmethod
    def _resolve_start_datetime(value, tz):
        if value:
            parsed = parse_datetime(value)
            if parsed:
                if timezone.is_naive(parsed):
                    parsed = timezone.make_aware(parsed, tz)
                local_date = timezone.localtime(parsed, tz).date()
                return timezone.make_aware(datetime.combine(local_date, time.min), tz)
            try:
                date_value = datetime.strptime(value, "%Y-%m-%d").date()
                return timezone.make_aware(datetime.combine(date_value, time.min), tz)
            except ValueError:
                pass

        today_local = timezone.localtime(timezone.now(), tz).date()
        return timezone.make_aware(datetime.combine(today_local, time.min), tz)

    def _determine_load(self, count):
        if count <= 0:
            return 'none'
        if count <= 2:
            return 'light'
        if count <= 4:
            return 'moderate'
        return 'heavy'