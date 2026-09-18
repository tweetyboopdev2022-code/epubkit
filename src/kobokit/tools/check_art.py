#!/usr/bin/env python3
"""Flag a book whose pictures are not in it.

InvestiGators: Case Files sat in the library for a week looking fine. It is a
graphic novel; the file held 16,518 words, 209 documents and two images - a
Calibre-made cover and one 1x1 pixel. The speech-bubble text had survived a
conversion that threw every panel away. Nothing noticed, because every other
check asks whether the file is well formed, and it was: valid, complete, and
empty of the thing the book is made of.

The obvious test - "does the text point at a suspiciously tiny image" - was
tried and thrown out. Measured across this library it flagged eighteen books
and was wrong about seventeen of them: novels lean on small ornaments (a scene
divider, a rule) far more than the broken book did.

    Case Films (broken)      tiny image used   2 times | real images   0
    And Another Thing (fine) tiny image used  32 times | real images   0
    Ender's Game (fine)      tiny image used  31 times | real images  11
    InvestiGators 01 (fine)  tiny image used   0 times | real images 210

A book with no pictures is not a fault. A book with no pictures sitting beside
nine of its own series that have two hundred each is. So the only test here is
that comparison, which is why it needs a folder of shelf-mates to speak at all.
"""
import sys, os, re, zipfile, argparse, statistics

IMG = re.compile(r"\.(jpe?g|png|gif|webp)$", re.I)
MIN_SHELF = 3         # fewer than this and there is nothing to compare against
RICH = 20             # a shelf counts as illustrated when the median holds this many
BARE = 2              # and a book counts as bare at this many or fewer


def real_images(path):
    """Pictures, not furniture: over 800 bytes and not the cover."""
    z = zipfile.ZipFile(path)
    n = sum(1 for f in z.namelist()
            if IMG.search(f) and z.getinfo(f).file_size >= 800
            and "cover" not in os.path.basename(f).lower())
    z.close()
    return n


def main(argv):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("folder")
    ap.add_argument("--quiet", action="store_true", help="print nothing when all is well")
    a = ap.parse_args(argv)

    files = [f for f in sorted(os.listdir(a.folder)) if f.endswith(".epub")]
    if len(files) < MIN_SHELF:
        if not a.quiet:
            print("  (%d book(s) here - nothing to compare against)" % len(files))
        return 0

    counts, sizes = {}, {}
    for f in files:
        p = os.path.join(a.folder, f)
        try:
            counts[f] = real_images(p)
        except Exception as e:
            print("  %-52s will not open: %s" % (f[:52], e)); continue
        sizes[f] = os.path.getsize(p)

    med_imgs = statistics.median(counts.values())
    med_size = statistics.median(sizes.values())
    if med_imgs < RICH:
        if not a.quiet:
            print("  (this shelf is not illustrated - median %d pictures a book)" % med_imgs)
        return 0

    issues = [(f, n) for f, n in counts.items() if n <= BARE]
    for f, n in sorted(issues):
        print("  %-52s %d pictures and %.1f MB, beside shelf-mates with about "
              "%d pictures and %.1f MB - the artwork is missing"
              % (f[:52], n, sizes[f] / 1e6, med_imgs, med_size / 1e6))
    if issues or not a.quiet:
        print("\n%d of %d books on an illustrated shelf have no pictures"
              % (len(issues), len(files)))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
