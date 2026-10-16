from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('myapp', '0081_api_checker_history'),
    ]

    operations = [
        migrations.AddField(
            model_name='aimodelcontrol',
            name='model_id',
            field=models.CharField(blank=True, max_length=200),
        ),
        migrations.AddField(
            model_name='aimodelcontrol',
            name='model_thinking_control',
            field=models.BooleanField(default=False),
        ),
    ]
