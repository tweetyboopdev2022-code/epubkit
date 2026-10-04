#!/usr/bin/env python3
"""Find and rebuild comics that were converted from Kindle and show blank pages.

A Kindle comic (Kindle Comic Creator, "panel view") converted to EPUB keeps
Amazon's layout: every page is one picture pinned with `position: absolute`
inside a box whose size works out to nothing, plus invisible magnification
links. The book is not marked fixed-layout, so a Kobo and Grimmory's reader
both lay it out as flowing text and the picture lands off the page. Nothing is
missing from the file - every picture is there - and every page is blank.
Dog Man 1 was the first one found.

    python3 kindle_comic.py check BOOK_OR_FOLDER...
    python3 kindle_comic.py rebuild BOOK OUT.kepub.epub

`rebuild` makes a proper fixed-layout kepub with fxl.py - one full page per
picture, the way pdf2kepub builds every other comic here - keeping the title,
authors, series, publisher, date, description and the book's own identifier,
so a library server and the Kobo still see the same book.
"""
from __future__ import annotations

import html
import io
import os
import posixpath
import re
import sys
import zipfile

# what gives a Kindle panel-view conversion away
AMAZON = re.compile(r"app-amzn-magnify|data-AmznRemoved|amzn-mag|target-mag-parent|"
                    r'name="generator" content="KC2|content="KC2', re.I)
IMG_SRC = re.compile(r'<img\b[^>]*?\bsrc="([^"]+)"', re.I)
SVG_SRC = re.compile(r'<image\b[^>]*?(?:xlink:)?href="([^"]+)"', re.I)
PICTURE = re.compile(r"\.(jpe?g|png|gif|webp)$", re.I)


def _opf(z):
    try:
        c = z.read("META-INF/container.xml").decode("utf-8", "ignore")
        m = re.search(r'full-path="([^"]+)"', c)
        if m and m.group(1) in z.namelist():
            return m.group(1)
    except KeyError:
        pass
    return next((n for n in z.namelist() if n.endswith(".opf")), None)


def _spine(z, opf_name, opf):
    base = posixpath.dirname(opf_name)
    man = {}
    for tag in re.findall(r"<item\b[^>]*>", opf):
        a = dict(re.findall(r'([\w:.-]+)\s*=\s*"([^"]*)"', tag))
        if a.get("id") and a.get("href"):
            man[a["id"]] = a["href"]
    out = []
    for idref in re.findall(r'<itemref\b[^>]*\bidref="([^"]+)"', opf):
        if idref in man:
            href = man[idref].split("#")[0]
            out.append(posixpath.normpath(posixpath.join(base, href)) if base else href)
    return out


def _visible_text(xhtml: str) -> str:
    body = xhtml.split("<body", 1)[-1]
    body = re.sub(r"(?is)<(script|style)\b.*?</\1>", " ", body)
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", body))).strip()


def _page_picture(z, page_path, xhtml, names):
    """The one picture that IS this page: the `singlepage` image when marked,
    otherwise the largest picture the page shows."""
    here = posixpath.dirname(page_path)
    marked = re.search(r'<img\b[^>]*class="[^"]*singlepage[^"]*"[^>]*>', xhtml, re.I)
    cands = []
    if marked:
        m = IMG_SRC.search(marked.group(0))
        if m:
            cands = [m.group(1)]
    if not cands:
        cands = IMG_SRC.findall(xhtml) + SVG_SRC.findall(xhtml)
    best, best_size = None, -1
    for ref in cands:
        p = posixpath.normpath(posixpath.join(here, ref.split("#")[0]))
        if p in names and PICTURE.search(p):
            size = z.getinfo(p).file_size
            if size > best_size:
                best, best_size = p, size
    return best


def _css_rules(css: str) -> dict:
    """class name -> its declarations, lower-cased, for the simple `.name {}` rules."""
    out = {}
    for sel, body in re.findall(r"([^{}]+)\{([^}]*)\}", css):
        for s in sel.split(","):
            m = re.fullmatch(r"\s*(?:[a-z0-9]*)\.([\w-]+)\s*", s, re.I)
            if m:
                out[m.group(1)] = out.get(m.group(1), "") + ";" + body.lower()
    return out


def _pinned(xhtml: str, pic_ref_name: str, rules: dict) -> bool:
    """Is the page's picture pinned with position:absolute/fixed - itself or by
    one of the boxes it sits in? That, not the Amazon markers, is what leaves
    a page blank: Bad Guys 4 carries the markers and reads fine; Dog Man 1
    pins every picture inside a zero-size box and shows nothing."""
    body = xhtml.split("<body", 1)[-1]
    at = body.find(pic_ref_name)
    if at < 0:
        return False
    head = body[:at]
    tag_start = head.rfind("<")
    img_tag = body[tag_start: body.find(">", at) + 1]
    # classes of the picture and of every element opened before it that is
    # still open is more than a regex can know; the enclosing divs of a
    # picture page are the ones opened before it and not yet closed
    opened = []
    for m in re.finditer(r"<(/?)(div|span|p|figure|section|a)\b([^>]*)>", head, re.I):
        if m.group(1):
            if opened:
                opened.pop()
        elif not m.group(3).rstrip().endswith("/"):
            opened.append(m.group(3))
    style_bits = [img_tag] + opened
    for bit in style_bits:
        st = re.search(r'style="([^"]*)"', bit)
        if st and re.search(r"position\s*:\s*(absolute|fixed)", st.group(1), re.I):
            return True
        cl = re.search(r'class="([^"]*)"', bit)
        for c in (cl.group(1).split() if cl else []):
            if re.search(r"position\s*:\s*(absolute|fixed)", rules.get(c, "")):
                return True
    return False


def check(path) -> dict:
    """What kind of book this is, as far as blank comic pages go.

    kind:
      blank-pages   - each page's picture is pinned with position:absolute in
                      a book not marked fixed-layout (Kindle panel view is the
                      usual source); pages show blank. Rebuild.
      lost-fixed-layout - built as a fixed-layout picture book, but the OPF has
                      lost its rendition:layout flag; put it back (repair_flag)
      picture-pages - one picture per page, not fixed-layout, but laid out in
                      the flow; it reads, if not always centred
      fixed-layout  - already a proper picture book
      prose         - an ordinary book
    """
    out = {"book": os.path.basename(str(path)), "kind": "prose", "pages": 0,
           "picture_pages": 0, "amazon_pages": 0, "picture_px": "", "low_res": False,
           "error": ""}
    try:
        with zipfile.ZipFile(path) as z:
            names = set(z.namelist())
            opf_name = _opf(z)
            if not opf_name:
                out["error"] = "no OPF"
                return out
            opf = z.read(opf_name).decode("utf-8", "ignore")
            spine = [p for p in _spine(z, opf_name, opf) if p in names]
            out["pages"] = len(spine)
            if "pre-paginated" in opf or re.search(r'fixed-layout"\s+content="true', opf):
                out["kind"] = "fixed-layout"
                return out
            viewport_pages = 0
            css = " ".join(z.read(n).decode("utf-8", "ignore")
                           for n in names if n.endswith(".css"))
            rules = _css_rules(css)
            pics = []
            out["pinned_pages"] = 0
            for p in spine:
                x = z.read(p).decode("utf-8", "ignore")
                for sheet in re.findall(r"<style\b[^>]*>(.*?)</style>", x, re.S | re.I):
                    rules.update(_css_rules(sheet))
                pic = _page_picture(z, p, x, names)
                if re.search(r'<meta[^>]*name="viewport"[^>]*width\s*=\s*\d+', x, re.I):
                    viewport_pages += 1
                if AMAZON.search(x):
                    out["amazon_pages"] += 1
                if pic and len(_visible_text(x)) < 200:
                    out["picture_pages"] += 1
                    pics.append(pic)
                    if _pinned(x, posixpath.basename(pic), rules):
                        out["pinned_pages"] += 1
            if AMAZON.search(css) or AMAZON.search(opf):
                out["amazon_pages"] = max(out["amazon_pages"], 1)
            share = out["picture_pages"] / max(1, len(spine))
            if len(spine) >= 8 and share >= 0.8:
                pinned = out["pinned_pages"] / max(1, out["picture_pages"])
                if viewport_pages >= 0.8 * len(spine):
                    # built as a fixed-layout book (every page declares its
                    # size) but the OPF no longer says so - Grimmory's metadata
                    # write-back drops the rendition metas
                    out["kind"] = "lost-fixed-layout"
                else:
                    out["kind"] = "blank-pages" if pinned >= 0.5 else "picture-pages"
            if pics:
                try:
                    from PIL import Image
                    sizes = []
                    for p in pics[len(pics) // 4: len(pics) // 4 + 5] or pics[:5]:
                        sizes.append(Image.open(io.BytesIO(z.read(p))).size)
                    w, h = sorted(sizes)[len(sizes) // 2]
                    out["picture_px"] = "%dx%d" % (w, h)
                    out["low_res"] = h < 1000      # a Clara panel is 1448 tall
                except Exception:                  # noqa: BLE001
                    pass
    except (zipfile.BadZipFile, OSError) as exc:
        out["error"] = str(exc)
    return out


def _metadata(opf: str) -> dict:
    def one(pat):
        m = re.search(pat, opf, re.S | re.I)
        return html.unescape(m.group(1).strip()) if m else ""

    authors = []
    for tag, name in re.findall(r"(<dc:creator\b[^>]*>)(.*?)</dc:creator>", opf, re.S):
        name = html.unescape(re.sub(r"<[^>]+>", "", name)).strip()
        fa = re.search(r'file-as="([^"]*)"', tag)
        cid = re.search(r'\bid="([^"]+)"', tag)
        if not fa and cid:
            fa = re.search(r'<meta[^>]*refines="#%s"[^>]*property="file-as"[^>]*>([^<]*)<'
                           % re.escape(cid.group(1)), opf)
        authors.append((name, html.unescape(fa.group(1)) if fa else name))
    series = one(r'name="calibre:series"\s+content="([^"]*)"') or \
        one(r'<meta[^>]*property="belongs-to-collection"[^>]*>([^<]*)<')
    index = one(r'name="calibre:series_index"\s+content="([^"]*)"') or \
        one(r'<meta[^>]*property="group-position"[^>]*>([^<]*)<')
    md = {"title": one(r"<dc:title\b[^>]*>(.*?)</dc:title>") or "Untitled",
          "authors": authors or [("Unknown", "Unknown")],
          "publisher": one(r"<dc:publisher\b[^>]*>(.*?)</dc:publisher>"),
          "date": one(r"<dc:date\b[^>]*>(.*?)</dc:date>"),
          "description": re.sub(r"<[^>]+>", "", one(r"<dc:description\b[^>]*>(.*?)</dc:description>")),
          "subjects": [html.unescape(s) for s in re.findall(r"<dc:subject\b[^>]*>(.*?)</dc:subject>", opf)],
          "series": series, "series_index": index or ""}
    if not md["series"]:
        md.pop("series")
    isbn = re.search(r"<dc:identifier[^>]*>\s*(?:urn:isbn:)?(97[89]\d{10})\s*<", opf)
    uid_ref = re.search(r'unique-identifier="([^"]+)"', opf)
    uid = None
    if uid_ref:
        uid = re.search(r'<dc:identifier[^>]*id="%s"[^>]*>([^<]+)<' % re.escape(uid_ref.group(1)), opf)
    # the book's own identifier first, so it stays the same book to Grimmory
    # and the Kobo; an ISBN only when it has nothing else
    if uid:
        md["identifier"] = uid.group(1).strip()
    elif isbn:
        md["identifier"] = "urn:isbn:" + isbn.group(1)
    return md


def rebuild(src, out_path, panel=None, quality=80) -> dict:
    """Rebuild a picture-page book as a fixed-layout kepub. Returns a summary."""
    from PIL import Image

    here = os.path.dirname(os.path.abspath(__file__))
    if here not in sys.path:
        sys.path.insert(0, here)
    import fxl
    if panel:
        w, _, h = str(panel).lower().partition("x")
        fxl.set_panel(int(w), int(h))

    with zipfile.ZipFile(src) as z:
        names = set(z.namelist())
        opf_name = _opf(z)
        opf = z.read(opf_name).decode("utf-8", "ignore")
        order, seen = [], set()
        skipped = []
        for p in _spine(z, opf_name, opf):
            if p not in names:
                continue
            x = z.read(p).decode("utf-8", "ignore")
            pic = _page_picture(z, p, x, names)
            if not pic:
                skipped.append(posixpath.basename(p))   # a generated contents page, say
                continue
            if pic not in seen:
                seen.add(pic)
                order.append(pic)
        raw = [z.read(p) for p in order]
        md = _metadata(opf)

    # the same picture twice in a row (cover, then a cover page) is one page
    def thumb(b):
        return list(Image.open(io.BytesIO(b)).convert("L").resize((16, 24)).getdata())

    th = [thumb(b) for b in raw]
    keep = [i for i in range(len(raw))
            if i == 0 or sum(abs(a - b) for a, b in zip(th[i], th[i - 1])) / len(th[i]) >= 4]  # noqa: B905
    pages = [Image.open(io.BytesIO(raw[i])) for i in keep]
    if not pages:
        raise ValueError("no page pictures found in %s" % src)
    tmp = str(out_path) + ".building"
    try:
        fxl.build(pages, tmp, md, quality=quality)
    finally:
        import shutil
        shutil.rmtree(tmp + ".parts", ignore_errors=True)
    os.replace(tmp, out_path)
    with zipfile.ZipFile(out_path) as z:
        bad = z.testzip()
        n = sum(1 for n in z.namelist() if n.startswith("OEBPS/images/"))
    if bad:
        raise ValueError("rebuilt file fails its CRC check at %s" % bad)
    return {"pages": n, "dropped_repeats": len(raw) - len(keep), "skipped_docs": skipped,
            "picture_px": "%dx%d" % pages[len(pages) // 2].size, "title": md["title"]}


RENDITION = ('<meta property="rendition:layout">pre-paginated</meta>'
             '<meta property="rendition:orientation">portrait</meta>'
             '<meta property="rendition:spread">none</meta>')


def repair_flag(path) -> bool:
    """Put back the fixed-layout flag a metadata rewrite dropped. In place.

    Only the OPF changes, and only by those three metas (plus the rendition
    prefix if that went too); every other file is checked byte for byte.
    Returns False, changing nothing, if there is nothing to do.
    """
    import shutil
    import tempfile
    with zipfile.ZipFile(path) as zin:
        opf_name = _opf(zin)
        opf = zin.read(opf_name).decode("utf-8")
        if "pre-paginated" in opf:
            return False
        new = opf.replace("</metadata>", RENDITION + "</metadata>", 1)
        if "rendition:" not in re.search(r"<package\b[^>]*>", new).group(0):
            new = re.sub(r"(<package\b)", r'\1 prefix="rendition: http://www.idpf.org/vocab/rendition/#"',
                         new, count=1) if "prefix=" not in re.search(r"<package\b[^>]*>", new).group(0) \
                else re.sub(r'(<package\b[^>]*prefix=")', r"\1rendition: http://www.idpf.org/vocab/rendition/# ",
                            new, count=1)
        fd, tmp = tempfile.mkstemp(suffix=".epubkit-tmp")
        os.close(fd)
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zout:
            for info in zin.infolist():
                data = zin.read(info.filename)
                if info.filename == opf_name:
                    data = new.encode("utf-8")
                zout.writestr(info, data)
    with zipfile.ZipFile(path) as a, zipfile.ZipFile(tmp) as b:
        assert b.testzip() is None
        for n in a.namelist():
            if n != opf_name:
                assert a.read(n) == b.read(n), n
    shutil.copyfile(tmp, path)          # same file, same path
    os.remove(tmp)
    return True


def _books(paths):
    for p in paths:
        if os.path.isdir(p):
            for root, dirs, files in os.walk(p):
                dirs[:] = [d for d in dirs if not d.startswith(".")]
                for f in sorted(files):
                    if f.lower().endswith(".epub") and not f.startswith("."):
                        yield os.path.join(root, f)
        else:
            yield p


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if not argv or argv[0] not in ("check", "rebuild"):
        print(__doc__)
        return 2
    if argv[0] == "rebuild":
        r = rebuild(argv[1], argv[2])
        print("rebuilt %s: %d pages (%d repeated page(s) dropped), pictures %s"
              % (r["title"], r["pages"], r["dropped_repeats"], r["picture_px"]))
        return 0
    found = 0
    for b in _books(argv[1:]):
        r = check(b)
        if r["kind"] in ("blank-pages", "lost-fixed-layout", "picture-pages") or r["error"]:
            found += 1
            print("%-14s %4d pages  %-9s %s  %s%s" % (
                r["kind"], r["pages"], r["picture_px"] or "?",
                "LOW-RES" if r["low_res"] else "       ", b,
                ("  ERROR " + r["error"]) if r["error"] else ""))
    print("\n%d book(s) to look at. blank-pages = pages show blank; rebuild them. "
          "picture-pages = one picture per page, not fixed-layout; it reads, "
          "but a rebuild would fit the pages to the screen." % found)
    return 0


if __name__ == "__main__":
    sys.exit(main())
