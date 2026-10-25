"""Several pictures in one message, laid out together as one picture.

The vision model, OCR, the picture-editing path and the saved message all work
with one image per message, so up to MAX_IMAGES attached pictures are placed in
a grid, each numbered in its top-left corner, and travel on as that one
picture, with note() telling the model how many there are.
"""
import base64
import io
import logging
import math

from PIL import Image, ImageDraw, ImageFont

from myapp import poster

logger = logging.getLogger(__name__)

MAX_IMAGES = 4
CELL = 768                      # each picture is fitted into a square this big
GAP = 16
MAX_DATA_URI_CHARS = 1_900_000  # under views.AI_IMAGE_MAX_DATA_URI_CHARS


def _open(data_uri):
    raw = base64.b64decode(data_uri.split(',', 1)[1])
    with Image.open(io.BytesIO(raw)) as opened:
        opened.load()
        picture = opened.convert('RGBA')
    flat = Image.new('RGB', picture.size, (255, 255, 255))
    flat.paste(picture, mask=picture.getchannel('A'))
    return flat


def _badge_font():
    try:
        return poster._font('Bold', 30)
    except Exception:
        return ImageFont.load_default()


def combine(data_uris):
    """One JPEG data URI showing every picture, numbered; '' when one of them
    can't be read."""
    try:
        pictures = [_open(uri) for uri in data_uris[:MAX_IMAGES]]
    except Exception:
        logger.info('An attached picture could not be read', exc_info=True)
        return ''
    if not pictures:
        return ''
    columns = 1 if len(pictures) == 1 else 2
    rows = math.ceil(len(pictures) / columns)
    sheet = Image.new('RGB', (columns * CELL + (columns + 1) * GAP, rows * CELL + (rows + 1) * GAP), (255, 255, 255))
    draw = ImageDraw.Draw(sheet)
    font = _badge_font()
    for index, picture in enumerate(pictures):
        picture.thumbnail((CELL, CELL))
        column, row = index % columns, index // columns
        x = GAP + column * (CELL + GAP) + (CELL - picture.width) // 2
        y = GAP + row * (CELL + GAP) + (CELL - picture.height) // 2
        sheet.paste(picture, (x, y))
        cx, cy, radius = x + 34, y + 34, 26
        draw.ellipse((cx - radius, cy - radius, cx + radius, cy + radius), fill=(17, 24, 39), outline=(255, 255, 255), width=3)
        draw.text((cx, cy), str(index + 1), font=font, fill=(255, 255, 255), anchor='mm')
    for quality in (85, 75, 65, 55, 45):
        out = io.BytesIO()
        sheet.save(out, 'JPEG', quality=quality, optimize=True)
        data_uri = 'data:image/jpeg;base64,' + base64.b64encode(out.getvalue()).decode()
        if len(data_uri) <= MAX_DATA_URI_CHARS:
            return data_uri
    return ''


def note(count):
    return (
        f'{count} images were attached. They are shown together in one picture, '
        f'numbered 1 to {count} in their top-left corners.'
    )
