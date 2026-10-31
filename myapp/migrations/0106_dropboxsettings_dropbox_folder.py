from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('myapp', '0105_tutorialvideo'),
    ]

    operations = [
        migrations.AddField(
            model_name='dropboxsettings',
            name='dropbox_folder',
            field=models.CharField(blank=True, max_length=40),
        ),
    ]
