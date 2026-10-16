"""Image and video models added on the dashboard's API Settings page.

NVIDIA hosts these on its own gateway, not the chat API: image models answer
at ai.api.nvidia.com/v1/genai/<publisher>/<model> and the Cosmos video models
at ai.api.nvidia.com/v1/cosmos/<publisher>/<model>. Each family wants a
slightly different request body, so the checks that run when a model is added
find the address and the request shape that work and keep them on the row
(CustomAIModel.endpoint_url / request_style). Slow jobs answer 202 with an
NVCF-REQID header and are polled until the result is ready.

Found against the live gateway (October 2026): an address that exists
answers 401 without a key, one that does not answers a plain 404 — so the
address is found without spending a generation. A model that exists but is
not open to the key's account answers 404 "Function …: Not found for account".
"""
import base64
import binascii
import logging
import re
import time

import requests

from myapp.provider_keys import get_key

logger = logging.getLogger(__name__)

GENAI_URL = 'https://ai.api.nvidia.com/v1/genai/'
COSMOS_URL = 'https://ai.api.nvidia.com/v1/cosmos/'
STATUS_URL = 'https://api.nvcf.nvidia.com/v2/nvcf/pexec/status/'
POLL_SECONDS = 3
IMAGE_TIMEOUT = 120
VIDEO_TIMEOUT = 15 * 60
CHECK_IMAGE_TIMEOUT = 90
CHECK_VIDEO_TIMEOUT = 5 * 60

_VIDEO_HINT = re.compile(r'cosmos|video|wan-?2|ltx|mochi|hunyuan|\bsvd\b|stable-video', re.I)
_IMAGE_HINT = re.compile(r'flux|stable-diffusion|sdxl|sd3|bria|edify-image|qwen-image|kandinsky|playground|image-gen|imagen', re.I)
# Cosmos "reason" models read videos and answer in text: chat models.
_TEXT_HINT = re.compile(r'reason', re.I)

IMAGE_STYLES = ('sized', 'plain', 'mode')
VIDEO_STYLES = ('cosmos', 'plain')


class MediaError(Exception):
    """A plain-language failure. ``kind`` is one of key, missing, account,
    limit, busy, network, refused, filtered, empty."""

    def __init__(self, message, kind, detail=''):
        super().__init__(message)
        self.kind = kind
        self.detail = detail


def guess_kind(model_id):
    """'image', 'video' or 'chat' from the model name."""
    if _TEXT_HINT.search(model_id or ''):
        return 'chat'
    if _VIDEO_HINT.search(model_id or ''):
        return 'video'
    if _IMAGE_HINT.search(model_id or ''):
        return 'image'
    return 'chat'


def candidate_urls(model_id, kind):
    """Every address the model could be served at, likeliest first. The page
    names some models with '_' where the API uses '.' (flux_2-klein-4b is
    served as flux.2-klein-4b)."""
    publisher, _, name = (model_id or '').strip('/').partition('/')
    if not name:
        return []
    names = []
    for variant in (name, name.replace('_', '.'), name.replace('_', '-')):
        if variant not in names:
            names.append(variant)
    prefixes = (COSMOS_URL, GENAI_URL) if kind == 'video' else (GENAI_URL, COSMOS_URL)
    return [f'{prefix}{publisher}/{variant}' for prefix in prefixes for variant in names]


def find_endpoint(model_id, kind):
    """The address NVIDIA serves the model at, or None. Asked without a key,
    so nothing is generated or billed."""
    for url in candidate_urls(model_id, kind):
        try:
            response = requests.post(url, json={}, timeout=15)
        except requests.RequestException:
            continue
        if response.status_code != 404:
            return url
    return None


def _post(url, api_key, body, timeout):
    """POST, then poll a 202 until the result is ready. Returns the final
    response; raises MediaError when it never arrives."""
    headers = {'Authorization': f'Bearer {api_key}', 'Accept': 'application/json', 'Content-Type': 'application/json'}
    deadline = time.monotonic() + timeout
    try:
        response = requests.post(url, headers=headers, json=body, timeout=timeout)
        while response.status_code == 202:
            request_id = response.headers.get('NVCF-REQID')
            if not request_id:
                break
            if time.monotonic() > deadline:
                raise requests.Timeout()
            time.sleep(POLL_SECONDS)
            response = requests.get(STATUS_URL + request_id, headers=headers,
                                    timeout=max(5, deadline - time.monotonic()))
    except requests.Timeout as exc:
        raise MediaError(f'NVIDIA did not finish within {int(timeout)} seconds — the model is busy. Try again shortly.', 'busy') from exc
    except requests.RequestException as exc:
        raise MediaError('Could not reach NVIDIA. Check the internet connection and try again.', 'network') from exc
    return response


def _raise_for(response):
    """MediaError for a failed response, in everyday words."""
    status = response.status_code
    text = response.text[:400]
    if status in (401, 403):
        raise MediaError('NVIDIA rejected the API key for this model. Copy the key again from build.nvidia.com → "Get API Key".', 'key', text)
    if status == 404:
        if 'not found for account' in text.lower():
            raise MediaError('This model is listed on build.nvidia.com but is not open to this key\'s NVIDIA account — '
                             'it may be retired or not free to use. Try another model.', 'account', text)
        raise MediaError('NVIDIA has no hosted endpoint for this model right now (it may only be downloadable). Try another model.', 'missing', text)
    if status == 429:
        raise MediaError('NVIDIA is rate-limiting this key right now. Wait a minute and try again.', 'limit', text)
    if status in (400, 413, 422):
        raise MediaError('NVIDIA refused the request.', 'refused', text)
    raise MediaError(f'NVIDIA is having trouble right now (error {status}). Try again shortly.', 'busy', text)


def _sizes(size):
    width, height = size or (1024, 1024)
    return {'width': int(width), 'height': int(height)}


def build_body(kind, style, prompt, *, size=None, test=False):
    if kind == 'image':
        if style == 'sized':
            return {'prompt': prompt, **_sizes((512, 512) if test else size), 'seed': 0}
        if style == 'mode':
            return {'model_mode': 'text2image', 'prompt': prompt}
        return {'prompt': prompt}
    width, height = size or (16, 9)
    ratio = '9_16' if height > width else '1_1' if height == width else '16_9'
    if style == 'cosmos':
        return {'model_mode': 'text2video', 'prompt': prompt, 'resolution': f'480_{ratio}',
                'num_frames': 25 if test else 121, 'fps': 24, 'seed': 0}
    return {'prompt': prompt, 'seed': 0}


def _b64(value):
    if not isinstance(value, str) or not value:
        return None
    if value.startswith('data:'):
        value = value.split(',', 1)[-1]
    try:
        return base64.b64decode(value, validate=False)
    except (ValueError, binascii.Error):
        return None


def _first(payload, *paths):
    for path in paths:
        value = payload
        for step in path:
            if isinstance(step, int):
                value = value[step] if isinstance(value, list) and len(value) > step else None
            else:
                value = value.get(step) if isinstance(value, dict) else None
        if value:
            return value
    return None


def extract_image(payload):
    if not isinstance(payload, dict):
        raise MediaError('NVIDIA sent back something that is not a picture.', 'empty')
    if _first(payload, ('artifacts', 0, 'finishReason')) == 'CONTENT_FILTERED':
        raise MediaError('NVIDIA\'s content filter blocked that picture.', 'filtered')
    content = _b64(_first(payload, ('artifacts', 0, 'base64'), ('b64_image',), ('image',), ('data', 0, 'b64_json'),
                          ('images', 0)))
    if not content:
        raise MediaError('NVIDIA sent back no picture.', 'empty')
    return content


def extract_video(payload):
    if not isinstance(payload, dict):
        raise MediaError('NVIDIA sent back something that is not a video.', 'empty')
    content = _b64(_first(payload, ('b64_video',), ('video',), ('artifacts', 0, 'base64'), ('data', 0, 'b64_json')))
    link = None if content else _first(payload, ('video_url',), ('url',), ('data', 0, 'url'))
    if link:
        # A signed download link: fetched without the API key.
        try:
            content = requests.get(link, timeout=120).content
        except requests.RequestException as exc:
            raise MediaError('The video was made but could not be downloaded from NVIDIA. Try again.', 'network') from exc
    if not content:
        raise MediaError('NVIDIA sent back no video.', 'empty')
    if content[4:8] == b'ftyp':
        return content, 'mp4'
    if content.startswith(b'\x1aE\xdf\xa3'):
        return content, 'webm'
    raise MediaError('NVIDIA sent back a file that is not a playable video.', 'empty')


def request_media(kind, url, style, api_key, prompt, *, size=None, test=False):
    """(bytes, extension) for one generation."""
    if kind == 'image':
        timeout = CHECK_IMAGE_TIMEOUT if test else IMAGE_TIMEOUT
    else:
        timeout = CHECK_VIDEO_TIMEOUT if test else VIDEO_TIMEOUT
    response = _post(url, api_key, build_body(kind, style, prompt, size=size, test=test), timeout)
    if response.status_code != 200:
        _raise_for(response)
    try:
        payload = response.json()
    except ValueError as exc:
        raise MediaError('NVIDIA sent back an unreadable answer.', 'empty') from exc
    if kind == 'image':
        from myapp.image_generation import ImageGenerationError, _validate_image_bytes
        try:
            image = _validate_image_bytes(extract_image(payload), service_name='NVIDIA')
        except ImageGenerationError as exc:
            raise MediaError(str(exc), 'empty') from exc
        return image.content, image.extension
    return extract_video(payload)


def _item(label, status, detail):
    return {'label': label, 'status': status, 'detail': detail}


def run_checks(kind, model_id, api_key):
    """Every check for an image or video model, in order. Returns (ok, report,
    found) — found holds the endpoint_url and request_style that worked."""
    what = 'picture' if kind == 'image' else 'video'
    found = {'endpoint_url': '', 'request_style': ''}
    if not model_id or '/' not in model_id:
        return False, [_item('Model name', 'fail', 'Enter the full model name, e.g. black-forest-labs/flux.2-klein-4b, or paste the model page link.')], found
    if not api_key:
        return False, [_item('API key', 'fail', 'Paste the API key for this model.')], found
    if not api_key.startswith('nvapi-'):
        return False, [_item('API key', 'fail', 'NVIDIA keys start with "nvapi-". Copy the key again from build.nvidia.com → "Get API Key".')], found
    report = [_item('API key', 'ok', 'The key format looks right; it is tried for real in the test below.')]

    url = find_endpoint(model_id, kind)
    if not url:
        report.append(_item('Model name', 'fail', f'NVIDIA has no hosted endpoint for {model_id} right now. On build.nvidia.com it may '
                                                  'only be downloadable, or its free API is not open yet. Try another model.'))
        return False, report, found
    report.append(_item('Model name', 'ok', f'{model_id} has a hosted endpoint on NVIDIA.'))
    found['endpoint_url'] = url

    started = time.monotonic()
    refused = ''
    for style in (IMAGE_STYLES if kind == 'image' else VIDEO_STYLES):
        try:
            content, extension = request_media(kind, url, style, api_key,
                                               'a red apple on a wooden table' if kind == 'image' else 'a red ball rolling slowly across a wooden table',
                                               test=True)
        except MediaError as exc:
            if exc.kind == 'refused':
                refused = exc.detail
                continue
            if exc.kind == 'limit':
                found['request_style'] = style
                report.append(_item(f'Test {what}', 'warn', 'The key works, but NVIDIA is rate-limiting it right now, so no test '
                                                             f'{what} was made. It is added anyway; use "Run all checks" later.'))
                return True, report, found
            report.append(_item(f'Test {what}', 'fail', str(exc)))
            return False, report, found
        found['request_style'] = style
        report.append(_item(f'Test {what}', 'ok', f'Made a test {what} ({len(content) // 1024} KB, {extension.upper()}) '
                                                   f'in {time.monotonic() - started:.0f} s.'))
        return True, report, found

    if re.search(r'"image"|\bimage\b.*required|input_image|reference', refused, re.I) and kind == 'image':
        detail = 'This model only edits an uploaded photo — it cannot make a picture from words alone, so it cannot be used here.'
    else:
        detail = f'NVIDIA refused every kind of request this site knows for this model, so it cannot be used yet. NVIDIA said: {refused[:200]}'
    report.append(_item(f'Test {what}', 'fail', detail))
    return False, report, found


def active(kind):
    """The switched-on image or video model, or None."""
    from myapp import model_controls
    from myapp.models import CustomAIModel
    try:
        rows = CustomAIModel.objects.filter(kind=kind, is_active=True).exclude(endpoint_url='')
        return next((row for row in rows if model_controls.is_enabled(row.model_key)), None)
    except Exception:
        return None


def generate(row, prompt, *, size=None):
    """(bytes, extension) from an added image/video model, counting the request
    on its API Settings panel."""
    from myapp import model_controls
    model_controls.record_request(row.model_key)
    try:
        api_key = get_key(row.key_setting).strip()
        if not api_key:
            raise MediaError('This model has no API key saved.', 'key')
        result = request_media(row.kind, row.endpoint_url, row.request_style or 'plain', api_key, prompt, size=size)
    except MediaError as exc:
        model_controls.record_error(row.model_key, str(exc))
        raise
    model_controls.record_success(row.model_key)
    return result
