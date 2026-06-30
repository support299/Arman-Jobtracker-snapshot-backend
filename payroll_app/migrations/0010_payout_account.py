from django.db import migrations, models
import django.db.models.deletion


def backfill_payout_accounts(apps, schema_editor):
    Payout = apps.get_model('payroll_app', 'Payout')
    TimeEntry = apps.get_model('payroll_app', 'TimeEntry')
    Job = apps.get_model('jobtracker_app', 'Job')

    for payout in Payout.objects.filter(account__isnull=True).iterator():
        account_id = None
        if payout.time_entry_id:
            account_id = (
                TimeEntry.objects.filter(pk=payout.time_entry_id)
                .values_list('account_id', flat=True)
                .first()
            )
        elif payout.job_id:
            account_id = (
                Job.objects.filter(pk=payout.job_id)
                .values_list('account_id', flat=True)
                .first()
            )
        if account_id is None and payout.employee_id:
            account_id = (
                Payout.objects.filter(pk=payout.pk)
                .values_list('employee__account_id', flat=True)
                .first()
            )
        if account_id:
            Payout.objects.filter(pk=payout.pk).update(account_id=account_id)


class Migration(migrations.Migration):

    dependencies = [
        ('accounts', '0012_remove_ghlmediastorage_accounts_ghlmediastorage_credentials_ghl_id_uniq_and_more'),
        ('payroll_app', '0009_timeentry_account'),
    ]

    operations = [
        migrations.AddField(
            model_name='payout',
            name='account',
            field=models.ForeignKey(
                blank=True,
                help_text='GHL subaccount this payout belongs to.',
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name='payouts',
                to='accounts.ghlauthcredentials',
            ),
        ),
        migrations.RunPython(backfill_payout_accounts, migrations.RunPython.noop),
    ]
