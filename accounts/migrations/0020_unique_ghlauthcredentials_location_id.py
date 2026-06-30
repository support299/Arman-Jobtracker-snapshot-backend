# Prevent duplicate GHLAuthCredentials rows per location_id.

from django.db import migrations, models
from django.db.models import Count


def dedupe_ghl_credentials(apps, schema_editor):
    GHLAuthCredentials = apps.get_model("accounts", "GHLAuthCredentials")
    User = apps.get_model("service_app", "User")

    duplicate_location_ids = (
        GHLAuthCredentials.objects.exclude(location_id__isnull=True)
        .exclude(location_id="")
        .values("location_id")
        .annotate(row_count=Count("id"))
        .filter(row_count__gt=1)
        .values_list("location_id", flat=True)
    )

    fk_updates = [
        (User, "account"),
    ]
    optional_fk_updates = [
        ("jobtracker_app", "Job", "account"),
        ("quote_app", "CustomerSubmission", "account"),
        ("accounts", "Contact", "account"),
        ("accounts", "Calendar", "account"),
        ("accounts", "GHLCustomField", "account"),
        ("accounts", "GHLLocationIndex", "account"),
        ("accounts", "GHLMediaStorage", "credentials"),
        ("service_app", "Location", "account"),
        ("service_app", "Service", "account"),
        ("service_app", "GlobalBasePrice", "account"),
        ("service_app", "Appointment", "account"),
        ("service_app", "GlobalSizePackage", "account"),
        ("dashboard_app", "Invoice", "account"),
    ]
    for app_label, model_name, field_name in optional_fk_updates:
        try:
            model = apps.get_model(app_label, model_name)
        except LookupError:
            continue
        fk_updates.append((model, field_name))

    for location_id in duplicate_location_ids:
        rows = list(
            GHLAuthCredentials.objects.filter(location_id=location_id).order_by(
                "-is_active", "-updated_at", "-id"
            )
        )
        if len(rows) <= 1:
            continue
        canonical = rows[0]
        for duplicate in rows[1:]:
            for model, field_name in fk_updates:
                model.objects.filter(**{field_name: duplicate}).update(
                    **{field_name: canonical}
                )
            duplicate.delete()


class Migration(migrations.Migration):

    # PostgreSQL cannot CREATE INDEX in the same transaction as DELETE (pending trigger events).
    atomic = False

    dependencies = [
        ("accounts", "0019_alter_ghlauthcredentials_user_id"),
    ]

    operations = [
        migrations.RunPython(dedupe_ghl_credentials, migrations.RunPython.noop),
        migrations.AddConstraint(
            model_name="ghlauthcredentials",
            constraint=models.UniqueConstraint(
                fields=("location_id",),
                condition=models.Q(location_id__isnull=False) & ~models.Q(location_id=""),
                name="unique_ghlauthcredentials_location_id",
            ),
        ),
    ]
