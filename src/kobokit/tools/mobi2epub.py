#!/usr/bin/env python3
"""Pull the EPUB out of a Kindle file.

    python3 mobi2epub.py book.azw3 --out ./staged

A KF8 file (.azw3, and any .mobi new enough to carry one) is an EPUB in a
different wrapper, so there is nothing to convert: mobi.extract unpacks it and
`mobi8/` holds a usable book with its real metadata already in place. That is
why this does not take a title or an author the way the PDF converters must.

An older MOBI 6 has no KF8 part and unpacks to `mobi7/` as loose HTML. Nothing
here rebuilds a book from that, so it fails and says so rather than staging
something broken.
"""
import argparse
import os
import shutil
import sys
import zipfile


def convert(src, out_dir):
    try:
        import mobi
    except ImportError:
        sys.exit('needs the mobi package:  pip3 install --user mobi')

    tmpdir, produced = None, None
    try:
        # mobi.extract raises out of its own struct unpacking on anything that
        # is not a Palm database, which is a traceback rather than an answer.
        try:
            tmpdir, produced = mobi.extract(src)
        except SystemExit:
            raise
        except Exception as e:
            sys.exit('%s could not be unpacked as a Kindle file: %s: %s'
                     % (os.path.basename(src), type(e).__name__, e))
        if not produced or not produced.lower().endswith('.epub') \
                or not os.path.exists(produced):
            sys.exit('%s has no KF8 part, so no EPUB came out of it. It is '
                     'probably an older MOBI 6; convert it by hand.'
                     % os.path.basename(src))
        # A truncated unpack still leaves a file. Opening the archive is cheap
        # and is the difference between failing here and failing three stages on.
        with zipfile.ZipFile(produced) as z:
            if z.testzip() is not None:
                sys.exit('the EPUB extracted from %s is corrupt'
                         % os.path.basename(src))
            if not any(n.endswith('.opf') for n in z.namelist()):
                sys.exit('the EPUB extracted from %s has no OPF'
                         % os.path.basename(src))

        os.makedirs(out_dir, exist_ok=True)
        stem = os.path.splitext(os.path.basename(src))[0]
        dst = os.path.join(out_dir, stem + '.epub')
        shutil.copy2(produced, dst)
        return dst
    finally:
        if tmpdir:
            shutil.rmtree(tmpdir, ignore_errors=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('src')
    ap.add_argument('--out', required=True)
    a = ap.parse_args()
    dst = convert(a.src, a.out)
    print('wrote %s\n  %.1f MB' % (dst, os.path.getsize(dst) / 1e6))


if __name__ == '__main__':
    main()
