#!/usr/bin/env python3
"""Work out where an omnibus's books actually start and stop, and draft the plan.

`split_omnibus.py --analyse` prints one line per spine item, which is 523 lines
for a seven-book bundle. This prints only the runs, and writes a split.json with
the ranges and titles already filled in, so the only thing left to supply is
researched metadata.

Boundaries come from three signals, best first:
  1. the directory each spine document sits in (epubmerge nests one folder per
     book, and nests again for a bundle of bundles)
  2. the manifest id prefix, for merges that kept a flat layout
  3. top-level table-of-contents entries

Run it, read the runs, then hand the draft to the metadata step.
"""
import sys, os, re, json, zipfile, argparse
from collections import Counter
import xml.etree.ElementTree as ET

OPF = "{http://www.idpf.org/2007/opf}"
NCX = "{http://www.daisy.org/z3986/2005/ncx/}"
NOISE = re.compile(r"^(cover|title|copyright|contents|toc|dedication|about the author|"
                   r"about the book|also by |by |sneak peek|excerpt|an extract|"
                   r"other titles|more from|more by|praise|reading group|"
                   r"discussion questions|newsletter|bonus|teaser|glossary|index|"
                   r"notes|acknowledge)", re.I)
# A run labelled "Chapter 12" or "Part Two" is a division of one novel, not a book.
WORDNUM = ("one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|"
           "thirteen|fourteen|fifteen|sixteen|seventeen|eighteen|nineteen|twenty|"
           "thirty|forty|fifty")
CHAPTERISH = re.compile(r"^(chapter|part|book|section|prologue|epilogue|interlude|"
                        r"foreword|preface|introduction|afterword|appendix|coda|"
                        r"\d+\s*[.:)\u2013-]|"          # "1: Syria, October 1973"
                        r"\d{1,3}\s+[A-Za-z]|"          # "1 The Decanter of Tokay"
                        r"(%s)\b|"                      # "Three", "Thirteen"
                        r"[\dIVXLC]+[.:) ]*$)" % WORDNUM, re.I)
# A file that really is a bundle nearly always says so somewhere in its own title.
BUNDLE = re.compile(r"\b(omnibus|anthology|collection|trilogy|quartet|trilogia|"
                    r"box(ed)? set|complete series|\d+[- ]book|books? \d+\s*[-\u2013]\s*\d+)\b",
                    re.I)


MIN_BOOK_BYTES = 150_000        # a novel's markup; a chapter or a season is far less


def load(path):
    z = zipfile.ZipFile(path)
    opf = next(n for n in z.namelist() if n.endswith(".opf"))
    base = opf.rsplit("/", 1)[0] + "/" if "/" in opf else ""
    root = ET.fromstring(z.read(opf))
    man = {i.get("id"): i.get("href") for i in root.iter(OPF + "item")}
    ids, spine = [], []
    for r in root.iter(OPF + "itemref"):
        h = man.get(r.get("idref"))
        if h:
            ids.append(r.get("idref")); spine.append(h)
    sizes = []
    for h in spine:
        n = (base + h).split("#")[0]
        try:
            sizes.append(z.getinfo(n).file_size)
        except KeyError:
            sizes.append(0)
    return z, base, ids, spine, sizes


def runs_by(keys):
    out, cur, start = [], None, 0
    for i, k in enumerate(keys):
        if k != cur:
            if cur is not None:
                out.append((cur, start, i - 1))
            cur, start = k, i
    out.append((cur, start, len(keys) - 1))
    return out


def toc_map(z, base, spine):
    name = next((n for n in z.namelist() if n.endswith(".ncx")), None)
    if not name:
        return []
    root = ET.fromstring(z.read(name))
    nav = root.find(NCX + "navMap")
    if nav is None:
        return []
    pos = {h.split("#")[0]: i for i, h in enumerate(spine)}
    out = []

    def walk(node, depth):
        for np in node.findall(NCX + "navPoint"):
            lbl = np.find(NCX + "navLabel/" + NCX + "text")
            c = np.find(NCX + "content")
            src = (c.get("src", "") if c is not None else "").split("#")[0]
            if lbl is not None:
                for cand in (src, base + src,
                             src[len(base):] if src.startswith(base) else src):
                    if cand in pos:
                        out.append((pos[cand], (lbl.text or "").strip(), depth))
                        break
            if depth < 2:
                walk(np, depth + 1)

    walk(nav, 0)
    return out


def clean_title(t):
    t = re.sub(r"\s*[:\-–]\s*[^:]*\bbook\s+(?:\w+|\d+)\s*$", "", t, flags=re.I)
    t = re.sub(r"\s*\bAnthology\b\s*$", "", t, flags=re.I).strip()
    t = re.sub(r"\s*[-–]\s*\d+\s*$", "", t).strip()
    return t



# --------------------------------------------------------------------------
# PDFs
#
# A bundle in PDF form has no manifest, no spine and no table of contents to
# read, so none of the machinery above applies. What a printed book does carry
# is a page number on nearly every page and a running head naming the work.
# Both are continuous within one book and both break at the seam between two:
# the numbering falls back towards 1 and the head starts naming something else.
# That is the whole test. It reports; it does not split, because splitting a PDF
# well means re-deriving the text anyway (see pdf2epub.py).
# --------------------------------------------------------------------------

def _pdf_open(path):
    try:
        import pymupdf
    except ImportError:
        try:
            import fitz as pymupdf
        except ImportError:
            raise SystemExit("this needs PyMuPDF:  pip3 install pymupdf")
    return pymupdf.open(path)


def _edge_lines(page, frac=0.12):
    """Text lines sitting in the top or bottom band of the page."""
    h = page.rect.height
    out = []
    for b in page.get_text("dict")["blocks"]:
        for l in b.get("lines", []):
            t = "".join(sp["text"] for sp in l["spans"]).strip()
            if t and (l["bbox"][1] < h * frac or l["bbox"][1] > h * (1 - frac)):
                out.append(t)
    return out


def pdf_scan(path, max_pages=0):
    """Printed page number and running-head wording for every page."""
    doc = _pdf_open(path)
    n = len(doc) if not max_pages else min(len(doc), max_pages)
    nums, heads = [], []
    for i in range(n):
        cands, words = [], []
        for t in _edge_lines(doc[i]):
            for m in re.finditer(r"\b(\d{1,4})\b", t):
                v = int(m.group(1))
                if 1 <= v <= 3000:
                    cands.append(v)
            w = re.sub(r"[^a-z ]", "", t.lower()).strip()
            if len(w) >= 6:
                words.append(re.sub(r"\s+", " ", w))
        nums.append(cands)
        heads.append(words)
    doc.close()
    return nums, heads


def pdf_sequence(nums):
    """Follow the printed numbering, picking the reading nearest to +1 each page.

    A page carries several numbers (a date in the head, a misread digit), so the
    number that continues the sequence is the one to believe. Pages with no
    reading at all are common - a chapter opening usually drops its head - and
    simply carry the expectation forward.

    The catch: a reading far from what we expect is usually a misread, but at the
    seam of a bundle it is the new book starting again at 1. Rejecting it outright
    makes the sequence sail past the restart and never recover, which hides
    exactly what this is for. So a far-off reading is held, and if the pages after
    it keep counting up from there, the sequence re-anchors to the new book.
    """
    seq, expect, pending = [], None, []
    for cands in nums:
        if not cands:
            seq.append(None)
            pending.append(None)
        elif expect is None:
            pick = min(cands)
            seq.append(pick)
            expect = pick + 1
            pending = []
            continue
        else:
            near = min(cands, key=lambda v: abs(v - expect))
            if abs(near - expect) <= 30:
                seq.append(near)
                expect = near + 1
                pending = []
                continue
            seq.append(None)
            pending.append(cands)

        # Does the recent run of rejects count up on its own? Then it is a new
        # book's numbering, not noise.
        run = [c for c in pending if c]
        if len(run) >= 3:
            anchor = min(run[0])
            chain = [anchor]
            for c in run[1:]:
                nxt = min(c, key=lambda v: abs(v - (chain[-1] + 1)))
                if abs(nxt - (chain[-1] + 1)) > 1:
                    chain = None
                    break
                chain.append(nxt)
            if chain:
                j = len(seq) - 1
                for v in reversed(chain):
                    while j >= 0 and seq[j] is not None:
                        j -= 1
                    if j < 0:
                        break
                    seq[j] = v
                    j -= 1
                expect = chain[-1] + 1
                pending = []
        if expect is not None and seq[-1] is None:
            expect += 1
    return seq


def pdf_boundaries(path, min_run=15):
    """Where a PDF looks like it stops being one book and starts being another.

    Two independent signals, both needing to hold over a run of pages so that a
    single OCR slip cannot raise a boundary on its own:
      - the printed numbering falls back towards 1 and then climbs again
      - the running head settles on different wording
    """
    nums, heads = pdf_scan(path)
    seq = pdf_sequence(nums)
    read = [v for v in seq if v is not None]
    hits = []

    for i in range(1, len(seq)):
        a, b = seq[i - 1], seq[i]
        if a is None or b is None or not (b < a - 20 and b <= 20):
            continue
        after = [v for v in seq[i:i + min_run] if v is not None]
        if len(after) >= 3 and after == sorted(after):
            hits.append((i, "numbering restarts at %d after %d" % (b, a)))

    # Where does each running head live in the document?
    #
    # A sliding window cannot answer this. The heads alternate - author on
    # versos, title on rectos - so any window straddling a seam holds half of
    # each and both fall below whatever threshold you set, which is precisely
    # the region you needed to see. Asking instead where each head FIRST and
    # LAST appears is immune to that: in one book every head spans the whole
    # document; in a bundle one head stops where another starts.
    N = len(heads)
    span = {}
    for i, hs in enumerate(heads):
        for h in set(hs):
            f, l, c = span.get(h, (i, i, 0))
            span[h] = (min(f, i), max(l, i), c + 1)
    common = {h: (f, l) for h, (f, l, c) in span.items() if c >= max(5, N * 0.03)}
    for h, (f, l) in common.items():
        if l > N * 0.9:
            continue                       # this head runs to the end: no seam here
        for g, (gf, gl) in common.items():
            if g == h or gf < N * 0.1 or gf < l or gf - l > 40:
                continue
            if h in g or g in h:
                continue
            hits.append(((l + gf) // 2,
                         "running head %r stops at page %d, %r starts at page %d"
                         % (h[:34], l + 1, g[:34], gf + 1)))

    step = max(min_run, 20)
    merged, last = [], -10 ** 9
    for i, why in sorted(hits):
        if i - last > step:
            merged.append((i, why))
        last = i
    return len(seq), read, merged


def report_pdf(path, min_run):
    pages, read, hits = pdf_boundaries(path, min_run)
    print("%s\n  %d pages, printed numbers read on %d of them"
          % (os.path.basename(path), pages, len(read)))
    if len(read) < pages * 0.25:
        print("  NOT ENOUGH PAGE NUMBERS TO JUDGE - this looks like a scan with "
              "no usable text layer, or a book that prints no folios.\n"
              "  Check it by eye before assuming it holds one work.")
        return 0
    print("  numbering runs %d -> %d" % (read[0], read[-1]))
    if not hits:
        print("  no restart in the numbering and no change of running head:\n"
              "  this is ONE book.")
        return 0
    print("  %d possible seam(s) - this may be more than one book:" % len(hits))
    for i, why in hits:
        print("    at PDF page %d: %s" % (i + 1, why))
    print("  Splitting a PDF means re-deriving its text: run pdf2epub.py once "
          "per range with --skip-pages.")
    return 1


def main(argv):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("epub", metavar="FILE", help="an .epub, or a .pdf "
                    "to check (report only) for being more than one book")
    ap.add_argument("--out", default="split.draft.json")
    ap.add_argument("--min-run", type=int, default=4,
                    help="ignore runs shorter than this many spine items (default 12)")
    ap.add_argument("--author", default="", help="author for the drafted filenames")
    ap.add_argument("--series", default="", help="series name for the drafted filenames")
    a = ap.parse_args(argv)

    if a.epub.lower().endswith(".pdf"):
        return report_pdf(a.epub, a.min_run)


    z, base, ids, spine, sizes = load(a.epub)
    _opf = next(nm for nm in z.namelist() if nm.endswith(".opf"))
    raw_meta = z.read(_opf).decode("utf-8", "ignore")[:4000]

    def run_bytes(s0, e0):
        return sum(sizes[s0:e0 + 1])
    depth = max(len(h.split("/")) for h in spine)
    n = len(spine)

    candidates = []
    for d in range(1, min(3, depth)):
        candidates.append(("directory depth %d" % d,
                           ["/".join(h.split("/")[:d]) for h in spine]))
    for k in (2, 3, 4):
        candidates.append(("manifest id prefix (%d)" % k, [i[:k] for i in ids]))

    selfdesc = "%s %s" % (os.path.basename(a.epub), re.sub(r"<[^>]+>", " ", raw_meta))
    tocs = toc_map(z, base, spine)

    # The table of contents is often the STRONGEST signal, not the weakest: a
    # publisher bundle names its novels there even when the filenames and
    # manifest ids are a uniform chapter_NNN sequence. Offer it as a candidate
    # so it competes on the same "how many runs look like books" score, instead
    # of only being reached when nothing else matched.
    # Gated on the book calling itself a bundle: a novel with named parts has a
    # top-level TOC that looks exactly like a list of books, and offering that as
    # a candidate split five single novels in the regression set.
    top = [(i, l) for i, l, d in tocs if d == 0]
    if len(top) >= 2 and BUNDLE.search(selfdesc):
        keys, cur = [], -1
        marks = dict(top)
        for i in range(n):
            if i in marks:
                cur += 1
            keys.append(cur)
        candidates.append(("table of contents", keys))


    def label_runs(runs):
        """Best book-looking TOC title for each run, keyed by run start."""
        got = {}
        for idx, lbl, _d in sorted(tocs, key=lambda t: (t[0], t[2])):
            if NOISE.match(lbl):
                continue
            for _, s0, e0 in runs:
                if s0 <= idx <= e0 and s0 not in got:
                    got[s0] = clean_title(lbl)
        return got

    # The right key is the one whose runs actually look like separate books, so
    # score on how many runs carry a book-like title first, coverage second.
    best, how, labels, score = [], "", {}, (-1, -1.0)
    for label, keys in candidates:
        r = [x for x in runs_by(keys)
             if x[2] - x[1] + 1 >= a.min_run and run_bytes(x[1], x[2]) >= MIN_BOOK_BYTES]
        if not (2 <= len(r) <= 15):
            continue
        lab = label_runs(r)
        titled = sum(1 for _, s0, _ in r
                     if lab.get(s0) and not CHAPTERISH.match(lab[s0]))
        cover = sum(e - s0 + 1 for _, s0, e in r) / float(n)
        if (titled, round(cover, 2)) > score:
            best, how, labels, score = r, label, lab, (titled, round(cover, 2))

    # The table of contents is the weakest signal - a novel with named parts or
    # interludes looks exactly like a bundle through it. So only fall back to it
    # when the book itself claims to be a bundle.
    if len(best) < 2 and BUNDLE.search(selfdesc):
        toc_books = [(i, clean_title(l)) for i, l, d in tocs
                     if d == 0 and not NOISE.match(l) and not CHAPTERISH.match(l)]
        if len(toc_books) >= 2:
            cand = [("toc", toc_books[k][0],
                     (toc_books[k + 1][0] - 1) if k + 1 < len(toc_books) else n - 1)
                    for k in range(len(toc_books))]
            keep = [(t, x) for t, x in zip(toc_books, cand)
                    if run_bytes(x[1], x[2]) >= MIN_BOOK_BYTES]
            if len(keep) >= 2:
                best = [x for _, x in keep]
                labels = {x[1]: t for (i, t), x in keep}
                how = "table of contents"

    # Front and back matter often survives the label filters (a bundle's closing
    # section can be titled anything). A real book in a bundle is comparable in
    # size to its siblings, so trim a leading or trailing run that is a fraction
    # of the median. Interior runs are left alone - a genuinely short novel
    # between two long ones is still a novel.
    while len(best) > 2:
        sizes_ = sorted(run_bytes(s0, e) for _, s0, e in best)
        med = sizes_[len(sizes_) // 2]
        first, last = best[0], best[-1]
        if run_bytes(last[1], last[2]) < 0.4 * med:
            best = best[:-1]; continue
        if run_bytes(first[1], first[2]) < 0.4 * med:
            best = best[1:]; continue
        break

    # Drop trailing apparatus that slipped in: a run labelled like a chapter,
    # or unlabelled, next to runs that carry real book titles. A "Sneak Peek of
    # Iron Gold" reads as "Chapter 1: Darrow" and is not a book.
    if best:
        keep = [x for x in best
                if labels.get(x[1]) and not CHAPTERISH.match(labels[x[1]])]
        if len(keep) >= 2:
            best = keep

    # A real bundle's books account for most of the spine: 100% for a seven-book
    # merge, 87% and 64% for two others. A single book whose chapters happen to
    # be long and image-heavy produces a few scattered runs covering a quarter of
    # it - which is what a 36 MB illustrated non-fiction title did.
    if best and sum(e - s0 + 1 for _, s0, e in best) / float(n) < 0.55:
        print("%s\n%d spine items - no omnibus structure found; the %d candidate runs "
              "cover only %d%% of the book"
              % (os.path.basename(a.epub), n, len(best),
                 100 * sum(e - s0 + 1 for _, s0, e in best) // n))
        return 0

    # Two runs is the shape a novel makes when its chapter ids happen to change
    # prefix halfway through. A genuine bundle either says so in its own title or
    # has at least three book-sized parts.
    if best and len(best) < 3 and not BUNDLE.search(selfdesc):
        print("%s\n%d spine items - no omnibus structure found; %d runs and nothing "
              "in the book calls itself a bundle" % (os.path.basename(a.epub), n, len(best)))
        return 0

    # Guard against splitting a single novel: at least half the runs must carry a
    # label that reads like a book title rather than a chapter heading.
    titled = [s for _, s, _ in best if labels.get(s) and not CHAPTERISH.match(labels[s])]
    if len(best) < 2 or len(titled) * 2 < len(best):
        print("%s\n%d spine items - no omnibus structure found%s" %
              (os.path.basename(a.epub), n,
               "" if len(best) < 2 else "; the %d runs look like chapters of one book"
               % len(best)))
        return 0

    print("%s\n%d spine items, boundaries from %s\n" %
          (os.path.basename(a.epub), len(spine), how))
    books = []
    for n, (key, s, e) in enumerate(best, 1):
        title = labels.get(s, "")
        print("  %2d. [%4d-%4d]  %3d pages   %s" % (n, s, e, e - s + 1, title or "(untitled)"))
        stem = "%s %02d - %s - %s" % (a.series, n, title or "Book %d" % n, a.author) \
            if a.series else "%s - %s" % (title or "Book %d" % n, a.author)
        books.append({
            "range": [s, e],
            "out": re.sub(r'[\\/:*?"<>|]', "", stem).strip(" -") + ".epub",
            "metadata": {"title": title, "authors": [[a.author, ""]],
                         "series": a.series, "series_index": str(n),
                         "publisher": "", "date": "", "language": "en",
                         "isbn": "", "subjects": [], "description": ""}})

    json.dump({"books": books}, open(a.out, "w"), indent=1, ensure_ascii=False)
    print("\ndraft written to %s - fill in the metadata, then run split_omnibus.py --plan"
          % a.out)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
