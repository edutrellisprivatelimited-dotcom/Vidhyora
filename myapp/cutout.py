"""Removing a plain background exactly, without redrawing the picture.

"Remove the background" used to go to myapp.image_edit, which draws the whole
picture again: logos and small words came back changed, and the "transparent"
background was a painted checkerboard. A logo, icon, product shot or
screenshot on one solid colour doesn't need any of that: the background is
cut away here, every other pixel stays exactly as it was, and the result is
a real transparent picture. A busy photo background can't be cut this way;
remove_plain_background() returns None and the caller redraws as before.

Pillow only (numpy isn't installed everywhere this runs): the background is
found by flood-filling from the border on a small copy, which keeps shapes of
the same colour inside the picture (the dark middle of a ring) untouched, and
the edge is then softened at full size by each pixel's distance from the
background colour.
"""
import base64
import binascii
import io
import logging
import re

from PIL import Image, ImageChops, ImageDraw

logger = logging.getLogger(__name__)

WANTS_RE = re.compile(
    r'\b(?:remove|delete|erase|cut\s*out|take\s*out|get\s*rid\s*of|clear|hatao|hata\s*do|nikal\w*)\b'
    r'[\s\S]{0,30}\b(?:background|backgound|bckground|bakground|bg|backdrop)\b|'
    r'\b(?:background|bg)\b[\s\S]{0,20}\b(?:remove|delete|erase|hatao|hata\s*do|nikal\w*)\b|'
    r'\btransparent\b|\bno\s+background\b|\bwithout\s+(?:a\s+|the\s+)?background\b|\bcut\s*out\b',
    re.IGNORECASE,
)
_DATA_URI_RE = re.compile(r'^data:image/[\w.+-]+;base64,(.+)$', re.DOTALL)
WORK_EDGE = 420          # flood fill runs on a copy no larger than this
MAX_EDGE = 2048          # the result is never larger than this
PLAIN_DISTANCE = 32      # border pixels this close to the median count as background
PLAIN_SHARE = 0.82       # ... and this share of the border must be background
FILL_THRESHOLD = 90      # Pillow's flood fill adds up the R, G and B differences
EDGE_LOW, EDGE_HIGH = 20, 70


def wants_background_removed(text):
    return bool(WANTS_RE.search(text or ''))


def _decode(source):
    if isinstance(source, bytes):
        return source
    match = _DATA_URI_RE.match(source or '')
    if not match:
        return None
    try:
        return base64.b64decode(match.group(1))
    except (binascii.Error, ValueError):
        return None


def _border(image):
    w, h = image.size
    pixels = [image.getpixel((x, y)) for x in range(w) for y in (0, h - 1)]
    pixels += [image.getpixel((x, y)) for y in range(1, h - 1) for x in (0, w - 1)]
    return pixels


def _distance(a, b):
    return max(abs(x - y) for x, y in zip(a, b))


def remove_plain_background(source):
    """PNG bytes with the background made transparent, or None when the
    picture has no plain background to cut away."""
    content = _decode(source)
    if not content:
        return None
    try:
        with Image.open(io.BytesIO(content)) as opened:
            opened.load()
            picture = opened.convert('RGBA')
    except Exception:
        return None
    if max(picture.size) > MAX_EDGE:
        picture.thumbnail((MAX_EDGE, MAX_EDGE), Image.LANCZOS)
    rgb = picture.convert('RGB')

    small = rgb.copy()
    small.thumbnail((WORK_EDGE, WORK_EDGE), Image.BILINEAR)
    border = _border(small)
    colour = tuple(sorted(p[i] for p in border)[len(border) // 2] for i in range(3))
    if sum(1 for p in border if _distance(p, colour) <= PLAIN_DISTANCE) < len(border) * PLAIN_SHARE:
        return None

    # Flood the background in from every border pixel that belongs to it,
    # painting it a colour the picture doesn't use; whatever was reached is
    # background, and same-coloured shapes enclosed by the picture are not.
    marker = next(
        (c for c in ((255, 0, 254), (1, 254, 3), (3, 1, 252), (250, 251, 0)) if _distance(c, colour) > FILL_THRESHOLD * 2),
        (255, 0, 254),
    )
    used = set(small.getdata())
    if marker in used:
        marker = next((c for c in ((7, 249, 250), (249, 6, 250), (250, 249, 7)) if c not in used), marker)
    flooded = small.copy()
    w, h = flooded.size
    for x, y in [(x, y) for x in range(w) for y in (0, h - 1)] + [(x, y) for y in range(h) for x in (0, w - 1)]:
        pixel = flooded.getpixel((x, y))
        if pixel != marker and _distance(pixel, colour) <= PLAIN_DISTANCE:
            ImageDraw.floodfill(flooded, (x, y), marker, thresh=FILL_THRESHOLD)
    found = Image.new('L', flooded.size, 0)
    found.putdata([255 if p == marker else 0 for p in flooded.getdata()])
    share = sum(found.getdata()) / 255 / (w * h)
    if not 0.05 <= share <= 0.97:
        return None

    # Full size: the flooded area, eased in at the edge by how far each pixel
    # is from the background colour, so outlines stay crisp and glows fade.
    background = found.resize(rgb.size, Image.BILINEAR)
    distance = ImageChops.difference(rgb, Image.new('RGB', rgb.size, colour))
    r, g, b = distance.split()
    distance = ImageChops.lighter(ImageChops.lighter(r, g), b)
    span = EDGE_HIGH - EDGE_LOW
    keep_by_colour = distance.point(lambda v: 0 if v <= EDGE_LOW else 255 if v >= EDGE_HIGH else (v - EDGE_LOW) * 255 // span)
    alpha = ImageChops.lighter(ImageChops.invert(background), keep_by_colour)
    alpha = ImageChops.multiply(alpha, picture.getchannel('A'))
    picture.putalpha(alpha)
    box = alpha.point(lambda v: 255 if v > 8 else 0).getbbox()
    if box:
        picture = picture.crop(box)
    out = io.BytesIO()
    picture.save(out, format='PNG', optimize=True)
    return out.getvalue()
