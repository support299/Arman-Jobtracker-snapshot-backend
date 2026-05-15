# Generated manually for accounts.Location.is_active

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("accounts", "0015_location"),
    ]

    operations = [
        migrations.AddField(
            model_name="location",
            name="is_active",
            field=models.BooleanField(default=True),
        ),
    ]
