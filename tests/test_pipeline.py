"""The orchestrator: isolation, ordering, and never writing a bad book."""
from __future__ import annotations

import logging
import zipfile

import pytest
from conftest import make_epub

from kobokit.config import Config
from kobokit.pipeline import Pipeline, find_books
from kobokit.stages.base import Stage, StageResult


def cfg_for(src, out, **over):
    o = {"source": src, "output": out, "run.workers": 1,
         "recaps.enabled": False,
         "run.stages": ("ingest", "repair", "validate")}
    o.update(over)
    return Config.load(None, o)


def run(cfg, stages=None):
    log = logging.getLogger("kobokit.test")
    p = Pipeline(cfg, log)
    if stages is not None:
        p.stages = stages
    return p.run()


# -- finding books ---------------------------------------------------------

def test_find_books_picks_up_every_supported_format(tmp_path):
    for name in ("a.epub", "b.PDF", "c.mobi", "d.azw3", "e.fb2"):
        (tmp_path / name).write_bytes(b"x")
    (tmp_path / "notes.txt").write_bytes(b"x")
    names = {p.name for p in find_books(tmp_path)}
    assert names == {"a.epub", "b.PDF", "c.mobi", "d.azw3", "e.fb2"}


def test_macos_resource_forks_are_not_books(tmp_path):
    (tmp_path / "._Real Book.epub").write_bytes(b"x")
    (tmp_path / "Real Book.epub").write_bytes(b"x")
    assert [p.name for p in find_books(tmp_path)] == ["Real Book.epub"]


def test_subfolders_are_searched(tmp_path):
    make_epub(tmp_path / "Series" / "One.epub")
    assert len(find_books(tmp_path)) == 1


# -- the run ---------------------------------------------------------------

def test_a_clean_run_writes_every_book(library, tmp_path):
    out = tmp_path / "lib"
    state = run(cfg_for(library, out))
    assert state.counts().get("done") == 2
    assert len(list(out.glob("*.epub"))) == 2


def test_the_original_is_never_modified(library, tmp_path):
    before = {p.name: p.read_bytes() for p in library.glob("*.epub")}
    run(cfg_for(library, tmp_path / "lib"))
    after = {p.name: p.read_bytes() for p in library.glob("*.epub")}
    assert before == after


def test_originals_are_backed_up(library, tmp_path):
    out = tmp_path / "lib"
    run(cfg_for(library, out))
    kept = {p.name for p in (out / ".kobokit-originals").glob("*.epub")}
    assert kept == {"Book One.epub", "Book Two.epub"}


def test_dry_run_touches_nothing(library, tmp_path):
    out = tmp_path / "lib"
    run(cfg_for(library, out, **{"run.dry_run": True}))
    assert list(out.glob("*.epub")) == []


class Boom(Stage):
    name = "boom"

    def run(self, ctx):
        if "One" in ctx.path.name:
            raise RuntimeError("this book breaks the tool")
        return StageResult()


def test_one_bad_book_does_not_take_the_batch_with_it(library, tmp_path):
    out = tmp_path / "lib"
    state = run(cfg_for(library, out), stages=[Boom()])
    assert state.counts() == {"failed": 1, "done": 1}
    assert [p.name for p in out.glob("*.epub")] == ["Book Two.epub"]
    assert "this book breaks the tool" in state.failed()[0].message


def test_continue_on_error_false_stops_the_run(library, tmp_path):
    cfg = cfg_for(library, tmp_path / "lib", **{"run.continue_on_error": False})
    with pytest.raises(RuntimeError):
        run(cfg, stages=[Boom()])


class Failing(Stage):
    name = "failing"

    def run(self, ctx):
        return StageResult(ok=False, message="not good enough")


def test_a_book_that_fails_a_stage_is_not_written(library, tmp_path):
    out = tmp_path / "lib"
    state = run(cfg_for(library, out), stages=[Failing()])
    assert list(out.glob("*.epub")) == []
    assert state.counts() == {"failed": 2}


def test_work_directory_is_cleaned_up(library, tmp_path):
    out = tmp_path / "lib"
    run(cfg_for(library, out))
    leftovers = [p for p in (out / ".kobokit-work").iterdir() if p.is_dir()]
    assert leftovers == []


def test_a_damaged_archive_fails_alone(library, tmp_path):
    (library / "Broken.epub").write_bytes(b"PK\x03\x04 and then nonsense")
    state = run(cfg_for(library, tmp_path / "lib"))
    assert state.counts().get("done") == 2
    assert [r.source for r in state.failed()][0].endswith("Broken.epub")


def test_the_written_book_still_opens(library, tmp_path):
    out = tmp_path / "lib"
    run(cfg_for(library, out))
    for p in out.glob("*.epub"):
        with zipfile.ZipFile(p) as z:
            assert z.testzip() is None
            assert z.read("mimetype") == b"application/epub+zip"


def test_workers_greater_than_one_still_processes_everything(library, tmp_path):
    out = tmp_path / "lib"
    state = run(cfg_for(library, out, **{"run.workers": 4}))
    assert state.counts().get("done") == 2


def test_books_are_renamed_from_their_metadata(library, tmp_path, monkeypatch):
    monkeypatch.setattr("kobokit.stages.metadata.MetadataStage._catalogue",
                        lambda self, *a: {})
    out = tmp_path / "lib"
    cfg = cfg_for(library, out, **{
        "run.stages": ("ingest", "metadata", "repair", "validate"),
        "metadata.use_ai_fallback": False})
    run(cfg)
    assert {p.name for p in out.glob("*.epub")} == {
        "Book One - Ada Tester.epub", "Book Two - Ada Tester.epub"}


def test_a_novel_comes_out_as_a_kepub(library, tmp_path):
    """koboSpans are what let a Kobo track pages and remember the position."""
    import zipfile as _z
    out = tmp_path / "lib"
    cfg = cfg_for(library, out, **{
        "run.stages": ("ingest", "repair", "kepub", "validate")})
    state = run(cfg)
    assert state.counts().get("done") == 2
    made = sorted(out.glob("*.kepub.epub"))
    assert len(made) == 2
    for p in made:
        with _z.ZipFile(p) as z:
            spans = sum(z.read(n).count(b"koboSpan") for n in z.namelist()
                        if n.endswith(".xhtml"))
        assert spans > 0


def test_kepub_can_be_turned_off(library, tmp_path):
    out = tmp_path / "lib"
    cfg = cfg_for(library, out, **{"convert.kepub": False,
                                   "run.stages": ("ingest", "kepub")})
    run(cfg)
    assert [p.suffix for p in out.glob("*.epub")] == [".epub", ".epub"]


def test_every_book_records_which_steps_did_not_apply(library, tmp_path):
    out = tmp_path / "lib"
    state = run(cfg_for(library, out, **{
        "run.stages": ("ingest", "repair", "validate")}))
    for rec in state.books.values():
        assert rec.tools, "no record of what ran for %s" % rec.source
        assert any(v.startswith("n/a: ") for v in rec.tools.values())


def test_the_library_inside_the_source_is_not_re_ingested(tmp_path):
    """Her real layout: output = <source>/Ready for Kobo. Without excluding
    it the run walks into its own output, treats every finished book as a new
    arrival, and grows the batch every time it runs."""
    src = tmp_path / "Books"
    make_epub(src / "One.epub", "One")
    out = src / "Ready for Kobo"
    out.mkdir(parents=True)
    make_epub(out / "One - Author.kepub.epub", "One")      # a previous run
    make_epub(out / ".kobokit-originals" / "One.epub", "One")

    found = [p.name for p in find_books(src, exclude=(out,))]
    assert found == ["One.epub"]


def test_a_run_into_a_nested_library_stays_the_same_size(tmp_path):
    src = tmp_path / "Books"
    make_epub(src / "One.epub", "One")
    make_epub(src / "Two.epub", "Two")
    cfg = cfg_for(src, src / "Ready for Kobo")

    first = run(cfg)
    assert first.counts().get("done") == 2

    second = run(cfg_for(src, src / "Ready for Kobo", **{"run.resume": False}))
    assert len(second.books) == 2, "the run picked up its own output"


def test_hidden_directories_are_skipped(tmp_path):
    make_epub(tmp_path / "Real.epub")
    make_epub(tmp_path / ".kobokit-work" / "scratch.epub")
    assert [p.name for p in find_books(tmp_path)] == ["Real.epub"]
