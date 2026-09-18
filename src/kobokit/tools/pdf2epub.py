#!/usr/bin/env python3
"""Convert a text-bearing PDF of a NOVEL into a reflowable EPUB.

The other direction from pdf2kepub.py. That one photographs each page, which is
right for a picture book and wrong for prose: 900 fixed page images on a 6-inch
screen cannot change their font size and cannot be read comfortably. This pulls
the text out and rebuilds a normal book around it.

    python3 pdf2epub.py book.pdf --out ./built --title "..." --author "..."

What it handles, because a scanned novel needs all of it:

  * running heads ("233 / Ken Follett") repeated on every page, stripped by
    finding the lines that recur in the same screen position page after page
    rather than by matching any particular wording
  * words broken across a line by a hyphen, rejoined
  * paragraphs, inferred from indentation and from where lines stop short
  * chapter and part breaks, RENUMBERED in sequence rather than trusted: OCR
    reads a stylised display capital as anything it likes, so a book can appear
    to contain two chapter 12s and no chapter 13
  * the handful of OCR confusions that are systematic enough to fix safely

It deliberately does NOT try to recover italics. In a scan they are a guess, and
a wrong guess is worse than plain text.
"""
import argparse, html, os, re, sys, uuid, zipfile
from collections import Counter, defaultdict

try:
    import pymupdf
except ImportError:                                    # older name
    import fitz as pymupdf

ROMAN = re.compile(r"^[IVXLC]{1,6}$")
CHAP = re.compile(r"^\s*chapter\s+([0-9ivxlc]+)\s*$", re.I)
PART = re.compile(r"^\s*part\s+([a-z0-9]+)\s*$", re.I)
FRONT = re.compile(r"^\s*(prologue|epilogue|foreword|preface|introduction)\s*$", re.I)
WORDNUM = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
           "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12}

# OCR slips that are safe to correct because the correct reading is unambiguous:
# a capital I before an apostrophe is never a T, and never a J.
OCR_FIX = [(re.compile(r"\b[TJ](['’](?:ll|ve|d|m|re))\b"), r"I\1"),
           (re.compile(r"\b[TJ](['’]m)\b"), r"I\1")]


def roman_to_int(s):
    vals = {"I": 1, "V": 5, "X": 10, "L": 50, "C": 100}
    out = prev = 0
    for ch in reversed(s.upper()):
        v = vals.get(ch, 0)
        out += -v if v < prev else v
        prev = max(prev, v)
    return out


def page_lines(page):
    """Lines with their vertical position and left edge, reading order kept."""
    out = []
    for b in page.get_text("dict")["blocks"]:
        for l in b.get("lines", []):
            txt = "".join(s["text"] for s in l["spans"])
            if txt.strip():
                out.append((l["bbox"][1], l["bbox"][0], l["bbox"][2], txt.rstrip()))
    out.sort(key=lambda t: (round(t[0], 1), t[1]))
    return out


def find_running_heads(doc, sample=60):
    """Lines that recur near the top or bottom across many pages.

    Matching on wording alone would miss "233 / Ken Follett" (the number differs
    every page) and would risk deleting a real line of prose that happens to
    repeat. Matching on shape - digits blanked - plus position is what makes this
    safe.
    """
    top, bottom = Counter(), Counter()
    n = len(doc)
    step = max(1, n // sample)
    seen = 0
    for i in range(0, n, step):
        lines = page_lines(doc[i])
        if not lines:
            continue
        seen += 1
        h = doc[i].rect.height
        for y, x0, x1, t in lines:
            shape = re.sub(r"\d+", "#", t.strip())[:60]
            if y < h * 0.10:
                top[shape] += 1
            elif y > h * 0.90:
                bottom[shape] += 1
    thresh = max(3, seen // 3)
    return ({s for s, c in top.items() if c >= thresh},
            {s for s, c in bottom.items() if c >= thresh})


def clean(text):
    for pat, rep in OCR_FIX:
        text = pat.sub(rep, text)
    # A paragraph that opens with a single quote and closes with a double one is
    # an OCR misread of the opening quotation mark, not a nested quote - nothing
    # nests that way round. Only fixed when the closing double mark is actually
    # present, so a genuine inner quotation is left alone.
    t = text.lstrip()
    if t[:1] in ("\u2018", "'") and "\u201d" in t and "\u201c" not in t:
        text = text.replace(t[0], "\u201c", 1)
    return text


def extract(doc, drop_top, drop_bottom, first_page, NAMES=frozenset()):
    """Every page's body lines, with running heads and bare page numbers gone."""
    pages = []
    for i in range(first_page, len(doc)):
        pageno = i + 1
        h = doc[i].rect.height
        keep = []
        for y, x0, x1, t in page_lines(doc[i]):
            s = t.strip()
            shape = re.sub(r"\d+", "#", s)[:60]
            near_top, near_bot = y < h * 0.10, y > h * 0.90
            if near_top and shape in drop_top:
                continue
            if near_bot and shape in drop_bottom:
                continue
            if (near_top or near_bot) and re.fullmatch(r"[\d\s./|-]{1,12}", s):
                continue
            # The head is sometimes split into separate text objects, so the
            # whole-line shape never matches: "233", "/", "Ken Follett". Compare
            # the letters alone against the title and author as a backstop.
            if near_top or near_bot:
                letters = re.sub(r"[^a-z]", "", s.lower())
                if letters and letters in NAMES and len(s) < 60:
                    continue
            keep.append((x0, x1, s))
        pages.append((pageno, mark_paragraphs(keep)))
    return pages


def mark_paragraphs(lines, indent_gap=7.0, short_gap=16.0):
    """Tag each line of ONE page with whether it begins a paragraph.

    Margins have to be measured per page and nowhere else. This is a photograph
    of a book: every page sits a little differently under the camera, so a right
    margin averaged over a chapter matches no page in it, every line then counts
    as "stopped short", and the result is a book of one-line paragraphs. Within a
    single page the margins are consistent and the signal is clean.

    A line begins a paragraph when it is indented past that page's body margin,
    or when the line before it stopped short of that page's right margin.
    """
    if not lines:
        return []
    lefts = sorted(x for x, _, _ in lines)
    rights = sorted(x1 for _, x1, _ in lines)
    left = lefts[len(lefts) // 4]
    right = rights[int(len(rights) * 0.85)]
    out, prev_short = [], None
    for x, x1, s in lines:
        indented = (x - left) > indent_gap
        starts = indented if prev_short is None else (prev_short or indented)
        out.append((starts, s))
        prev_short = (right - x1) > short_gap
    # The page's first line inherits nothing, so leave the decision to the
    # stitcher: a paragraph running across a page break must not be split.
    if out:
        out[0] = (out[0][0] and out[0][0], out[0][1])
    return out


def paragraphs(lines):
    """Stitch flagged lines into paragraphs, across page breaks included."""
    out, cur = [], ""
    for starts, s_ in lines:
        if cur and starts:
            out.append(cur)
            cur = s_
        elif not cur:
            cur = s_
        elif cur.endswith("-"):
            cur = cur[:-1] + s_.lstrip()               # word split across lines
        else:
            cur = cur + " " + s_.lstrip()
    if cur:
        out.append(cur)

    # A chapter's opening drop capital is its own text object, so the first
    # paragraph arrives as "I" followed by "N A BROAD VALLEY...". Glue a lone
    # capital back onto what follows it.
    joined = []
    for para in out:
        if joined and len(joined[-1].strip()) == 1 and joined[-1].strip().isupper():
            joined[-1] = joined[-1].strip() + para.lstrip()
        else:
            joined.append(para)
    return joined


def structure(pages, force_at=()):
    """Split the flat page list into (kind, label, [lines]) sections.

    Chapters are NUMBERED IN SEQUENCE, not by the number OCR read: a stylised
    display capital reads as anything, and this book alone yielded two chapter
    12s, a chapter 1 in the middle, and no chapter 13. Counting is right wherever
    every break is found, which is why a break OCR misses entirely has to be
    supplied by hand (--chapter-at) rather than papered over.

    Part dividers are deliberately NOT detected. In a scan they are a page of
    ornament with the part number set in display type, which OCR renders as
    "PART ThREeE" if it renders anything at all - so most are invisible here and
    numbering the few that are found produces confident nonsense ("Part One"
    printed over what is really Part Three). No parts beats wrong parts.
    """
    secs, cur, kind, label = [], [], "front", "Front Matter"
    chap_n = 0
    for pageno, pg in pages:
        forced = pageno in force_at
        for idx, (starts, s) in enumerate(pg):
            m_c, m_f = CHAP.match(s), FRONT.match(s)
            hit = None
            if forced and idx == 0:
                chap_n += 1
                hit = ("chapter", "Chapter %d" % chap_n)
            elif m_c:
                chap_n += 1
                hit = ("chapter", "Chapter %d" % chap_n)
            elif m_f:
                hit = ("front", s.strip().title())
            if hit:
                if cur:
                    secs.append((kind, label, cur))
                kind, label, cur = hit[0], hit[1], []
                if m_c or m_f:
                    continue          # the heading line itself is not body text
            cur.append((starts, s))
    if cur:
        secs.append((kind, label, cur))
    return secs


def build(secs, meta, dst):
    z = zipfile.ZipFile(dst, "w", zipfile.ZIP_DEFLATED)
    z.writestr("mimetype", "application/epub+zip", zipfile.ZIP_STORED)
    z.writestr("META-INF/container.xml",
               '<?xml version="1.0"?><container version="1.0" '
               'xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles>'
               '<rootfile full-path="OEBPS/content.opf" '
               'media-type="application/oebps-package+xml"/></rootfiles></container>')
    z.writestr("OEBPS/style.css",
               "body{margin:0 5%;line-height:1.45}"
               "h1{text-align:center;margin:3em 0 2em;font-weight:normal;"
               "letter-spacing:.08em}"
               "h2{text-align:center;margin:2em 0 1.5em;font-weight:normal}"
               "p{margin:0;text-indent:1.2em;text-align:justify}"
               "p.first{text-indent:0}p.sec{text-indent:0;text-align:center;"
               "margin:1.5em 0}")
    man, spine, nav = [], [], []
    words = 0
    for i, (kind, label, lines) in enumerate(secs, 1):
        paras = paragraphs(lines)
        body = []
        first = True
        for p in paras:
            p = clean(p)
            words += len(p.split())
            if ROMAN.fullmatch(p.strip()) and len(p.strip()) <= 6:
                body.append('<p class="sec">%s</p>' % html.escape(p.strip()))
                first = True
                continue
            body.append('<p%s>%s</p>' % (' class="first"' if first else "",
                                         html.escape(p)))
            first = False
        name = "s%03d.xhtml" % i
        z.writestr("OEBPS/" + name,
                   '<?xml version="1.0" encoding="utf-8"?>\n<!DOCTYPE html>\n'
                   '<html xmlns="http://www.w3.org/1999/xhtml"><head><title>%s</title>'
                   '<meta http-equiv="Content-Type" content="text/html; charset=utf-8"/>'
                   '<link rel="stylesheet" type="text/css" href="style.css"/></head>'
                   '<body><h1>%s</h1>%s</body></html>'
                   % (html.escape(label), html.escape(label), "".join(body)))
        man.append('<item id="s%d" href="%s" media-type="application/xhtml+xml"/>' % (i, name))
        spine.append('<itemref idref="s%d"/>' % i)
        nav.append('<navPoint id="n%d" playOrder="%d"><navLabel><text>%s</text>'
                   '</navLabel><content src="%s"/></navPoint>'
                   % (i, i, html.escape(label), name))
    uid = "urn:uuid:" + str(uuid.uuid4())
    z.writestr("OEBPS/toc.ncx",
               '<?xml version="1.0" encoding="utf-8"?>\n'
               '<ncx version="2005-1" xmlns="http://www.daisy.org/z3986/2005/ncx/">'
               '<head><meta name="dtb:uid" content="%s"/><meta name="dtb:depth" content="1"/>'
               '<meta name="dtb:totalPageCount" content="0"/>'
               '<meta name="dtb:maxPageNumber" content="0"/></head>'
               '<docTitle><text>%s</text></docTitle><navMap>%s</navMap></ncx>'
               % (uid, html.escape(meta["title"]), "".join(nav)))
    extra = ""
    for tag, key in (("date", "date"), ("publisher", "publisher"),
                     ("description", "description")):
        if meta.get(key):
            extra += "<dc:%s>%s</dc:%s>" % (tag, html.escape(meta[key]), tag)
    for s in (meta.get("subjects") or "").split(","):
        if s.strip():
            extra += "<dc:subject>%s</dc:subject>" % html.escape(s.strip())
    if meta.get("series"):
        extra += ('<meta name="calibre:series" content="%s"/>'
                  % html.escape(meta["series"]))
        if meta.get("series_index"):
            extra += ('<meta name="calibre:series_index" content="%s"/>'
                      % html.escape(str(meta["series_index"])))
    z.writestr("OEBPS/content.opf",
               '<?xml version="1.0" encoding="utf-8"?>\n<package '
               'xmlns="http://www.idpf.org/2007/opf" version="2.0" unique-identifier="bid">'
               '<metadata xmlns:dc="http://purl.org/dc/elements/1.1/" '
               'xmlns:opf="http://www.idpf.org/2007/opf">'
               '<dc:identifier id="bid">%s</dc:identifier><dc:title>%s</dc:title>'
               '<dc:creator opf:role="aut" opf:file-as="%s">%s</dc:creator>'
               '<dc:language>en</dc:language>%s</metadata>'
               '<manifest><item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>'
               '<item id="css" href="style.css" media-type="text/css"/>%s</manifest>'
               '<spine toc="ncx">%s</spine></package>'
               % (uid, html.escape(meta["title"]),
                  html.escape(meta.get("author_sort") or meta["author"]),
                  html.escape(meta["author"]), extra, "".join(man), "".join(spine)))
    z.close()
    return words


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("pdf")
    ap.add_argument("--out", default=".")
    ap.add_argument("--title", required=True)
    ap.add_argument("--author", required=True)
    ap.add_argument("--author-sort")
    ap.add_argument("--series")
    ap.add_argument("--series-index")
    ap.add_argument("--publisher")
    ap.add_argument("--date")
    ap.add_argument("--subjects")
    ap.add_argument("--description")
    ap.add_argument("--skip-pages", type=int, default=0,
                    help="ignore this many leading pages (cover scans and blanks "
                         "OCR into noise; nothing there is worth keeping)")
    ap.add_argument("--chapter-at", action="append", type=int, default=[],
                    metavar="PAGE",
                    help="force a chapter break at the top of this PDF page, for "
                         "a chapter whose heading OCR could not read at all. "
                         "Repeatable. Chapters are numbered in sequence, so a "
                         "missed break shifts every number after it")
    ap.add_argument("--filename")
    a = ap.parse_args()

    doc = pymupdf.open(a.pdf)
    top, bot = find_running_heads(doc)
    names = frozenset(re.sub(r"[^a-z]", "", x.lower())
                      for x in (a.title, a.author) if x)
    pages = extract(doc, top, bot, a.skip_pages, names)
    secs = structure(pages, set(a.chapter_at))
    doc.close()

    meta = {"title": a.title, "author": a.author, "author_sort": a.author_sort,
            "series": a.series, "series_index": a.series_index,
            "publisher": a.publisher, "date": a.date, "subjects": a.subjects,
            "description": a.description}
    os.makedirs(a.out, exist_ok=True)
    name = a.filename or re.sub(r'[\\/:*?"<>|]', "", "%s - %s.epub" % (a.title, a.author))
    dst = os.path.join(a.out, name)
    words = build(secs, meta, dst)

    print("running heads dropped: %s" % (sorted(top | bot) or "none found"))
    print("%d sections:" % len(secs))
    for kind, label, lines in secs:
        print("   %-8s %-16s %5d lines" % (kind, label, len(lines)))
    print("\nwrote %s\n  %s words, %.1f MB"
          % (dst, format(words, ","), os.path.getsize(dst) / 1e6))
    return 0


if __name__ == "__main__":
    sys.exit(main())
