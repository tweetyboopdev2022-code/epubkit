#!/usr/bin/env python3
"""Run real EPUBCheck against every book in a folder - the Kobo's own mount,
a Drive folder, a backup, anything - and report which ones will cause
Nickel trouble. This is the manual "check my whole library" command; the
pipeline's own per-book gate (verify_epubs.py, run automatically during
`pipeline.py build`) is what stops a BAD BOOK FROM BEING PRODUCED in the
first place. This script is for auditing books that already exist,
including ones built before that gate existed.

    python3 audit_epubcheck.py "<folder>" [--json out.json] [--recursive]

Needs a JVM and `pip install epubcheck --break-system-packages` (no
separate download - the pip package bundles the jar). On a Mac with no
Java: `brew install openjdk`, then the pip install. Nothing here needs a
Kobo connected - point it at any folder of .epub/.kepub.epub files, an
offline-synced Drive folder included.
"""
import argparse, os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import epubcheck_util as ec


def find_books(folder, recursive):
    if recursive:
        for root, dirs, files in os.walk(folder):
            for f in sorted(files):
                if f.lower().endswith((".epub",)) and not f.startswith("._"):
                    yield os.path.join(root, f)
    else:
        for f in sorted(os.listdir(folder)):
            if f.lower().endswith((".epub",)) and not f.startswith("._"):
                yield os.path.join(folder, f)


def main(argv):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("folder")
    ap.add_argument("--json", help="write full per-book results here")
    ap.add_argument("--recursive", action="store_true",
                    help="descend into subfolders (a Drive library sorted "
                         "into sections/authors, for instance)")
    ap.add_argument("--quiet", action="store_true",
                    help="only print books with problems, not the clean ones")
    a = ap.parse_args(argv)

    if not ec.available():
        print("java and/or epubcheck.jar not found. pip install epubcheck "
              "--break-system-packages (install a JVM first if `java` is "
              "missing - on a Mac, brew install openjdk).", file=sys.stderr)
        return 2

    books = list(find_books(a.folder, a.recursive))
    if not books:
        print("no .epub files found under %r" % a.folder, file=sys.stderr)
        return 1

    from collections import Counter
    code_counts = Counter()
    results = {}
    clean = dirty = 0
    for i, path in enumerate(books, 1):
        name = os.path.relpath(path, a.folder)
        res = ec.run(path)
        results[name] = res
        errs = [r for r in (res or []) if r["severity"] in ("FATAL", "ERROR")]
        if errs:
            dirty += 1
            for r in errs:
                code_counts[r["code"]] += 1
            print("%4d/%-4d %-70s %s" % (i, len(books), name[:70], ec.summarize(res)))
        else:
            clean += 1
            if not a.quiet:
                print("%4d/%-4d %-70s clean" % (i, len(books), name[:70]))

    print("\n%d books checked, %d clean, %d with EPUBCheck errors" %
          (len(books), clean, dirty))
    if code_counts:
        print("error codes, most common first:")
        for code, n in code_counts.most_common():
            print("   %-10s %d" % (code, n))

    if a.json:
        import json
        json.dump(results, open(a.json, "w"), indent=1)
        print("full results written to %s" % a.json)

    return 1 if dirty else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
