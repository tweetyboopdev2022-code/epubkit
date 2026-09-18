#!/usr/bin/env python3
"""Audit a folder of EPUBs: report what each file claims and flag defects.

    python3 scan_epubs.py <folder> [--json] [--toc]

Run this before changing anything. The point is to find out what is actually in
the files rather than trusting their names.
"""
import argparse, json, os, re, sys, zipfile
import xml.etree.ElementTree as ET

OPF = '{http://www.idpf.org/2007/opf}'
DC = '{http://purl.org/dc/elements/1.1/}'
NCX = '{http://www.daisy.org/z3986/2005/ncx/}'
XH = '{http://www.w3.org/1999/xhtml}'
EPUBNS = '{http://www.idpf.org/2007/ops}'

JUNK_DESC = ('<p>', '<p></p>', '')


def opf_path(z):
    c = z.read('META-INF/container.xml').decode('utf-8', 'replace')
    return re.search(r'full-path="([^"]+)"', c).group(1)


def read(path):
    out = {'file': os.path.basename(path), 'size': os.path.getsize(path), 'problems': []}
    z = zipfile.ZipFile(path)
    names = z.namelist()
    if names[0] != 'mimetype':
        out['problems'].append('mimetype not first in archive')
    elif z.getinfo('mimetype').compress_type != zipfile.ZIP_STORED:
        out['problems'].append('mimetype is compressed')
    op = opf_path(z)
    root = ET.fromstring(z.read(op))
    md = root.find(OPF + 'metadata')
    out['version'] = root.get('version')
    out['title'] = getattr(md.find(DC + 'title'), 'text', None)
    out['authors'] = [{'name': e.text, 'file_as': e.get(OPF + 'file-as')}
                      for e in md.findall(DC + 'creator')]
    out['publisher'] = getattr(md.find(DC + 'publisher'), 'text', None)
    out['dates'] = [e.text for e in md.findall(DC + 'date')]
    out['language'] = getattr(md.find(DC + 'language'), 'text', None)
    out['identifiers'] = [((e.get(OPF + 'scheme') or e.get('scheme') or '?') + ':' + (e.text or ''))
                          for e in md.findall(DC + 'identifier')]
    out['subjects'] = [e.text for e in md.findall(DC + 'subject')]
    desc = getattr(md.find(DC + 'description'), 'text', None) or ''
    out['description_len'] = len(desc.strip())
    metas = {m.get('name'): m.get('content') for m in md.findall(OPF + 'meta') if m.get('name')}
    out['series'] = metas.get('calibre:series')
    out['series_index'] = metas.get('calibre:series_index')
    out['calibre_junk'] = sorted(k for k in metas
                                 if k and k.startswith('calibre:')
                                 and k not in ('calibre:series', 'calibre:series_index'))
    man = {i.get('id'): i.get('href') for i in root.find(OPF + 'manifest')}
    spine = [i.get('idref') for i in root.find(OPF + 'spine')]
    out['spine'] = len(spine)
    cover = metas.get('cover')
    out['cover'] = cover if cover in man else None
    if cover and cover not in man:
        out['problems'].append('cover metadata points at a manifest id that does not exist')
    if not cover:
        out['problems'].append('no cover declared')

    # table of contents size
    tocid = root.find(OPF + 'spine').get('toc')
    n = 0
    base = os.path.dirname(op)
    try:
        if tocid and tocid in man:
            p = os.path.normpath(os.path.join(base, man[tocid])) if base else man[tocid]
            n = len(list(ET.fromstring(z.read(p)).iter(NCX + 'navPoint')))
        else:
            for it in root.find(OPF + 'manifest'):
                if 'nav' in (it.get('properties') or '').split():
                    p = os.path.normpath(os.path.join(base, it.get('href'))) if base else it.get('href')
                    n = len(list(ET.fromstring(z.read(p)).iter(XH + 'a')))
    except Exception:
        pass
    out['toc_entries'] = n

    # defects worth naming
    if not out['title']:
        out['problems'].append('no title')
    if not out['authors']:
        out['problems'].append('no author')
    for a in out['authors']:
        if not a['file_as']:
            out['problems'].append('author %r has no sort name' % a['name'])
        if a['name'] and (' and ' in a['name'] or '; ' in a['name']):
            out['problems'].append('author %r looks like several people in one field' % a['name'])
    if not out['series']:
        out['problems'].append('no series')
    if not out['subjects']:
        out['problems'].append('no subject tags')
    if desc.strip() in JUNK_DESC or out['description_len'] < 40:
        out['problems'].append('description missing or junk')
    if not out['dates']:
        out['problems'].append('no publication date')
    if out['calibre_junk']:
        out['problems'].append("carries another library's Calibre settings (%d keys)"
                               % len(out['calibre_junk']))
    if n and n < 3 and out['spine'] > 3:
        out['problems'].append('table of contents has only %d entries' % n)
    # an omnibus hiding as one book: lots of spine documents and a big file
    if out['size'] > 4_000_000 and out['spine'] > 60:
        out['problems'].append('large with a long spine - check the TOC, may be an omnibus')
    z.close()
    return out


def toc_top(path, limit=25):
    z = zipfile.ZipFile(path)
    op = opf_path(z)
    base = os.path.dirname(op)
    root = ET.fromstring(z.read(op))
    man = {i.get('id'): i.get('href') for i in root.find(OPF + 'manifest')}
    def full(h):
        return os.path.normpath(os.path.join(base, h)) if base else h
    labels = []
    tocid = root.find(OPF + 'spine').get('toc')
    try:
        if tocid and tocid in man:
            nr = ET.fromstring(z.read(full(man[tocid])))
            nm = nr.find(NCX + 'navMap')
            for np in nm.findall(NCX + 'navPoint'):
                labels.append(''.join(np.find(NCX + 'navLabel').itertext()).strip())
        else:
            for it in root.find(OPF + 'manifest'):
                if 'nav' in (it.get('properties') or '').split():
                    nv = ET.fromstring(z.read(full(it.get('href'))))
                    for nav in nv.iter(XH + 'nav'):
                        if nav.get(EPUBNS + 'type') == 'toc':
                            ol = nav.find(XH + 'ol')
                            for li in (ol.findall(XH + 'li') if ol is not None else []):
                                a = li.find(XH + 'a')
                                if a is not None:
                                    labels.append(''.join(a.itertext()).strip())
    except Exception as e:
        labels.append('<unreadable: %r>' % e)
    z.close()
    return labels[:limit]


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('folder')
    ap.add_argument('--json', action='store_true', help='machine-readable output')
    ap.add_argument('--toc', action='store_true', help='also list top-level TOC entries')
    a = ap.parse_args()

    files = sorted(f for f in os.listdir(a.folder) if f.lower().endswith('.epub')
                   and not f.startswith('._'))
    rows = []
    for f in files:
        try:
            rows.append(read(os.path.join(a.folder, f)))
        except Exception as e:
            rows.append({'file': f, 'problems': ['could not read: %r' % e]})

    if a.json:
        print(json.dumps(rows, indent=1, ensure_ascii=False))
        return

    print('%-46s %-26s %-22s %-5s %-4s %s' %
          ('FILE', 'TITLE', 'AUTHOR', 'SPINE', 'TOC', 'SERIES'))
    print('-' * 130)
    for r in rows:
        auth = ', '.join(x['name'] or '?' for x in r.get('authors', [])) or '-'
        ser = (r.get('series') or '-')
        if r.get('series_index'):
            ser += ' #' + r['series_index']
        print('%-46s %-26s %-22s %-5s %-4s %s' %
              (r['file'][:46], str(r.get('title'))[:26], auth[:22],
               r.get('spine', '-'), r.get('toc_entries', '-'), ser[:28]))
    print()
    n = 0
    for r in rows:
        if r['problems']:
            n += 1
            print(r['file'])
            for p in r['problems']:
                print('    - ' + p)
    print('\n%d of %d files have something worth fixing' % (n, len(rows)))

    if a.toc:
        print('\n--- top-level table of contents (several book titles here means an omnibus) ---')
        for f in files:
            print('\n%s' % f)
            for lab in toc_top(os.path.join(a.folder, f)):
                print('    ' + lab[:100])


if __name__ == '__main__':
    main()
