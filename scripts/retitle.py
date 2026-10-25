#!/usr/bin/env python3
"""Fix titles, series and author names in books epubkit already wrote.

    python3 scripts/retitle.py FOLDER            # show what would change
    python3 scripts/retitle.py FOLDER --apply    # change it

For books that went through a run before epubkit learned to take
"(Detective Gina Harte Book 5)" and "A totally gripping crime thriller" out
of a title. Only the package file's title, author and series lines change;
every other file in the book - text, koboSpans, recaps - is copied byte for
byte and checked, so reading positions and recaps are untouched. The file is
then renamed the way epubkit names books.
"""
from __future__ import annotations

import argparse
import html
import os
import re
import sys
import tempfile
import zipfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))
from epubkit.tools import series_hint as S  # noqa: E402

TEMPLATE = "{series} {index:02d} - {title} - {authors}"


def _opf_name(z):
    c = z.read("META-INF/container.xml").decode("utf-8", "ignore")
    return re.search(r'full-path="([^"]+)"', c).group(1)


def read(path):
    with zipfile.ZipFile(path) as z:
        opf = z.read(_opf_name(z)).decode("utf-8")
    def one(p):
        m = re.search(p, opf, re.S)
        return html.unescape(m.group(1).strip()) if m else None
    return opf, {
        "title": one(r"<dc:title\b[^>]*>(.*?)</dc:title>"),
        "authors": [html.unescape(a.strip()) for a in
                    re.findall(r"<dc:creator\b[^>]*>(.*?)</dc:creator>", opf, re.S)],
        "series": one(r'<meta name="calibre:series" content="([^"]*)"'),
        "series_index": S._index(one(r'<meta name="calibre:series_index" content="([^"]*)"')),
    }


def rewrite_opf(opf, new):
    esc = lambda s: html.escape(s, quote=False)
    opf = re.sub(r"(<dc:title\b[^>]*>).*?(</dc:title>)",
                 lambda m: m.group(1) + esc(new["title"]) + m.group(2), opf, count=1, flags=re.S)
    names = iter(new["authors"])
    opf = re.sub(r"(<dc:creator\b[^>]*>).*?(</dc:creator>)",
                 lambda m: m.group(1) + esc(next(names, "")) + m.group(2), opf, flags=re.S)
    if new.get("series"):
        opf = re.sub(r'\s*<meta name="calibre:series(?:_index)?" content="[^"]*"\s*/>', "", opf)
        idx = new.get("series_index")
        add = '\n    <meta name="calibre:series" content="%s"/>' % html.escape(new["series"])
        if idx is not None:
            add += '\n    <meta name="calibre:series_index" content="%s"/>' % float(idx)
        opf = opf.replace("</metadata>", add + "</metadata>", 1)
    return opf


def write(path, opf_text):
    """Same files, same order, same compression; only the OPF differs."""
    with zipfile.ZipFile(path) as z:
        name = _opf_name(z)
        infos = z.infolist()
        data = {i.filename: z.read(i.filename) for i in infos}
    fd, tmp = tempfile.mkstemp(suffix=".epub")
    os.close(fd)
    with zipfile.ZipFile(tmp, "w") as out:
        for i in infos:
            b = opf_text.encode("utf-8") if i.filename == name else data[i.filename]
            out.writestr(i, b, compress_type=i.compress_type)
    with zipfile.ZipFile(tmp) as z:
        if z.testzip():
            raise RuntimeError("rewritten file fails its CRC check")
        for i in infos:
            if i.filename != name and z.read(i.filename) != data[i.filename]:
                raise RuntimeError("%s changed" % i.filename)
    with open(tmp, "rb") as src, open(path, "r+b") as dst:   # same file, same inode
        dst.truncate(0)
        dst.write(src.read())
    os.unlink(tmp)


def filename(meta, ext):
    authors = ", ".join(meta["authors"]) or "Unknown"
    if meta.get("series") and meta.get("series_index") is not None:
        n = TEMPLATE.format(series=meta["series"], index=int(float(meta["series_index"])),
                            title=meta["title"], authors=authors)
    else:
        n = "%s - %s" % (meta["title"], authors)
    return re.sub(r'[\\/:*?"<>|]+', " ", n).strip() + ext


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("folder")
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args(argv)
    books = sorted(os.path.join(a.folder, f) for f in os.listdir(a.folder)
                   if f.endswith(".epub") and not f.startswith("."))
    cache = os.path.join(tempfile.gettempdir(), "retitle-series-lists.json")
    if os.path.exists(cache):
        os.unlink(cache)
    metas = {}
    for b in books:                      # every list first, so order does not matter
        opf, m = read(b)
        m, _ = S.tidy(m)
        S.from_book(b, m, cache=cache)
        metas[b] = (opf, m)
    changed = 0
    for b in books:
        opf, m = metas[b]
        _o, before = read(b)
        m, notes = S.tidy(before)
        if not m.get("series") or m.get("series_index") is None:
            s, i, how = S.from_book(b, m, cache=cache)
            if s:
                m["series"], m["series_index"] = s, i
                notes.append("series #%s from %s" % (i, how))
        ext = ".kepub.epub" if b.endswith(".kepub.epub") else ".epub"
        new_name = filename(m, ext)
        same = (m["title"], m["authors"], m.get("series"), m.get("series_index")) == \
            (before["title"], before["authors"], before.get("series"), before.get("series_index"))
        if same and os.path.basename(b) == new_name:
            continue
        changed += 1
        print("%s\n  -> %s%s" % (os.path.basename(b), new_name,
                                 ("\n     " + "; ".join(notes)) if notes else ""))
        if a.apply:
            if not same:
                write(b, rewrite_opf(opf, m))
            target = os.path.join(a.folder, new_name)
            if os.path.basename(b) != new_name:
                if os.path.exists(target):
                    print("     NOT renamed: %s already exists" % new_name)
                else:
                    os.rename(b, target)
    print("\n%d book(s) %s." % (changed, "changed" if a.apply else "would change (add --apply)"))


if __name__ == "__main__":
    main()
