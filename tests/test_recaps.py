"""Recaps down to the passage, and a who's who of everyone met or heard of."""
from __future__ import annotations

import re
import zipfile
from pathlib import Path

from epubkit.config import Config
from epubkit.stages import recaps as R
from epubkit.stages.base import Context
from epubkit.state import BookRecord
from epubkit.tools import kepubify

CONTAINER = """<?xml version="1.0"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
  <rootfiles><rootfile full-path="OEBPS/content.opf"
      media-type="application/oebps-package+xml"/></rootfiles>
</container>"""

OPF = """<?xml version="1.0" encoding="utf-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="2.0" unique-identifier="id">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
    <dc:identifier id="id">urn:uuid:recap</dc:identifier>
    <dc:title>Recap Test</dc:title><dc:language>en</dc:language>
  </metadata>
  <manifest>
    <item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>
    {items}
  </manifest>
  <spine toc="ncx">{refs}</spine>
</package>"""

NCX = """<?xml version="1.0" encoding="utf-8"?>
<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1">
  <navMap>{points}</navMap>
</ncx>"""

DOC = """<?xml version="1.0" encoding="utf-8"?>
<html xmlns="http://www.w3.org/1999/xhtml"><head><title>x</title></head>
<body>{paras}</body></html>"""

FILLER = "The road ran on and the rain came down over the hills again. " * 6


def build(path: Path, chapters: list[list[str]], kepub: bool = True) -> Path:
    """Chapters as lists of paragraph texts, optionally already a kepub."""
    items, refs, points = [], [], []
    with zipfile.ZipFile(path, "w") as z:
        zi = zipfile.ZipInfo("mimetype")
        zi.compress_type = zipfile.ZIP_STORED
        z.writestr(zi, "application/epub+zip")
        z.writestr("META-INF/container.xml", CONTAINER)
        for i, paras in enumerate(chapters, 1):
            href = "Text/ch%d.xhtml" % i
            items.append('<item href="%s" id="c%d" media-type="application/xhtml+xml"/>'
                         % (href, i))
            refs.append('<itemref idref="c%d"/>' % i)
            points.append('<navPoint id="n%d"><navLabel><text>Chapter %d</text>'
                          '</navLabel><content src="%s"/></navPoint>' % (i, i, href))
            body = DOC.format(paras="".join("<p>%s</p>" % p for p in paras)).encode()
            if kepub:
                body = kepubify.convert_doc(body)
            z.writestr("OEBPS/" + href, body)
        z.writestr("OEBPS/content.opf", OPF.format(items="".join(items),
                                                   refs="".join(refs)))
        z.writestr("OEBPS/toc.ncx", NCX.format(points="".join(points)))
    return path


class Fake:
    """Answers the hero question, then lists whoever each passage names."""

    def __init__(self, cast=None):
        self.cast = cast or {}
        self.calls = []

    def complete(self, prompt, system=None, **kw):
        self.calls.append(prompt)
        if "Who is its main character" in prompt:
            return type("R", (), {"text": "Wren"})()
        passage = prompt.split("Passage:", 1)[1]
        people = [line for name, line in self.cast.items() if name in passage]
        tag = re.search(r"MARK(\d+)", passage)
        text = "SUMMARY: passage %s happened.\nPEOPLE:\n%s" % (
            tag.group(1) if tag else "?", "\n".join(people))
        return type("R", (), {"text": text})()


def run(tmp_path, book, provider, **over):
    opts = {"source": tmp_path, "output": tmp_path / "o",
            "recaps.min_chars": 100, "recaps.passage_words": 350}
    opts.update(over)
    cfg = Config.load(None, opts)
    work = tmp_path / "work"
    work.mkdir(exist_ok=True)
    import logging
    ctx = Context(config=cfg, record=BookRecord(source=str(book)), path=book,
                  work=work, log=logging.getLogger("t"), provider=provider)
    assert R.RecapStage().run(ctx).ok
    return ctx


def rows(book, name):
    with zipfile.ZipFile(book) as z:
        return [r.split("\t") for r in
                z.read("OEBPS/recaps/%s" % name).decode().splitlines() if r]


def para_of(book, member, needle):
    with zipfile.ZipFile(book) as z:
        paras = R._paragraphs(z.read(member))
    return next(n for n, t in paras if needle in t)


# -- position ---------------------------------------------------------------

def test_a_chapter_is_recapped_in_passages_that_end_on_real_paragraphs(tmp_path):
    ch = ["MARK%d %s" % (i, FILLER) for i in range(12)]
    book = build(tmp_path / "b.kepub.epub", [ch])
    run(tmp_path, book, Fake())
    parts = rows(book, "parts.tsv")
    assert len(parts) > 1, "a long chapter must be more than one passage"
    ends = [int(p[2]) for p in parts]
    assert ends == sorted(ends)
    with zipfile.ZipFile(book) as z:
        real = {n for n, _t in R._paragraphs(z.read("OEBPS/Text/ch1.xhtml"))}
    assert set(ends) <= real
    assert all(p[1] == "OEBPS/Text/ch1.xhtml" for p in parts)
    # the recap of a passage never describes a paragraph past its end
    for p in parts:
        mark = int(re.search(r"passage (\d+)", p[4]).group(1))
        assert para_of(book, "OEBPS/Text/ch1.xhtml", "MARK%d " % mark) <= int(p[2])


def test_a_plain_epub_is_numbered_as_its_kepub_will_be(tmp_path):
    ch = ["MARK%d %s" % (i, FILLER) for i in range(6)]
    plain = build(tmp_path / "p.epub", [ch], kepub=False)
    kep = build(tmp_path / "k.kepub.epub", [ch], kepub=True)
    with zipfile.ZipFile(plain) as a, zipfile.ZipFile(kep) as b:
        assert (R._paragraphs(a.read("OEBPS/Text/ch1.xhtml"))
                == R._paragraphs(b.read("OEBPS/Text/ch1.xhtml")))


# -- the who's who ----------------------------------------------------------

def test_someone_only_told_about_is_filed_where_they_are_first_named(tmp_path):
    ch = ["MARK%d %s" % (i, FILLER) for i in range(8)]
    ch[1] = "Over supper the ferryman told them about Queen Isolde of the old tales. " + FILLER
    book = build(tmp_path / "b.kepub.epub", [ch])
    run(tmp_path, book, Fake({
        "Isolde": "Queen Isolde | heard | a queen in the ferryman's story | "
                  "a legend Wren has only heard of",
    }))
    people = [r for r in rows(book, "people.tsv") if r[3] == "Queen Isolde"]
    first = people[0]
    assert int(first[2]) == para_of(book, "OEBPS/Text/ch1.xhtml", "Queen Isolde")
    assert first[4] == "heard"
    assert "ferryman" in first[6]
    # and nobody else sees her name before that paragraph
    assert all(int(r[2]) >= int(first[2]) for r in people)


def test_heard_of_becomes_met_and_never_goes_back(tmp_path):
    c1 = ["MARK1 Wren's letters were always about her brother Tam. " + FILLER] + [FILLER] * 3
    c2 = ["MARK2 Tam walked in out of the rain. " + FILLER] + [FILLER] * 3
    c3 = ["MARK3 Wren remembered what Tam had said. " + FILLER] + [FILLER] * 3
    book = build(tmp_path / "b.kepub.epub", [c1, c2, c3])

    class Staged(Fake):
        def complete(self, prompt, system=None, **kw):
            if "MARK2" in prompt:
                self.cast = {"Tam": "Tam | met | a young man soaked through | "
                                    "Wren's brother, home at last"}
            elif "MARK3" in prompt:
                self.cast = {"Tam": "Tam | heard | remembered | Wren's brother"}
            else:
                self.cast = {"Tam": "Tam | heard | Wren's brother, in letters | "
                                    "Wren's brother, away"}
            return super().complete(prompt, system)

    run(tmp_path, book, Staged(), **{"ai.concurrency": 1})
    tam = [r for r in rows(book, "people.tsv") if r[3] == "Tam"]
    by_chapter = {}
    for r in tam:
        by_chapter.setdefault(r[1], set()).add(r[4])
    assert by_chapter["OEBPS/Text/ch1.xhtml"] == {"heard"}
    assert by_chapter["OEBPS/Text/ch2.xhtml"] == {"met"}
    # a later mention that says nothing new is thinned away, but never demotes
    assert by_chapter.get("OEBPS/Text/ch3.xhtml", {"met"}) == {"met"}
    # introduced once; after that only the relationship moves on
    assert sum(1 for r in tam if "in letters" in r[6]) == 1


def test_a_person_the_passage_never_names_is_left_out(tmp_path):
    """The model filling in from what it knows of the book (Grindelwald in
    Half-Blood Prince) - nobody the passage itself does not name gets in."""
    ch = ["MARK%d %s" % (i, FILLER) for i in range(4)]
    book = build(tmp_path / "b.kepub.epub", [ch])
    run(tmp_path, book, Fake({"MARK0": "Nobody Named | heard | a rumour | unknown"}))
    with zipfile.ZipFile(book) as z:
        people = (z.read("OEBPS/recaps/people.tsv").decode()
                  if "OEBPS/recaps/people.tsv" in z.namelist() else "")
    assert "Nobody Named" not in people


def test_the_main_character_is_said_to_be_so(tmp_path):
    ch = ["MARK0 That morning Wren woke early. " + FILLER] + [FILLER] * 3
    book = build(tmp_path / "b.kepub.epub", [ch])
    run(tmp_path, book, Fake({"Wren": "Wren | met | a girl at the window | protagonist"}))
    rels = [r[6] for r in rows(book, "people.tsv") if r[3] == "Wren"]
    assert "the main character" in rels


# -- the files ----------------------------------------------------------------

def test_recap_files_go_in_the_manifest_and_never_the_spine(tmp_path):
    ch = ["MARK0 That morning Wren woke. " + FILLER] + [FILLER] * 3
    book = build(tmp_path / "b.kepub.epub", [ch])
    # an earlier version left flat files beside the OPF; they must not linger
    with zipfile.ZipFile(book, "a") as z:
        z.writestr("OEBPS/recaps.tsv", "0\tc\told")
    run(tmp_path, book, Fake({"Wren": "Wren | met | a girl | the main character"}))
    with zipfile.ZipFile(book) as z:
        names = z.namelist()
        opf = z.read("OEBPS/content.opf").decode()
    for f in ("parts.tsv", "people.tsv", "spine.tsv", "meta.tsv"):
        assert "OEBPS/recaps/" + f in names
        assert 'href="recaps/%s"' % f in opf
    # the chapter-level copies only repeat the passages, so by default they go
    for f in ("recaps.tsv", "cast.tsv"):
        assert "OEBPS/recaps/" + f not in names
        assert 'href="recaps/%s"' % f not in opf
    assert "OEBPS/recaps.tsv" not in names
    assert "recaps/" not in opf.split("<spine")[1]
    assert names.count("OEBPS/recaps/parts.tsv") == 1
    assert rows(book, "spine.tsv") == [["0", "OEBPS/Text/ch1.xhtml"]]
    meta = dict(r[:2] for r in rows(book, "meta.tsv"))
    assert meta["format"] == str(R.FORMAT) and meta["main_character"] == "Wren"


def test_legacy_files_on_request(tmp_path):
    ch = ["MARK0 Wren woke. " + FILLER] + [FILLER] * 3
    book = build(tmp_path / "b.kepub.epub", [ch])
    run(tmp_path, book, Fake({"Wren": "Wren | met | a girl | the main character"}),
        **{"recaps.legacy_files": True})
    assert rows(book, "recaps.tsv") and rows(book, "cast.tsv")
    # and switching them off again removes them rather than leaving them stale
    run(tmp_path, book, Fake())
    with zipfile.ZipFile(book) as z:
        assert "OEBPS/recaps/recaps.tsv" not in z.namelist()


def test_running_twice_does_not_stack_declarations(tmp_path):
    ch = ["MARK0 Wren woke. " + FILLER] + [FILLER] * 3
    book = build(tmp_path / "b.kepub.epub", [ch])
    run(tmp_path, book, Fake())
    run(tmp_path, book, Fake())
    with zipfile.ZipFile(book) as z:
        opf = z.read("OEBPS/content.opf").decode()
    assert opf.count('href="recaps/parts.tsv"') == 1


def test_nothing_but_the_recaps_changes_and_the_file_keeps_its_identity(tmp_path):
    """The guarantee that makes it safe to rewrite books already in a library."""
    ch = ["MARK%d %s" % (i, FILLER) for i in range(6)]
    book = build(tmp_path / "b.kepub.epub", [ch, ch])
    with zipfile.ZipFile(book) as z:
        before = {n: z.read(n) for n in z.namelist()}
        dates = {n: z.getinfo(n).date_time for n in z.namelist()}
    inode = book.stat().st_ino
    run(tmp_path, book, Fake({"Wren": "Wren | met | a girl | the main character"}))
    assert book.stat().st_ino == inode, "rewritten in place, not replaced"
    with zipfile.ZipFile(book) as z:
        assert z.namelist()[0] == "mimetype"
        for n, data in before.items():
            if n.endswith(".opf"):
                continue
            assert z.read(n) == data, n
            assert z.getinfo(n).date_time == dates[n], n
        opf = z.read("OEBPS/content.opf")
    assert R._strip_recap_items(opf) == R._strip_recap_items(before["OEBPS/content.opf"])


def test_a_book_is_left_alone_if_anything_else_would_change(tmp_path, monkeypatch):
    ch = ["MARK0 Wren woke. " + FILLER] + [FILLER] * 3
    book = build(tmp_path / "b.kepub.epub", [ch])
    original = book.read_bytes()
    monkeypatch.setattr(R, "_declare", lambda opf, present: opf.replace("Recap Test", "Oops"))
    import pytest
    with pytest.raises(RuntimeError, match="refusing"):
        R._embed(book, [(0, "OEBPS/Text/ch1.xhtml", 3, "Chapter 1", "s")], [], [], [])
    assert book.read_bytes() == original
    assert not [p for p in tmp_path.iterdir() if p.name.startswith("tmp")]


def test_each_passage_sees_the_end_of_the_one_before(tmp_path):
    ch = ["MARK%d %s" % (i, FILLER) for i in range(12)]
    book = build(tmp_path / "b.kepub.epub", [ch])
    fake = Fake()
    run(tmp_path, book, fake, **{"ai.concurrency": 1})
    prompts = [p for p in fake.calls if "Passage:" in p]
    assert len(prompts) > 1
    assert "Just before this passage" not in prompts[0]
    second = prompts[1]
    before, passage = second.split("Passage:", 1)
    last_of_first = re.findall(r"MARK\d+", prompts[0].split("Passage:", 1)[1])[-1]
    assert last_of_first in before and last_of_first not in passage


# -- audit -------------------------------------------------------------------

def test_audit_tells_the_kinds_apart(tmp_path):
    ch = ["MARK0 That morning Wren woke, and she thought of Old Maud. " + FILLER] + [FILLER] * 3
    none = build(tmp_path / "none.kepub.epub", [ch])
    flat = build(tmp_path / "flat.kepub.epub", [ch])
    with zipfile.ZipFile(flat, "a") as z:
        z.writestr("OEBPS/recaps.tsv", "1\tc1.xhtml\ta summary")
    chapters = build(tmp_path / "chap.kepub.epub", [ch])
    with zipfile.ZipFile(chapters, "a") as z:
        z.writestr("OEBPS/recaps/recaps.tsv", "0\tOEBPS/Text/ch1.xhtml\tChapter 1\tx")
        z.writestr("OEBPS/recaps/cast.tsv", "0\tOEBPS/Text/ch1.xhtml\tAda\ta tester")
    done = build(tmp_path / "done.kepub.epub", [ch])
    run(tmp_path, done, Fake({"Wren": "Wren | met | a girl | the main character",
                              "Maud": "Old Maud | heard | a cook | a stranger"}))
    kinds = {p.name: R.audit(p) for p in (none, flat, chapters, done)}
    assert kinds["none.kepub.epub"]["kind"] == "none"
    assert kinds["flat.kepub.epub"]["kind"] == "flat"
    assert kinds["chap.kepub.epub"]["kind"] == "chapters"
    assert kinds["chap.kepub.epub"]["people"] == 1
    d = kinds["done.kepub.epub"]
    assert d["kind"] == "passages" and d["format"] == R.FORMAT
    assert d["people"] == 2 and d["heard_of"] == 1 and d["kepub"]
    assert d["recap_kb"] > 0


# -- parsing -----------------------------------------------------------------

def test_parse_reads_the_people_table_and_ignores_decoration():
    s, people = R._parse(
        "**SUMMARY:** Wren left home.\n\nPEOPLE:\n"
        "- Wren | met | a girl | the main character\n"
        "2. Old Maud (the cook) | Heard | a cook in a story | nobody to Wren yet\n"
        "a group of soldiers | met | x | y\n"
        "Broken line with no pipes\n")
    assert s == "Wren left home."
    assert ("Wren", "met", "a girl", "the main character") in people
    assert ("Old Maud", "heard", "a cook in a story", "nobody to Wren yet") in people
    assert len(people) == 2


def test_a_reply_without_a_summary_is_discarded():
    assert R._parse("I'm afraid I can't help with that.") == ("", [])


def test_repeats_are_thinned_but_still_counted():
    rows = [(0, "p", 5, "Tam", "heard", "Wren's brother, away at sea"),
            (1, "p", 9, "Tam", "heard", "Wren's brother, away at sea."),
            (2, "p", 3, "Tam", "met", "Wren's brother, away at sea"),
            (3, "p", 4, "Tam", "met", "Wren's brother, now home and hiding something")]
    out = R._thin(rows)
    assert [(r[0], r[4], r[5]) for r in out] == [(0, "heard", 1), (2, "met", 3), (3, "met", 4)]


def test_the_lead_is_whoever_runs_through_the_book_not_the_opening(tmp_path):
    """Half-Blood Prince opens on the Prime Minister; the lead is Harry."""
    opening = ["In his office the Prime Minister waited, and then Fudge arrived. " + FILLER] * 4
    rest = [["Later that night Harry looked at Ron and then Harry laughed. " + FILLER] * 4
            for _ in range(6)]
    book = build(tmp_path / "b.kepub.epub", [opening] + rest)
    docs = R._documents(book, 100)
    names = R._recurring_names(docs)
    assert names[0] == "Harry"
    assert "Prime" not in names and "Minister" not in names

    class Wrong(Fake):
        def complete(self, prompt, system=None, **kw):
            if "Who is its main character" in prompt:
                assert "Harry" in prompt.split("{", 1)[0]
                return type("R", (), {"text": "The Prime Minister"})()
            return super().complete(prompt, system)

    cfg = Config.load(None, {"source": tmp_path, "output": tmp_path / "o"})
    ctx = Context(config=cfg, record=BookRecord(source="b"), path=book, work=tmp_path,
                  log=__import__("logging").getLogger("t"), provider=Wrong())
    # an answer outside the recurring names falls back to the most widespread
    assert R._hero(ctx, docs) == "Harry"


def test_the_main_character_can_be_set_by_hand(tmp_path):
    ch = ["MARK0 That morning Wren woke. " + FILLER] + [FILLER] * 3
    book = build(tmp_path / "b.kepub.epub", [ch])
    run(tmp_path, book, Fake(), **{"recaps.main_character": "Tam"})
    meta = dict(r[:2] for r in rows(book, "meta.tsv"))
    assert meta["main_character"] == "Tam"


# -- what the model gets wrong -------------------------------------------------

def test_a_contents_page_is_never_recapped(tmp_path):
    """Given a list of chapter titles, the model recapped Half-Blood Prince
    from its own knowledge of the book - Horcruxes and all - before chapter 1."""
    toc = ["<a href='ch%d.xhtml'>Chapter %d: The Cave of Horcruxes</a>" % (i, i) for i in range(30)]
    story = ["MARK1 That morning Wren woke. " + FILLER] + [FILLER] * 3
    book = build(tmp_path / "b.kepub.epub", [toc, story])
    fake = Fake()
    run(tmp_path, book, fake)
    assert not any("Horcrux" in p for p in fake.calls)
    assert all(p[1] != "OEBPS/Text/ch1.xhtml" for p in rows(book, "parts.tsv"))


def test_groups_and_descriptions_are_not_people():
    for bad in ("The Gaunt family", "The Hogwarts staff", "The builders",
                "The ugly little man", "Obliviators", "Lord Vol-", "The art historian",
                "The Ministry of Magic", "The Office of Misinformation"):
        assert not R._individual(bad), bad
    for good in ("Harry Potter", "The Prime Minister", "Vincent van Gogh",
                 "The Duke", "Dobby", "Rufus Scrimgeour"):
        assert R._individual(good), good


def test_only_the_lead_is_the_main_character():
    assert R._is_hero("Harry Potter", "Harry") and R._is_hero("Harry", "Harry")
    assert not R._is_hero("The Prime Minister", "Harry")
    rel = "the main character of the story, the head of the Muggle government"
    assert R._not_the_lead(rel, "a man") == "the head of the Muggle government"
    assert R._not_the_lead("the main character", "the head of the government") \
        == "the head of the government"


def test_long_descriptions_are_cut_at_a_clause():
    long = ("a Muggle official, a friend to the Prime Minister, and a part of the "
            "story as he is a key figure in the events that are unfolding")
    out = R._tidy(long)
    assert len(out.split()) <= 16 and not out.endswith(",")


def test_first_name_and_full_name_are_one_person():
    from epubkit.tools import dedupe_cast
    got = dedupe_cast.identities([(5, "", "Harry", ""), (5, "", "Harry Potter", ""),
                                  (7, "", "Mrs. Finnigan", ""), (7, "", "Seamus Finnigan", "")])
    assert got["Harry"] == got["Harry Potter"]
    assert got["Mrs. Finnigan"] != got["Seamus Finnigan"]


def test_a_misspelt_or_invented_name_is_left_out():
    paras = [(1, "The Prime Minister remembered Sirius Black and Fudge.")]
    assert R._all_words_present("Sirius Black", paras)
    assert not R._all_words_present("Serious Black", paras)
    assert R._all_words_present("The Prime Minister", paras)


def test_the_is_not_part_of_who_someone_is():
    from epubkit.tools import dedupe_cast
    got = dedupe_cast.identities([(1, "", "The Prime Minister", ""), (9, "", "Prime Minister", "")])
    assert got["The Prime Minister"] == got["Prime Minister"]


def test_no_tie_to_the_lead_where_the_lead_is_absent():
    assert R._unclaim("Harry's acquaintance, a woman who distrusts Snape", "Harry") == \
        "not yet connected to Harry; a woman who distrusts Snape"
    assert R._unclaim("Harry's friend", "Harry") == "not yet connected to Harry"
    assert R._unclaim("a government official", "Harry") == "a government official"


def test_a_name_broken_off_mid_word_is_not_a_person():
    paras = [(1, "He said Lord Vol\u2014 and stopped. Vol\u2014 again.")]
    assert R._cut_off("Lord Vol", paras)
    assert not R._cut_off("Fudge", [(1, "Fudge said.")])


# -- language ---------------------------------------------------------------

FILLER_FR = ("La route continuait et la pluie tombait sur les collines, mais elle "
             "avançait dans le froid avec son frère qui ne disait rien. ") * 4


class FakeFr:
    """Answers in French, the way the French prompt asks."""

    def __init__(self, cast=None):
        self.cast = cast or {}
        self.calls = []

    def complete(self, prompt, system=None, **kw):
        self.calls.append((prompt, system))
        if "Qui en est le personnage principal" in prompt:
            return type("R", (), {"text": "Hélène"})()
        passage = prompt.split("Passage :", 1)[1]
        people = [line for name, line in self.cast.items() if name in passage]
        text = "RÉSUMÉ : Hélène marchait sous la pluie.\nPERSONNAGES :\n%s" % "\n".join(people)
        return type("R", (), {"text": text})()


def french_book(tmp_path):
    c1 = ["Ce matin-là, Hélène quitta la maison avec Tam. " + FILLER_FR] + [FILLER_FR] * 3
    c2 = ["Loin de là, le juge Morel relisait une lettre. " + FILLER_FR] + [FILLER_FR] * 3
    # labelled "en", as converted books often are: the text decides
    return build(tmp_path / "fr.kepub.epub", [c1, c2])


def test_a_french_book_is_recapped_entirely_in_french(tmp_path):
    book = french_book(tmp_path)
    fake = FakeFr({
        "Tam": "Tam | rencontré | un jeune homme trempé | le frère d'Hélène",
        "Hélène": "Hélène | rencontré | une jeune femme pressée | l'héroïne",
        "Morel": "Morel | rencontré | un juge qui lit une lettre | "
                 "ami d'Hélène, un juge de la ville",
    })
    run(tmp_path, book, fake)
    # every prompt went out in French
    assert fake.calls and all("français" in (sys or "") for _p, sys in fake.calls)
    assert not any("Who is its main character" in p for p, _s in fake.calls)
    parts = rows(book, "parts.tsv")
    assert parts and all("pluie" in p[4] for p in parts)
    people = {r[3]: r for r in rows(book, "people.tsv")}
    assert people["Tam"][4] == "met"                       # rencontré
    assert people["Hélène"][6] == "le personnage principal"
    # a passage without Hélène cannot show a tie to her; said in French
    assert people["Morel"][6].startswith("pas encore lié à Hélène")
    meta = dict(r[:2] for r in rows(book, "meta.tsv"))
    assert meta["language"] == "fr" and meta["main_character"] == "Hélène"
    assert R.audit(book)["lang_off"] is False


def test_an_english_book_stays_english(tmp_path):
    ch = ["MARK%d %s" % (i, FILLER) for i in range(4)]
    book = build(tmp_path / "b.kepub.epub", [ch])
    fake = Fake()
    run(tmp_path, book, fake)
    assert all("RÉSUMÉ" not in p for p in fake.calls)
    assert dict(r[:2] for r in rows(book, "meta.tsv"))["language"] == "en"
    assert R.audit(book)["lang_off"] is False


def test_french_recaps_from_before_french_support_are_redone(tmp_path):
    book = french_book(tmp_path)
    run(tmp_path, book, Fake(), **{"recaps.language": "en"})
    a = R.audit(book)
    assert a["kind"] == "passages" and a["lang_off"] is True


def test_language_setting_and_label(tmp_path):
    fr = "Elle était dans la cuisine avec les enfants et ne disait rien pour une fois. " * 8
    en = "She was in the kitchen with the children and said nothing for once. " * 8
    assert R._language("auto", fr, "<dc:language>en</dc:language>") == "fr"
    assert R._language("auto", en, "<dc:language>fr</dc:language>") == "en"
    assert R._language("auto", "", "<dc:language>fr-FR</dc:language>") == "fr"
    assert R._language("fr", en, "") == "fr"
    # a Spanish book is not taken for French; the English prompt asks for its own language
    es = "Ella estaba en la cocina con los niños y no decía nada por una vez. " * 8
    assert R._language("auto", es, "<dc:language>es</dc:language>") == "en"


def test_french_names_and_replies_are_understood():
    s, people = R._parse("RÉSUMÉ : Ils partirent.\nPERSONNAGES :\n"
                         "d'Artagnan | rencontré | un jeune Gascon | ami de Hélène\n"
                         "« Madame Bonacieux » | évoqué | une lingère | pas encore liée")
    assert s == "Ils partirent."
    assert people[1][0] == "Madame Bonacieux" and people[1][1] == "heard"
    assert R._individual("d'Artagnan") and R._individual("Le Duc de Guise")
    assert not R._individual("La famille Morel")
    docs = [(i, "c%d" % i, "", [(0, "Ce jour-là, Hélène et Émile virent Monsieur Dupont.")])
            for i in range(3)]
    names = R._recurring_names(docs)
    assert "Émile" in names and "Monsieur" not in names


# -- speed and resuming ---------------------------------------------------

def test_a_stopped_run_resumes_instead_of_starting_over(tmp_path):
    ch = ["MARK%d %s" % (i, FILLER) for i in range(40)]
    book = build(tmp_path / "b.kepub.epub", [ch])

    class Stops(Fake):
        def complete(self, prompt, system=None, **kw):
            if len(self.calls) == 4:
                raise KeyboardInterrupt          # Ctrl-C, a reboot
            return super().complete(prompt, system)

    import pytest
    with pytest.raises(KeyboardInterrupt):
        run(tmp_path, book, Stops(), **{"ai.concurrency": 1})
    again = Fake()
    run(tmp_path, book, again, **{"ai.concurrency": 1})
    total = len(rows(book, "parts.tsv"))
    # the four passages answered before the stop are not asked again
    assert total > 5 and len(again.calls) == total - 4
    assert not any(R._Answers(book, "").path.parent.glob("*.jsonl"))


def test_a_failed_passage_gets_a_second_try(tmp_path):
    ch = ["MARK%d %s" % (i, FILLER) for i in range(12)]
    book = build(tmp_path / "b.kepub.epub", [ch])

    class Flaky(Fake):
        failed = False

        def complete(self, prompt, system=None, **kw):
            if "MARK5 " in prompt.split("Passage:")[-1] and not self.failed:
                self.failed = True
                raise TimeoutError("gemma went quiet")
            return super().complete(prompt, system)

    run(tmp_path, book, Flaky(), **{"ai.concurrency": 1})
    marks = {int(re.search(r"passage (\d+)", p[4]).group(1)) for p in rows(book, "parts.tsv")}
    with zipfile.ZipFile(book) as z:
        paras = R._paragraphs(z.read("OEBPS/Text/ch1.xhtml"))
    seg = [s for s in R._segments(paras, 350 * 6)]
    holder = next(i for i, s in enumerate(seg) if any("MARK5 " in t for _n, t in s))
    first = int(re.search(r"MARK(\d+)", seg[holder][0][1]).group(1))
    assert first in marks


def test_lean_cast_asks_only_for_news_but_still_counts_everyone(tmp_path):
    ch = ["MARK%d Tam waited by the door. %s" % (i, FILLER) for i in range(12)]
    book = build(tmp_path / "b.kepub.epub", [ch])

    class Lean(Fake):
        def complete(self, prompt, system=None, **kw):
            # a lean reply leaves Tam out once the reader knows him
            self.cast = {} if "already knows" in prompt else {
                "Tam": "Tam | met | a man by the door | Wren's brother"}
            return super().complete(prompt, system)

    fake = Lean()
    run(tmp_path, book, fake, **{"ai.concurrency": 1, "recaps.lean_cast": True})
    passages = [p for p in fake.calls if "Passage:" in p]
    assert "already knows" not in passages[0]
    assert all("- Tam (met): Wren's brother" in p for p in passages[1:])
    tam = [r for r in rows(book, "people.tsv") if r[3] == "Tam"]
    assert tam and tam[0][4] == "met"
    # the count rides on the kept rows; seen in every passage is still known
    assert len(passages) > 1


def test_a_series_title_word_like_the_is_not_a_wrong_lead(tmp_path):
    """"The Chronicles of Narnia" and "The White Witch" share only "The":
    that flagged every Narnia book as written around the wrong lead."""
    ch = ["MARK%d %s" % (i, FILLER) for i in range(4)]
    ch[0] = "Lucy met The White Witch in the snow. " + FILLER
    book = build(tmp_path / "n.kepub.epub", [ch])
    run(tmp_path, book, Fake({"Witch": "The White Witch | met | a queen in a sledge | "
                                       "not yet connected to Lucy; a queen"}),
        **{"recaps.main_character": "Lucy"})
    _set_series(book, "The Chronicles of Narnia")
    assert R.audit(book)["lead_off"] is False
    # a series that really is named after someone else still counts
    _set_series(book, "The White Witch Saga")
    assert R.audit(book)["lead_off"] is True


def _set_series(book, series):
    with zipfile.ZipFile(book) as z:
        items = [(i, z.read(i.filename)) for i in z.infolist()]
    with zipfile.ZipFile(book, "w") as z:
        for info, data in items:
            if info.filename.endswith(".opf"):
                data = re.sub(rb'<meta name="calibre:series"[^>]*/>', b"", data).replace(
                    b"</metadata>", b'<meta name="calibre:series" content="%s"/></metadata>'
                    % series.encode())
            z.writestr(info, data)
