"""Logos and pictures whose words must be spelled right.

The image model cannot spell (see myapp.poster): "a logo with brand name
ClaudeChats" came back reading "Claudce Chiats". Posters already get their
words lettered by the server; two more kinds of request get the same split -
artwork from the model with no letters at all, the user's exact words set on
top here in the bundled Poppins font:

* a logo with a name ("logo for Sweet Crumbs", "brand name ClaudeChats",
  "logo called nova"): the model draws only the symbol on a plain background
  and the name (and any tagline) is set underneath;
* any other picture with words in quotes ("a birthday card that says "Happy
  Birthday Riya""): the poster layout with just that heading.

The words always come from the user's own message, never from a model.
"""
import io
import logging
import re

from PIL import Image, ImageChops, ImageDraw, features

from myapp import image_generation, poster

logger = logging.getLogger(__name__)

LOGO_CUE_RE = re.compile(r'\b(?:logos?|logo\s*design|wordmark|word\s*mark|brand\s*mark|emblem|monogram)\b', re.IGNORECASE)
# Words in quotes: straight or curly double quotes, or single quotes that are
# not an apostrophe ("Riya's").
_QUOTED_RES = (
    re.compile(r'["“”]([^"“”\n]{1,60}?)["“”]'),
    re.compile(r"(?<![\w])['‘’]([^'‘’\n]{1,60}?)['‘’](?![\w])"),
)
_NAME_CUE_RE = re.compile(
    r'\b(?:brand\s*name|company\s*name|business\s*name|shop\s*name|store\s*name|app\s*name|channel\s*name|'
    r'named|called|titled|name)\s*(?:is|:|=|as|-)?\s+',
    re.IGNORECASE,
)
_LOGO_FOR_RE = re.compile(r'\b(?:logos?|wordmark|emblem|monogram)\s+(?:for|of)\s+(?:my\s+|a\s+|an\s+|the\s+|our\s+)?', re.IGNORECASE)
_TAGLINE_RE = re.compile(r'\b(?:tagline|slogan|subtitle|caption)\s*(?:is|:|=|-|as)?\s+', re.IGNORECASE)
_STOP_WORDS = frozenset('''
a an and the with for in on of at to by from as like or but also then that which who
logo logos image images picture pictures photo poster design wordmark emblem icon symbol
similar simialr same style styled colour colours color colors theme background create make
generate draw design new please using use having has have where it its this these those
modern simple minimal minimalist clean flat cute bold elegant professional tagline slogan
'''.split())
_TOKEN_TRIM = ',.;:!?)(]["“”\'‘’'
MAX_NAME_CHARS = 40


def _clean(text):
    return re.sub(r'\s+', ' ', text or '').strip(' -–—:')


def _quoted(prompt, skip=''):
    for pattern in _QUOTED_RES:
        for match in pattern.finditer(prompt):
            words = _clean(match.group(1))
            if words and re.search(r'\w', words) and words.lower() != skip.lower():
                return words
    return ''


def _name_after(rest, need_capital=False):
    """The name starting at ``rest``: a run of Capitalised words ("Sweet
    Crumbs", "ClaudeChats AI"), or up to three plain lower-case words
    ("sweet crumbs"); it ends at a joining word or punctuation."""
    tokens = []
    capitalised = None
    for raw in rest[:120].split():
        token = raw.strip(_TOKEN_TRIM)
        if not token or token.lower() in _STOP_WORDS:
            break
        starts_upper = token[0].isupper() or token[0].isdigit()
        if capitalised is None:
            if need_capital and not starts_upper:
                break
            capitalised = starts_upper
        elif capitalised and not starts_upper:
            break
        tokens.append(token)
        if len(tokens) == (4 if capitalised else 3) or raw[-1:] in ',.;:!?':
            break
    name = ' '.join(tokens)
    return name if len(name) <= MAX_NAME_CHARS else ''


def find_name(prompt):
    """The name the user wants on the logo, exactly as typed, or ''."""
    quoted = _quoted(prompt, skip=find_tagline(prompt))
    if quoted and len(quoted) <= MAX_NAME_CHARS:
        return quoted
    for match in _NAME_CUE_RE.finditer(prompt):
        name = _name_after(prompt[match.end():])
        if name:
            return name
    for match in _LOGO_FOR_RE.finditer(prompt):
        name = _name_after(prompt[match.end():], need_capital=True)
        if name:
            return name
    return ''


def find_tagline(prompt, name=''):
    match = _TAGLINE_RE.search(prompt)
    if not match:
        return ''
    rest = prompt[match.end():]
    quoted = _quoted(rest[:80])
    if quoted and rest.lstrip()[:1] in '"“\'‘':
        return quoted
    words = re.split(r'[,.;!?\n]|\b(?:and|with|in|using)\b', rest, maxsplit=1)[0]
    tagline = _clean(' '.join(words.split()[:8]))
    return '' if tagline.lower() == name.lower() else tagline


def _model_name(prompt):
    """When the wording hides the name ("logo for my shop, it's called
    Sweet Crumbs"), a small model may point at it - but only words that are
    really in the message are used."""
    try:
        from myapp import ai_chat
        reply = ai_chat.complete_json(
            'Find the brand or business name the user wants written on their logo. Copy it EXACTLY as the user '
            'wrote it. If no name is given, use an empty string. Reply with ONLY {"name": "..."}.',
            prompt[:1500], model_key='quick', max_tokens=60, timeout=15.0, temperature=0,
        )
    except Exception as exc:
        logger.info('Logo name could not be read by the model (%s)', exc.__class__.__name__)
        return ''
    name = _clean(str((reply or {}).get('name') or '')) if isinstance(reply, dict) else ''
    return name if name and len(name) <= MAX_NAME_CHARS and name.lower() in prompt.lower() else ''


def _without_words(prompt, *words):
    text = prompt
    for pattern in _QUOTED_RES:
        text = pattern.sub(' ', text)
    for word in words:
        if word:
            text = re.sub(re.escape(word), ' ', text, flags=re.IGNORECASE)
    text = _NAME_CUE_RE.sub(' ', text)
    text = _TAGLINE_RE.sub(' ', text)
    return _clean(text)[:350]


def symbol_prompt(prompt, name, tagline=''):
    described = _without_words(prompt, name, tagline)
    return (
        f'Logo symbol only, for this request: {described}. One clean, memorable emblem centred on a plain solid '
        'white background with plenty of empty space around it, crisp flat vector style. '
        'No text, no letters, no words, no numbers, no watermark.'
    )


def _printable_ok(*words):
    if features.check('raqm'):
        return True
    return not any(poster._COMPLEX_SCRIPT_RE.search(w) for w in words if w)


def _plain_colour(image):
    """(colour, is it plain) for the picture's border."""
    small = image.convert('RGB').resize((64, 64))
    w, h = small.size
    border = [small.getpixel((x, y)) for x in range(w) for y in (0, h - 1)]
    border += [small.getpixel((x, y)) for y in range(h) for x in (0, w - 1)]
    colour = tuple(sorted(c[i] for c in border)[len(border) // 2] for i in range(3))
    close = sum(1 for c in border if max(abs(a - b) for a, b in zip(c, colour)) <= 26)
    return colour, close >= len(border) * 0.85


def compose_logo(artwork, name, tagline, size):
    """The finished logo as PNG bytes: the symbol, then the exact name."""
    width, height = size
    art = artwork.convert('RGB')
    background, plain = _plain_colour(art)
    canvas = Image.new('RGB', size, background)
    s = min(width, height) / 1080
    light = sum(background) / 3 > 140
    ink = (20, 22, 28) if light else (250, 250, 252)
    soft = poster._mix(ink, background, 0.38)

    # One line when it can stay a good size; two only for a long name.
    name_font, name_lines = poster._fit(name, 'Bold', 150 * s, 70 * s, width * 0.84, 1)
    if len(name_lines) > 1 or name_font.getlength(name_lines[0]) > width * 0.84:
        name_font, name_lines = poster._fit(name, 'Bold', 110 * s, 44 * s, width * 0.84, 2)
    name_h = poster._line_height(name_font) * len(name_lines)
    tag_font = tag_lines = None
    tag_h = 0
    if tagline:
        tag_font, tag_lines = poster._fit(tagline, 'Medium', 50 * s, 22 * s, width * 0.8, 2)
        tag_h = poster._line_height(tag_font) * len(tag_lines) + 14 * s

    # The symbol, trimmed to what was drawn when the background is plain.
    if plain:
        diff = ImageChops.difference(art, Image.new('RGB', art.size, background)).convert('L')
        box = diff.point(lambda v: 255 if v > 30 else 0).getbbox()
        if box:
            margin = int(max(art.size) * 0.03)
            box = (max(0, box[0] - margin), max(0, box[1] - margin),
                   min(art.width, box[2] + margin), min(art.height, box[3] + margin))
            art = art.crop(box)
    gap = 46 * s
    room = height - 2 * 70 * s - gap - name_h - tag_h
    side = max(80 * s, min(width * 0.62, room))
    scale = min(side / art.width, side / art.height)
    symbol = art.resize((max(1, round(art.width * scale)), max(1, round(art.height * scale))), Image.LANCZOS)
    total = symbol.height + gap + name_h + tag_h
    y = (height - total) / 2
    x = (width - symbol.width) / 2
    if plain:
        canvas.paste(symbol, (round(x), round(y)))
    else:
        mask = Image.new('L', symbol.size, 0)
        ImageDraw.Draw(mask).rounded_rectangle((0, 0, symbol.width - 1, symbol.height - 1), radius=round(symbol.width * 0.08), fill=255)
        canvas.paste(symbol, (round(x), round(y)), mask)
    y += symbol.height + gap

    draw = ImageDraw.Draw(canvas)
    for line in name_lines:
        draw.text((width / 2, y), line, font=name_font, fill=ink, anchor='mt')
        y += poster._line_height(name_font)
    if tag_lines:
        y += 14 * s
        for line in tag_lines:
            draw.text((width / 2, y), line, font=tag_font, fill=soft, anchor='mt')
            y += poster._line_height(tag_font)
    out = io.BytesIO()
    canvas.save(out, format='PNG', optimize=True)
    return out.getvalue()


def _open(content):
    with Image.open(io.BytesIO(content)) as opened:
        opened.load()
        return opened.copy()


def make_logo(prompt):
    """A logo whose name is spelled exactly, or None when this is not a logo
    with a name (the caller then draws it the usual way)."""
    prompt = prompt or ''
    if not LOGO_CUE_RE.search(prompt):
        return None
    name = poster.printable(find_name(prompt) or _model_name(prompt))
    if not name:
        return None
    tagline = poster.printable(find_tagline(prompt, name))
    if not _printable_ok(name, tagline):
        return None
    flux_size = image_generation.resolve_dimensions(prompt)
    artwork = image_generation.generate_image(symbol_prompt(prompt, name, tagline), size=flux_size)
    try:
        content = compose_logo(_open(artwork.content), name, tagline, poster.output_size(prompt, flux_size))
    except Exception:
        logger.exception('Could not letter the logo; drawing it the usual way')
        return None
    return image_generation.GeneratedImage(content=content, extension='png')


def make_text_picture(prompt):
    """Any other picture with words in quotes: artwork from the model with no
    letters, the quoted words set on it exactly. None when nothing is quoted."""
    prompt = prompt or ''
    words = poster.printable(_quoted(prompt))
    if not words or not _printable_ok(words):
        return None
    spec = poster.PosterSpec(title=words, scene=_without_words(prompt, words) or 'A colourful picture')
    flux_size = image_generation.resolve_dimensions(prompt)
    artwork = image_generation.generate_image(poster.background_prompt(spec), size=flux_size)
    try:
        content = poster.compose(_open(artwork.content), spec, poster.output_size(prompt, flux_size))
    except Exception:
        logger.exception('Could not letter the picture; drawing it the usual way')
        return None
    return image_generation.GeneratedImage(content=content, extension='jpg')
