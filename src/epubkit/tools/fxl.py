#!/usr/bin/env python3
"""Build a Kobo fixed-layout .kepub.epub from a folder of page images."""
import os, zipfile, html, uuid
from PIL import Image

# Panel size of the target device. Clara Colour / Clara HD / Clara 2E are
# 1072x1448; Libra 1264x1680; Sage 1440x1920; Elipsa 1404x1872.
VW, VH = 1072, 1448
MOD = '2026-08-16T00:00:00Z'


def set_panel(w, h):
    """Point the builder at a different device before calling build()."""
    global VW, VH, CSS
    VW, VH = int(w), int(h)
    CSS = CSS_TEMPLATE.format(w=VW, h=VH)


def esc(s):
    return html.escape(str(s), quote=False).replace('"', '&quot;')


CSS_TEMPLATE = """@page {{ margin: 0; padding: 0; }}
html, body {{ margin: 0; padding: 0; }}
body {{ width: {w}px; height: {h}px; }}
div.page {{ position: absolute; top: 0; left: 0; width: {w}px; height: {h}px; }}
img.pg {{ position: absolute; top: 0; left: 0; width: {w}px; height: {h}px; }}
"""
CSS = CSS_TEMPLATE.format(w=VW, h=VH)

PAGE = """<?xml version="1.0" encoding="utf-8"?>
<!DOCTYPE html>
<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops">
<head>
  <title>{title}</title>
  <meta name="viewport" content="width={w}, height={h}"/>
  <link rel="stylesheet" type="text/css" href="style.css"/>
</head>
<body>
  <div class="page"><img class="pg" src="{img}" alt="" width="{w}" height="{h}"/></div>
</body>
</html>
"""

DISPLAY_OPTS = """<?xml version="1.0" encoding="UTF-8"?>
<display_options>
  <platform name="*">
    <option name="fixed-layout">true</option>
    <option name="open-to-spread">false</option>
  </platform>
</display_options>
"""

CONTAINER = """<?xml version="1.0" encoding="UTF-8"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
  <rootfiles>
    <rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>
  </rootfiles>
</container>
"""


def letterbox(im, bg=None):
    """Fit the page inside the panel, padding with a colour sampled from its edge."""
    im = im.convert('RGB')
    if bg is None:
        e = im.resize((min(im.width, 60), min(im.height, 60)))
        px = list(e.crop((0, 0, e.width, 1)).getdata()) + \
             list(e.crop((0, e.height - 1, e.width, e.height)).getdata()) + \
             list(e.crop((0, 0, 1, e.height)).getdata()) + \
             list(e.crop((e.width - 1, 0, e.width, e.height)).getdata())
        px.sort()
        bg = px[len(px) // 2]
    # never upscale past the source: the page is displayed at the panel's
    # viewport regardless, so storing more pixels than the scan has just
    # inflates the file. Canvas keeps the panel's aspect ratio either way.
    ar = VW / VH
    cw = max(im.width, im.height * ar)
    ch = cw / ar
    if cw > VW:
        cw, ch = VW, VH
    cw, ch = int(round(cw)), int(round(ch))
    s = min(cw / im.width, ch / im.height, 1.0)
    nw, nh = max(1, round(im.width * s)), max(1, round(im.height * s))
    if s < 1.0:
        im = im.resize((nw, nh), Image.LANCZOS)
    canvas = Image.new('RGB', (cw, ch), bg)
    canvas.paste(im, ((cw - nw) // 2, (ch - nh) // 2))
    return canvas


def build(images, out_path, md, quality=88):
    """images: PIL Images OR paths to already-fitted page files, in reading order.

    Paths are the normal case for a long book: holding a few hundred decoded
    pages costs more memory than the machine wants to give, so the caller fits
    them to the panel in parallel, writes them out, and hands over filenames.
    """
    n = len(images)
    ids = ['pg%04d' % (i + 1) for i in range(n)]
    manifest, spine, navlis, navpoints = [], [], [], []

    tmp = out_path + '.parts'
    os.makedirs(tmp, exist_ok=True)
    files = {}
    for i, im in enumerate(images):
        if isinstance(im, str):
            im = Image.open(im)
        canvas = letterbox(im)
        p = os.path.join(tmp, 'p%04d.jpg' % (i + 1))
        canvas.save(p, 'JPEG', quality=quality, optimize=True, progressive=False)
        files['OEBPS/images/p%04d.jpg' % (i + 1)] = p
        label = 'Cover' if i == 0 else 'Page %d' % i
        manifest.append('    <item id="img%04d" href="images/p%04d.jpg" media-type="image/jpeg"%s/>'
                        % (i + 1, i + 1, ' properties="cover-image"' if i == 0 else ''))
        manifest.append('    <item id="%s" href="p%04d.xhtml" media-type="application/xhtml+xml"/>'
                        % (ids[i], i + 1))
        spine.append('    <itemref idref="%s"/>' % ids[i])
        if i == 0 or (i % 10 == 0):
            navlis.append('        <li><a href="p%04d.xhtml">%s</a></li>' % (i + 1, esc(label)))
            navpoints.append(
                '    <navPoint id="n%d" playOrder="%d"><navLabel><text>%s</text></navLabel>'
                '<content src="p%04d.xhtml"/></navPoint>' % (i + 1, len(navpoints) + 1, esc(label), i + 1))

    uid = 'urn:uuid:' + str(uuid.uuid5(uuid.NAMESPACE_URL, md['title']))
    meta = ['    <dc:identifier id="bookid">%s</dc:identifier>' % uid,
            '    <dc:title id="t1">%s</dc:title>' % esc(md['title']),
            '    <dc:language>en</dc:language>']
    for k, (nm, fa) in enumerate(md['authors'], 1):
        meta.append('    <dc:creator id="cr%d" opf:role="aut" opf:file-as="%s">%s</dc:creator>'
                    % (k, esc(fa), esc(nm)))
        meta.append('    <meta refines="#cr%d" property="file-as">%s</meta>' % (k, esc(fa)))
        meta.append('    <meta refines="#cr%d" property="role" scheme="marc:relators">aut</meta>' % k)
    if md.get('publisher'):
        meta.append('    <dc:publisher>%s</dc:publisher>' % esc(md['publisher']))
    if md.get('date'):
        meta.append('    <dc:date>%s</dc:date>' % esc(md['date']))
    for s in md.get('subjects', []):
        meta.append('    <dc:subject>%s</dc:subject>' % esc(s))
    if md.get('description'):
        meta.append('    <dc:description>%s</dc:description>' % esc(md['description']))
    if md.get('series'):
        meta.append('    <meta name="calibre:series" content="%s"/>' % esc(md['series']))
        meta.append('    <meta name="calibre:series_index" content="%s"/>' % esc(md['series_index']))
        meta.append('    <meta property="belongs-to-collection" id="ser">%s</meta>' % esc(md['series']))
        meta.append('    <meta refines="#ser" property="collection-type">series</meta>')
        meta.append('    <meta refines="#ser" property="group-position">%s</meta>' % esc(md['series_index']))
    meta.append('    <meta name="cover" content="img0001"/>')
    meta.append('    <meta property="rendition:layout">pre-paginated</meta>')
    meta.append('    <meta property="rendition:orientation">portrait</meta>')
    meta.append('    <meta property="rendition:spread">none</meta>')
    meta.append('    <meta property="dcterms:modified">%s</meta>' % MOD)

    opf = ('<?xml version="1.0" encoding="utf-8"?>\n'
           '<package xmlns="http://www.idpf.org/2007/opf" xmlns:opf="http://www.idpf.org/2007/opf" '
           'version="3.0" unique-identifier="bookid" prefix="rendition: http://www.idpf.org/vocab/rendition/#">\n'
           '  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">\n%s\n  </metadata>\n'
           '  <manifest>\n%s\n'
           '    <item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>\n'
           '    <item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>\n'
           '    <item id="css" href="style.css" media-type="text/css"/>\n'
           '  </manifest>\n'
           '  <spine toc="ncx">\n%s\n  </spine>\n'
           '</package>\n' % ('\n'.join(meta), '\n'.join(manifest), '\n'.join(spine)))

    nav = ('<?xml version="1.0" encoding="utf-8"?>\n<!DOCTYPE html>\n'
           '<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops">\n'
           '<head><title>Contents</title></head><body>\n'
           '  <nav epub:type="toc" id="toc"><h1>Contents</h1><ol>\n%s\n  </ol></nav>\n'
           '</body></html>\n' % '\n'.join(navlis))

    ncx = ('<?xml version="1.0" encoding="utf-8"?>\n'
           '<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1">\n'
           '  <head><meta name="dtb:uid" content="%s"/><meta name="dtb:depth" content="1"/>\n'
           '  <meta name="dtb:totalPageCount" content="0"/><meta name="dtb:maxPageNumber" content="0"/></head>\n'
           '  <docTitle><text>%s</text></docTitle>\n  <navMap>\n%s\n  </navMap>\n</ncx>\n'
           % (uid, esc(md['title']), '\n'.join(navpoints)))

    with zipfile.ZipFile(out_path, 'w') as z:
        z.writestr(zipfile.ZipInfo('mimetype'), 'application/epub+zip', compress_type=zipfile.ZIP_STORED)
        z.writestr('META-INF/container.xml', CONTAINER, zipfile.ZIP_DEFLATED)
        z.writestr('META-INF/com.kobobooks.display-options.xml', DISPLAY_OPTS, zipfile.ZIP_DEFLATED)
        z.writestr('OEBPS/content.opf', opf, zipfile.ZIP_DEFLATED)
        z.writestr('OEBPS/nav.xhtml', nav, zipfile.ZIP_DEFLATED)
        z.writestr('OEBPS/toc.ncx', ncx, zipfile.ZIP_DEFLATED)
        z.writestr('OEBPS/style.css', CSS, zipfile.ZIP_DEFLATED)
        for i in range(n):
            z.writestr('OEBPS/p%04d.xhtml' % (i + 1),
                       PAGE.format(title=esc(md['title']), w=VW, h=VH,
                                   img='images/p%04d.jpg' % (i + 1)), zipfile.ZIP_DEFLATED)
        for arc, path in files.items():
            z.write(path, arc, zipfile.ZIP_STORED)   # JPEGs: already compressed

    for p in files.values():
        os.remove(p)
    os.rmdir(tmp)
    return os.path.getsize(out_path), n
