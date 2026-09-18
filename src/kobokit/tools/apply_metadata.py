#!/usr/bin/env python3
"""Write corrected metadata into EPUBs, from a plan file.

    python3 apply_metadata.py <folder> --plan plan.json [--out <dir>] [--cover-for "file.epub"]

Only the OPF <metadata> block is replaced; every other byte of the archive is
carried across unchanged, so this is safe to run on books you care about. Any
Calibre user-library settings embedded in the file are dropped.

plan.json maps the current filename to the metadata it should carry:

{
  "The Hobbit - J. R. R. Tolkien.epub": {
    "title": "The Hobbit",
    "authors": [["J. R. R. Tolkien", "Tolkien, J. R. R."]],
    "series": "", "series_index": "",
    "publisher": "Houghton Mifflin Harcourt",
    "date": "1937-09-21",
    "language": "en",
    "isbn": "9780544115552",
    "subjects": ["Fantasy fiction", "Adventure stories"],
    "description": "Bilbo Baggins is a comfort-loving hobbit ..."
  }
}

authors is a list of [display name, sort name] pairs -- one per person.
Use --cover-for (repeatable) to generate a typographic cover for a book whose
source has no cover art at all.
"""
import argparse, json, os, sys, zipfile
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from epubmeta import rewrite


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('folder')
    ap.add_argument('--plan', required=True)
    ap.add_argument('--out', help='write here instead of overwriting in place')
    ap.add_argument('--cover-for', action='append', default=[],
                    help='filename that needs a generated cover (repeatable)')
    a = ap.parse_args()

    plan = json.load(open(a.plan))
    out_dir = a.out or a.folder
    os.makedirs(out_dir, exist_ok=True)

    ok, bad = 0, []
    for fn, md in plan.items():
        src = os.path.join(a.folder, fn)
        if not os.path.exists(src):
            bad.append((fn, 'source missing')); continue
        dst = os.path.join(out_dir, fn)
        tmp = dst + '.new' if os.path.abspath(src) == os.path.abspath(dst) else dst
        try:
            gen = None
            if fn in a.cover_for:
                from makecover import make
                cp = os.path.join(out_dir, '.cover_tmp.jpg')
                make(cp, md['title'], md['authors'][0][0],
                     series=md.get('series'), index=md.get('series_index'))
                gen = open(cp, 'rb').read()
                os.remove(cp)
            ver, cover = rewrite(src, tmp, md, gen_cover_bytes=gen)
            if tmp != dst:
                os.replace(tmp, dst)
            z = zipfile.ZipFile(dst); assert z.testzip() is None; z.close()
            print('ok    %-64s epub%s cover=%s' % (fn[:64], ver, cover))
            ok += 1
        except Exception as e:
            bad.append((fn, repr(e)))

    print('\n%d written, %d failed' % (ok, len(bad)))
    for f, e in bad:
        print('FAIL  %s  %s' % (f, e))
    return 1 if bad else 0


if __name__ == '__main__':
    sys.exit(main())
