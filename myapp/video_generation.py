"""Video generation for the AI chat. A video model takes minutes, far longer
than a chat request may stay open, so the chat turn creates an AIVideoJob,
this module makes the video on a background thread, and the chat page polls
the job (views.ai_video_status) until the clip is on the message."""
import logging
import secrets
import threading
from datetime import timedelta

from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.db import close_old_connections
from django.utils import timezone

from myapp import media_models

logger = logging.getLogger(__name__)

WORKING_TEXT = '🎬 Making your video… this usually takes a few minutes. It will appear right here — you can keep chatting meanwhile.'
# A job still "working" after this long was cut off (e.g. a server restart).
STALE_AFTER = timedelta(minutes=20)


def start(job_id):
    threading.Thread(target=_run, args=(job_id,), daemon=True, name=f'video-job-{job_id}').start()


def _row_for(model_key):
    from myapp.models import CustomAIModel
    try:
        return CustomAIModel.objects.get(pk=int(model_key.split('-', 1)[1]), kind=CustomAIModel.KIND_VIDEO)
    except (CustomAIModel.DoesNotExist, ValueError, IndexError):
        return None


def _run(job_id):
    from myapp.models import AIVideoJob
    close_old_connections()
    try:
        job = AIVideoJob.objects.select_related('message').get(pk=job_id)
        row = _row_for(job.model_key)
        try:
            if row is None:
                raise media_models.MediaError('The video model was removed.', 'missing')
            content, extension = media_models.generate(row, job.prompt)
            name = f"ai_videos/{timezone.now():%Y/%m/%d}/{secrets.token_hex(16)}.{extension}"
            url = default_storage.url(default_storage.save(name, ContentFile(content)))
        except media_models.MediaError as exc:
            finish(job, error=_user_error(exc))
            return
        except Exception:
            logger.exception('Video job %s failed', job_id)
            finish(job, error='The video could not be made this time. Please try again.')
            return
        finish(job, url=url)
    finally:
        close_old_connections()


def _user_error(exc):
    """What the person in the chat is told; the dashboard keeps the detail."""
    if exc.kind == 'filtered':
        return 'That video request was blocked by the content filter. Try describing it differently.'
    if exc.kind in ('limit', 'busy', 'network'):
        return 'The video service is busy right now. Please try again in a few minutes.'
    return 'Video generation is not working right now. Please try again later.'


def finish(job, *, url='', error=''):
    from myapp.models import AIVideoJob
    job.status = AIVideoJob.STATUS_READY if url else AIVideoJob.STATUS_FAILED
    job.video_url, job.error, job.finished_at = url, error[:500], timezone.now()
    job.save(update_fields=['status', 'video_url', 'error', 'finished_at'])
    message = job.message
    message.video_url = url
    message.content = '' if url else f'⚠️ {error}'
    message.save(update_fields=['video_url', 'content'])


def expire_if_stale(job):
    """A job cut off mid-way never finishes on its own; give up on it."""
    from myapp.models import AIVideoJob
    if job.status == AIVideoJob.STATUS_WORKING and timezone.now() - job.created_at > STALE_AFTER:
        finish(job, error='The video took too long and was stopped. Please try again.')
