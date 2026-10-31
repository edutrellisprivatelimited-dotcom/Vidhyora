from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('myapp', '0106_dropboxsettings_dropbox_folder'),
    ]

    operations = [
        migrations.AddField(
            model_name='pwasettings',
            name='apk_file',
            field=models.FileField(blank=True, help_text='Android app file (.apk) people can download from "Install app".', upload_to='app_apk/'),
        ),
        migrations.AddField(
            model_name='pwasettings',
            name='apk_version',
            field=models.CharField(blank=True, help_text='Optional, e.g. 1.2.0 — shown next to the download.', max_length=30),
        ),
        migrations.AddField(
            model_name='pwasettings',
            name='apk_uploaded_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]
