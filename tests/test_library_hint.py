"""Kids and Teens only on a real say-so; Adult otherwise."""
from __future__ import annotations

from pathlib import Path

from epubkit.library_hint import ADULT, KIDS, TEENS, recommend
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


def test_series_index_formats_whatever_type_it_arrives_as():
    from epubkit.stages.report import _index_text
    assert _index_text("1") == " #1"
    assert _index_text(1) == " #1"
    assert _index_text(1.5) == " #1.5"
    assert _index_text("2.0") == " #2"
    assert _index_text("Book 3") == " #Book 3"
    assert _index_text(None) == ""
    assert _index_text("") == ""
