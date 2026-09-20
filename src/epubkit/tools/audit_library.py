#!/usr/bin/env python3
"""Find books whose filename and embedded metadata disagree about the series.

The filename is what the Kobo uses to identify a book; the metadata is what
builds the shelf. When they drift apart you get a shelf that reads 1, 2, 3,
3.5, 5 with a book 4 that never existed, or picture books sorting
alphabetically because their numbers live only in the metadata.

Three kinds of difference are NOT faults, and this ignores them - an audit that
cries wolf gets ignored, which is worse than no audit:

  * a colon or other character illegal in a filename. "Dragonlance: Chaos War"
    is the real series name and belongs in the metadata; the filename has to
    drop the colon, and rename_files.py --series-label exists to do exactly
    that on purpose.
  * accents composed differently on either side. macOS hands out decomposed
    filenames; the OPF usually holds precomposed ones. They are the same string.
  * &amp; and friends - the OPF is XML, the filename is not.
  * a filename series that is a shortening of the metadata one
    ("Dragonlance Icewall" for "Dragonlance: Icewall Trilogy") - also a
    deliberate --series-label choice.

Standalone books are not faults either: a title that happens to end in a number
("From a Buick 8", "The Woman in Cabin 10") is not a series number, and a
one-book series needs no number in its filename.

Usage:  python3 audit_library.py "<library root>"
"""
import sys, os, re, html, zipfile, argparse, unicodedata

ILLEGAL = r'[:/\\*?"<>|]'


def key(s):
    """Compare series names the way the filesystem forces us to write them."""
    s = unicodedata.normalize("NFC", html.unescape(s or ""))
    s = re.sub(ILLEGAL, "", s)
    return re.sub(r"\s+", " ", s).strip().lower()


def opf_of(path):
    z = zipfile.ZipFile(path)
    op = re.search(r'full-path="([^"]+)"',
                   z.read("META-INF/container.xml").decode("utf-8", "replace")).group(1)
    return z.read(op).decode("utf-8", "replace")


def meta(x, k):
    m = (re.search(r'<meta[^>]*name="%s"[^>]*content="([^"]*)"' % k, x)
         or re.search(r'<meta[^>]*content="([^"]*)"[^>]*name="%s"' % k, x))
    return html.unescape(m.group(1)) if m else ""


def main(argv):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("root")
    a = ap.parse_args(argv)

    # How many books each series has, so a lone book is not scolded for
    # having no number.
    counts = {}
    for root, dirs, files in os.walk(a.root):
        for f in files:
            if not f.endswith(".kepub.epub"):
                continue
            try:
                ser = meta(opf_of(os.path.join(root, f)), "calibre:series")
            except Exception:
                continue
            if ser:
                counts[key(ser)] = counts.get(key(ser), 0) + 1

    issues, n = [], 0
    for root, dirs, files in os.walk(a.root):
        for f in sorted(files):
            if not f.endswith(".kepub.epub"):
                continue
            n += 1
            try:
                x = opf_of(os.path.join(root, f))
            except Exception as e:
                issues.append((f, "will not open: %s" % e)); continue
            ser, idx = meta(x, "calibre:series"), meta(x, "calibre:series_index")
            m = re.match(r"^(.*?) (\d+(?:\.\d+)?) - ", f)
            if not m:
                if ser and idx and counts.get(key(ser), 0) > 1:
                    issues.append((f, "no number in the filename, but the file says "
                                      "%s #%s" % (ser, idx)))
                continue
            fs, fi = m.group(1), m.group(2)
            if not ser:
                # A standalone book whose title ends in a number. Nothing to
                # disagree with.
                continue
            if float(fi) != float(idx or -1):
                issues.append((f, "filename %s, metadata #%s" % (fi, idx)))
            elif key(fs) != key(ser) and not key(ser).startswith(key(fs)):
                issues.append((f, "filename series %r, metadata %r" % (fs, ser)))

    for f, w in issues:
        print("  %-62s %s" % (f[:62], w))
    print("\n%d books, %d disagree with themselves" % (n, len(issues)))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
