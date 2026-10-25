"""Changing a picture when the image service can only draw new ones.

The connected image model (FLUX.2 Klein on NVIDIA's hosted API) draws from
words alone and cannot take an uploaded picture (image_generation raises
ImageGenerationError with editing_unavailable). So "change the name to
Claude" on an attached or earlier picture is done in three steps:

1. read the picture: its exact words by OCR (myapp.image_ocr) and its look -
   kind of picture, layout, subjects, colours, style - by the vision model;
2. a text model writes the brief for the new picture: the requested change
   worked in, everything else kept as it was;
3. draw the new picture from that brief, in the original's shape. A poster or
   flyer that is mostly words is lettered by myapp.poster so every word is
   spelled right; anything else is drawn whole by the image model.

The brief is saved with the new picture (StoredImage.brief), so editing the
result again starts from its exact words rather than reading them back off a
generated picture.
"""
import base64
import io
import logging
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

from PIL import Image

from myapp import ai_chat, image_generation, image_ocr, poster

logger = logging.getLogger(__name__)

READ_EDGE = 1024           # the vision model gets a copy no longer than this
# The three steps together must fit the server's 120 s request limit.
DESCRIBE_TIMEOUT = 25.0    # longest wait for the next piece of the description
DESCRIBE_DEADLINE = 40.0   # stop reading the description after this long
BRIEF_TIMEOUT = 25.0
BRIEF_MAX_CHARS = 2000
CHANGE_MAX_CHARS = 200
REDRAWN_NOTE = '_Drawn again from your picture with this change, so small details may differ._'

# How a small or photographed copy gets described; never wanted in the new artwork.
_POOR_COPY_RE = re.compile(r'\b(?:blurr?(?:y|ed)?|pixel+ated|low[- ](?:quality|resolution)|screenshot)\b', re.IGNORECASE)

UNREADABLE = (
    "I couldn't read that picture just now, so I couldn't change it. "
    'Please try again in a moment.'
)

_BRIEF_SYSTEM = (
    'A picture must be changed, but the image generator can only draw a new picture from words. '
    "You get the user's change request, a description of the current picture, the words read off it "
    '(OCR, which may have small reading mistakes) and, when there is one, the brief the current picture '
    'was drawn from. Apply ONLY the requested change and keep everything else - the kind of picture, '
    'subject, layout, colours, style and every other word - the same. Correct obvious OCR mistakes. '
    'Describe the new picture as a crisp, finished design: never as blurry, pixelated, low quality or a '
    'screenshot, even when the copy you were given looks like one. '
    'Reply with ONLY a JSON object:\n'
    '{"prompt": "the new picture for the image generator, in English, under 450 characters: kind of '
    'picture, subject, layout, style and colours first, then the few most important words it shows in '
    'double quotes", '
    '"brief": "the whole new picture in plain prose with every word on it written exactly, under 1500 '
    'characters", '
    '"change": "one short sentence telling the user what you changed, e.g. Changed the name ChatGPT to '
    'Claude.", '
    '"poster": null, or - only when the picture is a poster, flyer, banner, advert or card that is mostly '
    'words - {"title": "the brand or main headline", "tagline": "the subtitle, or empty", '
    '"features": ["each listed line, at most 6"], "offer": "a price or offer line, or empty", '
    '"cta": "the call to action, or empty", "emails": [], "phones": [], "websites": [], '
    '"scene": "the background artwork only: rich, fairly dark artwork in the picture\'s main colours (the '
    'words are printed on top in white), never a plain or white background; style, mood and objects, '
    'under 45 words, no words"}}'
)


@dataclass
class Redrawn:
    image: image_generation.GeneratedImage
    brief: str    # saved with the new picture, for the next edit
    change: str   # shown to the user above the new picture


def _open(source_image):
    try:
        raw = base64.b64decode(source_image.split(',', 1)[1])
        picture = Image.open(io.BytesIO(raw))
        picture.load()
        return picture
    except Exception:
        return None


def model_size(picture):
    """The image model's size closest to the picture's own shape."""
    if picture is None:
        return image_generation.DEFAULT_SIZE
    return image_generation._size_for_ratio(*picture.size) or image_generation.DEFAULT_SIZE


def reading_copy(picture, source_image):
    """A smaller JPEG for the vision model: a full-size poster takes it much
    longer to read and was timing out."""
    if picture is None:
        return source_image
    copy = picture.convert('RGB')
    copy.thumbnail((READ_EDGE, READ_EDGE))
    out = io.BytesIO()
    copy.save(out, 'JPEG', quality=85)
    return 'data:image/jpeg;base64,' + base64.b64encode(out.getvalue()).decode()


def _clean(value, limit):
    text = re.sub(r'\s+', ' ', str(value or '')).strip()
    return text[:limit].strip()


def write_plan(request, description, words, earlier_brief=''):
    """The text model's plan for the new picture (a dict), or None when it
    could not be written; the caller then falls back to plain words."""
    parts = [f'Change request: {request.strip()}']
    parts.append('Description of the current picture:\n' + (description or '(not available)'))
    parts.append('Words read off the current picture:\n' + (words or '(none found)'))
    if earlier_brief:
        parts.append('Brief the current picture was drawn from:\n' + earlier_brief)
    try:
        reply = ai_chat.complete_json(
            _BRIEF_SYSTEM, '\n\n'.join(parts)[:12000], model_key='quick',
            max_tokens=1400, timeout=BRIEF_TIMEOUT, temperature=0.2,
        )
    except Exception as exc:
        logger.info('The brief for a redrawn picture could not be written (%s)', exc.__class__.__name__)
        return None
    return reply if isinstance(reply, dict) else None


def poster_spec(raw, source_text):
    """The poster's words from the plan, each checked against the words that
    were on the picture or in the request: the model may only copy them."""
    if not isinstance(raw, dict):
        return None

    def grounded(value):
        return poster.printable(poster._grounded(value, source_text))

    def grounded_list(values, limit):
        values = values if isinstance(values, list) else []
        return [v for v in (grounded(x) for x in values) if v][:limit]

    spec = poster.PosterSpec(
        title=grounded(raw.get('title')), tagline=grounded(raw.get('tagline')),
        features=grounded_list(raw.get('features'), poster.MAX_FEATURES),
        offer=grounded(raw.get('offer')), cta=grounded(raw.get('cta')),
        emails=grounded_list(raw.get('emails'), 2), phones=grounded_list(raw.get('phones'), 2),
        websites=grounded_list(raw.get('websites'), 2),
        scene=poster._scrub_scene(_POOR_COPY_RE.sub('', str(raw.get('scene') or '')))[:400],
    )
    if not spec.title or not poster.can_letter(spec):
        return None
    if not spec.scene:
        spec.scene = 'premium abstract background with soft glowing light'
    return spec


def _lettered(spec, size):
    """A poster with the plan's words lettered on new artwork, or None."""
    artwork = image_generation.generate_image(poster.background_prompt(spec), size=size)
    try:
        with Image.open(io.BytesIO(artwork.content)) as opened:
            opened.load()
            content = poster.compose(opened, spec, poster.output_size('', size))
    except Exception:
        logger.exception('Could not letter the redrawn poster; drawing it whole instead')
        return None
    return image_generation.GeneratedImage(content=content, extension='jpg')


def _fallback_prompt(request, description, words):
    """Without a plan: the request first, then what the picture showed."""
    request = request.strip().rstrip('.')
    if description:
        return f'{request}. {description}'
    lines = ', '.join(f'"{line}"' for line in (words or '').splitlines()[:6] if line.strip())
    return f'{request}. A picture showing the words {lines}.' if lines else request


def redraw(source_image, request, *, words=None, earlier_brief=''):
    """Draw ``source_image`` again with ``request`` applied. ``words`` is its
    OCR text when the caller already has it; ``earlier_brief`` is the brief it
    was drawn from, when it was redrawn here before. Raises
    ImageGenerationError when it cannot be read or drawn."""
    picture = _open(source_image)
    size = model_size(picture)
    with ThreadPoolExecutor(max_workers=1) as pool:
        # OCR runs on this machine while the vision model reads the picture.
        reading = pool.submit(image_ocr.extract_data_uri, source_image) if words is None else None
        description = ai_chat.describe_image_for_edit(
            reading_copy(picture, source_image), timeout=DESCRIBE_TIMEOUT, deadline=DESCRIBE_DEADLINE,
        )
        if reading is not None:
            words = reading.result()
    if not (description or words or earlier_brief):
        raise image_generation.ImageGenerationError(UNREADABLE, status_code=502)

    plan = write_plan(request, description, words, earlier_brief) or {}
    source_text = '\n'.join((words or '', earlier_brief or '', description or '', request))
    spec = poster_spec(plan.get('poster'), source_text)
    generated = _lettered(spec, size) if spec else None
    if generated is None:
        prompt = _clean(plan.get('prompt'), image_generation.FLUX_SAFE_PROMPT_CHARS)
        generated = image_generation.generate_image(
            prompt or _fallback_prompt(request, description, words), size=size,
        )
    brief = _clean(plan.get('brief'), BRIEF_MAX_CHARS) or _clean(
        f'{earlier_brief or description or words}. Then changed: {request}', BRIEF_MAX_CHARS,
    )
    return Redrawn(image=generated, brief=brief, change=_clean(plan.get('change'), CHANGE_MAX_CHARS))
