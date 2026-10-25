from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('myapp', '0102_dropbox_image_storage'),
    ]

    operations = [
        migrations.AddField(
            model_name='storedimage',
            name='brief',
            field=models.TextField(blank=True),
        ),
    ]
