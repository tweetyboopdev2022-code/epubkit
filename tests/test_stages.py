"""The stage rules that books get ruined by when they are wrong."""
from __future__ import annotations

import logging
import zipfile

import pytest
from conftest import make_epub

from kobokit.config import Config, MetadataConfig
from kobokit.stages import recaps as recaps_mod
from kobokit.stages.base import Context
from kobokit.stages.metadata import _filename_for, _safe, _sort_name
from kobokit.stages.recaps import RecapStage, _cast_rows, _manifest, _parse
from kobokit.stages.repair import RepairStage
from kobokit.stages.validate import ValidateStage
from kobokit.state import BookRecord


def context(cfg, path, work, provider=None):
    return Context(config=cfg, record=BookRecord(source=str(path)), path=path,
                   work=work, log=logging.getLogger("kobokit.test"),
                   provider=provider)


# -- filenames -------------------------------------------------------------

M = MetadataConfig()


def test_series_books_are_named_for_sorting():
    name = _filename_for({"title": "Goblet of Fire", "authors": ["J. K. Rowling"],
                          "series": "Harry Potter", "series_index": 4}, M)
    assert name == "Harry Potter 04 - Goblet of Fire - J. K. Rowling"


def test_a_standalone_has_no_series_prefix():
    assert _filename_for({"title": "Carrie", "authors": ["Stephen King"]}, M) \
        == "Carrie - Stephen King"


def test_a_long_series_name_is_never_abbreviated():
    """The whole point: the full series name and the full title stay."""
    series = "The Extraordinarily Long Chronicles of the Somewhat Weary Traveller"
    title = "In Which Rather a Lot Happens Over the Course of a Single Afternoon"
    name = _filename_for({"title": title, "authors": ["A Writer"],
                          "series": series, "series_index": 3}, M)
    assert series in name and title in name


def test_the_author_gives_way_before_the_title_does():
    title = "T" * 150
    authors = ["An Author With A Very Long Name Indeed"] * 4
    name = _filename_for({"title": title, "authors": authors,
                          "series": "S" * 60, "series_index": 2}, M)
    assert title in name                     # the title survives whole
    assert "S" * 60 in name                  # so does the series
    assert len(name.encode("utf-8")) <= 240  # and it still fits on disk


def test_a_name_that_cannot_fit_is_cut_on_a_word_boundary():
    title = " ".join(["word"] * 200)
    name = _filename_for({"title": title, "authors": ["A"]}, M)
    assert len(name.encode("utf-8")) <= 240
    assert not name.endswith("wor")          # no chopped word


def test_path_separators_cannot_escape_the_folder():
    assert "/" not in _safe("Anna Karenina / War and Peace")
    assert _safe("Vol 1: The Beginning") == "Vol 1 - The Beginning"


def test_sort_name_leaves_an_existing_sort_form_alone():
    assert _sort_name("Douglas Adams") == "Adams, Douglas"
    assert _sort_name("Adams, Douglas") == "Adams, Douglas"
    assert _sort_name("Homer") == "Homer"


# -- recaps ----------------------------------------------------------------

def test_manifest_parsing_does_not_depend_on_attribute_order():
    """href-before-id is just as valid, and dropping those items silently
    drops chapters out of the recap set."""
    opf = ('<manifest>'
           '<item id="a" href="a.xhtml" media-type="application/xhtml+xml"/>'
           '<item href="b.xhtml" id="b" media-type="application/xhtml+xml"/>'
           '<item media-type="application/xhtml+xml" href="c.xhtml" id="c"/>'
           '</manifest>')
    assert _manifest(opf) == {"a": "a.xhtml", "b": "b.xhtml", "c": "c.xhtml"}


def test_a_recap_is_shown_a_chapter_behind_the_reader(tmp_path, monkeypatch):
    """The rule that stops a recap spoiling the chapter being read."""
    book = make_epub(tmp_path / "b.epub", chapters=4, words=900)
    cfg = Config.load(None, {"source": tmp_path, "output": tmp_path / "o",
                             "recaps.min_chars": 100, "recaps.lag_chapters": 1})

    class FakeProvider:
        def complete(self, prompt, system=None, **kw):
            import re as _re
            which = _re.search(r"Chapter (\d+)", prompt).group(1)
            return type("R", (), {"text": "SUMMARY: things happened in %s.\n"
                                          "CHARACTERS:\nAda - a tester" % which})()

    ctx = context(cfg, book, tmp_path / "work", FakeProvider())
    (tmp_path / "work").mkdir()
    assert RecapStage().run(ctx).ok

    with zipfile.ZipFile(book) as z:
        rows = [r.split("\t") for r in
                z.read("OEBPS/recaps.tsv").decode().splitlines()]
    # chapter 1's recap is filed against spine position 1, not 0: a reader
    # sitting in chapter 1 must not be handed chapter 1's ending
    by_index = {int(i): s for i, _h, s in rows}
    assert "in 1" in by_index[1]
    assert 0 not in by_index


def test_recaps_go_in_the_manifest_but_never_the_spine(tmp_path):
    book = make_epub(tmp_path / "b.epub", chapters=2, words=800)
    recaps_mod._embed(book, [(1, "c1.xhtml", "a summary")], [(1, "Ada", "a tester")])
    with zipfile.ZipFile(book) as z:
        opf = z.read("OEBPS/content.opf").decode()
        assert "OEBPS/recaps.tsv" in z.namelist()
    assert 'href="recaps.tsv"' in opf
    assert "recaps-tsv" not in opf.split("<spine")[1]


def test_a_character_is_introduced_at_first_appearance():
    results = {
        0: ("c1", "s", "Ada - a tester"),
        2: ("c3", "s", "Ada - now revealed to be the villain\nBob - a friend"),
    }
    rows = _cast_rows(results)
    assert (0, "Ada", "a tester") in rows          # the first description wins
    assert (2, "Bob", "a friend") in rows
    assert not any("villain" in r[2] for r in rows)


def test_a_reply_without_a_summary_is_discarded():
    assert _parse("I'm afraid I can't help with that.") == ("", "")


def test_recaps_are_skipped_without_a_provider(tmp_path):
    cfg = Config.load(None, {"source": tmp_path, "output": tmp_path / "o"})
    ctx = context(cfg, tmp_path / "b.epub", tmp_path)
    assert RecapStage().applies(ctx) is False


# -- repair and validate ---------------------------------------------------

def test_repair_promotes_the_package_to_epub3(tmp_path, epub):
    cfg = Config.load(None, {"source": epub.parent, "output": tmp_path / "o"})
    work = tmp_path / "work"
    work.mkdir()
    ctx = context(cfg, epub, work)
    assert RepairStage().run(ctx).ok
    with zipfile.ZipFile(epub) as z:
        opf = z.read("OEBPS/content.opf").decode()
    assert 'version="3.0"' in opf
    assert "dcterms:modified" in opf


def test_repair_keeps_every_document(tmp_path, epub):
    before = {n for n in zipfile.ZipFile(epub).namelist()
              if n.endswith(".xhtml")}
    cfg = Config.load(None, {"source": epub.parent, "output": tmp_path / "o"})
    work = tmp_path / "work"
    work.mkdir()
    RepairStage().run(context(cfg, epub, work))
    after = {n for n in zipfile.ZipFile(epub).namelist() if n.endswith(".xhtml")}
    assert before <= after


def test_validate_reports_rather_than_failing_when_epubcheck_is_absent(
        tmp_path, epub, monkeypatch):
    from kobokit.tools import epubcheck_util as ec
    monkeypatch.setattr(ec, "available", lambda: False)
    cfg = Config.load(None, {"source": epub.parent, "output": tmp_path / "o"})
    ctx = context(cfg, epub, tmp_path)
    result = ValidateStage().run(ctx)
    assert result.ok and result.message == "not checked"
    assert any("unavailable" in n for n in ctx.record.notes)


def test_a_book_with_errors_is_not_written_by_default(tmp_path, epub, monkeypatch):
    from kobokit.tools import epubcheck_util as ec
    monkeypatch.setattr(ec, "available", lambda: True)
    monkeypatch.setattr(ec, "run", lambda *a, **k: [
        {"severity": "ERROR", "code": "RSC-005", "message": "bad", "file": "x"}])
    cfg = Config.load(None, {"source": epub.parent, "output": tmp_path / "o"})
    assert ValidateStage().run(context(cfg, epub, tmp_path)).ok is False


def test_accept_improvement_ships_a_book_that_got_better(tmp_path, epub, monkeypatch):
    from kobokit.tools import epubcheck_util as ec
    monkeypatch.setattr(ec, "available", lambda: True)
    monkeypatch.setattr(ec, "run", lambda *a, **k: [
        {"severity": "ERROR", "code": "RSC-005", "message": "bad", "file": "x"}])
    cfg = Config.load(None, {"source": epub.parent, "output": tmp_path / "o",
                             "validate.accept_improvement": True})
    ctx = context(cfg, epub, tmp_path)
    ctx.record.errors_before = 40
    assert ValidateStage().run(ctx).ok is True


# -- metadata, end to end --------------------------------------------------

def test_the_book_own_metadata_is_actually_read(tmp_path, monkeypatch):
    """The bundled tools take a folder, not a file. Handing them a file made
    every book come out as "unresolved: title, authors" while the run still
    reported success."""
    from kobokit.stages.metadata import MetadataStage
    book = make_epub(tmp_path / "src" / "download-xyz.epub", "The Real Title")
    work = tmp_path / "work"
    work.mkdir()
    import shutil
    staged = work / book.name
    shutil.copy2(book, staged)

    cfg = Config.load(None, {"source": book.parent, "output": tmp_path / "o",
                             "metadata.use_ai_fallback": False})
    ctx = context(cfg, staged, work)
    monkeypatch.setattr("kobokit.stages.metadata.MetadataStage._catalogue",
                        lambda self, *a: {})          # no network in a test
    result = MetadataStage().run(ctx)

    assert ctx.record.title == "The Real Title"
    assert ctx.record.authors == "Ada Tester"
    assert result.path.name == "The Real Title - Ada Tester.epub"
    assert not any("unresolved" in n for n in ctx.record.notes)


# -- ingest and convert ----------------------------------------------------

def test_an_unrecognised_file_is_named_not_silently_dropped(tmp_path):
    from kobokit.stages.ingest import IngestStage
    odd = tmp_path / "something.cbz"
    odd.write_bytes(b"x")
    cfg = Config.load(None, {"source": tmp_path, "output": tmp_path / "o"})
    result = IngestStage().run(context(cfg, odd, tmp_path))
    assert result.ok is False and result.stop is True
    assert ".cbz" in result.message


def test_a_kepub_is_recognised_as_a_kepub(tmp_path):
    from kobokit.stages.ingest import classify
    plain = make_epub(tmp_path / "plain.epub")
    assert classify(plain) == "epub"

    kepub = tmp_path / "wrapped.epub"
    import shutil
    shutil.copy2(plain, kepub)
    with zipfile.ZipFile(kepub, "a") as z:
        z.writestr("OEBPS/k.xhtml",
                   '<html><body><span class="koboSpan" id="kobo.1.1">x</span>'
                   "</body></html>")
    assert classify(kepub) == "kepub"


pymupdf = pytest.importorskip("pymupdf", reason="the pdf extra is not installed")


def test_a_text_pdf_is_reflowed_and_keeps_its_words(tmp_path):
    from kobokit.stages.convert import ConvertStage
    para = ("This is ordinary prose that runs on for long enough that the "
            "page is routed as a novel rather than as a scan. ") * 12
    doc = pymupdf.open()
    for i in range(4):
        page = doc.new_page()
        page.insert_textbox(pymupdf.Rect(60, 60, 540, 760),
                            "Chapter %d\n\n%s" % (i + 1, para), fontsize=11)
    doc.set_metadata({"title": "A Paper Novel", "author": "Ada Tester"})
    src = tmp_path / "scan.pdf"
    doc.save(str(src))

    cfg = Config.load(None, {"source": tmp_path, "output": tmp_path / "o"})
    work = tmp_path / "work"
    work.mkdir()
    ctx = context(cfg, src, work)
    ctx.meta["format_in"] = "pdf"
    result = ConvertStage().run(ctx)

    assert result.ok, result.message
    assert result.path.suffix == ".epub"
    assert ctx.meta["fixed_layout"] is False
    with zipfile.ZipFile(result.path) as z:
        import html as _html
        import re as _re
        text = " ".join(
            _html.unescape(_re.sub(r"<[^>]+>", " ", z.read(n).decode("utf-8", "ignore")))
            for n in z.namelist() if n.endswith((".xhtml", ".html")))
    assert len(text.split()) > 800          # the prose actually came across


def test_json_is_taken_from_the_model_s_final_answer():
    """A reasoning model leaves braces either side of the real answer; taking
    everything between the first and the last one parses to nothing."""
    from kobokit.stages.metadata import _json_from
    reply = ('Let me think. The shape is {"title": "", "authors": [""]} as an '
             'example.\n\nHere is the answer:\n'
             '{"title": "Carrie", "authors": ["Stephen King"], '
             '"series": null, "series_index": null}')
    got = _json_from(reply)
    assert got["title"] == "Carrie"
    assert got["authors"] == ["Stephen King"]


def test_json_with_braces_inside_a_string_is_still_read():
    from kobokit.stages.metadata import _json_from
    assert _json_from('{"title": "A Study in {Scarlet}", "authors": []}'
                      )["title"] == "A Study in {Scarlet}"


# -- two-page spreads ------------------------------------------------------

def test_a_wide_pdf_is_split_into_single_pages(tmp_path):
    """A landscape page in a picture book is a spread. Left whole, both pages
    end up at half size on a six-inch screen."""
    from kobokit.stages.convert import ConvertStage
    doc = pymupdf.open()
    for i in range(4):
        page = doc.new_page(width=1400, height=800)       # 1.75:1
        page.draw_rect(pymupdf.Rect(0, 0, 700, 800), color=None,
                       fill=(0.92, 0.88, 0.78))
        page.draw_rect(pymupdf.Rect(700, 0, 1400, 800), color=None,
                       fill=(0.78, 0.86, 0.92))
        page.insert_text((80, 120), "page %d" % (i * 2 + 1), fontsize=40)
        page.insert_text((780, 120), "page %d" % (i * 2 + 2), fontsize=40)
    doc.set_metadata({"title": "Wide Book", "author": "Ada Tester"})
    src = tmp_path / "wide.pdf"
    doc.save(str(src))

    cfg = Config.load(None, {"source": tmp_path, "output": tmp_path / "o"})
    work = tmp_path / "work"
    work.mkdir()
    ctx = context(cfg, src, work)
    ctx.meta["format_in"] = "pdf"
    result = ConvertStage().run(ctx)

    assert result.ok, result.message
    assert ctx.meta["fixed_layout"] is True
    assert any("splitting" in n for n in ctx.record.notes)
    with zipfile.ZipFile(result.path) as z:
        opf = z.read([n for n in z.namelist() if n.endswith(".opf")][0]).decode()
    import re as _re
    assert len(_re.findall(r"<itemref", opf)) == 8        # 4 spreads -> 8 pages


def test_split_spreads_never_overrides_a_no(tmp_path):
    from kobokit.stages.convert import ConvertStage
    doc = pymupdf.open()
    for _ in range(2):
        doc.new_page(width=1400, height=800)
    src = tmp_path / "wide.pdf"
    doc.save(str(src))

    cfg = Config.load(None, {"source": tmp_path, "output": tmp_path / "o",
                             "convert.split_spreads": "never"})
    work = tmp_path / "work"
    work.mkdir()
    ctx = context(cfg, src, work)
    ctx.meta["format_in"] = "pdf"
    ConvertStage().run(ctx)
    assert not any("splitting" in n for n in ctx.record.notes)
    assert any("split_spreads" in n for n in ctx.record.notes)


def test_a_portrait_pdf_is_not_split(tmp_path):
    from kobokit.tools import pdf_route
    doc = pymupdf.open()
    for _ in range(3):
        doc.new_page(width=800, height=1100)              # 0.73:1
    src = tmp_path / "tall.pdf"
    doc.save(str(src))
    assert pdf_route.inspect(str(src))["spread"] is False


def test_force_splits_a_page_whose_art_crosses_the_gutter(tmp_path):
    """The reported bug: the safety check keeps a spread whole when artwork
    runs across the middle, so a book where every illustration does that gets
    no pages split at all."""
    from kobokit.stages.convert import ConvertStage
    doc = pymupdf.open()
    for i in range(4):
        page = doc.new_page(width=1400, height=800)
        page.draw_rect(pymupdf.Rect(0, 0, 1400, 800), color=None,
                       fill=(0.95, 0.93, 0.85))
        page.draw_rect(pymupdf.Rect(300, 300, 1100, 520), color=None,
                       fill=(0.2, 0.35, 0.7))       # crosses the gutter
        page.insert_text((80, 120), "p%d" % (i * 2 + 1), fontsize=36)
    src = tmp_path / "gutter.pdf"
    doc.save(str(src))

    def spine_of(setting):
        cfg = Config.load(None, {"source": tmp_path,
                                 "output": tmp_path / ("o-" + setting),
                                 "convert.split_spreads": setting})
        work = tmp_path / ("w-" + setting)
        work.mkdir()
        ctx = context(cfg, src, work)
        ctx.meta["format_in"] = "pdf"
        res = ConvertStage().run(ctx)
        assert res.ok, res.message
        with zipfile.ZipFile(res.path) as z:
            opf = z.read([n for n in z.namelist()
                          if n.endswith(".opf")][0]).decode()
        import re as _re
        return len(_re.findall(r"<itemref", opf)), ctx.record.notes

    auto_pages, auto_notes = spine_of("auto")
    assert auto_pages == 4                     # every page kept whole
    assert any("left whole" in n for n in auto_notes)   # and it says so

    forced_pages, _ = spine_of("force")
    assert forced_pages == 8                   # every spread cut in two


def test_the_split_outcome_is_always_reported(tmp_path):
    """Silence after 'splitting them' is how half a book stays in spreads."""
    from kobokit.stages.convert import ConvertStage
    doc = pymupdf.open()
    for _ in range(3):
        page = doc.new_page(width=1400, height=800)
        page.draw_rect(pymupdf.Rect(0, 0, 700, 800), color=None,
                       fill=(0.9, 0.87, 0.8))
        page.draw_rect(pymupdf.Rect(700, 0, 1400, 800), color=None,
                       fill=(0.8, 0.86, 0.9))
    src = tmp_path / "w.pdf"
    doc.save(str(src))
    cfg = Config.load(None, {"source": tmp_path, "output": tmp_path / "o"})
    work = tmp_path / "work"
    work.mkdir()
    ctx = context(cfg, src, work)
    ctx.meta["format_in"] = "pdf"
    ConvertStage().run(ctx)
    assert any(n.startswith("spreads:") for n in ctx.record.notes)


def test_a_pdf_is_not_reported_as_a_clean_epub(tmp_path):
    """EPUBCheck answers 'clean' for a file it does not recognise as a book,
    so a PDF passed validation without a word. A pass that means 'I found
    nothing wrong with a file I never read' is worse than no check."""
    from kobokit.stages.validate import ValidateStage, is_epub
    pdf = tmp_path / "book.pdf"
    pdf.write_bytes(b"%PDF-1.4\n%%EOF\n")
    assert is_epub(pdf) is False

    cfg = Config.load(None, {"source": tmp_path, "output": tmp_path / "o"})
    ctx = context(cfg, pdf, tmp_path)
    result = ValidateStage().run(ctx)
    assert result.ok is False
    assert "still a pdf" in result.message
    assert ctx.record.tools["epubcheck"].startswith("n/a")


def test_a_file_merely_named_epub_is_not_an_epub(tmp_path):
    from kobokit.stages.validate import is_epub
    fake = tmp_path / "not-really.epub"
    fake.write_bytes(b"this is not a zip")
    assert is_epub(fake) is False

    import zipfile as _z
    wrong = tmp_path / "wrong-mimetype.epub"
    with _z.ZipFile(wrong, "w") as z:
        z.writestr("mimetype", "application/zip")
    assert is_epub(wrong) is False


def test_a_real_epub_still_validates(epub):
    from kobokit.stages.validate import is_epub
    assert is_epub(epub) is True


# -- block elements inside inline ones --------------------------------------

def test_a_div_inside_a_span_is_unwrapped():
    """Calibre wraps a page in <span class="underline"> and puts the page-break
    <div> inside it. No EPUB version accepts that, so promoting the package
    cannot fix it - the markup has to be repaired."""
    from kobokit.tools.fix_epub3 import unwrap_inline_around_block
    src = (b'<body><span class="underline"><span><div class="c5">'
           b'<div class="pb" id="p0"></div><br/></div></span></span>'
           b'<p>Real text</p></body>')
    out, n = unwrap_inline_around_block(src)
    assert n == 2                                  # both spans went
    assert b"<span" not in out
    assert b'<div class="c5">' in out              # the divs stayed
    assert b"Real text" in out                     # and so did the text


def test_a_div_inside_a_p_becomes_a_div_not_a_deletion():
    """A <p> carries the paragraph's styling, so it is retagged rather than
    unwrapped - <div> holds the same content legally."""
    from kobokit.tools.fix_epub3 import unwrap_inline_around_block
    src = b'<p class="calibre7"><div class="c3"><img src="x.jpg"/></div></p>'
    out, n = unwrap_inline_around_block(src)
    assert n == 1
    assert b'<div class="calibre7">' in out        # the class survives
    assert out.count(b"</div>") == 2
    assert b"<p" not in out
    assert b'<img src="x.jpg"/>' in out


def test_healthy_markup_is_left_exactly_alone():
    from kobokit.tools.fix_epub3 import unwrap_inline_around_block
    for good in (
        b'<p>Plain <span class="bold">emphasis</span> and more.</p>',
        b'<div><p>A paragraph</p><p>And another</p></div>',
        b'<p><a href="x.html"><img src="i.jpg" alt=""/></a></p>',
        b'<body><span>just inline</span></body>',
    ):
        out, n = unwrap_inline_around_block(good)
        assert n == 0 and out == good


def test_kobospans_are_never_unwrapped():
    """A koboSpan is where the device's saved reading position points."""
    from kobokit.tools.fix_epub3 import unwrap_inline_around_block
    src = (b'<p><span class="koboSpan" id="kobo.1.1">Some words</span></p>')
    out, n = unwrap_inline_around_block(src)
    assert n == 0
    assert b'id="kobo.1.1"' in out


def test_nested_wrappers_all_come_off():
    from kobokit.tools.fix_epub3 import unwrap_inline_around_block
    src = b'<b><i><span><div>block</div></span></i></b>'
    out, _ = unwrap_inline_around_block(src)
    assert out == b'<div>block</div>'


def test_unclosed_tags_do_not_derail_it():
    from kobokit.tools.fix_epub3 import unwrap_inline_around_block
    src = b'<p><span><div>x</div><p>y</p>'
    out, _ = unwrap_inline_around_block(src)
    assert b"x" in out and b"y" in out
