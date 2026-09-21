"""Kids and Teens only on a real say-so; Adult otherwise."""
from __future__ import annotations

from pathlib import Path

from epubkit.library_hint import ADULT, KIDS, TEENS, recommend
from epubkit.stages.report import _library_section
from epubkit.state import BookRecord


def test_a_book_that_says_young_adult_goes_to_teens():
    s = recommend(["Young adult fiction", "Dystopian"], None)
    assert s.library == TEENS and s.confidence in ("medium", "high")
    assert "young adult" in s.reasons[0]


def test_children_and_middle_grade_go_to_kids():
    assert recommend(["Juvenile Fiction", "Children's stories"]).library == KIDS
    assert recommend([], "A middle grade adventure for ages 8-12.").library == KIDS
    assert recommend(["Picture books"]).library == KIDS


def test_an_adult_thriller_is_adult_and_fiction():
    s = recommend(["Thrillers", "Suspense fiction", "Murder"], "A detective hunts a killer.")
    assert s.library == ADULT and s.folder == "Fiction"


def test_nonfiction_is_recognised():
    s = recommend(["Biography & Autobiography", "Astronauts"])
    assert s.library == ADULT and s.folder == "Non-fiction"


def test_nothing_to_go_on_defaults_to_adult_and_says_so():
    s = recommend([], None)
    assert s.library == ADULT and s.confidence == "low"
    assert "default" in s.reasons[0]


def test_conflicting_labels_choose_the_older_shelf_and_flag_it():
    s = recommend(["Juvenile Fiction", "Young Adult Fiction"])
    assert s.library == TEENS and s.confidence == "low"


def test_the_word_adult_alone_does_not_make_a_book_teens():
    assert recommend(["Adult fiction", "Crime"]).library == ADULT


def test_a_blurb_that_merely_mentions_young_people_stays_adult():
    """Adult novels are full of teenagers and children's authors."""
    for blurb in ("A teenage girl vanishes; decades later her sister remembers.",
                  "A children's author with a dark secret returns to her town.",
                  "The killer preyed on young adults and children."):
        assert recommend(["Thrillers"], blurb).library == ADULT, blurb
    assert recommend([], "A middle-grade adventure about a haunted lighthouse.").library == KIDS


def test_the_report_groups_books_and_flags_the_unsure_ones(tmp_path: Path):
    def rec(title, shelf, conf, why=""):
        return BookRecord(source=str(tmp_path / (title + ".epub")), status="done",
                          title=title, authors="A. Writer", shelf=shelf,
                          shelf_confidence=conf, shelf_why=why)
    text = "\n".join(_library_section([
        rec("Cat Tales", "Kids", "high", "says: picture books"),
        rec("Maybe Kids", "Kids", "medium", "says: juvenile fiction"),
        rec("Night Shift", "Adult › Fiction", "medium", "nothing marks it"),
        rec("No Idea", "Adult", "low", "no subjects"),
    ]))
    assert "## Suggested libraries" in text
    assert text.index("**Kids**") < text.index("**Adult**")
    assert "Cat Tales — A. Writer (says: picture books)\n" in text + "\n"
    assert "Maybe Kids" in text and "*check*" in text.split("Maybe Kids")[1].splitlines()[0]
    assert "*check*" in text.split("No Idea")[1].splitlines()[0]
    assert "*check*" not in text.split("Night Shift")[1].splitlines()[0]


def test_no_section_when_nothing_was_rated(tmp_path):
    assert _library_section([BookRecord(source=str(tmp_path / "a.epub"), status="done")]) == []


def test_the_suggestion_is_bold_in_each_books_entry(tmp_path):
    from epubkit.stages.report import write
    from epubkit.state import RunState
    state = RunState(tmp_path / "state.json")
    src = tmp_path / "a.epub"
    state.books[str(src)] = BookRecord(source=str(src), status="done", title="A Book",
                                       shelf="Teens", shelf_confidence="high",
                                       shelf_why="says: young adult fiction")
    text = write(state, tmp_path, 1.0).read_text()
    assert "  **suggested library: Teens**" in text
