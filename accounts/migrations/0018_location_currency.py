from django.db import migrations, models

from accounts.currency import currency_for_country


def backfill_location_currency(apps, schema_editor):
    Location = apps.get_model("accounts", "Location")
    for loc in Location.objects.all().only("id", "country", "currency"):
        code = currency_for_country(loc.country)
        if loc.currency != code:
            Location.objects.filter(pk=loc.pk).update(currency=code)


def noop_reverse(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("accounts", "0017_ghlauthcredentials_is_active"),
        ("accounts", "0015_ghlauthcredentials_company_branding"),
    ]

    operations = [
        migrations.AddField(
            model_name="location",
            name="currency",
            field=models.CharField(blank=True, default="USD", max_length=3),
        ),
        migrations.RunPython(backfill_location_currency, noop_reverse),
    ]
