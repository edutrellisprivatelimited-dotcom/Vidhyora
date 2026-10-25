"""Move every AI image saved before images went to Dropbox, in one go.

The live server does this by itself, a few images every half hour; this is
for doing it all at once:  python manage.py move_images_to_dropbox
"""
from django.core.management.base import BaseCommand

from myapp import dropbox_images, image_store


class Command(BaseCommand):
    help = 'Compress older AI images to WebP and upload them to Dropbox.'

    def handle(self, *args, **options):
        if not (dropbox_images.is_enabled() and dropbox_images.is_configured()):
            self.stderr.write('Dropbox is not connected (Admin Panel -> Backup & Restore), so nothing was moved.')
            return
        moved_total = missing_total = 0
        while True:
            moved, missing = image_store.adopt_legacy(limit=50)
            moved_total += moved
            missing_total += missing
            self.stdout.write(f'Moved {moved_total} so far...')
            if not moved:
                break
        image_store.upload_pending(limit=10000)
        self.stdout.write('Waiting for the uploads to finish...')
        dropbox_images.flush(timeout=600)
        self.stdout.write(self.style.SUCCESS(
            f'Done: {moved_total} images moved, {missing_total} could not be found anywhere.'
        ))
