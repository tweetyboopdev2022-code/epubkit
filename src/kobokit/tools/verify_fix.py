#!/usr/bin/env python3
"""Prove a rewritten book still contains the book.

EPUBCheck says the file is valid; it says nothing about whether the text
survived. This compares before and after and refuses anything that lost
content. The koboSpan check is the important one: ChapterIDBookmarked in
KoboReader.sqlite points at "OEBPS/file.html#kobo.N.M", so if a span id
moves, the reader's saved position lands somewhere else in the book.

    python3 verify_fix.py <original.epub> <rewritten.epub>
"""
import os, re, sys, zipfile

TAG_RE = re.compile(rb"<[^>]+>")
WS_RE = re.compile(rb"\s+")
SPAN_RE = re.compile(rb'id\s*=\s*["\'](kobo\.[\d.]+)["\']')
IMG_EXT = (".jpg", ".jpeg", ".png", ".gif", ".svg", ".webp")
CONTENT_RE = re.compile(r"\.(x?html?|xhtm)$", re.I)


def profile(path):
    z = zipfile.ZipFile(path)
    bad = z.testzip()
    if bad is not None:
        z.close()
        raise ValueError("CRC failure in %s" % bad)
    words = 0
    spans = set()
    docs = 0
    images = set()
    navpoints = 0
    for n in z.namelist():
        low = n.lower()
        if low.endswith(IMG_EXT):
            images.add(os.path.basename(low))
        if low.endswith(".ncx"):
            navpoints += len(re.findall(rb"<navPoint\b", z.read(n)))
        if not CONTENT_RE.search(n):
            continue
        if os.path.basename(n).startswith("nav"):
            # the generated nav is new content, not part of the book's text
            pass
        d = z.read(n)
        spans |= {m.group(1) for m in SPAN_RE.finditer(d)}
        if os.path.basename(n).startswith("nav"):
            continue
        docs += 1
        text = WS_RE.sub(b" ", TAG_RE.sub(b" ", d)).strip()
        words += len([w for w in text.split(b" ") if w])
    z.close()
    return {"words": words, "spans": spans, "docs": docs,
            "images": images, "navpoints": navpoints}


def compare(src, dst, word_tol=0.02):
    """Returns (ok, list of problems)."""
    a = profile(src)
    b = profile(dst)
    bad = []

    # koboSpans must be identical - this is what reading positions point at
    lost = a["spans"] - b["spans"]
    if lost:
        bad.append("lost %d koboSpans (e.g. %s)"
                   % (len(lost), sorted(lost)[0]))

    if b["docs"] < a["docs"]:
        bad.append("content documents %d -> %d" % (a["docs"], b["docs"]))

    missing_img = a["images"] - b["images"]
    if missing_img:
        bad.append("lost %d images (e.g. %s)"
                   % (len(missing_img), sorted(missing_img)[0]))

    # entities become numeric and tags change, so allow a small drift, but a
    # real loss of prose shows up far above this
    if a["words"]:
        drift = (a["words"] - b["words"]) / float(a["words"])
        if drift > word_tol:
            bad.append("word count %d -> %d (-%.1f%%)"
                       % (a["words"], b["words"], drift * 100))
    elif b["words"] == 0 and a["words"] == 0:
        pass

    return (not bad), bad, a, b


def main(argv):
    ok, bad, a, b = compare(argv[0], argv[1])
    print("words %d -> %d | docs %d -> %d | spans %d -> %d | images %d -> %d"
          % (a["words"], b["words"], a["docs"], b["docs"],
             len(a["spans"]), len(b["spans"]), len(a["images"]), len(b["images"])))
    if ok:
        print("OK")
        return 0
    for x in bad:
        print("PROBLEM:", x)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
