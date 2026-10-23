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
