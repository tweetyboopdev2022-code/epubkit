"""The fixed-layout builder writes EPUB 3 metadata, not EPUB 2 leftovers."""
from __future__ import annotations

import re
import zipfile
from xml.dom import minidom

import pytest

pytest.importorskip("PIL")
from PIL import Image  # noqa: E402

from epubkit.tools import fxl  # noqa: E402

MD = dict(title="Creepshow", authors=[("Stephen King", "King, Stephen"),
                                      ("Bernie Wrightson", "Wrightson, Bernie")],
          publisher="Gallery 13", date="2017-05-09", subjects=["Graphic novels"])


def build(tmp_path, **extra):
    imgs = [Image.new("RGB", (200, 260), c) for c in ("white", "red")]
    out = tmp_path / "book.kepub.epub"
    fxl.build(imgs, str(out), dict(MD, **extra))
    return zipfile.ZipFile(out)


def test_no_epub2_attributes_in_the_package(tmp_path):
    opf = build(tmp_path).read("OEBPS/content.opf").decode()
    assert "opf:" not in opf and "xmlns:opf" not in opf
    # the EPUB 3 way of saying the same things is still there
    assert re.search(r'refines="#cr1" property="role" scheme="marc:relators">aut<', opf)
    assert re.search(r'refines="#cr2" property="file-as">Wrightson, Bernie<', opf)
    assert '<dc:creator id="cr2">Bernie Wrightson</dc:creator>' in opf


def test_identifier_defaults_to_a_stable_uuid(tmp_path):
    a = build(tmp_path).read("OEBPS/content.opf").decode()
    b = build(tmp_path).read("OEBPS/content.opf").decode()
    ident = re.search(r'<dc:identifier id="bookid">([^<]+)<', a).group(1)
    assert ident.startswith("urn:uuid:")
    assert ident in b


def test_a_supplied_identifier_is_kept_in_the_opf_and_the_ncx(tmp_path):
    z = build(tmp_path, identifier="urn:isbn:9781501163227")
    opf, ncx = z.read("OEBPS/content.opf").decode(), z.read("OEBPS/toc.ncx").decode()
    assert '<dc:identifier id="bookid">urn:isbn:9781501163227<' in opf
    assert 'name="dtb:uid" content="urn:isbn:9781501163227"' in ncx      # the two must agree


def test_the_package_is_well_formed_fixed_layout(tmp_path):
    z = build(tmp_path)
    assert z.namelist()[0] == "mimetype" and z.getinfo("mimetype").compress_type == 0
    for n in z.namelist():
        if n.endswith((".xhtml", ".opf", ".ncx", ".xml")):
            minidom.parseString(z.read(n))
    opf = z.read("OEBPS/content.opf").decode()
    assert 'rendition:layout">pre-paginated<' in opf
    assert 'name="viewport" content="width=%d, height=%d"' % (fxl.VW, fxl.VH) in \
        z.read("OEBPS/p0001.xhtml").decode()
