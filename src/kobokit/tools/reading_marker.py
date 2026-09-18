#!/usr/bin/env python3
"""Write down exactly where the reader is, before anything disturbs the books.

Editing a sideloaded file makes a Kobo re-import it, which throws away the
reading position. Run this first and it turns the device's bookmark into
something a person can act on: the chapter, how far in, and the actual sentence
to search for once the book comes back.

    python3 reading_marker.py <KoboReader.sqlite> <books folder> [--out FILE]
"""
import sys, os, re, html, sqlite3, zipfile, argparse, urllib.parse
import xml.etree.ElementTree as ET

OPF = "{http://www.idpf.org/2007/opf}"


def spine_of(z):
    opf = next((n for n in z.namelist() if n.endswith(".opf")), None)
    if not opf:
        return "", []
    base = opf.rsplit("/", 1)[0] + "/" if "/" in opf else ""
    root = ET.fromstring(z.read(opf))
    man = {i.get("id"): i.get("href") for i in root.iter(OPF + "item")}
    return base, [man[r.get("idref")] for r in root.iter(OPF + "itemref")
                  if man.get(r.get("idref"))]


def text_at(raw, span_id):
    """The sentence the bookmark sits on, plus the nearest heading above it."""
    heading = ""
    for m in re.finditer(r"<h[1-6][^>]*>(.*?)</h[1-6]>", raw, re.S | re.I):
        heading = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", m.group(1))).strip()
        break
    sentence = ""
    if span_id:
        m = re.search(r'<span[^>]+id="%s"[^>]*>' % re.escape(span_id), raw)
        if m:
            # One koboSpan can be three words of dialogue, which is useless to
            # search for - so read forward from the bookmark until there is
            # enough text to actually find again.
            tail = re.sub(r"<[^>]+>", " ", raw[m.start():])
            sentence = re.sub(r"\s+", " ", tail).strip()[:260]
    if not sentence:
        body = re.search(r"<body[^>]*>(.*)</body>", raw, re.S | re.I)
        if body:
            t = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", body.group(1))).strip()
            sentence = t[:160]
    return html.unescape(heading), html.unescape(sentence)


def main(argv):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("db")
    ap.add_argument("books")
    ap.add_argument("--out", default="reading-marker.md")
    a = ap.parse_args(argv)

    con = sqlite3.connect("file:%s?mode=ro" % a.db, uri=True)
    rows = con.execute(
        "SELECT ContentID,Title,Attribution,ChapterIDBookmarked,___PercentRead,"
        "DateLastRead FROM content WHERE ContentType=6 AND ReadStatus=1 "
        "AND ___PercentRead BETWEEN 1 AND 99 ORDER BY DateLastRead DESC").fetchall()

    out = ["# Where you were", "",
           "Reading positions are lost when a sideloaded book is re-imported, so "
           "these are the places to get back to. Search for the quoted sentence.", ""]
    for cid, title, author, mark, pct, when in rows:
        name = urllib.parse.unquote(cid.split("/")[-1])
        path = os.path.join(a.books, name)
        doc, span = (mark or "").split("#") if mark else ("", "")
        where = ""
        if os.path.exists(path):
            z = zipfile.ZipFile(path)
            base, spine = spine_of(z)
            idx = next((i for i, h in enumerate(spine) if h.endswith(doc.split("/")[-1])), None)
            if idx is not None:
                raw = z.read(base + spine[idx]).decode("utf-8", "ignore")
                heading, sentence = text_at(raw, span)
                where = "  - section %d of %d%s\n" % (
                    idx + 1, len(spine), " - **%s**" % heading if heading else "")
                if sentence:
                    where += '  - resume at: "%s"\n' % sentence[:300]
            z.close()
        else:
            where = "  - (book file not found next to the database)\n"
        out.append("## %s - %s" % (title, author))
        out.append("  - **%s%% in**, last read %s" % (pct, (when or "")[:10]))
        out.append(where.rstrip())
        out.append("")

    if not rows:
        out.append("_Nothing part-read - nothing to lose._")
    open(a.out, "w").write("\n".join(out))
    print("\n".join(out))
    print("\nwritten to %s" % a.out)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
