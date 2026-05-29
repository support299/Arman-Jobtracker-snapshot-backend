from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("quote_app", "0030_remove_customersubmission_reschedule_of_job"),
    ]

    operations = [
        migrations.AddField(
            model_name="customersubmission",
            name="is_persisted_snapshot",
            field=models.BooleanField(
                default=False,
                help_text="When True, this submission is an immutable copy of the original proposal.",
            ),
        ),
        migrations.AddField(
            model_name="customersubmission",
            name="source_submission",
            field=models.OneToOneField(
                blank=True,
                help_text="Working submission this snapshot was copied from.",
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name="persisted_snapshot",
                to="quote_app.customersubmission",
            ),
        ),
    ]
