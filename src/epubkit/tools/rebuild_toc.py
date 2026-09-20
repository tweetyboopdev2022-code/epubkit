#!/usr/bin/env python3
"""Rebuild a thin NCX from the headings the spine documents actually carry.

Numeric headings ("1", "2", ...) become "Chapter N" nested under the most recent
non-numeric heading (a part title); everything else sits at the top level.
Only touches books whose existing TOC is far smaller than their spine.
"""
import sys, os, re, io, html, zipfile
import xml.etree.ElementTree as ET

OPF = "{http://www.idpf.org/2007/opf}"
DC = "{http://purl.org/dc/elements/1.1/}"
NCXNS = "http://www.daisy.org/z3986/2005/ncx/"
FRONT = {"titlepage": "Cover", "title": "Title Page", "copy": "Copyright",
         "copyright": "Copyright", "ded": "Dedication", "dedication": "Dedication",
         "toc": "Contents", "epi": "Epigraph", "ack": "Acknowledgements",
         "ata": "About the Author", "atr": "About the Author", "bm": "About the Author"}


def headings(raw):
    m = re.search(r"<h[1-3][^>]*>(.*?)</h[1-3]>", raw, re.S | re.I)
    if not m:
        return None
    t = re.sub(r"<[^>]+>", " ", m.group(1))
    t = html.unescape(re.sub(r"\s+", " ", t)).strip()
    return t or None


def build(path, min_ratio=0.25):
    z = zipfile.ZipFile(path)
    opf = [n for n in z.namelist() if n.endswith(".opf")][0]
    base = opf.rsplit("/", 1)[0] + "/" if "/" in opf else ""
    root = ET.fromstring(z.read(opf))
    man = {i.get("id"): i.get("href") for i in root.iter(OPF + "item")}
    spine = [man[r.get("idref")] for r in root.iter(OPF + "itemref") if man.get(r.get("idref"))]
    ncxname = next((n for n in z.namelist() if n.endswith(".ncx")), None)
    if not ncxname:
        return None, "no ncx"

    old = ET.fromstring(z.read(ncxname))
    oldn = len(list(old.iter("{%s}navPoint" % NCXNS)))
    if oldn >= max(4, len(spine) * min_ratio):
        return None, "toc already %d entries for %d pages" % (oldn, len(spine))

    ident = next((e.text for e in root.iter(DC + "identifier") if e.text), None)
    ident = ident or os.path.basename(path)[:40]

    items = []
    for h in spine:
        stem = os.path.splitext(os.path.basename(h))[0].lower()
        raw = z.read(base + h).decode("utf-8", "ignore")
        t = headings(raw)
        key = re.sub(r"\d+$", "", stem)
        if t and re.fullmatch(r"[\dIVXLC]+", t):
            items.append((1, "Chapter " + t, h))
        elif t:
            items.append((0, t if t.isupper() is False else t.title(), h))
        elif key in FRONT:
            items.append((0, FRONT[key], h))

    n = 0
    out = ['<?xml version="1.0" encoding="utf-8"?>',
           '<ncx version="2005-1" xmlns="%s">' % NCXNS, "<head>",
           '<meta name="dtb:uid" content="%s"/>' % html.escape(ident),
           '<meta name="dtb:depth" content="2"/>',
           '<meta name="dtb:totalPageCount" content="0"/>',
           '<meta name="dtb:maxPageNumber" content="0"/>', "</head>",
           "<docTitle><text>%s</text></docTitle>" % html.escape(os.path.basename(path)),
           "<navMap>"]
    open_part = False
    for lvl, lbl, href in items:
        n += 1
        ref = href[len(base):] if href.startswith(base) else href
        node = ('<navPoint id="np%d" playOrder="%d"><navLabel><text>%s</text></navLabel>'
                '<content src="%s"/>' % (n, n, html.escape(lbl), html.escape(ref)))
        if lvl == 0:
            if open_part:
                out.append("</navPoint>")
            out.append(node)
            open_part = True
        else:
            out.append(node + "</navPoint>")
    if open_part:
        out.append("</navPoint>")
    out.append("</navMap></ncx>")
    ncx = "\n".join(out).encode()
    ET.fromstring(ncx)

    # Never trade a table of contents for a worse one. A play, a graphic novel or
    # any book that marks its chapters with styled <p> rather than <h1> yields no
    # headings here, and the rebuild would then write an EMPTY navMap over a
    # perfectly good one. (Cursed Child: 14 real entries replaced by 0.) The only
    # rebuild worth doing is one that ends up with strictly more entries than the
    # book already had.
    if n <= oldn or n < 2:
        z.close()
        return None, "kept existing toc (%d entries; rebuild would give %d)" % (oldn, n)

    tmp = path + ".tmp"
    zo = zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED)
    zo.writestr("mimetype", "application/epub+zip", zipfile.ZIP_STORED)
    for it in z.infolist():
        if it.filename == "mimetype":
            continue
        zo.writestr(it, ncx if it.filename == ncxname else z.read(it.filename))
    zo.close(); z.close()
    os.replace(tmp, path)
    return n, "was %d entries for %d pages" % (oldn, len(spine))


if __name__ == "__main__":
    for f in sorted(os.listdir(sys.argv[1])):
        if not f.endswith(".epub"):
            continue
        try:
            n, why = build(os.path.join(sys.argv[1], f))
        except Exception as e:
            print("skipped  %-56s %s: %s" % (f[:56], type(e).__name__, e))
            continue
        if n:
            print("rebuilt  %-56s %3d entries  (%s)" % (f[:56], n, why))
