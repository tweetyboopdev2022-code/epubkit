#!/usr/bin/env python3
"""Convert an image-based PDF (picture book, comic, scan) into a Kobo
fixed-layout book.

    python3 pdf2kepub.py "book.pdf" --out ./built \
        --title "The Pigeon Needs a Bath!" --author "Mo Willems" \
        --series "Pigeon" [--series-index 3] [--panel 1072x1448]

What it does, and why:

  * renders every page (scans have no text layer worth keeping)
  * drops blank pages -- Calibre-made PDFs often interleave one after every
    real page, which silently doubles the page count
  * trims white borders and the date/URL strips that "flipbook" web captures
    bake into every page
  * fits each page to the device panel without ever upscaling past the source,
    which is what keeps the file from tripling in size for no visible gain
  * writes a .kepub.epub -- the extension matters, see references/kobo-facts.md

Useful options:

  --split-spreads   cut two-page scans into single pages, skipping any spread
                    where the artwork crosses the gutter
  --swap-first-two  when the cover spread scanned back-cover-first
  --pages A-B       only this range of (real, non-blank) pages
  --quality N       JPEG quality, default 88; 80 is plenty for flat line art
  --contact-sheet   write a PNG of the finished pages to eyeball before shipping

Requires poppler-utils (pdftoppm, pdfinfo), Pillow and numpy.
"""
import argparse, glob, os, re, shutil, subprocess, sys, tempfile
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from PIL import Image
import crop, fxl


def have_poppler():
    return bool(shutil.which('pdfinfo') and shutil.which('pdftoppm'))


def page_info(pdf):
    """Page size in points and the page count, from poppler or pymupdf."""
    if shutil.which('pdfinfo'):
        info = subprocess.run(['pdfinfo', pdf], capture_output=True, text=True).stdout
        m = re.search(r'Page size:\s+([\d.]+) x ([\d.]+)', info)
        if m:
            return (float(m.group(1)), float(m.group(2)),
                    int(re.search(r'Pages:\s+(\d+)', info).group(1)))
    try:
        import pymupdf
    except ImportError:
        try:
            import fitz as pymupdf
        except ImportError:
            raise SystemExit(
                'need poppler-utils (pdfinfo, pdftoppm) or pymupdf.\n'
                '  pip3 install --user pymupdf')
    with pymupdf.open(pdf) as doc:
        r = doc[0].rect
        return r.width, r.height, len(doc)


def render_pymupdf(pdf, out_dir, dpi, width, lo, hi):
    """Render without poppler. Single-process, so the chunking above is what
    keeps a long book inside one shell call, not the worker fan-out."""
    try:
        import pymupdf
    except ImportError:
        import fitz as pymupdf
    zoom = dpi / 72.0
    with pymupdf.open(pdf) as doc:
        for i in range(lo, hi + 1):
            dst = os.path.join(out_dir, 'p-%0*d.png' % (width, i))
            tmp = dst + '.part'
            doc[i - 1].get_pixmap(matrix=pymupdf.Matrix(zoom, zoom)).save(tmp, output="png")
            os.replace(tmp, dst)


def render(pdf, out_dir, long_edge=1700, chunk=0, jobs=1):
    """Render the PDF to PNGs, a chunk at a time and only where one is missing.

    A few hundred pages takes several minutes, and a shell that gets collected
    after a couple of them will lose the lot. Rendering in slices into a
    directory that survives the call means the work accumulates: run the command
    again and it carries on from the first page that has no PNG yet.
    """
    w, h, n = page_info(pdf)
    dpi = max(110, min(400, round(long_edge / (max(w, h) / 72))))
    os.makedirs(out_dir, exist_ok=True)
    width = len(str(n))

    def complete(path):
        # A run killed mid-write leaves a PNG that exists but stops short. Asking
        # only whether the file is there makes the next run accept it and blow up
        # later with "image file is truncated". Every finished PNG ends with the
        # 8-byte IEND chunk, so checking for that is exact and costs one seek.
        try:
            with open(path, 'rb') as fh:
                if os.path.getsize(path) < 16:
                    return False
                fh.seek(-8, os.SEEK_END)
                return fh.read() == b'IEND\xaeB`\x82'
        except OSError:
            return False

    def have(i):
        found = glob.glob(os.path.join(out_dir, 'p*%0*d.png' % (width, i))) or \
                glob.glob(os.path.join(out_dir, 'p-%d.png' % i))
        for f in found:
            if not complete(f):
                os.remove(f)
                return []
        return found

    todo = [i for i in range(1, n + 1) if not have(i)]
    if todo and chunk:
        todo = todo[:chunk]
    if todo:
        lo, hi = todo[0], todo[-1]
        # pdftoppm is single-threaded, so one call renders one page at a time and
        # a 200-page book takes several minutes - far longer than a shell call
        # survives. Splitting the range across workers is the difference between
        # six round trips and one; the pages are independent, so there is nothing
        # to coordinate.
        span = hi - lo + 1
        workers = max(1, min(jobs, span))
        size = -(-span // workers)
        if have_poppler():
            procs = []
            for w in range(workers):
                a = lo + w * size
                b = min(hi, a + size - 1)
                if a > hi:
                    break
                procs.append(subprocess.Popen(
                    ['pdftoppm', '-png', '-r', str(dpi), '-f', str(a), '-l', str(b),
                     pdf, os.path.join(out_dir, 'p')],
                    stdout=subprocess.DEVNULL, stderr=subprocess.PIPE))
            for pr in procs:
                _, err = pr.communicate()
                if pr.returncode:
                    raise SystemExit('pdftoppm failed: %s' % err.decode('utf-8', 'ignore')[:200])
        else:
            render_pymupdf(pdf, out_dir, dpi, width, lo, hi)
    pages = sorted(glob.glob(os.path.join(out_dir, 'p*.png')))
    left = n - len(pages)
    print('rendered %d of %d pages at %d dpi%s'
          % (len(pages), n, dpi, ', %d to go' % left if left > 0 else ''))
    if left > 0:
        print('run the same command again to carry on rendering')
        raise SystemExit(2)
    return pages


def _fit_one(job):
    """Crop, optionally split, fit to the panel and encode ONE page.

    A module-level function because a multiprocessing Pool has to pickle it.
    """
    src, dst, box, split, pw, ph, quality = job
    im = crop.crop_page(Image.open(src).convert('RGB'), box)
    parts = [im]
    if split:
        ok, ink, gap = crop.gutter_split(im)
        if ok:
            c = im.width // 2
            parts = [im.crop((0, 0, c, im.height)), im.crop((c, 0, im.width, im.height))]
    for n, part in enumerate(parts):
        fxl.letterbox(part).save(dst if n == 0 else dst[:-4] + '.b.jpg', 'JPEG',
                           quality=quality, optimize=True, progressive=False)
    return dst


def contact_sheet(images, path, cols=6, cell=(300, 400)):
    rows = (len(images) + cols - 1) // cols
    sheet = Image.new('RGB', (cell[0] * cols, cell[1] * rows), (235, 235, 240))
    for i, im in enumerate(images):
        t = im.copy(); t.thumbnail((cell[0] - 8, cell[1] - 8))
        sheet.paste(t, ((i % cols) * cell[0] + (cell[0] - t.width) // 2,
                        (i // cols) * cell[1] + (cell[1] - t.height) // 2))
    sheet.save(path)
    print('contact sheet -> %s  (look at it before shipping)' % path)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('pdf')
    ap.add_argument('--out', default='.')
    ap.add_argument('--title', required=True)
    ap.add_argument('--author', required=True)
    ap.add_argument('--author-sort')
    ap.add_argument('--series', default='')
    ap.add_argument('--series-index', default='')
    ap.add_argument('--publisher', default='')
    ap.add_argument('--date', default='')
    ap.add_argument('--subjects', default='', help='comma separated')
    ap.add_argument('--description', default='')
    ap.add_argument('--panel', default='1072x1448', help='WxH of the device screen')
    ap.add_argument('--quality', type=int, default=88)
    ap.add_argument('--split-spreads', action='store_true')
    ap.add_argument('--swap-first-two', action='store_true')
    ap.add_argument('--keep-blanks', action='store_true')
    ap.add_argument('--no-crop', action='store_true')
    ap.add_argument('--pages', help='range of real pages to include, e.g. 1-75')
    ap.add_argument('--filename', help='output name; defaults to "<title> - <author>.kepub.epub"')
    ap.add_argument('--contact-sheet')

    ap.add_argument('--render-dir', help='keep rendered pages here so an '
        'interrupted run can resume (default: a scratch dir named after the PDF)')
    ap.add_argument('--render-jobs', type=int, default=4, metavar='N',
        help='render this many page ranges in parallel (default 4)')
    ap.add_argument('--render-chunk', type=int, default=0, metavar='N',
        help='render at most N pages per run, for shells that will not hold a '
             'long job open; re-run until it stops asking')
    a = ap.parse_args()

    pw, ph = (int(x) for x in a.panel.lower().split('x'))
    fxl.set_panel(pw, ph)
    os.makedirs(a.out, exist_ok=True)

    # A scratch directory named after the PDF, not a fresh mkdtemp, so that a
    # rendering run interrupted by the shell can be resumed instead of restarted.
    tmp = a.render_dir or os.path.join(tempfile.gettempdir(),
                                       'pdf2kepub-' + re.sub(r'\W+', '_',
                                                             os.path.basename(a.pdf))[:60])
    try:
        pages = render(a.pdf, tmp, chunk=a.render_chunk, jobs=a.render_jobs)
        if not a.keep_blanks:
            live = [p for p in pages if not crop.is_blank(Image.open(p))]
            if len(live) < len(pages):
                print('dropped %d blank filler pages -> %d real pages'
                      % (len(pages) - len(live), len(live)))
            pages = live
        if a.pages:
            lo, hi = (int(x) for x in a.pages.split('-'))
            pages = pages[lo - 1:hi]
            print('using pages %d-%d (%d)' % (lo, hi, len(pages)))

        box = None
        if not a.no_crop:
            box, size = crop.book_crop(pages)
            print('crop box: %s of %s' % (box, size) if box else 'no crop needed')

        # Fit each page to the panel FIRST, in parallel, writing the result beside
        # the render. Two reasons, both learned the hard way on a 217-page book:
        # holding every full-size page decoded costs over a gigabyte of RAM, and
        # doing the resize and encode one page at a time takes longer than a shell
        # call survives - so the book could never finish however often it was
        # retried. The fitted pages are cached, so a killed run resumes here too.
        fit_dir = os.path.join(tmp, '_fit')
        os.makedirs(fit_dir, exist_ok=True)
        jobs = [(p, os.path.join(fit_dir, os.path.basename(p) + '.jpg'), box,
                 a.split_spreads, pw, ph, a.quality) for p in pages]
        todo = [j for j in jobs if not os.path.exists(j[1])]
        if todo:
            from multiprocessing import Pool
            with Pool(min(a.render_jobs, len(todo))) as pool:
                for _ in pool.imap_unordered(_fit_one, todo, chunksize=4):
                    pass
        imgs, split_n = [], 0
        for _, dst, _, _, _, _, _ in jobs:
            imgs.append(dst)
            if dst.endswith('.b.jpg'):
                split_n += 1
        extra = sorted(f for f in os.listdir(fit_dir) if f.endswith('.b.jpg'))
        if extra:
            merged = []
            for dst in imgs:
                merged.append(dst)
                b = dst[:-4] + '.b.jpg'
                if os.path.exists(b):
                    merged.append(b)
            imgs, split_n = merged, len(extra)
        if a.split_spreads:
            print('split %d of %d spreads; kept %d whole where art crosses the gutter'
                  % (split_n, len(pages), len(pages) - split_n))
        if a.swap_first_two and len(imgs) > 1:
            imgs[0], imgs[1] = imgs[1], imgs[0]

        sort = a.author_sort or (a.author.rsplit(' ', 1)[-1] + ', ' +
                                 ' '.join(a.author.split(' ')[:-1])).strip(', ')
        md = dict(title=a.title, authors=[[a.author, sort]],
                  series=a.series, series_index=a.series_index,
                  publisher=a.publisher, date=a.date,
                  subjects=[s.strip() for s in a.subjects.split(',') if s.strip()],
                  description=a.description)
        name = a.filename or ('%s - %s.kepub.epub' % (a.title, a.author))
        name = re.sub(r'[\\/:*?"<>|]', '', name)
        out = os.path.join(a.out, name)
        size, n = fxl.build(imgs, out, md, quality=a.quality)
        print('\nwrote %s\n  %d pages, %.1f MB, panel %dx%d' % (out, n, size / 1e6, pw, ph))
        if a.contact_sheet:
            contact_sheet(imgs, a.contact_sheet)
    except SystemExit:
        # The resume path exits 2 with pages still to render. Deleting the
        # scratch directory here would throw away everything rendered so far and
        # make the next run start from page one - the exact opposite of resuming.
        raise
    finally:
        if not a.render_dir and not os.environ.get('PDF2KEPUB_KEEP'):
            if sys.exc_info()[0] is None:
                shutil.rmtree(tmp, ignore_errors=True)


if __name__ == '__main__':
    main()
