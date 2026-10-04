import io
import zipfile

from epubkit.stages.ingest import classify
from epubkit.tools import fix_epub3, series_hint as S


def test_series_and_blurb_come_out_of_the_title():
    m, _ = S.tidy({"title": "Her Dark Heart: A totally gripping crime thriller "
                            "(Detective Gina Harte Book 5)", "authors": ["Carla Kovach"]})
    assert (m["title"], m["series"], m["series_index"]) == ("Her Dark Heart", "Detective Gina Harte", 5)
    m, _ = S.tidy({"title": "Her Last Mistake - Detective Gina Harte Series 06 (2020)",
                   "authors": ["Kovach, Carla"]})
    assert (m["title"], m["series_index"], m["authors"]) == ("Her Last Mistake", 6, ["Carla Kovach"])
    m, _ = S.tidy({"title": "Detective Gina Harte 11: One Girl Missing",
                   "series": "Detective Gina Harte", "series_index": 11, "authors": []})
    assert m["title"] == "One Girl Missing"


def test_real_titles_are_left_alone():
    for t in ("Fahrenheit 451", "Apollo 13: The Inside Story",
              "Harry Potter and the Half-Blood Prince", "Catch-22"):
        assert S.tidy({"title": t, "authors": []})[0]["title"] == t
    assert S.tidy({"title": "Catch-22: A Novel", "authors": []})[0]["title"] == "Catch-22"


def _book(tmp_path, name, pages, lead_times=40):
    p = tmp_path / name
    with zipfile.ZipFile(p, "w") as z:
        z.writestr("mimetype", "application/epub+zip")
        for i, body in enumerate(pages):
            z.writestr("OEBPS/p%d.xhtml" % i, "<html><body>%s</body></html>" % body)
        z.writestr("OEBPS/story.xhtml", "<html><body><p>%s</p></body></html>"
                   % ("Gina walked. " * lead_times))
    return p


LIST = ("<h1>Books by Carla Kovach</h1><p><span>D</span>ETECTIVE <span>G</span>INA "
        "<span>H</span>ARTE SERIES</p><p><i>The Next Girl</i></p><p><i>Her Final Hour</i></p>"
        "<p><i>Her Pretty Bones</i></p><hr/><p><i>Find Me</i></p>")


def test_place_from_the_books_own_list(tmp_path):
    b = _book(tmp_path, "b.epub", [LIST])
    assert S.from_book(b, {"title": "Her Final Hour", "authors": ["Carla Kovach"]})[:2] == \
        ("Detective Gina Harte", 2)
    # after the break is not the series
    assert S.from_book(b, {"title": "Find Me", "authors": ["Carla Kovach"]})[0] is None


def test_a_book_without_its_lead_is_not_placed(tmp_path):
    b = _book(tmp_path, "b.epub", [LIST], lead_times=0)
    assert S.from_book(b, {"title": "Her Final Hour", "authors": ["C K"]})[0] is None


def test_lists_are_remembered_for_other_books(tmp_path):
    cache = tmp_path / "lists.json"
    S.from_book(_book(tmp_path, "a.epub", [LIST]), {"title": "x", "authors": ["Carla Kovach"]}, cache=cache)
    b = _book(tmp_path, "b.epub", [])
    assert S.from_book(b, {"title": "The Next Girl", "authors": ["Carla Kovach"]}, cache=cache)[:2] == \
        ("Detective Gina Harte", 1)


def test_li_value_only_inside_ol():
    out, n = fix_epub3.drop_li_value_outside_ol(
        b'<ul><li value="1">a</li></ul><ol><li value="3">b</li></ol>')
    assert n == 1 and out == b'<ul><li>a</li></ul><ol><li value="3">b</li></ol>'


def test_dead_script_dropped():
    out, n = fix_epub3.drop_dead_scripts(
        b'<script type="text/javascript" src="../js/kobo.js"></script><script src="k.js"/>',
        lambda t: t == "k.js")
    assert n == 1 and out == b'<script src="k.js"/>'


def test_misnamed_kindle_file_is_a_mobi(tmp_path):
    p = tmp_path / "Dog_man.pdf"
    p.write_bytes(b"Dog_Man".ljust(60, b"\0") + b"BOOKMOBI" + b"\0" * 200)
    assert classify(p) == "mobi"
    q = tmp_path / "real.pdf"
    q.write_bytes(b"%PDF-1.7\n")
    assert classify(q) == "pdf"


def test_metadata_rewrite_keeps_fixed_layout(tmp_path):
    from epubkit.tools import epubmeta
    src, dst = tmp_path / "a.epub", tmp_path / "b.epub"
    opf = ('<?xml version="1.0"?><package xmlns="http://www.idpf.org/2007/opf" version="3.0" '
           'unique-identifier="id" prefix="rendition: http://www.idpf.org/vocab/rendition/#">'
           '<metadata xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:identifier id="id">x</dc:identifier>'
           '<dc:title>Old</dc:title><meta property="rendition:layout">pre-paginated</meta>'
           '<meta name="original-resolution" content="1072x1448"/></metadata>'
           '<manifest><item id="p" href="p.xhtml" media-type="application/xhtml+xml"/></manifest>'
           '<spine><itemref idref="p"/></spine></package>')
    with zipfile.ZipFile(src, "w") as z:
        z.writestr("mimetype", "application/epub+zip")
        z.writestr("META-INF/container.xml", '<container><rootfiles><rootfile full-path="o.opf"/>'
                   '</rootfiles></container>')
        z.writestr("o.opf", opf)
        z.writestr("p.xhtml", "<html/>")
    epubmeta.rewrite(str(src), str(dst), {"title": "New", "authors": [("A B", "B, A")]})
    out = zipfile.ZipFile(dst).read("o.opf").decode()
    assert "New" in out and "pre-paginated" in out and "original-resolution" in out


def test_series_from_the_download_name():
    n = ("Dog Man_ From the Creator of Captain Underpants (Dog Man #1) -- Dav Pilkey -- "
         "Dog Man 1, 2016 -- Scholastic Inc_ -- abc -- x.azw3")
    assert S.from_filename(n, "Dog Man") == ("Dog Man", 1)
    assert S.from_filename(n, "Cat Kid") == (None, None)
    assert S.from_filename("_OceanofPDF.com_Her_Dark_Heart_-_Carla_Kovach.epub", "Her Dark Heart") \
        == (None, None)


def _recapped(tmp_path, hero, series="Detective Gina Harte"):
    p = tmp_path / ("%s.epub" % hero.replace(" ", ""))
    with zipfile.ZipFile(p, "w") as z:
        z.writestr("mimetype", "application/epub+zip")
        z.writestr("META-INF/container.xml", '<rootfile full-path="OEBPS/c.opf"/>')
        z.writestr("OEBPS/c.opf", '<package><metadata><meta name="calibre:series" '
                   'content="%s"/></metadata></package>' % series)
        z.writestr("OEBPS/recaps/parts.tsv", "1\tp.xhtml\t5\tCh 1\tx\n")
        z.writestr("OEBPS/recaps/people.tsv", "1\tp.xhtml\t2\tGina Harte\tmet\t1\tDI\n"
                   "1\tp.xhtml\t3\tAnnabel\tmet\t1\tvictim\n")
        z.writestr("OEBPS/recaps/meta.tsv", "format\t3\nmain_character\t%s\n" % hero)
    return p


def test_audit_flags_recaps_written_around_the_wrong_lead(tmp_path):
    from epubkit.stages import recaps as R
    assert R.audit(_recapped(tmp_path, "Annabel"))["lead_off"] is True
    assert R.audit(_recapped(tmp_path, "Gina"))["lead_off"] is False
    # a series not named after anyone in the book says nothing about the lead
    assert R.audit(_recapped(tmp_path, "Annabel", "The Hunger Games"))["lead_off"] is False
