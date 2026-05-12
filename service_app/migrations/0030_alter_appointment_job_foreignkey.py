from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('service_app', '0029_user_management_tool_access_flags'),
    ]

    operations = [
        migrations.AlterField(
            model_name='appointment',
            name='job',
            field=models.ForeignKey(
                blank=True,
                help_text='Related job if appointment was created from a job',
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name='appointment',
                to='jobtracker_app.job',
            ),
        ),
    ]
