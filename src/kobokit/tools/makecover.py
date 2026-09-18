#!/usr/bin/env python3
"""Generate a clean typographic cover for books whose source file has no cover art."""
import os
import sys

from PIL import Image, ImageDraw, ImageFont

W, H = 1400, 2100

# The Google-fonts paths below exist only on the Linux container. On macOS this
# raised before drawing anything, which made makecover the one repair with no
# working fallback for a converted PDF.
FACES = {
    'SERIF': ['/usr/share/fonts/truetype/google-fonts/Lora-Variable.ttf',
              '/System/Library/Fonts/Supplemental/Georgia.ttf',
              '/System/Library/Fonts/Supplemental/Times New Roman.ttf',
              '/usr/share/fonts/truetype/dejavu/DejaVuSerif.ttf'],
    'SERIF_I': ['/usr/share/fonts/truetype/google-fonts/Lora-Italic-Variable.ttf',
                '/System/Library/Fonts/Supplemental/Georgia Italic.ttf',
                '/System/Library/Fonts/Supplemental/Times New Roman Italic.ttf',
                '/usr/share/fonts/truetype/dejavu/DejaVuSerif-Italic.ttf'],
    'SANS': ['/usr/share/fonts/truetype/google-fonts/Poppins-Bold.ttf',
             '/System/Library/Fonts/Supplemental/Arial Bold.ttf',
             '/System/Library/Fonts/Supplemental/Arial.ttf',
             '/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf'],
}


def pick(role):
    for path in FACES[role]:
        if os.path.exists(path):
            return path
    sys.exit('no %s font found. Looked for:\n  %s' % (role, '\n  '.join(FACES[role])))


SERIF = pick('SERIF')
SERIF_I = pick('SERIF_I')
SANS = pick('SANS')


def wrap(draw, text, font, maxw):
    words, lines, cur = text.split(), [], ''
    for w in words:
        t = (cur + ' ' + w).strip()
        if draw.textlength(t, font=font) <= maxw or not cur:
            cur = t
        else:
            lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines


def make(path, title, author, series=None, index=None,
         bg=(28, 30, 38), fg=(240, 238, 232), accent=(186, 156, 100)):
    img = Image.new('RGB', (W, H), bg)
    d = ImageDraw.Draw(img)
    margin = 130

    # frame
    d.rectangle([margin - 40, margin - 40, W - margin + 40, H - margin + 40],
                outline=accent, width=4)

    y = 300
    if series:
        f = ImageFont.truetype(SANS, 52)
        label = series.upper()
        if index:
            label += '  •  BOOK %s' % index
        for line in wrap(d, label, f, W - 2 * margin):
            w = d.textlength(line, font=f)
            d.text(((W - w) / 2, y), line, font=f, fill=accent)
            y += 78
        y += 40
        d.line([(W / 2 - 90, y), (W / 2 + 90, y)], fill=accent, width=3)
        y += 90

    size = 150
    f = ImageFont.truetype(SERIF, size)
    lines = wrap(d, title, f, W - 2 * margin)
    while len(lines) > 4 and size > 70:
        size -= 12
        f = ImageFont.truetype(SERIF, size)
        lines = wrap(d, title, f, W - 2 * margin)
    for line in lines:
        w = d.textlength(line, font=f)
        d.text(((W - w) / 2, y), line, font=f, fill=fg)
        y += int(size * 1.24)

    fa = ImageFont.truetype(SERIF_I, 76)
    ay = H - margin - 220
    d.line([(W / 2 - 140, ay - 70), (W / 2 + 140, ay - 70)], fill=accent, width=2)
    for line in wrap(d, author, fa, W - 2 * margin):
        w = d.textlength(line, font=fa)
        d.text(((W - w) / 2, ay), line, font=fa, fill=fg)
        ay += 96

    img.save(path, 'JPEG', quality=88, optimize=True)
    return path
