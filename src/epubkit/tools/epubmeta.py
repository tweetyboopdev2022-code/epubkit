#!/usr/bin/env python3
"""Rewrite the OPF <metadata> block of an EPUB, leaving everything else byte-identical."""
import zipfile, re, os, shutil, uuid, html
import xml.etree.ElementTree as ET

OPF = 'http://www.idpf.org/2007/opf'
DC = 'http://purl.org/dc/elements/1.1/'
ET.register_namespace('opf', OPF)
ET.register_namespace('dc', DC)

MOD = '2026-08-15T00:00:00Z'


def esc(s):
    return html.escape(str(s), quote=False).replace('"', '&quot;')


LAYOUT_META = re.compile(
    r'<meta\b[^>]*property="rendition:[^"]*"[^>]*>[^<]*</meta>|'
    r'<meta\b[^>]*name="(?:fixed-layout|original-resolution|book-type|'
    r'orientation-lock|RegionMagnification|primary-writing-mode)"[^>]*/?>', re.S)


def find_opf(z):
    c = z.read('META-INF/container.xml').decode('utf-8', 'replace')
    return re.search(r'full-path="([^"]+)"', c).group(1)


def build_metadata(md, pkg_version, uid_id, cover_id):
    """Return the replacement <metadata>...</metadata> XML string."""
    v3 = str(pkg_version).startswith('3')
    L = []
    L.append('<metadata xmlns:dc="http://purl.org/dc/elements/1.1/" '
             'xmlns:opf="http://www.idpf.org/2007/opf">')
    # identifier(s)
    isbn = md.get('isbn') or ''
    if isbn:
        L.append('    <dc:identifier id="%s" opf:scheme="ISBN">urn:isbn:%s</dc:identifier>'
                 % (esc(uid_id), esc(isbn)))
    else:
        L.append('    <dc:identifier id="%s">urn:uuid:%s</dc:identifier>'
                 % (esc(uid_id), md.get('uuid') or str(uuid.uuid4())))
    md['_written_identifier'] = ('urn:isbn:%s' % isbn) if isbn else L[-1].split('>')[1].split('<')[0]
    L.append('    <dc:title id="t1">%s</dc:title>' % esc(md['title']))
    if v3:
        L.append('    <meta refines="#t1" property="title-type">main</meta>')
        L.append('    <meta refines="#t1" property="file-as">%s</meta>'
                 % esc(md.get('title_sort') or md['title']))
    for n, (name, file_as) in enumerate(md['authors'], 1):
        aid = 'creator%d' % n
        L.append('    <dc:creator id="%s" opf:role="aut" opf:file-as="%s">%s</dc:creator>'
                 % (aid, esc(file_as), esc(name)))
        if v3:
            L.append('    <meta refines="#%s" property="role" scheme="marc:relators">aut</meta>' % aid)
            L.append('    <meta refines="#%s" property="file-as">%s</meta>' % (aid, esc(file_as)))
    L.append('    <dc:language>%s</dc:language>' % esc(md.get('language') or 'en'))
    if md.get('publisher'):
        L.append('    <dc:publisher>%s</dc:publisher>' % esc(md['publisher']))
    if md.get('date'):
        L.append('    <dc:date>%s</dc:date>' % esc(md['date']))
    for s in md.get('subjects') or []:
        L.append('    <dc:subject>%s</dc:subject>' % esc(s))
    if md.get('description'):
        L.append('    <dc:description>%s</dc:description>' % esc(md['description']))
    # series -- calibre form (read by Kobo, Calibre, KOReader) + EPUB3 collection
    ser = md.get('series') or ''
    if ser:
        # A blank position must stay blank. Defaulting it to 1 is how a companion
        # volume that belongs to a series without a number in it - a short-story
        # collection, an activity tie-in - becomes "book 1" and takes the real
        # book 1's place in the filename and on the shelf. Better to carry the
        # series with no position than to invent one.
        idx = str(md.get('series_index') or '').strip()
        L.append('    <meta name="calibre:series" content="%s"/>' % esc(ser))
        if idx:
            L.append('    <meta name="calibre:series_index" content="%s"/>' % esc(idx))
        if v3:
            L.append('    <meta property="belongs-to-collection" id="series">%s</meta>' % esc(ser))
            L.append('    <meta refines="#series" property="collection-type">series</meta>')
            if idx:
                L.append('    <meta refines="#series" property="group-position">%s</meta>' % esc(idx))
    if cover_id:
        L.append('    <meta name="cover" content="%s"/>' % esc(cover_id))
    if v3:
        L.append('    <meta property="dcterms:modified">%s</meta>' % MOD)
    L.append('  </metadata>')
    return '\n'.join(L)


def manifest_ids(opf_xml):
    return set(re.findall(r'<item\b[^>]*\bid="([^"]+)"', opf_xml))


def pick_cover_id(opf_xml):
    ids = manifest_ids(opf_xml)
    m = re.search(r'<meta[^>]*name="cover"[^>]*content="([^"]+)"', opf_xml)
    if m and m.group(1) in ids:
        return m.group(1)
    m = re.search(r'<item([^>]*properties="[^"]*cover-image[^"]*"[^>]*)/?>', opf_xml)
    if m:
        mid = re.search(r'id="([^"]+)"', m.group(1))
        if mid:
            return mid.group(1)
    # fall back to an image item that looks like cover art
    # (_cvi_ is the common publisher convention for "cover image")
    for pat in ('_cvi_', 'cover'):
        for m in re.finditer(r'<item([^>]*)/?>', opf_xml):
            attrs = m.group(1)
            if 'image/' in attrs and pat in attrs.lower() and '_cvt_' not in attrs.lower():
                mid = re.search(r'id="([^"]+)"', attrs)
                if mid:
                    return mid.group(1)
    return None


COVER_DOC = ('<?xml version="1.0" encoding="utf-8"?>\n'
             '<!DOCTYPE html PUBLIC "-//W3C//DTD XHTML 1.1//EN" '
             '"http://www.w3.org/TR/xhtml11/DTD/xhtml11.dtd">\n'
             '<html xmlns="http://www.w3.org/1999/xhtml">\n<head><title>Cover</title>\n'
             '<style type="text/css">body{margin:0;padding:0;text-align:center;}\n'
             'img{max-width:100%;height:auto;}</style></head>\n'
             '<body><div><img src="cover_generated.jpg" alt="Cover"/></div></body>\n'
             '</html>\n')


def sync_ncx_uid(ncx_xml, new_uid):
    """Point toc.ncx's dtb:uid at the identifier the OPF now carries, so a
    validator never sees the two disagree (NCX-001)."""
    new_meta = '<meta name="dtb:uid" content="%s"/>' % esc(new_uid)
    if re.search(r'<meta\s+name="dtb:uid"[^>]*/?>', ncx_xml):
        return re.sub(r'<meta\s+name="dtb:uid"[^>]*/?>', new_meta, ncx_xml, count=1)
    return re.sub(r'(<head[^>]*>)', r'\1' + new_meta, ncx_xml, count=1)


def rewrite(src, dst, md, gen_cover_bytes=None):
    z = zipfile.ZipFile(src)
    opf_path = find_opf(z)
    opf_xml = z.read(opf_path).decode('utf-8', 'replace')

    pkg = re.search(r'<package\b[^>]*>', opf_xml).group(0)
    ver = (re.search(r'version="([^"]+)"', pkg) or [None, '2.0'])[1]
    uid_id = (re.search(r'unique-identifier="([^"]+)"', pkg) or [None, 'bookid'])[1]
    if 'unique-identifier=' not in pkg:
        newpkg = pkg[:-1].rstrip('/') + ' unique-identifier="bookid">'
        opf_xml = opf_xml.replace(pkg, newpkg, 1)
        pkg, uid_id = newpkg, 'bookid'

    extra = {}
    if gen_cover_bytes:
        import posixpath
        d = posixpath.dirname(opf_path)
        cimg = posixpath.join(d, 'cover_generated.jpg') if d else 'cover_generated.jpg'
        cdoc = posixpath.join(d, 'cover_generated.xhtml') if d else 'cover_generated.xhtml'
        extra[cimg] = gen_cover_bytes
        extra[cdoc] = COVER_DOC.encode('utf-8')
        new_items = ('    <item id="gen-cover-image" href="cover_generated.jpg" '
                     'media-type="image/jpeg" properties="cover-image"/>\n'
                     '    <item id="gen-cover-page" href="cover_generated.xhtml" '
                     'media-type="application/xhtml+xml"/>\n  </manifest>')
        opf_xml = opf_xml.replace('</manifest>', new_items, 1)
        opf_xml = re.sub(r'(<spine\b[^>]*>)',
                         r'\1\n    <itemref idref="gen-cover-page" linear="yes"/>',
                         opf_xml, count=1)

    cover_id = pick_cover_id(opf_xml)
    new_md = build_metadata(md, ver, uid_id, cover_id)
    # Layout settings are not metadata about the book, they are how it is
    # drawn. Dropping rendition:layout turned a rebuilt Dog Man back into
    # a reflowable book - the same thing Grimmory's write-back does.
    old_md = re.search(r'<metadata\b.*?</metadata\s*>', opf_xml, re.S)
    keep = LAYOUT_META.findall(old_md.group(0)) if old_md else []
    if keep:
        new_md = new_md.replace('  </metadata>', ''.join(
            '    %s\n' % k.strip() for k in keep) + '  </metadata>')

    new_opf, n = re.subn(r'<metadata\b.*?</metadata\s*>', lambda _m: new_md, opf_xml,
                         count=1, flags=re.S)
    if n != 1:
        raise RuntimeError('could not locate <metadata> in %s' % src)
    # sanity: must still parse
    ET.fromstring(new_opf)

    ncx_path = next((n for n in z.namelist() if n.lower().endswith('.ncx')), None)
    new_uid = md.get('_written_identifier')

    tmp = dst + '.tmp'
    zin = z
    with zipfile.ZipFile(tmp, 'w') as zo:
        zo.writestr(zipfile.ZipInfo('mimetype'), 'application/epub+zip',
                    compress_type=zipfile.ZIP_STORED)
        for item in zin.infolist():
            if item.filename == 'mimetype':
                continue
            data = zin.read(item.filename)
            if item.filename == opf_path:
                data = new_opf.encode('utf-8')
            elif item.filename == ncx_path and new_uid:
                data = sync_ncx_uid(data.decode('utf-8', 'replace'), new_uid).encode('utf-8')
            zi = zipfile.ZipInfo(item.filename, date_time=item.date_time)
            zi.external_attr = item.external_attr
            zo.writestr(zi, data, compress_type=zipfile.ZIP_DEFLATED)
        for p, data in extra.items():
            zo.writestr(p, data, compress_type=zipfile.ZIP_DEFLATED)
    z.close()
    shutil.move(tmp, dst)
    return ver, cover_id
