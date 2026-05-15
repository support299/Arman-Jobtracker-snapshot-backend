# Add GHLAuthCredentials.is_active (model field existed without migration).

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("accounts", "0016_location_is_active"),
    ]

    operations = [
        migrations.AddField(
            model_name="ghlauthcredentials",
            name="is_active",
            field=models.BooleanField(default=True),
        ),
    ]
