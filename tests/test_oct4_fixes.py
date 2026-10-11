"""Fixes from the 4 October 2026 batch (Murderbot / Bobiverse / 1984)."""
import json
import zipfile

from epubkit.tools import fix_epub3 as f
from epubkit.tools import series_hint as sh


def test_invented_sentence_tags_are_unwrapped_keeping_text():
    d = b'<p><span class="x"><sentence data-sentence-id="0">About the Author</sentence></span></p>'
    out, names = f.unwrap_unknown_elements(d)
    assert out == b'<p><span class="x">About the Author</span></p>'
    assert names == ["sentence"]


def test_unknown_tags_left_alone_beside_svg_except_known_junk():
    d = b'<svg><path d="M0"/></svg><sentence>a</sentence>'
    out, _ = f.unwrap_unknown_elements(d)
    assert b"<path" in out and b"<sentence" not in out


def test_div_inside_list_is_wrapped_in_hidden_li():
    d = b'<ol start="2" class="c"> <div class="c10" id="calibre_pb_18"></div> </ol>'
    out, n = f.wrap_list_children(d)
    assert n == 1
    assert b'<li style="list-style-type:none"><div class="c10" id="calibre_pb_18"></div></li>' in out


def test_real_li_untouched():
    d = b"<ul><li>a<div>b</div></li></ul>"
    assert f.wrap_list_children(d) == (d, 0)


def test_data_uri_with_extension_for_subtype():
    out, n = f.fix_data_uri_types(b'<img src="data:image/.jpg;base64,AAA"/>')
    assert out == b'<img src="data:image/jpeg;base64,AAA"/>' and n == 1


def test_data_attribute_names_lowercased_only_in_tags():
    out, n = f.lower_data_attrs(b'<span data-AmznRemoved="x">text data-Foo="y"</span>')
    assert out == b'<span data-amznremoved="x">text data-Foo="y"</span>' and n == 1


def test_css_direction_dropped():
    out, n = f.drop_css_direction(b"p { margin: 0; direction: ltr; unicode-bidi: embed }")
    assert b"direction" not in out and b"margin: 0;" in out and n == 2


def test_prose_equals_sign_is_not_scripted():
    assert "scripted" not in f.scan_props(b"<p>(Unidentified One = Target One)</p>")
    assert "scripted" in f.scan_props(b'<a onclick="x()">y</a>')


def test_nav_page_list_drops_dead_entries_toc_keeps_entry():
    nav = (b'<nav epub:type="toc"><ol><li><a href="c1.xhtml#gone">One</a></li></ol></nav>'
           b'<nav epub:type="page-list"><ol><li><a href="c1.xhtml#pg_9">9</a></li></ol></nav>')
    out, dropped, stripped = f.fix_nav_fragments(nav, lambda h: {"top"})
    assert b'href="c1.xhtml">One' in out
    assert b"page-list" not in out
    assert (dropped, stripped) == (1, 1)


def test_empty_nav_detected_and_dropped():
    d = b'<body><nav epub:type="toc" id="toc">\n  </nav><nav epub:type="landmarks"></nav></body>'
    assert f.nav_is_empty(d)
    out, n = f.drop_empty_navs(d)
    assert n == 2 and b"<nav" not in out


def test_files_with_spaces_renamed_with_references():
    raw = {"content.opf": b'<item href="content/Orwell%20-%201984.html"/><item href="content/Orwell - 1984.html"/>',
           "content/Orwell - 1984.html": b"<p>x</p>",
           "content/a.html": b'<a href="Orwell%20-%201984.html#x">y</a>'}
    out, names, n = f.rename_unsafe_files(raw, list(raw))
    assert n == 1 and "content/Orwell_-_1984.html" in out
    assert b"Orwell_-_1984.html" in out["content.opf"] and b" " not in out["content.opf"].split(b'"')[1]
    assert b'href="Orwell_-_1984.html#x"' in out["content/a.html"]


ALSO_BY = b"""<html><body>
<p class="ACH">ALSO BY <a href="x">MARTHA WELLS</a></p>
<p class="ACL1">THE MURDERBOT DIARIES</p>
<p><i>All Systems Red</i></p><p><i>Artificial Condition</i></p><p><i>Rogue Protocol</i></p>
<p class="ACL1">YA NOVELS</p>
<p><i>Emilie and the Hollow World</i></p>
<p class="ACL1">THE FALL OF ILE-RIEN TRILOGY</p>
<p><i>The Wizard Hunters</i></p><p><i>The Ships of Air</i> (coming soon)</p>
</body></html>"""


def _epub(tmp_path, name, page, opf_extra=b""):
    p = tmp_path / name
    with zipfile.ZipFile(p, "w") as z:
        z.writestr("content.opf", b'<package><metadata><dc:title>T</dc:title>'
                   b"<dc:creator>Martha Wells</dc:creator>" + opf_extra + b"</metadata></package>")
        z.writestr("adcard.xhtml", page)
    return p


def test_all_caps_series_headings_on_also_by_page(tmp_path):
    lists = sh.book_lists(_epub(tmp_path, "a.epub", ALSO_BY))
    assert ("The Murderbot Diaries", ["All Systems Red", "Artificial Condition", "Rogue Protocol"]) in lists
    assert ("The Fall of Ile-Rien Trilogy", ["The Wizard Hunters", "The Ships of Air"]) in lists
    assert not any(s.startswith("Ya") for s, _ in lists)


def test_title_with_series_suffix_is_placed_and_trimmed():
    lists = [("The Murderbot Diaries", ["All Systems Red", "Artificial Condition", "Rogue Protocol"])]
    assert sh._place("Rogue Protocol: The Murderbot Diaries", lists)[:2] == ("The Murderbot Diaries", 3)
    assert sh._place("Artificial Condition--The Murderbot Diaries", lists)[:2] == ("The Murderbot Diaries", 2)
    assert sh.strip_series_suffix("Rogue Protocol: The Murderbot Diaries", "The Murderbot Diaries") == "Rogue Protocol"
    assert sh.strip_series_suffix("Compulsory: A Murderbot Diaries Story", "The Murderbot Diaries") == \
        "Compulsory: A Murderbot Diaries Story"


def test_harvest_lets_an_earlier_book_use_a_later_books_list(tmp_path):
    later = _epub(tmp_path, "later.epub", ALSO_BY)
    cache = tmp_path / "lists.json"
    sh.harvest([later], cache)
    assert "martha wells" in json.loads(cache.read_text())


def test_author_stray_semicolon_removed():
    meta, notes = sh.tidy({"title": "There Is No Antimemetics Division", "authors": ["qntm;"]})
    assert meta["authors"] == ["qntm"]


def test_series_consensus_moves_stragglers():
    from epubkit.shelves import series_consensus
    rows = [{"name": "a", "series": "S", "shelf": "Adult › Fiction", "confidence": "high", "why": ""},
            {"name": "b", "series": "S", "shelf": "Adult › Fiction", "confidence": "medium", "why": ""},
            {"name": "c", "series": "S", "shelf": "Teens › Fiction", "confidence": "medium", "why": ""}]
    out = series_consensus(rows)
    assert out[2]["shelf"].startswith("Adult")
