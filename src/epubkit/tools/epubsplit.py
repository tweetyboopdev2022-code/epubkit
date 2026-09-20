#!/usr/bin/env python3
"""Split a multi-book EPUB omnibus into a standalone EPUB for one contained novel."""
import zipfile, re, os, posixpath, html, mimetypes, sys
import xml.etree.ElementTree as ET
sys.path.insert(0, '/home/claude/work')
from epubmeta import build_metadata, esc

OPFNS = '{http://www.idpf.org/2007/opf}'
NCXNS = '{http://www.daisy.org/z3986/2005/ncx/}'
XH = '{http://www.w3.org/1999/xhtml}'
EPUBNS = '{http://www.idpf.org/2007/ops}'

DOC_TYPES = {'application/xhtml+xml', 'text/html'}

MT = {'.xhtml': 'application/xhtml+xml', '.html': 'application/xhtml+xml',
      '.htm': 'application/xhtml+xml', '.css': 'text/css', '.jpg': 'image/jpeg',
      '.jpeg': 'image/jpeg', '.png': 'image/png', '.gif': 'image/gif',
      '.svg': 'image/svg+xml', '.webp': 'image/webp', '.ttf': 'application/x-font-ttf',
      '.otf': 'application/vnd.ms-opentype', '.woff': 'application/font-woff',
      '.woff2': 'font/woff2', '.ncx': 'application/x-dtbncx+xml',
      '.js': 'text/javascript', '.xml': 'application/xml', '.mp3': 'audio/mpeg'}


def norm(p):
    return posixpath.normpath(p).lstrip('/')


class Omnibus:
    def __init__(self, path):
        self.z = zipfile.ZipFile(path)
        self.names = set(self.z.namelist())
        c = self.z.read('META-INF/container.xml').decode('utf-8', 'replace')
        self.opf_path = re.search(r'full-path="([^"]+)"', c).group(1)
        self.base = posixpath.dirname(self.opf_path)
        self.root = ET.fromstring(self.z.read(self.opf_path))
        self.manifest = {}
        for it in self.root.find(OPFNS + 'manifest'):
            self.manifest[it.get('id')] = (it.get('href'), it.get('media-type'),
                                           it.get('properties'))
        self.spine = [i.get('idref') for i in self.root.find(OPFNS + 'spine')]
        self.spine_props = [i.get('properties') for i in self.root.find(OPFNS + 'spine')]
        self.linear = [i.get('linear') for i in self.root.find(OPFNS + 'spine')]
        self.href_by_id = {k: self.full(v[0]) for k, v in self.manifest.items()}
        self.id_by_href = {}
        for k, v in self.href_by_id.items():
            self.id_by_href.setdefault(v, k)
        self.spine_files = [self.href_by_id[i] for i in self.spine]

    def full(self, href):
        href = href.split('#')[0]
        return norm(posixpath.join(self.base, href)) if self.base else norm(href)

    def read(self, full_path):
        return self.z.read(full_path)

    # ---------- resource closure ----------
    def refs_in(self, full_path):
        try:
            raw = self.read(full_path)
        except KeyError:
            return []
        ext = os.path.splitext(full_path)[1].lower()
        d = posixpath.dirname(full_path)
        out = []
        if ext == '.css':
            txt = raw.decode('utf-8', 'replace')
            for m in re.finditer(r'url\(\s*[\'"]?([^\'")]+)[\'"]?\s*\)', txt):
                out.append(m.group(1))
            for m in re.finditer(r'@import\s+[\'"]([^\'"]+)[\'"]', txt):
                out.append(m.group(1))
        else:
            txt = raw.decode('utf-8', 'replace')
            for m in re.finditer(r'(?:href|src|xlink:href)\s*=\s*["\']([^"\']+)["\']', txt):
                out.append(m.group(1))
        res = []
        for r in out:
            r = r.strip()
            if not r or r.startswith(('http:', 'https:', 'data:', 'mailto:', '#')):
                continue
            r = r.split('#')[0]
            if not r:
                continue
            p = norm(posixpath.join(d, r)) if d else norm(r)
            if p in self.names:
                res.append(p)
        return res

    def closure(self, docs):
        seen = set(docs)
        queue = list(docs)
        while queue:
            cur = queue.pop()
            for r in self.refs_in(cur):
                if r not in seen:
                    seen.add(r)
                    queue.append(r)
        return seen

    # ---------- table of contents ----------
    def toc_tree(self):
        """Return nested list of (label, target_full_path, children)."""
        tocid = self.root.find(OPFNS + 'spine').get('toc')
        if tocid and tocid in self.manifest:
            ncx_path = self.href_by_id[tocid]
            nr = ET.fromstring(self.read(ncx_path))
            d = posixpath.dirname(ncx_path)

            def walk(node):
                out = []
                for np in node.findall(NCXNS + 'navPoint'):
                    lab = ''.join(np.find(NCXNS + 'navLabel').itertext()).strip()
                    src = np.find(NCXNS + 'content').get('src')
                    tgt = norm(posixpath.join(d, src)) if d else norm(src.split('#')[0])
                    frag = src.split('#')[1] if '#' in src else None
                    out.append((lab, tgt.split('#')[0], frag, walk(np)))
                return out
            return walk(nr.find(NCXNS + 'navMap'))
        # EPUB3 nav
        navid = None
        for k, (h, mt, pr) in self.manifest.items():
            if pr and 'nav' in pr.split():
                navid = k
        if not navid:
            return []
        nav_path = self.href_by_id[navid]
        d = posixpath.dirname(nav_path)
        nr = ET.fromstring(self.read(nav_path))

        def walk_ol(ol):
            out = []
            for li in ol.findall(XH + 'li'):
                a = li.find(XH + 'a') or li.find(XH + 'span')
                if a is None:
                    continue
                lab = ''.join(a.itertext()).strip()
                href = a.get('href') or ''
                tgt = norm(posixpath.join(d, href.split('#')[0])) if d else norm(href.split('#')[0])
                frag = href.split('#')[1] if '#' in href else None
                sub = li.find(XH + 'ol')
                out.append((lab, tgt, frag, walk_ol(sub) if sub is not None else []))
            return out
        for nav in nr.iter(XH + 'nav'):
            if nav.get(EPUBNS + 'type') == 'toc':
                ol = nav.find(XH + 'ol')
                if ol is not None:
                    return walk_ol(ol)
        return []


def prune_toc(tree, allowed):
    """Keep entries whose target is in `allowed`; promote kept descendants of dropped nodes."""
    out = []
    for lab, tgt, frag, kids in tree:
        pk = prune_toc(kids, allowed)
        if tgt in allowed:
            out.append((lab, tgt, frag, pk))
        else:
            out.extend(pk)
    return out


def ncx_xml(tree, uid, title, author):
    lines = ['<?xml version="1.0" encoding="utf-8"?>',
             '<!DOCTYPE ncx PUBLIC "-//NISO//DTD ncx 2005-1//EN" '
             '"http://www.daisy.org/z3986/2005/ncx-2005-1.dtd">',
             '<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1">',
             '  <head>',
             '    <meta name="dtb:uid" content="%s"/>' % esc(uid),
             '    <meta name="dtb:depth" content="2"/>',
             '    <meta name="dtb:totalPageCount" content="0"/>',
             '    <meta name="dtb:maxPageNumber" content="0"/>',
             '  </head>',
             '  <docTitle><text>%s</text></docTitle>' % esc(title),
             '  <docAuthor><text>%s</text></docAuthor>' % esc(author),
             '  <navMap>']
    counter = [0]

    def emit(nodes, depth):
        for lab, href, frag, kids in nodes:
            counter[0] += 1
            n = counter[0]
            tgt = href + ('#' + frag if frag else '')
            pad = '    ' + '  ' * depth
            lines.append('%s<navPoint id="np%d" playOrder="%d">' % (pad, n, n))
            lines.append('%s  <navLabel><text>%s</text></navLabel>' % (pad, esc(lab or 'Section')))
            lines.append('%s  <content src="%s"/>' % (pad, esc(tgt)))
            emit(kids, depth + 1)
            lines.append('%s</navPoint>' % pad)
    emit(tree, 0)
    lines.append('  </navMap>')
    lines.append('</ncx>')
    return '\n'.join(lines)


COVER_DOC = ('<?xml version="1.0" encoding="utf-8"?>\n'
             '<!DOCTYPE html PUBLIC "-//W3C//DTD XHTML 1.1//EN" '
             '"http://www.w3.org/TR/xhtml11/DTD/xhtml11.dtd">\n'
             '<html xmlns="http://www.w3.org/1999/xhtml">\n<head><title>Cover</title>\n'
             '<style type="text/css">body{margin:0;padding:0;text-align:center;}\n'
             'img{max-width:100%;height:auto;}</style></head>\n'
             '<body><div><img src="cover_generated.jpg" alt="Cover"/></div></body>\n'
             '</html>\n')


def split_book(src_epub, out_path, spine_range, md, extra_spine=(), cover_hint=None,
               gen_cover_bytes=None):
    om = Omnibus(src_epub)
    lo, hi = spine_range
    idxs = list(range(lo, hi + 1)) + list(extra_spine)
    docs = [om.spine_files[i] for i in idxs]
    keep = om.closure(docs)
    extra_files = {}
    if gen_cover_bytes:
        cimg = posixpath.join(om.base, 'cover_generated.jpg') if om.base else 'cover_generated.jpg'
        cdoc = posixpath.join(om.base, 'cover_generated.xhtml') if om.base else 'cover_generated.xhtml'
        extra_files[cimg] = gen_cover_bytes
        extra_files[cdoc] = COVER_DOC.encode('utf-8')
        docs = [cdoc] + docs
        keep |= {cimg, cdoc}

    # drop the omnibus-wide nav/ncx from the payload -- we generate our own
    tocid = om.root.find(OPFNS + 'spine').get('toc')
    drop = set()
    if tocid and tocid in om.href_by_id:
        drop.add(om.href_by_id[tocid])
    for k, (h, mt, pr) in om.manifest.items():
        if pr and 'nav' in pr.split():
            drop.add(om.href_by_id[k])
    keep -= drop
    keep |= set(docs)

    # ---- cover image ----
    cover_file = None
    if gen_cover_bytes:
        cover_file = cimg
    if cover_hint and not cover_file:
        cands = [p for p in keep if cover_hint.lower() in p.lower()]
        if cands:
            cover_file = sorted(cands)[0]
    if not cover_file:
        for d in docs[:3]:
            imgs = [r for r in om.refs_in(d)
                    if os.path.splitext(r)[1].lower() in
                    ('.jpg', '.jpeg', '.png', '.gif', '.svg', '.webp')]
            pref = [i for i in imgs if 'cover' in os.path.basename(i).lower()]
            if pref or imgs:
                cover_file = (pref or imgs)[0]
                break

    # ---- manifest ----
    base = om.base
    def rel(p):
        return posixpath.relpath(p, base) if base else p

    items, used_ids = [], set()
    def mkid(p):
        cand = om.id_by_href.get(p)
        if cand and cand not in used_ids:
            used_ids.add(cand)
            return cand
        stem = re.sub(r'[^A-Za-z0-9_.-]', '_', os.path.basename(p))
        cand = 'x_' + stem
        i = 0
        while cand in used_ids:
            i += 1
            cand = 'x%d_%s' % (i, stem)
        used_ids.add(cand)
        return cand

    ids = {}
    for p in sorted(keep):
        mid = mkid(p)
        ids[p] = mid
        src_mt = None
        oid = om.id_by_href.get(p)
        if oid:
            src_mt = om.manifest[oid][1]
        mt = src_mt or MT.get(os.path.splitext(p)[1].lower()) or \
            mimetypes.guess_type(p)[0] or 'application/octet-stream'
        props = ' properties="cover-image"' if p == cover_file and mt.startswith('image/') else ''
        items.append('    <item id="%s" href="%s" media-type="%s"%s/>'
                     % (esc(mid), esc(rel(p)), esc(mt), props))
    items.append('    <item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>')

    # ---- spine ----
    sp = ['    <itemref idref="%s"/>' % esc(ids[d]) for d in docs]

    # ---- toc ----
    tree = prune_toc(om.toc_tree(), set(docs))
    if not tree:
        tree = [(md['title'], docs[0], None, [])]
    tree = [(lab, rel(t), frag, [(l2, rel(t2), f2, [(l3, rel(t3), f3, []) for l3, t3, f3, _ in k2])
                                 for l2, t2, f2, k2 in kids])
            for lab, t, frag, kids in tree]
    uid = 'urn:isbn:' + md['isbn'] if md.get('isbn') else 'urn:uuid:split'
    ncx = ncx_xml(tree, uid, md['title'], md['authors'][0][0])

    # ---- opf ----
    metadata = build_metadata(md, '2.0', 'bookid', ids.get(cover_file))
    guide = ''
    first = rel(docs[0])
    if 'cover' in os.path.basename(docs[0]).lower():
        guide = ('  <guide>\n    <reference type="cover" title="Cover" href="%s"/>\n  </guide>\n'
                 % esc(first))
    opf = ('<?xml version="1.0" encoding="utf-8"?>\n'
           '<package xmlns="http://www.idpf.org/2007/opf" version="2.0" '
           'unique-identifier="bookid">\n  %s\n  <manifest>\n%s\n  </manifest>\n'
           '  <spine toc="ncx">\n%s\n  </spine>\n%s</package>\n'
           % (metadata, '\n'.join(items), '\n'.join(sp), guide))
    ET.fromstring(opf.encode('utf-8'))
    ET.fromstring(ncx.encode('utf-8'))

    container = ('<?xml version="1.0" encoding="UTF-8"?>\n'
                 '<container version="1.0" '
                 'xmlns="urn:oasis:names:tc:opendocument:xmlns:container">\n'
                 '  <rootfiles>\n    <rootfile full-path="%s" '
                 'media-type="application/oebps-package+xml"/>\n'
                 '  </rootfiles>\n</container>\n' % om.opf_path)

    ncx_path = posixpath.join(base, 'toc.ncx') if base else 'toc.ncx'
    with zipfile.ZipFile(out_path, 'w') as zo:
        zo.writestr(zipfile.ZipInfo('mimetype'), 'application/epub+zip',
                    compress_type=zipfile.ZIP_STORED)
        zo.writestr('META-INF/container.xml', container, zipfile.ZIP_DEFLATED)
        zo.writestr(om.opf_path, opf.encode('utf-8'), zipfile.ZIP_DEFLATED)
        zo.writestr(ncx_path, ncx.encode('utf-8'), zipfile.ZIP_DEFLATED)
        for p in sorted(keep):
            data = extra_files[p] if p in extra_files else om.read(p)
            zo.writestr(p, data, zipfile.ZIP_DEFLATED)
    om.z.close()
    return {'files': len(keep) + 3, 'docs': len(docs), 'cover': cover_file,
            'toc_entries': len(tree)}
