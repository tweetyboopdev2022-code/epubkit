#!/usr/bin/env python3
"""Minimal kepub converter: applies the koboSpan transform kepubify does.

Written because the kepubify binary is not reachable from this environment.
Transform, per Kobo's own kepub format:
  - every run of text inside the body is wrapped in
    <span class="koboSpan" id="kobo.<para>.<seg>">
  - body children are wrapped in <div id="book-columns"><div id="book-inner">
  - a #kobostylehacks style block is added to the head
  - the file is written as <name>.kepub.epub
Everything else in the archive is carried across byte-identical.
"""
import re, sys, os, html, zipfile, shutil
from lxml import etree

XH = "http://www.w3.org/1999/xhtml"
X = "{%s}" % XH

SKIP = {X + t for t in ("script", "style", "svg", "math", "head", "title", "pre", "textarea")}
BLOCK = {X + t for t in (
    "p", "div", "h1", "h2", "h3", "h4", "h5", "h6", "li", "td", "th",
    "blockquote", "dd", "dt", "figcaption", "caption", "section", "article", "aside")}

# split after sentence-ending punctuation (incl. closing quotes/brackets) + space
SENT = re.compile(r'(?<=[.!?…])(?=["\'’”\)\]]*\s)')

STYLE = ("div#book-inner { margin-top: 0; margin-bottom: 0; }\n"
         "div#book-columns { width: auto; }\n")

# XHTML 1.1 (EPUB 2 content docs) does not allow id/class on <style> - the DTD
# only permits dir/lang/media/title/xml:lang/xml:space there - so on EPUB 2 we
# match the existing block by its text instead of by id. This also fixes a
# real bug: earlier kepub output used id="kobostylehack" (no trailing s) while
# this check looked for "kobostylehacks", so the id never matched and a
# second pass kept stacking duplicate style blocks. Matching on content is
# immune to either spelling.
XHTML11_DOCTYPE = ('<!DOCTYPE html PUBLIC "-//W3C//DTD XHTML 1.1//EN" '
                   '"http://www.w3.org/TR/xhtml11/DTD/xhtml11.dtd">')


def opf_version(z):
    """EPUB package version ("2.0", "3.0", ...), defaulting to 2.0 - the
    stricter format - when no OPF can be found."""
    opf = next((n for n in z.namelist() if n.endswith(".opf")), None)
    if not opf:
        return "2.0"
    raw = z.read(opf).decode("utf-8", "ignore")
    m = re.search(r'<[^>]*\bpackage\b[^>]*\bversion="([^"]+)"', raw)
    return m.group(1) if m else "2.0"


def segments(text):
    """Split a text run into sentence-ish segments, keeping all characters."""
    parts = SENT.split(text)
    out = []
    for p in parts:
        if not p:
            continue
        if out and (not p.strip() or not out[-1].strip()):
            out[-1] += p           # never leave a whitespace-only span
        else:
            out.append(p)
    return out or [text]


class Spanner:
    def __init__(self):
        self.para = 0
        self.seg = 0

    def span(self, text):
        self.seg += 1
        e = etree.Element(X + "span")
        e.set("class", "koboSpan")
        e.set("id", "kobo.%d.%d" % (self.para, self.seg))
        e.text = text
        return e

    def wrap_run(self, parent, index, text):
        """Insert spans for `text` at `index` in parent; returns count inserted."""
        if not text or not text.strip():
            return 0
        made = [self.span(s) for s in segments(text)]
        for off, e in enumerate(made):
            parent.insert(index + off, e)
        return len(made)

    def walk(self, el):
        if el.tag in SKIP or not isinstance(el.tag, str):
            return
        if el.tag in BLOCK:
            self.para += 1
            self.seg = 0

        # snapshot the ORIGINAL children before we insert any spans of our own,
        # otherwise we would walk into the spans we just created.
        kids = [c for c in el if isinstance(c.tag, str)]

        # element's own leading text
        if el.text and el.text.strip():
            txt, el.text = el.text, None
            self.wrap_run(el, 0, txt)

        for child in kids:
            self.walk(child)
            if child.tail and child.tail.strip():
                txt, child.tail = child.tail, None
                self.wrap_run(el, el.index(child) + 1, txt)


# A bare "&" is invalid XML. lxml's recovering parser drops it silently, which
# turns "Good & Plenty" into "Good Plenty" - a real loss of text that only shows
# up if you compare before and after. Escape any ampersand that does not already
# begin a character entity, before the document is parsed at all.
BARE_AMP = re.compile(rb"&(?![a-zA-Z][a-zA-Z0-9]{0,31};|#[0-9]{1,8};|#x[0-9a-fA-F]{1,8};)")


def convert_doc(data, version="2.0"):
    data = BARE_AMP.sub(b"&amp;", data)
    parser = etree.XMLParser(recover=True, resolve_entities=False, huge_tree=True)
    try:
        tree = etree.fromstring(data, parser)
    except Exception:
        return None
    if tree is None:
        return None
    root = tree.getroottree().getroot()
    body = root.find(X + "body")
    if body is None:
        return None
    if body.find(".//" + X + "span[@class='koboSpan']") is not None:
        return None  # already converted

    Spanner().walk(body)

    # wrap body contents
    outer = etree.Element(X + "div"); outer.set("id", "book-columns")
    inner = etree.SubElement(outer, X + "div"); inner.set("id", "book-inner")
    inner.text = body.text; body.text = None
    for c in list(body):
        body.remove(c); inner.append(c)
    body.append(outer)

    # style hack - matched by content, not id, so both spellings of the id
    # kepubify has used over time ("kobostylehack"/"kobostylehacks") are
    # recognised and we never stack a duplicate block on a repeat pass.
    head = root.find(X + "head")
    is_epub3 = str(version).startswith("3")
    if head is not None and not any((e.text or "") == STYLE for e in head.findall(X + "style")):
        st = etree.SubElement(head, X + "style")
        st.set("type", "text/css")
        if is_epub3:
            st.set("id", "kobostylehacks")  # only valid on EPUB 3 content docs
        st.text = STYLE

    doctype = '<!DOCTYPE html>' if is_epub3 else XHTML11_DOCTYPE
    out = etree.tostring(root.getroottree(), xml_declaration=True,
                         encoding="utf-8", doctype=doctype)
    return out


def already_kepub(z):
    """A file that carries the .kepub.epub name is not necessarily a kepub
    inside. What matters is whether the content documents carry koboSpans."""
    docs = [n for n in z.namelist() if n.lower().endswith((".xhtml", ".html", ".htm"))]
    if not docs:
        return True
    hits = sum(1 for n in docs if b"koboSpan" in z.read(n))
    return hits * 2 >= len(docs)


def is_fixed_layout(z):
    opf = next((n for n in z.namelist() if n.endswith(".opf")), None)
    if not opf:
        return False
    raw = z.read(opf).decode("utf-8", "ignore")
    return "pre-paginated" in raw or "fixed-layout" in raw


def convert(src, dst):
    zin = zipfile.ZipFile(src)
    version = opf_version(zin)
    names = zin.namelist()
    docs = [n for n in names if n.lower().endswith((".xhtml", ".html", ".htm"))]
    changed = 0
    zout = zipfile.ZipFile(dst, "w", zipfile.ZIP_DEFLATED)
    zout.writestr("mimetype", "application/epub+zip", zipfile.ZIP_STORED)
    for info in zin.infolist():
        if info.filename == "mimetype":
            continue
        data = zin.read(info.filename)
        if info.filename in docs:
            new = convert_doc(data, version)
            if new:
                data = new; changed += 1
        zout.writestr(info, data)
    zout.close(); zin.close()
    return changed, len(docs)


def body_text(data):
    """Visible text of a document body, whitespace-normalised.

    Deliberately does NOT go through the XML parser. The converted file carries a
    different doctype from the source, and lxml treats entity references
    differently depending on the doctype and on resolve_entities - which made
    "Good &amp; Plenty" compare unequal to itself and produced false TEXT
    MISMATCH refusals on books that had converted perfectly. Stripping tags and
    unescaping by hand is dumber and gives the same answer on both sides.
    """
    try:
        text = data.decode("utf-8", "ignore") if isinstance(data, bytes) else data
    except Exception:
        return None
    m = re.search(r"<body[^>]*>(.*)</body>", text, re.S | re.I)
    if not m:
        return None
    body = m.group(1)
    body = re.sub(r"<(script|style)\b.*?</\1>", " ", body, flags=re.S | re.I)
    body = re.sub(r"<!--.*?-->", " ", body, flags=re.S)
    body = re.sub(r"<[^>]+>", "", body)
    return re.sub(r"\s+", " ", html.unescape(body)).strip()


def verify(src, dst):
    """Confirm every document's visible text survived the transform."""
    za, zb = zipfile.ZipFile(src), zipfile.ZipFile(dst)
    docs = mism = spans = blank = 0
    for n in za.namelist():
        if not n.lower().endswith((".xhtml", ".html", ".htm")):
            continue
        docs += 1
        if body_text(za.read(n)) != body_text(zb.read(n)):
            mism += 1
        raw = zb.read(n).decode("utf-8", "ignore")
        spans += raw.count("koboSpan")
        blank += len(re.findall(r'<span class="koboSpan" id="kobo\.\d+\.\d+">\s*</span>', raw))
    za.close(); zb.close()
    return docs, mism, spans, blank


def convert_in_place(path):
    """Convert a book that is named .kepub.epub but has no koboSpans inside.

    Always verifies the visible text before swapping the file in, because there
    is no original left to compare against afterwards.
    """
    z = zipfile.ZipFile(path)
    if is_fixed_layout(z):
        z.close(); return "fixed-layout"
    if already_kepub(z):
        z.close(); return "already kepub"
    z.close()

    # Build the candidate OUTSIDE the folder being edited. A synced or removable
    # volume may refuse deletes, which would strand a .tmp beside the book (and
    # sync it to the cloud); scratch space we control never has that problem.
    scratch = os.path.join(os.path.expanduser("~"), ".kepubify-scratch")
    os.makedirs(scratch, exist_ok=True)
    tmp = os.path.join(scratch, os.path.basename(path) + ".candidate")
    try:
        changed, total = convert(path, tmp)
        docs, mism, spans, blank = verify(path, tmp)
        if mism:
            return "TEXT MISMATCH in %d of %d docs - left alone" % (mism, docs)
        if not changed:
            return "nothing to do"
        with open(tmp, "rb") as src, open(path, "wb") as dst:
            shutil.copyfileobj(src, dst)
        # the file that now sits on disk is the one we verified, so re-open it
        with zipfile.ZipFile(path) as check:
            if check.testzip() is not None:
                return "!! written file failed its CRC check"
        return "converted %d/%d docs, %d spans" % (changed, total, spans)
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass


def main(argv):
    import argparse
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("src", help="folder of .epub files (or a single .epub)")
    ap.add_argument("out", nargs="?", help="folder to write .kepub.epub files into "
                                           "(omit with --in-place)")
    ap.add_argument("--verify", action="store_true",
                    help="compare visible text before and after, per document")
    ap.add_argument("--in-place", action="store_true",
                    help="rewrite each book where it sits, keeping its name. Skips "
                         "fixed-layout books and anything already carrying koboSpans, "
                         "and never swaps a file in unless its text verified identical")
    ap.add_argument("--recursive", action="store_true",
                    help="also walk subfolders")
    ap.add_argument("--skip-existing", action="store_true",
                    help="leave books that already have a .kepub.epub in the "
                         "output folder alone, so an interrupted run can be "
                         "restarted without redoing the ones that finished")
    a = ap.parse_args(argv)

    if a.in_place:
        def walk(folder):
            for name in sorted(os.listdir(folder)):
                full = os.path.join(folder, name)
                if a.recursive and os.path.isdir(full) and not name.startswith("."):
                    for x in walk(full):
                        yield x
                elif os.path.isfile(full) and name.endswith(".epub") \
                        and not name.startswith("."):
                    yield full
        done = skipped = failed = 0
        for p in walk(a.src):
            r = convert_in_place(p)
            if r.startswith("converted"):
                done += 1
                print("  %-58s %s" % (os.path.basename(p)[:58], r))
            elif "MISMATCH" in r:
                failed += 1
                print("  %-58s !! %s" % (os.path.basename(p)[:58], r))
            else:
                skipped += 1
        print("\n%d converted, %d already fine or fixed-layout, %d refused on verification"
              % (done, skipped, failed))
        return 1 if failed else 0

    os.makedirs(a.out, exist_ok=True)
    if os.path.isfile(a.src):
        files, srcdir = [os.path.basename(a.src)], os.path.dirname(a.src) or "."
    else:
        files, srcdir = sorted(os.listdir(a.src)), a.src

    tot = bad = allspans = allblank = 0
    for f in files:
        if not f.endswith(".epub") or f.endswith(".kepub.epub"):
            continue
        src = os.path.join(srcdir, f)
        dst = os.path.join(a.out, f[:-5] + ".kepub.epub")
        if a.skip_existing and os.path.exists(dst):
            continue
        c, t = convert(src, dst)
        line = "%-58s %3d/%-3d docs %5.1f MB" % (f[:58], c, t, os.path.getsize(dst) / 1e6)
        if a.verify:
            docs, mism, spans, blank = verify(src, dst)
            tot += docs; bad += mism; allspans += spans; allblank += blank
            line += "  %s" % ("text OK" if not mism else "!! %d MISMATCH" % mism)
        print(line)

    if a.verify:
        print("\n%d documents compared, %d text mismatches, %s koboSpans, %d blank spans"
              % (tot, bad, format(allspans, ","), allblank))
        return 1 if bad else 0
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
