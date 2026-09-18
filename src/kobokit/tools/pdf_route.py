#!/usr/bin/env python3
"""Decide how to convert a PDF, and dig out whatever it claims about itself.

    python3 pdf_route.py book.pdf           -> "reflow" or "fixed"
    python3 pdf_route.py book.pdf --meta    -> route, title, author as TSV

Getting the route wrong is not cosmetic in either direction: 900 fixed page
images on a six-inch screen cannot change their font size, and a picture book
pulled apart into reflowed text loses the pictures that are the book.

The signal is text density per page. A novel carries well over a thousand
characters a page; a picture book carries a caption. A scan with no text layer
reads as 0 and routes to fixed, which is correct - there is nothing to reflow
until someone OCRs it, and that is a decision with its own flags.

The metadata is a starting point, never an answer. pdf2epub and pdf2kepub both
require a title and an author, and a PDF's embedded pair is usually a library
catalogue record: sort-order names with their role appended, spaced colons.
This cleans the mechanical noise and leaves the judgement to research and to
whoever reads the report.
"""
import argparse
import os
import re
import statistics
import sys

REFLOW_CHARS = 1200
ROLES = ('author', 'editor', 'illustrator', 'translator', 'compiler')


def load(path):
    try:
        import pymupdf
    except ImportError:
        try:
            import fitz as pymupdf
        except ImportError:
            sys.exit('needs pymupdf:  pip3 install --user pymupdf')
    return pymupdf.open(path)


def clean_author(raw):
    a = re.sub(r'\s+', ' ', (raw or '')).strip().strip(',')
    # A catalogue record can carry several roles ("Tolkien, J. R. R., author,
    # illustrator"), so strip until the tail stops changing rather than once.
    while True:
        before = a
        for role in ROLES:
            a = re.sub(r',?\s*\b%s\b\.?$' % role, '', a, flags=re.I).strip().strip(',')
        if a == before:
            break
    # "Pendergrass, Daphne" is a sort form, not a name. Flip it, but only on a
    # single comma - "Mays, L. J., and Carter, R." is a list and must be left be.
    if a.count(',') == 1:
        last, first = (p.strip() for p in a.split(','))
        if last and first:
            a = '%s %s' % (first, last)
    return a


def clean_title(raw):
    t = re.sub(r'\s+', ' ', (raw or '')).strip()
    t = re.sub(r'\s+:\s+', ': ', t)
    return t.strip(' .')


def from_filename(path):
    stem = os.path.splitext(os.path.basename(path))[0]
    # Anna's Archive and similar dumps use "Title -- Author -- publisher -- ..."
    if ' -- ' in stem:
        parts = [p.strip() for p in stem.split(' -- ')]
        return clean_title(parts[0]), clean_author(parts[1] if len(parts) > 1 else '')
    if ' - ' in stem:
        a, _, b = stem.partition(' - ')
        return clean_title(a), clean_author(b)
    return clean_title(stem), ''


def inspect(path, samples=24):
    with load(path) as doc:
        n = len(doc)
        if not n:
            sys.exit('%s has no pages' % path)
        step = max(1, n // samples)
        idx = list(range(0, n, step))[:samples]
        chars = [len(doc[i].get_text().strip()) for i in idx]
        imgs = [len(doc[i].get_images(full=True)) for i in idx]
        meta = dict(doc.metadata or {})
    title = clean_title(meta.get('title'))
    author = clean_author(meta.get('author'))
    f_title, f_author = from_filename(path)
    return {
        'pages': n,
        'chars': statistics.median(chars),
        'images': statistics.median(imgs),
        'title': title or f_title,
        'author': author or f_author,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('pdf')
    ap.add_argument('--chars', type=int, default=REFLOW_CHARS,
                    help='median characters a page at or above which to reflow')
    ap.add_argument('--meta', action='store_true',
                    help='print route, title and author as TSV')
    ap.add_argument('--quiet', action='store_true')
    a = ap.parse_args()

    d = inspect(a.pdf)
    route = 'reflow' if d['chars'] >= a.chars else 'fixed'
    if not a.quiet:
        print('%d pages, median %d chars and %d images a page -> %s'
              % (d['pages'], d['chars'], d['images'], route), file=sys.stderr)
    if a.meta:
        print('%s\t%s\t%s' % (route, d['title'], d['author']))
    else:
        print(route)


if __name__ == '__main__':
    main()
