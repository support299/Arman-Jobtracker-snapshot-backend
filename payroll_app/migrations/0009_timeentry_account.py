from django.db import migrations, models
import django.db.models.deletion


def backfill_time_entry_accounts(apps, schema_editor):
    TimeEntry = apps.get_model('payroll_app', 'TimeEntry')
    for entry in TimeEntry.objects.filter(account__isnull=True).iterator():
        employee_account_id = (
            TimeEntry.objects.filter(pk=entry.pk)
            .values_list('employee__account_id', flat=True)
            .first()
        )
        if employee_account_id:
            TimeEntry.objects.filter(pk=entry.pk).update(account_id=employee_account_id)


class Migration(migrations.Migration):

    dependencies = [
        ('accounts', '0012_remove_ghlmediastorage_accounts_ghlmediastorage_credentials_ghl_id_uniq_and_more'),
        ('payroll_app', '0008_employeetimeoff_flexible_coverage'),
    ]

    operations = [
        migrations.AddField(
            model_name='timeentry',
            name='account',
            field=models.ForeignKey(
                blank=True,
                help_text='GHL subaccount where this clock entry was created.',
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name='time_entries',
                to='accounts.ghlauthcredentials',
            ),
        ),
        migrations.RunPython(backfill_time_entry_accounts, migrations.RunPython.noop),
    ]
