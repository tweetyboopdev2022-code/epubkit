"""A split part carries its own pages and their images, never the other books'."""
import sys
import zipfile
from pathlib import Path

import epubkit.tools

# the bundled tools import each other by bare name
sys.path.insert(0, str(Path(epubkit.tools.__file__).parent))
from epubsplit import Omnibus  # noqa: E402

OPF = """<?xml version="1.0"?>
<package xmlns="http://www.idpf.org/2007/opf" version="2.0" unique-identifier="id">
<metadata xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:title>Set</dc:title>
<dc:identifier id="id">x</dc:identifier></metadata>
<manifest>
<item id="a" href="a.xhtml" media-type="application/xhtml+xml"/>
<item id="b" href="b.xhtml" media-type="application/xhtml+xml"/>
<item id="c" href="c.xhtml" media-type="application/xhtml+xml"/>
<item id="n" href="notes.xhtml" media-type="application/xhtml+xml"/>
<item id="i" href="pic.jpg" media-type="image/jpeg"/>
</manifest><spine><itemref idref="a"/><itemref idref="b"/><itemref idref="c"/></spine></package>"""
CONTAINER = ('<?xml version="1.0"?><container xmlns="urn:oasis:names:tc:opendocument:xmlns:container" '
             'version="1.0"><rootfiles><rootfile full-path="content.opf" '
             'media-type="application/oebps-package+xml"/></rootfiles></container>')


def test_the_closure_stops_at_other_books_but_keeps_notes_and_pictures(tmp_path):
    p = tmp_path / "set.epub"
    with zipfile.ZipFile(p, "w") as z:
        z.writestr("mimetype", "application/epub+zip")
        z.writestr("META-INF/container.xml", CONTAINER)
        z.writestr("content.opf", OPF)
        z.writestr("a.xhtml", '<html><body><img src="pic.jpg"/><a href="b.xhtml">next</a>'
                              '<a href="notes.xhtml">notes</a></body></html>')
        z.writestr("b.xhtml", '<html><body><a href="c.xhtml">next</a></body></html>')
        z.writestr("c.xhtml", "<html><body>third</body></html>")
        z.writestr("notes.xhtml", "<html><body>notes</body></html>")
        z.writestr("pic.jpg", b"jpg")
    kept = Omnibus(str(p)).closure(["a.xhtml"])
    assert kept == {"a.xhtml", "pic.jpg", "notes.xhtml"}
