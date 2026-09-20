#!/usr/bin/env python3
"""Structural check on finished EPUBs: zip integrity, mimetype placement, OPF
well-formedness, manifest completeness, spine resolution, a real decodable cover,
table-of-contents size, and dangling internal links.

    python3 verify_epubs.py <folder> [--json out.json]

Run this on the output before telling anyone the job is done. Report anything
that survives, and be clear about which problems came from the source file
rather than from the processing.
"""
import argparse
import zipfile, os, re, sys, posixpath, io, json
from urllib.parse import unquote
import xml.etree.ElementTree as ET
sys.path.insert(0, '/home/claude/work')
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from PIL import Image
import epubcheck_util

OPFNS = '{http://www.idpf.org/2007/opf}'
DCNS = '{http://purl.org/dc/elements/1.1/}'
NCXNS = '{http://www.daisy.org/z3986/2005/ncx/}'
XH = '{http://www.w3.org/1999/xhtml}'

IMG = ('.jpg', '.jpeg', '.png', '.gif', '.svg', '.webp')


def norm(p):
    return posixpath.normpath(p).lstrip('/')


def check(path):
    prob, info = [], {}
    z = zipfile.ZipFile(path)
    if z.testzip() is not None:
        prob.append('corrupt zip member')
    names = z.namelist()
    if names[0] != 'mimetype':
        prob.append('mimetype not first')
    elif z.getinfo('mimetype').compress_type != zipfile.ZIP_STORED:
        prob.append('mimetype compressed')
    if z.read('mimetype') != b'application/epub+zip':
        prob.append('bad mimetype content')
    nameset = set(names)

    opf_path = re.search(r'full-path="([^"]+)"',
                         z.read('META-INF/container.xml').decode('utf-8', 'replace')).group(1)
    if opf_path not in nameset:
        prob.append('OPF missing: ' + opf_path)
        return prob, info
    root = ET.fromstring(z.read(opf_path))
    base = posixpath.dirname(opf_path)

    def full(h):
        h = h.split('#')[0]
        return norm(posixpath.join(base, h)) if base else norm(h)

    md = root.find(OPFNS + 'metadata')
    info['version'] = root.get('version')
    info['title'] = (md.find(DCNS + 'title').text if md.find(DCNS + 'title') is not None else None)
    info['authors'] = [(e.text, e.get(OPFNS + 'file-as')) for e in md.findall(DCNS + 'creator')]
    info['publisher'] = getattr(md.find(DCNS + 'publisher'), 'text', None)
    info['date'] = getattr(md.find(DCNS + 'date'), 'text', None)
    info['language'] = getattr(md.find(DCNS + 'language'), 'text', None)
    info['identifiers'] = [e.text for e in md.findall(DCNS + 'identifier')]
    info['subjects'] = [e.text for e in md.findall(DCNS + 'subject')]
    info['desc_len'] = len(getattr(md.find(DCNS + 'description'), 'text', '') or '')
    metas = {m.get('name'): m.get('content') for m in md.findall(OPFNS + 'meta') if m.get('name')}
    info['series'] = metas.get('calibre:series')
    info['series_index'] = metas.get('calibre:series_index')
    cover_id = metas.get('cover')

    if not info['title']:
        prob.append('no title')
    if not info['authors']:
        prob.append('no author')
    for n, fa in info['authors']:
        if not fa:
            prob.append('creator %r missing file-as' % n)
    if not info['language']:
        prob.append('no language')

    man = {}
    for it in root.find(OPFNS + 'manifest'):
        man[it.get('id')] = (full(it.get('href')), it.get('media-type'))
    missing = [h for h, _ in man.values() if h not in nameset]
    if missing:
        prob.append('%d manifest items missing from zip: %s' % (len(missing), missing[:3]))
    unlisted = [n for n in nameset
                if n not in {h for h, _ in man.values()}
                and n not in ('mimetype', 'META-INF/container.xml', opf_path)
                and not n.startswith('META-INF/')
                and not n.endswith('/')]
    info['unlisted'] = len(unlisted)

    spine = [i.get('idref') for i in root.find(OPFNS + 'spine')]
    info['spine'] = len(spine)
    bad = [s for s in spine if s not in man]
    if bad:
        prob.append('spine idrefs not in manifest: %s' % bad[:3])
    if not spine:
        prob.append('empty spine')

    # cover
    info['cover'] = None
    if cover_id and cover_id in man:
        cp = man[cover_id][0]
        info['cover'] = cp.split('/')[-1]
        if cp not in nameset:
            prob.append('cover file missing')
        elif cp.lower().endswith(IMG) and not cp.lower().endswith('.svg'):
            try:
                im = Image.open(io.BytesIO(z.read(cp)))
                info['cover_size'] = '%dx%d' % im.size
                if im.size[0] < 100 or im.size[1] < 100:
                    prob.append('cover suspiciously small %s' % (im.size,))
            except Exception as e:
                prob.append('cover unreadable: %r' % e)
    elif cover_id:
        prob.append('cover meta points at unknown id %r' % cover_id)

    # toc
    tocid = root.find(OPFNS + 'spine').get('toc')
    navpts = 0
    if tocid and tocid in man and man[tocid][0] in nameset:
        try:
            nr = ET.fromstring(z.read(man[tocid][0]))
            navpts = len(list(nr.iter(NCXNS + 'navPoint')))
        except Exception as e:
            prob.append('NCX unparseable: %r' % e)
    else:
        for k, (h, mt) in man.items():
            if h.endswith(('nav.xhtml',)) or 'nav' in (k or ''):
                pass
        navs = [h for h, mt in man.values() if h.endswith('.xhtml')]
    if navpts == 0:
        # EPUB3 nav?
        for it in root.find(OPFNS + 'manifest'):
            if (it.get('properties') or '').find('nav') >= 0:
                try:
                    nv = ET.fromstring(z.read(full(it.get('href'))))
                    navpts = len(list(nv.iter(XH + 'a')))
                except Exception:
                    pass
    info['toc_points'] = navpts
    if navpts < 2:
        prob.append('table of contents has %d entries' % navpts)

    # dangling internal links from spine documents
    dangling = 0
    for sid in spine:
        h = man[sid][0]
        if h not in nameset:
            continue
        try:
            txt = z.read(h).decode('utf-8', 'replace')
        except Exception:
            continue
        d = posixpath.dirname(h)
        for m in re.finditer(r'(?:href|src)\s*=\s*["\']([^"\']+)["\']', txt):
            r = m.group(1).strip()
            if r.startswith(('http', 'data:', 'mailto:', '#')) or not r:
                continue
            r0 = unquote(r.split('#')[0])
            t = norm(posixpath.join(d, r0)) if d else norm(r0)
            if t and t not in nameset:
                dangling += 1
    info['dangling'] = dangling
    z.close()

    # Real EPUBCheck, on top of the structural checks above - catches spec
    # violations Nickel sometimes mishandles (wrong DOCTYPE for the package
    # version, invalid attributes, a stale NCX identifier) that a hand-rolled
    # check has no reason to know about. FATAL/ERROR block the batch the same
    # as any other problem here; WARNING is recorded but not fatal.
    ec = epubcheck_util.run(path)
    info['epubcheck'] = epubcheck_util.summarize(ec)
    if ec:
        for r in ec:
            if r['severity'] in ('FATAL', 'ERROR'):
                prob.append('epubcheck %s: %s' % (r['code'], r['message'][:120]))

    return prob, info


if __name__ == '__main__':
    _ap = argparse.ArgumentParser(description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    _ap.add_argument('folder')
    _ap.add_argument('--json')
    _a = _ap.parse_args()
    OUT = _a.folder
    files = sorted(os.listdir(OUT))
    allprob = {}
    rows = []
    for f in files:
        if not f.endswith('.epub') or f.startswith('._'):
            continue
        p, i = check(os.path.join(OUT, f))
        rows.append((f, i, p))
        if p:
            allprob[f] = p
    print('%-52s %-4s %-30s %-5s %-6s %-5s %-5s' %
          ('FILE', 'VER', 'SERIES', 'IDX', 'SPINE', 'TOC', 'DANG'))
    for f, i, p in rows:
        print('%-52s %-4s %-30s %-5s %-6s %-5s %-5s' %
              (f[:52], i.get('version'), (i.get('series') or '-')[:30],
               i.get('series_index') or '-', i.get('spine'), i.get('toc_points'),
               i.get('dangling')))
    print('\n--- issues ---')
    if not allprob:
        print('none')
    for f, p in allprob.items():
        print(f)
        for x in p:
            print('   ', x)
    print('\n%d files checked, %d with issues' % (len(rows), len(allprob)))
    if _a.json:
        json.dump({f: {'info': i, 'problems': p} for f, i, p in rows},
                  open(_a.json, 'w'), indent=1)
    sys.exit(1 if allprob else 0)
