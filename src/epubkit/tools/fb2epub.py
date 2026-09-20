#!/usr/bin/env python3
"""Convert an FB2 file to a plain EPUB 2.

FB2 is a single XML document: <description> holds the metadata, <body> holds
nested <section>s, and <binary> elements carry the images base64-encoded at the
end of the file. Sections become one XHTML file each, which is what gives the
Kobo something to page through and what the TOC points at.
"""
import sys, os, re, base64, zipfile, html, uuid
import xml.etree.ElementTree as ET

FB = "{http://www.gribuser.ru/xml/fictionbook/2.0}"
XL = "{http://www.w3.org/1999/xlink}"
INLINE = {"strong": "strong", "emphasis": "em", "style": "span", "a": "a",
          "strikethrough": "s", "sub": "sub", "sup": "sup", "code": "code"}


def render(el, out):
    """FB2 body markup -> XHTML, keeping the inline formatting."""
    tag = el.tag.replace(FB, "")
    if tag == "empty-line":
        out.append("<p class='empty'>&#160;</p>"); return
    if tag == "image":
        href = (el.get(XL + "href") or el.get("href") or "").lstrip("#")
        if href:
            out.append('<div class="img"><img src="images/%s" alt=""/></div>' % html.escape(href))
        return
    if tag in ("p", "v", "subtitle", "text-author"):
        cls = {"v": "verse", "subtitle": "subtitle", "text-author": "author"}.get(tag, "")
        out.append('<p%s>' % (' class="%s"' % cls if cls else ""))
        inline(el, out)
        out.append("</p>"); return
    if tag in ("poem", "cite", "epigraph", "stanza", "annotation"):
        out.append('<div class="%s">' % tag)
        for c in el:
            render(c, out)
        out.append("</div>"); return
    if tag == "title":
        out.append("<h2>")
        for c in el:
            inline(c, out); out.append("<br/>")
        out.append("</h2>"); return
    for c in el:
        render(c, out)


def inline(el, out):
    if el.text:
        out.append(html.escape(el.text))
    for c in el:
        t = c.tag.replace(FB, "")
        if t == "image":
            render(c, out)
        else:
            tagname = INLINE.get(t)
            if tagname:
                out.append("<%s>" % tagname)
                inline(c, out)
                out.append("</%s>" % tagname)
            else:
                inline(c, out)
        if c.tail:
            out.append(html.escape(c.tail))


def text_of(el):
    return re.sub(r"\s+", " ", "".join(el.itertext())).strip() if el is not None else ""


def read_fb2(src):
    """The bytes of the FB2 document, whether or not it arrived in a zip.

    FB2 is handed out both ways and the zipped form is the more common of the
    two, so refusing it means refusing most of the format. A zip holding
    anything but exactly one FB2 is rejected rather than guessed at: several
    means it is a bundle nobody asked this to split, and none means it is some
    other archive that happens to be named alike.
    """
    if not zipfile.is_zipfile(src):
        return open(src, "rb").read()
    with zipfile.ZipFile(src) as z:
        members = [n for n in z.namelist()
                   if n.lower().endswith(".fb2") and not n.startswith("__MACOSX/")]
        if len(members) != 1:
            raise SystemExit(
                "%s holds %d .fb2 files, expected exactly one"
                % (os.path.basename(src), len(members)))
        return z.read(members[0])


def base_name(src):
    """The filename without its extension, counting .fb2.zip as one."""
    stem = os.path.basename(src)
    for ext in (".fb2.zip", ".fb2", ".zip"):
        if stem.lower().endswith(ext):
            return stem[:-len(ext)]
    return os.path.splitext(stem)[0]


def convert(src, dst):
    raw = read_fb2(src)
    root = ET.fromstring(raw)
    desc = root.find(FB + "description")
    ti = desc.find(FB + "title-info") if desc is not None else None

    def g(path):
        return text_of(ti.find(path)) if ti is not None else ""
    title = g(FB + "book-title") or base_name(src)
    au = ti.find(FB + "author") if ti is not None else None
    author = " ".join(x for x in [text_of(au.find(FB + "first-name")) if au is not None else "",
                                  text_of(au.find(FB + "last-name")) if au is not None else ""] if x)
    lang = g(FB + "lang") or "en"
    ann = ti.find(FB + "annotation") if ti is not None else None
    blurb = text_of(ann)
    date = g(FB + "date")

    images = {}
    for b in root.iter(FB + "binary"):
        bid = b.get("id")
        try:
            images[bid] = base64.b64decode((b.text or "").strip())
        except Exception:
            pass

    bodies = [b for b in root.findall(FB + "body") if b.get("name") != "notes"]
    notes = [b for b in root.findall(FB + "body") if b.get("name") == "notes"]
    chunks = []
    for body in bodies + notes:
        secs = body.findall(FB + "section")
        if not secs:
            secs = [body]
        for s in secs:
            t = text_of(s.find(FB + "title")) or "Section %d" % (len(chunks) + 1)
            out = []
            for c in s:
                render(c, out)
            chunks.append((t, "".join(out)))

    z = zipfile.ZipFile(dst, "w", zipfile.ZIP_DEFLATED)
    z.writestr("mimetype", "application/epub+zip", zipfile.ZIP_STORED)
    z.writestr("META-INF/container.xml",
               '<?xml version="1.0"?><container version="1.0" '
               'xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
               '<rootfiles><rootfile full-path="OEBPS/content.opf" '
               'media-type="application/oebps-package+xml"/></rootfiles></container>')
    z.writestr("OEBPS/style.css",
               "body{margin:0 5%;line-height:1.4}h2{text-align:center;margin:2em 0 1em}"
               "p{margin:0;text-indent:1.2em}p.empty{text-indent:0}"
               "p.verse{text-indent:0;margin-left:1.5em}p.subtitle{text-align:center;"
               "font-weight:bold;text-indent:0;margin:1em 0}p.author{text-align:right;"
               "font-style:italic;text-indent:0}div.epigraph{margin:1.5em 2em;font-style:italic}"
               "div.img{text-align:center;margin:1em 0}img{max-width:100%}")

    manifest, spine, nav = [], [], []
    for i, (t, bodyhtml) in enumerate(chunks, 1):
        name = "ch%03d.xhtml" % i
        z.writestr("OEBPS/" + name,
                   '<?xml version="1.0" encoding="utf-8"?>\n'
                   '<!DOCTYPE html>\n<html xmlns="http://www.w3.org/1999/xhtml">'
                   '<head><title>%s</title><meta http-equiv="Content-Type" '
                   'content="text/html; charset=utf-8"/>'
                   '<link rel="stylesheet" type="text/css" href="style.css"/></head>'
                   '<body><h2>%s</h2>%s</body></html>'
                   % (html.escape(t), html.escape(t), bodyhtml))
        manifest.append('<item id="c%d" href="%s" media-type="application/xhtml+xml"/>' % (i, name))
        spine.append('<itemref idref="c%d"/>' % i)
        nav.append('<navPoint id="np%d" playOrder="%d"><navLabel><text>%s</text></navLabel>'
                   '<content src="%s"/></navPoint>' % (i, i, html.escape(t), name))

    # FB2 names its cover in <title-info><coverpage><image xlink:href="#id"/>.
    # Guessing instead - taking the first binary, or any id containing "cover" -
    # picks up an 18x18 ornament, because binaries are stored in no useful order.
    cover_bid = None
    cp = ti.find(FB + "coverpage") if ti is not None else None
    if cp is not None:
        for im in cp.iter(FB + "image"):
            h = (im.get(XL + "href") or im.get("href") or "").lstrip("#")
            if h in images:
                cover_bid = h
                break
    if cover_bid is None and images:
        cover_bid = max(images, key=lambda k: len(images[k]))

    cover_id = None
    for j, (bid, data) in enumerate(images.items()):
        ext = ".png" if data[:8] == b"\x89PNG\r\n\x1a\n" else ".jpg"
        safe = bid if bid.lower().endswith((".jpg", ".jpeg", ".png")) else bid + ext
        z.writestr("OEBPS/images/" + safe, data)
        mid = "img%d" % j
        manifest.append('<item id="%s" href="images/%s" media-type="%s"/>'
                        % (mid, safe, "image/png" if ext == ".png" else "image/jpeg"))
        if bid == cover_bid:
            cover_id = mid

    uid = "urn:uuid:" + str(uuid.uuid4())
    z.writestr("OEBPS/toc.ncx",
               '<?xml version="1.0" encoding="utf-8"?>\n'
               '<ncx version="2005-1" xmlns="http://www.daisy.org/z3986/2005/ncx/">'
               '<head><meta name="dtb:uid" content="%s"/><meta name="dtb:depth" content="1"/>'
               '<meta name="dtb:totalPageCount" content="0"/>'
               '<meta name="dtb:maxPageNumber" content="0"/></head>'
               '<docTitle><text>%s</text></docTitle><navMap>%s</navMap></ncx>'
               % (uid, html.escape(title), "".join(nav)))

    z.writestr("OEBPS/content.opf",
               '<?xml version="1.0" encoding="utf-8"?>\n'
               '<package xmlns="http://www.idpf.org/2007/opf" version="2.0" unique-identifier="bid">'
               '<metadata xmlns:dc="http://purl.org/dc/elements/1.1/" '
               'xmlns:opf="http://www.idpf.org/2007/opf">'
               '<dc:identifier id="bid">%s</dc:identifier>'
               '<dc:title>%s</dc:title><dc:creator opf:role="aut">%s</dc:creator>'
               '<dc:language>%s</dc:language>%s%s%s</metadata>'
               '<manifest><item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>'
               '<item id="css" href="style.css" media-type="text/css"/>%s</manifest>'
               '<spine toc="ncx">%s</spine></package>'
               % (uid, html.escape(title), html.escape(author), html.escape(lang),
                  "<dc:date>%s</dc:date>" % html.escape(date) if date else "",
                  "<dc:description>%s</dc:description>" % html.escape(blurb) if blurb else "",
                  '<meta name="cover" content="%s"/>' % cover_id if cover_id else "",
                  "".join(manifest), "".join(spine)))
    z.close()
    return title, author, len(chunks), len(images)


if __name__ == "__main__":
    print(convert(sys.argv[1], sys.argv[2]))
