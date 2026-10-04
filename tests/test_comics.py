"""Comics whose pages show blank: found, rebuilt, and kept out of recaps."""
from __future__ import annotations

import io
import zipfile

from PIL import Image

from epubkit.tools import kindle_comic as K

CONTAINER = ('<?xml version="1.0"?><container version="1.0" '
             'xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles>'
             '<rootfile full-path="OEBPS/content.opf" '
             'media-type="application/oebps-package+xml"/></rootfiles></container>')

OPF = """<?xml version="1.0" encoding="utf-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="2.0" unique-identifier="uid">
<metadata xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:opf="http://www.idpf.org/2007/opf">
<dc:identifier id="uid">urn:uuid:1234-abcd</dc:identifier>
<dc:title>Pup Cop</dc:title>
<dc:creator opf:file-as="Penn, Ada" opf:role="aut">Ada Penn</dc:creator>
<dc:publisher>Test House</dc:publisher><dc:date>2016-08-30</dc:date>
<meta name="calibre:series" content="Pup Cop"/><meta name="calibre:series_index" content="1"/>
</metadata><manifest>{items}</manifest><spine>{refs}</spine></package>"""

PINNED_CSS = (".fs {display:block; position:relative; width:880px; height:1280px}\n"
              ".calibre2 {display:block; position:absolute; margin:0}\n"
              ".singlepage {position:absolute; width:880px; height:1280px}\n")
PINNED_PAGE = ('<html xmlns="http://www.w3.org/1999/xhtml"><head>'
               '<link rel="stylesheet" href="s.css"/></head><body><div class="fs">'
               '<div class="calibre2"><img src="i{n}.jpg" class="singlepage"/></div>'
               '<div class="x"><a class="app-amzn-magnify" data-AmznRemoved="mobi7"/></div>'
               '</div></body></html>')
FLOW_CSS = ".pic {width:800px; height:1010px}\n"
FLOW_PAGE = ('<html xmlns="http://www.w3.org/1999/xhtml"><head>'
             '<link rel="stylesheet" href="s.css"/></head><body><div>'
             '<img src="i{n}.jpg" class="pic"/></div></body></html>')


def jpeg(shade):
    b = io.BytesIO()
    Image.new("RGB", (60, 90), (shade, 255 - shade, 120)).save(b, "JPEG")
    return b.getvalue()


def book(path, page, css, n=10, repeat_cover=True):
    items, refs = [], []
    with zipfile.ZipFile(path, "w") as z:
        zi = zipfile.ZipInfo("mimetype")
        z.writestr(zi, "application/epub+zip")
        z.writestr("META-INF/container.xml", CONTAINER)
        z.writestr("OEBPS/s.css", css)
        for i in range(n):
            z.writestr("OEBPS/p%d.xhtml" % i, page.format(n=i))
            shade = 20 if (repeat_cover and i == 1) else 20 + i * 20
            z.writestr("OEBPS/i%d.jpg" % i, jpeg(shade))
            items.append('<item id="p%d" href="p%d.xhtml" media-type="application/xhtml+xml"/>'
                         '<item id="i%d" href="i%d.jpg" media-type="image/jpeg"/>' % (i, i, i, i))
            refs.append('<itemref idref="p%d"/>' % i)
        z.writestr("OEBPS/content.opf", OPF.format(items="".join(items), refs="".join(refs)))
    return path


def test_pinned_pictures_are_blank_pages(tmp_path):
    r = K.check(book(tmp_path / "a.epub", PINNED_PAGE, PINNED_CSS))
    assert r["kind"] == "blank-pages" and r["pinned_pages"] == 10


def test_amazon_markers_alone_do_not_mean_blank(tmp_path):
    """Bad Guys 4 carries Amazon's markers and reads fine."""
    page = FLOW_PAGE.replace("</div></body>", '<a class="app-amzn-magnify"/></div></body>')
    r = K.check(book(tmp_path / "b.epub", page, FLOW_CSS))
    assert r["kind"] == "picture-pages"


def test_a_novel_is_prose(tmp_path):
    page = '<html><body><p>%s</p></body></html>' % ("Words and more words. " * 40)
    assert K.check(book(tmp_path / "c.epub", page, "")) ["kind"] == "prose"


def test_rebuild_makes_a_fixed_layout_book_that_is_the_same_book(tmp_path):
    src = book(tmp_path / "a.epub", PINNED_PAGE, PINNED_CSS)
    out = tmp_path / "a.kepub.epub"
    r = K.rebuild(str(src), str(out))
    assert r["pages"] == 9 and r["dropped_repeats"] == 1     # the repeated cover
    assert K.check(out)["kind"] == "fixed-layout"
    with zipfile.ZipFile(out) as z:
        assert z.testzip() is None
        assert z.namelist()[0] == "mimetype"
        opf = z.read("OEBPS/content.opf").decode()
    assert "urn:uuid:1234-abcd" in opf                     # same identity
    assert "Pup Cop" in opf and "Penn, Ada" in opf and 'series_index" content="1"' in opf
    assert not (tmp_path / "a.kepub.epub.parts").exists()


def test_a_lost_fixed_layout_flag_is_found_and_put_back(tmp_path):
    """Grimmory's metadata write-back dropped rendition:layout from books
    fxl.py built; every page still declares its size."""
    src = book(tmp_path / "a.epub", PINNED_PAGE, PINNED_CSS)
    out = tmp_path / "fx.kepub.epub"
    K.rebuild(str(src), str(out))
    stripped = tmp_path / "stripped.epub"
    with zipfile.ZipFile(out) as zin, zipfile.ZipFile(stripped, "w") as zout:
        for info in zin.infolist():
            data = zin.read(info.filename)
            if info.filename.endswith(".opf"):
                data = data.replace(b'<meta property="rendition:layout">pre-paginated</meta>', b"")
            zout.writestr(info, data)
    assert K.check(stripped)["kind"] == "lost-fixed-layout"
    with zipfile.ZipFile(stripped) as z:
        before = {n: z.read(n) for n in z.namelist()}
    assert K.repair_flag(str(stripped)) is True
    assert K.check(stripped)["kind"] == "fixed-layout"
    with zipfile.ZipFile(stripped) as z:
        for n, data in before.items():
            if not n.endswith(".opf"):
                assert z.read(n) == data
    assert K.repair_flag(str(stripped)) is False        # nothing more to do
