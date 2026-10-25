"""Series memory, a running memory inside the book, and first-person books."""
from __future__ import annotations

import json

from test_recaps import FILLER, Fake, _set_series, build, rows, run

from epubkit import series_memory as SM
from epubkit.stages import recaps as R

I_FILLER = ("I walked down the corridor and my boots rang on the stone. I kept my "
            "eyes on the next door, and I counted the steps to myself. ") * 4
HE_FILLER = ("He walked down the corridor and his boots rang on the stone. "
             "“I am tired,” he said, “and I want my bed.” ") * 4


class SeriesFake(Fake):
    """A Fake that also answers the after-the-book questions."""

    def complete(self, prompt, system=None, **kw):
        if "Answer with the line only" in prompt:
            self.calls.append(prompt)
            return type("R", (), {"text": "Carl's cat and his partner in the crawl"})()
        if "Write what happened" in prompt:
            self.calls.append(prompt)
            return type("R", (), {"text": "Carl and Donut went down into the dungeon."})()
        return super().complete(prompt, system)


def dcc(tmp_path, n, cast, chapters=None):
    name = "Dungeon Crawler Carl %02d - Book %d - Matt Dinniman.kepub.epub" % (n, n)
    ch = chapters or ["MARK%d Donut sat by the door. %s" % (i, I_FILLER) for i in range(10)]
    book = build(tmp_path / name, [ch])
    _set_series(book, "Dungeon Crawler Carl")
    fake = SeriesFake(cast)
    run(tmp_path, book, fake, **{"ai.concurrency": 1, "recaps.main_character": "Carl",
                                 "recaps.series_dir": str(tmp_path / "series")})
    return book, fake


def latest(book, name):
    got = [r for r in rows(book, "people.tsv") if r[3] == name]
    return got[-1][6] if got else None


# -- which book of which series ------------------------------------------------

def test_the_file_name_gives_the_number(tmp_path):
    book = build(tmp_path / "Dungeon Crawler Carl 03 - The Dungeon Anarchist's "
                            "Cookbook - Matt Dinniman.kepub.epub", [[FILLER]])
    _set_series(book, "Dungeon Crawler Carl")
    assert SM.series_of(book) == ("Dungeon Crawler Carl", 3)


def test_series_books_are_done_in_reading_order(tmp_path):
    made = []
    for n in (3, 1, 2):
        b = build(tmp_path / ("Dungeon Crawler Carl %02d - X - Y.kepub.epub" % n), [[FILLER]])
        _set_series(b, "Dungeon Crawler Carl")
        made.append(b)
    loose = build(tmp_path / "Standalone.kepub.epub", [[FILLER]])
    order = R.reading_order([made[0], loose, made[1], made[2]])
    assert [SM.series_of(b)[1] for b in order[:3]] == [1, 2, 3]
    assert order[3] == loose


# -- first person --------------------------------------------------------------

def test_first_person_narration_is_recognised_and_dialogue_is_not():
    docs_i = [(1, "a", "c1", [(1, I_FILLER)] * 5)]
    docs_he = [(1, "a", "c1", [(1, HE_FILLER)] * 5)]
    assert R._first_person(docs_i)
    assert not R._first_person(docs_he)


def test_a_first_person_lead_keeps_their_ties_where_their_name_is_not_printed(tmp_path):
    """Carl tells DCC as "I". His friends came out "not yet connected to
    Carl" in every passage that did not happen to print his name."""
    book, fake = dcc(tmp_path, 1, {"Donut": "Donut | met | a cat by the door | "
                                            "Carl's cat, a talking show cat"})
    assert latest(book, "Donut") == "Carl's cat, a talking show cat"
    prompts = [p for p in fake.calls if "Passage:" in p]
    assert 'Carl tells this story as "I"' in prompts[0]


# -- the series memory ---------------------------------------------------------

def test_a_finished_book_goes_into_the_series_memory(tmp_path):
    dcc(tmp_path, 1, {"Donut": "Donut | met | a cat | Carl's cat"})
    data = json.loads((tmp_path / "series" / "Dungeon Crawler Carl.json").read_text())
    assert data["lead"] == "Carl"
    assert data["first_person"] is True
    assert data["people"]["Donut"]["by_book"]["1"] == "Carl's cat and his partner in the crawl"
    assert "dungeon" in data["books"]["1"]["story"]


def test_the_next_book_knows_who_everyone_is(tmp_path):
    dcc(tmp_path, 1, {"Donut": "Donut | met | a cat | Carl's cat"})
    # book 2's model forgets the cat; the reader has not
    book, fake = dcc(tmp_path, 2, {"Donut": "Donut | met | a cat by the door | "
                                            "not yet connected to Carl; a cat"})
    first = [p for p in fake.calls if "Passage:" in p][0]
    assert "From earlier books" in first
    assert "- Donut: Carl's cat and his partner in the crawl" in first
    # the story so far only goes in when asked for; recaps stay as they were
    assert "Carl and Donut went down into the dungeon." not in first
    assert latest(book, "Donut") == "Carl's cat and his partner in the crawl"


def test_a_book_never_sees_its_own_or_later_books(tmp_path):
    mem = SM.Memory.open(tmp_path, "S")
    mem.record_book(1, "One", "one happened", {"Mo": "Carl's guide"})
    mem.record_book(3, "Three", "three happened", {"Mo": "Carl's guide, later a traitor",
                                                    "Zev": "Carl's handler"})
    mem.save()
    view = SM.Memory.open(tmp_path, "S").before(3)
    assert view.tie("Mo") == "Carl's guide"
    assert view.tie("Zev") == ""
    assert [k for k, _t, _s in view.stories] == ["1"]
    notes = view.notes("Mo waited.", R.TEXT["en"])
    assert "traitor" not in notes and "three happened" not in notes


def test_a_hand_fix_wins(tmp_path):
    mem = SM.Memory.open(tmp_path, "S")
    mem.record_book(1, "One", "", {"Mo": "a stranger"})
    mem.data["people"]["Mo"]["fixed"] = "Carl's game guide, a former crawler"
    mem.data["people"]["Mo"]["aliases"] = ["Mordecai"]
    mem.save()
    view = SM.Memory.open(tmp_path, "S").before(2)
    assert view.tie("Mordecai") == "Carl's game guide, a former crawler"


# -- the running memory inside a book --------------------------------------------

def test_each_passage_is_told_who_people_are_so_far(tmp_path):
    ch = ["MARK%d Tam waited by the door. %s" % (i, FILLER) for i in range(30)]
    book = build(tmp_path / "b.kepub.epub", [ch])
    fake = Fake({"Tam": "Tam | met | a man by the door | Wren's brother"})
    run(tmp_path, book, fake, **{"ai.concurrency": 1})
    prompts = [p for p in fake.calls if "Passage:" in p]
    assert len(prompts) >= 3
    assert "Reader's notes" not in prompts[0]
    notes = prompts[2].split("Passage:", 1)[0]
    assert "- Tam (met): Wren's brother" in notes
    assert "Earlier in this book" not in notes


def test_the_story_so_far_on_request(tmp_path):
    ch = ["MARK%d Tam waited by the door. %s" % (i, FILLER) for i in range(30)]
    book = build(tmp_path / "b.kepub.epub", [ch])
    fake = Fake()
    run(tmp_path, book, fake, **{"ai.concurrency": 1, "recaps.running_story": True})
    notes = [p for p in fake.calls if "Passage:" in p][2].split("Passage:", 1)[0]
    assert "Earlier in this book" in notes and "passage 0 happened." in notes


def test_one_scene_does_not_unmake_who_someone_is():
    known = "Carl's cat and crawl partner"
    assert R._settle_tie("a crawler in the room", "Carl", True, known) == known
    assert R._settle_tie("not yet connected to Carl; a cat", "Carl", True, known) == known
    # news about the lead is kept
    assert R._settle_tie("Carl's cat, now a Princess", "Carl", True, known) == \
        "Carl's cat, now a Princess"
    # with nothing known, an unshown tie is still struck out
    assert R._settle_tie("Carl's friend", "Carl", False, "").startswith("not yet connected")


def test_a_book_is_left_alone_when_the_model_goes_away(tmp_path):
    """Ollama was stopped two minutes into a run; the book was still written
    with 17 of its 163 passages, and the memory built from them."""
    import logging

    from epubkit.config import Config
    from epubkit.stages.base import Context
    from epubkit.state import BookRecord

    ch = ["MARK%d Tam waited by the door. %s" % (i, FILLER) for i in range(40)]
    book = build(tmp_path / "Saga 01 - One - A.kepub.epub", [ch])
    _set_series(book, "Saga")
    before = book.read_bytes()

    class Dies(Fake):
        def complete(self, prompt, system=None, **kw):
            if len([c for c in self.calls if "Passage:" in c]) >= 2:
                raise ConnectionError("connection refused")
            return super().complete(prompt, system)

    cfg = Config.load(None, {"source": tmp_path, "output": tmp_path / "o",
                             "recaps.min_chars": 100, "recaps.passage_words": 350,
                             "recaps.series_dir": str(tmp_path / "series"),
                             "recaps.main_character": "Wren", "ai.concurrency": 1})
    (tmp_path / "work").mkdir()
    ctx = Context(config=cfg, record=BookRecord(source=str(book)), path=book,
                  work=tmp_path / "work", log=logging.getLogger("t"), provider=Dies())
    assert not R.RecapStage().run(ctx).ok
    assert book.read_bytes() == before
    assert not (tmp_path / "series").exists()


# -- weak spots found in the first real run (DCC 1 on Qwen) --------------------

def test_a_scene_is_not_who_someone_is():
    assert R._scene_only("not yet connected to Carl; clutching the skill potion "
                         "to her chest, staring at Carl")
    assert R._scene_only("a person who suggested opening the door")
    assert not R._scene_only("Frank's partner, a crawler who ambushed Carl")
    assert not R._scene_only("not yet connected to Carl; the town's magistrate")
    known = "Frank's partner, a crawler"
    assert R._settle_tie("staring at Carl", "Carl", True, known) == known


def test_a_write_up_may_not_borrow_a_word_from_someone_else():
    lines = ["Carl's cat, a talking show cat", "Carl's companion, a cat",
             "Carl's cat, who met Mordecai, a Bugaboo"]
    assert not R._backed("Carl's companion, a Bugaboo", lines, "Donut", "Carl")
    assert R._backed("Carl's cat and companion", lines, "Donut", "Carl")
    assert not R._backed("Carl's cat, a Wizard", lines, "Donut", "Carl")


def test_a_short_name_is_the_longer_one_unless_both_are_listed():
    names = {"Bea": "Bea", "Beatrice": "Beatrice", "Carl": "Carl", "Carlos": "Carlos"}
    got = R._nicknames(names, set(), "Carl")
    assert got["Bea"] == "Beatrice"
    assert got["Carl"] == "Carl"          # the lead is never folded into anyone
    apart = R._nicknames(names, {frozenset(("Bea", "Beatrice"))}, "Carl")
    assert apart["Bea"] == "Bea"


def test_the_memory_files_a_short_name_under_the_full_one(tmp_path):
    mem = SM.Memory.open(tmp_path, "S")
    mem.record_book(1, "One", "", {"Beatrice": "Carl's ex-girlfriend"})
    mem.record_book(1, "One", "", {"Bea": "someone who advised Carl"})
    assert "Bea" not in mem.data["people"]
    assert mem.data["people"]["Beatrice"]["aliases"] == ["Bea"]
    assert mem.data["people"]["Beatrice"]["by_book"]["1"] == "Carl's ex-girlfriend"


def test_a_hand_fix_made_during_a_book_survives_its_save(tmp_path):
    """Book 4 wrote back the memory it had read at its start, emptying the
    "fixed" lines added while it ran."""
    dcc(tmp_path, 1, {"Donut": "Donut | met | a cat | Carl's cat"})
    path = tmp_path / "series" / "Dungeon Crawler Carl.json"

    class Fixer(SeriesFake):
        def complete(self, prompt, system=None, **kw):
            if "Write what happened" in prompt:     # the book is nearly done
                data = json.loads(path.read_text())
                data["people"]["Donut"]["fixed"] = "Carl's cat and partner"
                path.write_text(json.dumps(data))
            return super().complete(prompt, system)

    name = "Dungeon Crawler Carl 02 - Book 2 - Matt Dinniman.kepub.epub"
    book = build(tmp_path / name, [["MARK%d Donut sat by the door. %s" % (i, I_FILLER)
                                    for i in range(10)]])
    _set_series(book, "Dungeon Crawler Carl")
    run(tmp_path, book, Fixer({"Donut": "Donut | met | a cat | Carl's cat"}),
        **{"ai.concurrency": 1, "recaps.main_character": "Carl",
           "recaps.series_dir": str(tmp_path / "series")})
    data = json.loads(path.read_text())
    assert data["people"]["Donut"]["fixed"] == "Carl's cat and partner"
    assert "2" in data["people"]["Donut"]["by_book"]


def test_what_someone_is_doing_right_now_is_cut_off():
    assert R._drop_scene("Carl's ally who has triggered the trap and is escaping "
                         "via the subterranean stairwell") == ("Carl's ally", True)
    assert R._drop_scene("Carl's loyal companion who comments on the world going "
                         "insane") == ("Carl's loyal companion", True)
    assert R._drop_scene("Frank's partner, a crawler who ambushed Carl") == \
        ("Frank's partner, a crawler who ambushed Carl", False)
    known = "Carl's ally and fellow crawler"
    assert R._settle_tie("Carl's ally who is escaping down the stairs", "Carl",
                         True, known) == known


def test_book_5s_scene_lines_are_cut_too():
    for line, rest in [
            ("Carl's enemy lying dying on the floor after having her limbs ripped off",
             "Carl's enemy"),
            ("Carl's enemy cowering behind a crocodilian in terror", "Carl's enemy"),
            ("Carl's companion who ambushes Vrah from the ceiling and bites her",
             "Carl's companion")]:
        assert R._drop_scene(line) == (rest, True)
    # what lasts stays
    assert R._drop_scene("Carl's ally, a crawler who leads the group")[1] is False
    assert R._drop_scene("Carl's pet dinosaur chicken")[1] is False


# -- characters only -------------------------------------------------------------

def test_characters_only_keeps_the_recaps_word_for_word(tmp_path):
    ch = ["MARK%d Tam waited by the door. %s" % (i, FILLER) for i in range(12)]
    book = build(tmp_path / "b.kepub.epub", [ch])
    run(tmp_path, book, Fake({"Tam": "Tam | met | a man | a man by the door"}))
    before = rows(book, "parts.tsv")

    class Other(Fake):
        def complete(self, prompt, system=None, **kw):
            r = super().complete(prompt, system)
            r.text = r.text.replace("happened", "went differently")
            return r

    run(tmp_path, book, Other({"Tam": "Tam | met | a man | Wren's brother"}),
        **{"recaps.cast_only": True})
    assert rows(book, "parts.tsv") == before
    assert any("Wren's brother" in r[6] for r in rows(book, "people.tsv"))
    meta = dict(r[:2] for r in rows(book, "meta.tsv"))
    assert "people_model" in meta


def test_characters_only_leaves_a_book_without_recaps_alone(tmp_path):
    book = build(tmp_path / "b.kepub.epub", [["MARK1 " + FILLER] * 6])
    data = book.read_bytes()
    run(tmp_path, book, Fake(), **{"recaps.cast_only": True})
    assert book.read_bytes() == data


def test_keep_recaps_writes_them_where_a_book_has_none(tmp_path):
    book = build(tmp_path / "b.kepub.epub", [["MARK%d %s" % (i, FILLER) for i in range(6)]])
    run(tmp_path, book, Fake(), **{"recaps.keep_recaps": True})
    assert rows(book, "parts.tsv")
    before = rows(book, "parts.tsv")

    class Other(Fake):
        def complete(self, prompt, system=None, **kw):
            r = super().complete(prompt, system)
            r.text = r.text.replace("happened", "went differently")
            return r

    run(tmp_path, book, Other(), **{"recaps.keep_recaps": True})
    assert rows(book, "parts.tsv") == before


# -- name variants and shared lines (DCC run 2) -------------------------------

def test_a_cut_short_chat_handle_is_the_full_name():
    from epubkit.tools import dedupe_cast as D
    got = D.identities([(1, "", "Imani", ""), (2, "", "Imani C", ""),
                        (3, "", "Imani Carter", ""), (4, "", "Elle McGib", ""),
                        (5, "", "Elle McGibbons", ""), (6, "", "Elle", "")])
    assert len({got["Imani"], got["Imani C"], got["Imani Carter"]}) == 1
    assert len({got["Elle"], got["Elle McGib"], got["Elle McGibbons"]}) == 1
    # two surnames that both begin the short one: no telling which
    two = D.identities([(1, "", "Frank Q", ""), (2, "", "Frank Quinn", ""),
                        (3, "", "Frank Quartz", "")])
    assert two["Frank Q"] == "Frank Q"


def test_name_variants_are_one_person():
    assert SM.same_person("Miss Beatrice", "Beatrice")
    assert SM.same_person("Elle McGib", "Elle")
    assert SM.same_person("Imani C", "Imani Carter")
    assert SM.same_person("Chaco the Bard", "Chaco")
    assert not SM.same_person("Grace Bautista", "Daniel Bautista")
    assert not SM.same_person("Mrs. McGibbons", "Elle McGibbons")
    assert not SM.same_person("Imani Carter", "Imani Cole")


def test_the_memory_files_every_form_of_a_name_under_one_person(tmp_path):
    mem = SM.Memory.open(tmp_path, "S")
    mem.record_book(1, "One", "", {"Beatrice": "Carl's ex-girlfriend",
                                   "Imani C": "Carl's ally", "Elle": "a Frost Maiden"})
    mem.record_book(2, "Two", "", {"Miss Beatrice": "Donut's old owner",
                                   "Imani": "Carl's ally, with wings",
                                   "Elle McGib": "a level 17 Frost Maiden"})
    ppl = mem.data["people"]
    assert set(ppl) == {"Beatrice", "Imani C", "Elle"}
    assert ppl["Elle"]["by_book"]["2"] == "a level 17 Frost Maiden"
    view = mem.before(3)
    assert view.name_of("Elle McGib") == "Elle"
    assert view.tie("Miss Beatrice") == "Donut's old owner"
    # two remembered Franks: a lone "Frank" is neither
    mem.record_book(3, "Three", "", {"Frank Q": "a crawler", "Frank Smith": "a guard"})
    assert mem.before(4).name_of("Frank") == ""


def _passage(order, text, listed):
    return ((order, "c%d.xhtml" % order, "Ch", [(0, text)]), "summary", listed)


def test_a_book_uses_the_name_the_earlier_books_knew():
    got = [_passage(1, "Elle McGib froze the water near Carl.",
                    [("Elle McGib", "met", "a crawler", "Carl's ally, a Frost Maiden")])]
    rows = R._people_rows(got, "Carl", None, settled=True,
                          series_name=lambda n, exact=False:
                          "Elle" if n.startswith("Elle") and not exact else "")
    assert {r[3] for r in rows} == {"Elle"}


def test_one_line_copied_onto_two_people_tells_neither():
    line = "Carl's acquaintance, a crawler who secured the plane"
    got = [_passage(1, "Louis and Firas waved at Carl.",
                    [("Louis", "met", "a crawler", line), ("Firas", "met", "a crawler", line)])]
    rows = R._people_rows(got, "Carl", None, settled=True)
    assert line not in {r[6] for r in rows}
    assert {r[3] for r in rows} == {"Louis", "Firas"}


def test_a_moment_tacked_onto_a_role_is_cut():
    assert R._drop_scene("Carl's acquaintance, awake and hugging Britney at the top "
                         "of the stairs") == ("Carl's acquaintance", True)
    assert R._drop_scene("Carl's friend, kind and caring") == \
        ("Carl's friend, kind and caring", False)


# -- run 3 (DCC, names merged) ------------------------------------------------

def test_a_name_the_memory_holds_joins_its_person_even_beside_them():
    """Book 4 listed "Princess Donut" beside "Donut"; the memory knew the
    alias, so they are one."""
    mem = SM.Memory.open(None, "S")
    mem.data["people"] = {"Donut": {"aliases": ["Princess Donut"], "fixed": "",
                                    "by_book": {"1": "Carl's cat"}}}
    view = mem.before(2)
    got = [_passage(1, "Donut and Princess Donut and Carl.",
                    [("Donut", "met", "a cat", "Carl's cat, angry"),
                     ("Princess Donut", "met", "a cat", "Carl's cat, royal")])]
    rows = R._people_rows(got, "Carl", None, settled=True, series_name=view.name_of)
    assert {r[3] for r in rows} == {"Donut"}


def test_ranks_do_not_split_a_person():
    from epubkit.tools import dedupe_cast as D
    got = D.identities([(1, "", "Imogen", ""), (2, "", "Princess Imogen", ""),
                        (3, "", "Queen Imogen", ""), (4, "", "High Elf King Finian", ""),
                        (5, "", "King Finian", ""), (6, "", "Crawler Bobby", ""),
                        (7, "", "Bobby", "")])
    assert len({got["Imogen"], got["Princess Imogen"], got["Queen Imogen"]}) == 1
    assert got["High Elf King Finian"] == got["King Finian"]
    assert got["Crawler Bobby"] == got["Bobby"]
    assert SM.same_person("Empress D'Nadia", "Princess D'Nadia")


def test_someone_elses_remembered_line_is_not_theirs():
    fixed = "Carl's cat and his partner in the dungeon"
    got = [_passage(1, "Ferdinand sat with Carl.",
                    [("Ferdinand", "met", "a cat", fixed)])]
    rows = R._people_rows(got, "Carl", None, settled=True,
                          series_lines={fixed: "Donut"})
    assert fixed not in {r[6] for r in rows}


def test_the_memory_keeps_no_levels_and_no_lone_deaths():
    lines = ["Carl's guide", "Carl's changeling guide", "Carl's guide and trainer",
             "Carl's deceased guide"]
    assert R._lasting("Carl's deceased changeling guide and trainer", lines) == \
        "Carl's changeling guide and trainer"
    assert R._lasting("Carl's companion, a level nine doppelganger", lines) == \
        "Carl's companion, a doppelganger"
    assert R._lasting("a level 9 angel", lines) == "an angel"
    dead = ["killed by Quetzalcoatlus", "dead on the bridge", "a principal"]
    assert "killed" in R._lasting("a principal killed by Quetzalcoatlus", dead)


def test_a_known_tie_is_not_forgotten_by_a_later_passage():
    """Imani C ended book 3 "not yet connected to Carl" though the book had
    already put her on his team."""
    got = [_passage(1, "Imani fought beside Carl.",
                    [("Imani", "met", "a crawler", "Carl's ally on his team")]),
           _passage(2, "Imani C waited.",
                    [("Imani C", "met", "a crawler", "not yet connected to Carl; a crawler")])]
    rows = R._people_rows(got, "Carl", None, settled=True)
    assert rows[-1][6] == "Carl's ally on his team"
