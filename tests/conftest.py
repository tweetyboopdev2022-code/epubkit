"""Shared fixtures: a real EPUB on disk, small enough to build in a test."""
from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

CONTAINER = """<?xml version="1.0"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
  <rootfiles><rootfile full-path="OEBPS/content.opf"
      media-type="application/oebps-package+xml"/></rootfiles>
</container>"""

OPF = """<?xml version="1.0" encoding="utf-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="2.0" unique-identifier="bookid">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/"
            xmlns:opf="http://www.idpf.org/2007/opf">
    <dc:identifier id="bookid">urn:uuid:test-0001</dc:identifier>
    <dc:title>{title}</dc:title>
    <dc:creator opf:role="aut" opf:file-as="Tester, Ada">Ada Tester</dc:creator>
    <dc:language>en</dc:language>
  </metadata>
  <manifest>
    <item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>
    {items}
  </manifest>
  <spine toc="ncx">
    {refs}
  </spine>
  <guide><reference type="cover" title="Cover" href="c1.xhtml"/></guide>
</package>"""

NCX = """<?xml version="1.0" encoding="utf-8"?>
<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1">
  <head><meta name="dtb:uid" content="urn:uuid:MISMATCH"/></head>
  <docTitle><text>{title}</text></docTitle>
  <navMap>{points}</navMap>
</ncx>"""

CHAPTER = """<?xml version="1.0" encoding="utf-8"?>
<!DOCTYPE html PUBLIC "-//W3C//DTD XHTML 1.1//EN"
  "http://www.w3.org/TR/xhtml11/DTD/xhtml11.dtd">
<html xmlns="http://www.w3.org/1999/xhtml">
<head><meta http-equiv="Content-Type" content="text/html; charset=utf-8"/>
<title>{name}</title></head>
<body><h1>{name}</h1><p align="center">{body}</p></body>
</html>"""


def make_epub(path: Path, title: str = "A Test Book", chapters: int = 3,
              words: int = 40) -> Path:
    items, refs, points = [], [], []
    bodies = {}
    for i in range(1, chapters + 1):
        href = "c%d.xhtml" % i
        items.append('<item id="c%d" href="%s" media-type="application/xhtml+xml"/>'
                     % (i, href))
        refs.append('<itemref idref="c%d"/>' % i)
        points.append(
            '<navPoint id="np%d" playOrder="%d"><navLabel><text>Chapter %d'
            '</text></navLabel><content src="%s"/></navPoint>' % (i, i, i, href))
        bodies[href] = CHAPTER.format(
            name="Chapter %d" % i,
            body=" ".join(["word%d" % n for n in range(words)]))

    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as z:
        zi = zipfile.ZipInfo("mimetype")
        zi.compress_type = zipfile.ZIP_STORED
        z.writestr(zi, "application/epub+zip")
        z.writestr("META-INF/container.xml", CONTAINER)
        z.writestr("OEBPS/content.opf",
                   OPF.format(title=title, items="\n    ".join(items),
                              refs="\n    ".join(refs)))
        z.writestr("OEBPS/toc.ncx",
                   NCX.format(title=title, points="".join(points)))
        for href, body in bodies.items():
            z.writestr("OEBPS/" + href, body)
    return path


@pytest.fixture
def epub(tmp_path: Path) -> Path:
    return make_epub(tmp_path / "src" / "A Test Book.epub")


@pytest.fixture
def library(tmp_path: Path) -> Path:
    src = tmp_path / "src"
    make_epub(src / "Book One.epub", "Book One")
    make_epub(src / "Book Two.epub", "Book Two")
    return src


@pytest.fixture(autouse=True)
def no_ambient_config(monkeypatch):
    """A test must never pick up a real epubkit.toml from the machine it runs on."""
    monkeypatch.setattr("epubkit.config.Config.discover", staticmethod(lambda: None))
    for var in list(__import__("os").environ):
        if var.startswith("EPUBKIT_") or var in ("RECAP_ENDPOINT", "RECAP_MODEL"):
            monkeypatch.delenv(var, raising=False)


@pytest.fixture(autouse=True)
def _private_cache(tmp_path_factory, monkeypatch):
    """Recap answers kept for resuming go to a throwaway folder, not ~/.cache."""
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path_factory.mktemp("cache")))
