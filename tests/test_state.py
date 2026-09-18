"""Run state: what resume promises, and what it must never do."""
from __future__ import annotations

import json

from kobokit.state import BookRecord, RunState, fingerprint


def test_fingerprint_changes_when_the_file_does(tmp_path):
    p = tmp_path / "b.epub"
    p.write_bytes(b"x" * 200000)
    first = fingerprint(p)
    assert fingerprint(p) == first          # stable across reads
    p.write_bytes(b"x" * 199999 + b"y")     # same-ish size, different tail
    assert fingerprint(p) != first


def test_truncation_is_caught(tmp_path):
    p = tmp_path / "b.epub"
    p.write_bytes(b"abc" * 100000)
    before = fingerprint(p)
    with open(p, "r+b") as fh:
        fh.truncate(150000)
    assert fingerprint(p) != before


def test_finished_book_is_skipped_and_stays_skipped(tmp_path):
    """The bug this guards: marking a done book 'skipped' on a resume run
    made the run after that one process it all over again."""
    book = tmp_path / "b.epub"
    book.write_bytes(b"content" * 5000)
    state = RunState(tmp_path / "state.json")
    rec = state.record(book)
    rec.status = "done"
    rec.fingerprint = fingerprint(book)
    state.update(rec)

    for _ in range(3):
        assert state.should_skip(book, resume=True) is True
        state.skipped_now.add(str(book))
        state.save()
        state = RunState(tmp_path / "state.json")     # as a fresh process would


def test_changed_source_is_processed_again(tmp_path):
    book = tmp_path / "b.epub"
    book.write_bytes(b"one" * 5000)
    state = RunState(tmp_path / "state.json")
    rec = state.record(book)
    rec.status = "done"
    rec.fingerprint = fingerprint(book)
    state.update(rec)
    book.write_bytes(b"two" * 5000)
    assert state.should_skip(book, resume=True) is False


def test_no_resume_flag_ignores_the_record(tmp_path):
    book = tmp_path / "b.epub"
    book.write_bytes(b"x" * 100)
    state = RunState(tmp_path / "state.json")
    rec = state.record(book)
    rec.status = "done"
    rec.fingerprint = fingerprint(book)
    state.update(rec)
    assert state.should_skip(book, resume=False) is False


def test_failed_books_are_not_skipped(tmp_path):
    book = tmp_path / "b.epub"
    book.write_bytes(b"x" * 100)
    state = RunState(tmp_path / "state.json")
    rec = state.record(book)
    rec.status = "failed"
    rec.fingerprint = fingerprint(book)
    state.update(rec)
    assert state.should_skip(book, resume=True) is False


def test_state_survives_a_round_trip(tmp_path):
    state = RunState(tmp_path / "state.json")
    rec = BookRecord(source="/x/y.epub", status="done", title="Y",
                     errors_before=40, errors_after=0, notes=["fixed the ncx"])
    state.update(rec)
    reloaded = RunState(tmp_path / "state.json")
    got = reloaded.books["/x/y.epub"]
    assert got.title == "Y" and got.errors_before == 40
    assert got.notes == ["fixed the ncx"]


def test_a_corrupt_state_file_does_not_end_the_run(tmp_path):
    p = tmp_path / "state.json"
    p.write_text("{not json at all")
    state = RunState(p)                 # must not raise
    assert state.books == {}


def test_unknown_fields_from_a_newer_version_are_ignored(tmp_path):
    p = tmp_path / "state.json"
    p.write_text(json.dumps({"schema": 1, "books": {
        "/a.epub": {"source": "/a.epub", "status": "done", "invented": 1}}}))
    assert RunState(p).books["/a.epub"].status == "done"


def test_counts_describe_this_run_not_the_whole_file(tmp_path):
    state = RunState(tmp_path / "state.json")
    for name in ("a", "b"):
        state.update(BookRecord(source=name, status="done"))
    state.skipped_now.add("a")
    assert state.counts() == {"done": 1, "skipped": 1}
