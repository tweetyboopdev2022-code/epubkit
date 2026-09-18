#!/usr/bin/env python3
"""Split a multi-book omnibus EPUB into one file per novel.

    python3 split_omnibus.py <omnibus.epub> --analyse
    python3 split_omnibus.py <omnibus.epub> --plan split.json --out <dir>

--analyse prints the spine grouped into runs and the top-level table of contents
with the spine index each entry lands on. Those indices are what the plan file
uses, so start there.

split.json:

{
  "books": [
    {"range": [3, 83],
     "out": "A Game of Thrones - George R. R. Martin.epub",
     "extra_spine": [],
     "metadata": {
       "title": "A Game of Thrones",
       "authors": [["George R. R. Martin", "Martin, George R. R."]],
       "series": "A Song of Ice and Fire", "series_index": "1",
       "publisher": "Bantam Books", "date": "1996-08-01",
       "language": "en", "isbn": "9780553103540",
       "subjects": ["Fantasy fiction"], "description": "..."
     }}
  ]
}

range is inclusive, in spine positions. extra_spine appends further spine items
(a shared copyright page, say) to that book.

Each split gets only the resources its own pages reference, its own cover, and a
table of contents rebuilt from the omnibus's, so the results are real standalone
books rather than the whole archive with a different spine.
"""
import argparse, json, os, sys, zipfile
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from epubsplit import Omnibus, split_book


def analyse(path):
    om = Omnibus(path)
    print('OPF %s   version %s   %d spine items' %
          (om.opf_path, om.root.get('version'), len(om.spine)))
    print('\n--- spine, grouped by directory and id prefix ---')
    import re, os as _os
    def key(sid, href):
        return (_os.path.dirname(href), re.split(r'[-_]', sid or '')[0])
    prev, start = None, 0
    for n, sid in enumerate(om.spine + [None]):
        k = key(sid, om.href_by_id.get(sid, '')) if sid else None
        if k != prev:
            if prev is not None:
                print('  [%4d-%4d]  %-34s  %s' %
                      (start, n - 1, str(prev), om.spine_files[start]))
            prev, start = k, n
    print('\n--- top-level table of contents -> spine index ---')
    idx = {h.split('#')[0]: n for n, h in enumerate(om.spine_files)}
    for lab, tgt, frag, kids in om.toc_tree():
        print('  spine %-5s  %s' % (idx.get(tgt, '?'), lab[:80]))
    om.z.close()


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('epub')
    ap.add_argument('--analyse', action='store_true')
    ap.add_argument('--plan')
    ap.add_argument('--out', default='.')
    a = ap.parse_args()

    if a.analyse or not a.plan:
        analyse(a.epub)
        return 0

    plan = json.load(open(a.plan))
    os.makedirs(a.out, exist_ok=True)
    fails = []
    for b in plan['books']:
        out = os.path.join(a.out, b['out'])
        try:
            info = split_book(a.epub, out, tuple(b['range']), b['metadata'],
                              extra_spine=tuple(b.get('extra_spine', ())),
                              cover_hint=b.get('cover_hint'))
            z = zipfile.ZipFile(out); assert z.testzip() is None; z.close()
            print('ok    %-54s %6.1f MB  %3d pages  cover=%s'
                  % (b['out'][:54], os.path.getsize(out) / 1e6, info['docs'],
                     (info['cover'] or 'NONE').split('/')[-1]))
        except Exception as e:
            fails.append((b['out'], repr(e)))
    for f, e in fails:
        print('FAIL  %s  %s' % (f, e))
    print('\n%d of %d books written' % (len(plan['books']) - len(fails), len(plan['books'])))
    return 1 if fails else 0


if __name__ == '__main__':
    sys.exit(main())
