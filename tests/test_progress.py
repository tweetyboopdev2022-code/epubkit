"""The live display: quiet when it should be, truthful when it is drawn."""
from __future__ import annotations

import io
import logging

from conftest import make_epub

from epubkit.config import Config
from epubkit.pipeline import Pipeline
from epubkit.progress import Progress, clock, wanted
from epubkit.stages.base import Stage, StageResult


class Tty(io.StringIO):
    encoding = "utf-8"

    def isatty(self):
        return True


class Ticker:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t


def live(**kw):
    t = Ticker()
    return Progress(Tty(), enabled=True, now=t, **kw), t


# -- when it draws at all --------------------------------------------------

def test_only_a_real_terminal_gets_the_display():
    assert wanted(Tty(), env={"TERM": "xterm"})
    assert not wanted(io.StringIO(), env={"TERM": "xterm"})          # piped
    assert not wanted(Tty(), env={"TERM": "dumb"})
    assert not wanted(Tty(), plain=True, env={"TERM": "xterm"})      # --plain


def test_a_disabled_display_writes_nothing():
    out = io.StringIO()
    p = Progress(out)
    p.begin(3)
    p.book_start("a", "a.epub")
    p.stage("a", "repair", 1, 3)
    p.book_end("a")
    p.end()
    assert out.getvalue() == ""


# -- what it says ----------------------------------------------------------

def test_clock_formats():
    assert clock(0) == "0:00"
    assert clock(75) == "1:15"
    assert clock(3725) == "1:02:05"


def test_it_names_the_book_the_stage_and_how_long_it_has_been_there():
    p, t = live()
    p._total, p._t0 = 4, t.t
    p.book_start("a", "Falling - T. J. Newman.epub")
    p.stage("a", "recaps", 5, 8)
    p.detail("a", "chapter 4/22")
    t.t += 75
    head, row = p.lines(100)
    assert "0/4 books" in head and "1 running" in head and "1:15" in head
    assert "Falling - T. J. Newman.epub" in row
    assert "recaps (5/8)" in row and "chapter 4/22" in row and "1:15" in row


def test_the_stage_clock_restarts_when_the_stage_changes():
    p, t = live()
    p._total, p._t0 = 1, t.t
    p.book_start("a", "a.epub")
    p.stage("a", "repair", 1, 3)
    t.t += 50
    p.stage("a", "validate", 2, 3)
    t.t += 7
    assert "0:07" in p.lines(100)[1]


def test_finished_books_count_and_give_an_estimate():
    p, t = live()
    p._total, p._t0, p._workers = 4, t.t, 2
    p.book_start("a", "a.epub")
    t.t += 60
    p.book_end("a")
    p.book_start("b", "b.epub")
    p.book_end("b", failed=True)
    head = p.lines(100)[0]
    assert "2/4 books" in head and "1 failed" in head
    assert "left" in head                                  # an estimate now exists


def test_no_estimate_before_the_first_book_finishes():
    p, t = live()
    p._total, p._t0 = 3, t.t
    p.book_start("a", "a.epub")
    assert "left" not in p.lines(100)[0]


def test_lines_never_exceed_the_terminal_width():
    p, t = live()
    p._total, p._t0 = 1, t.t
    p.book_start("a", "A Very Long Title Indeed - " * 6 + ".epub")
    p.stage("a", "recaps", 5, 8)
    p.detail("a", "chapter 12/40")
    for width in (40, 60, 120):
        assert all(len(line) < width for line in p.lines(width))


def test_only_a_handful_of_books_are_listed():
    p, t = live()
    p._total, p._t0 = 20, t.t
    for i in range(10):
        p.book_start(str(i), "book%d.epub" % i)
    lines = p.lines(100)
    assert len(lines) == 1 + 6 + 1 and "4 more" in lines[-1]


def test_ascii_terminals_get_ascii():
    p, t = live(unicode=False)
    p._total, p._t0 = 1, t.t
    p.book_start("a", "a.epub")
    assert all(ord(c) < 128 for line in p.lines(80) for c in line)


# -- not tangling with the log --------------------------------------------

def test_a_log_line_clears_the_block_first():
    p, t = live()
    p._total, p._t0 = 1, t.t
    p.book_start("a", "a.epub")
    p._draw()
    log = logging.getLogger("epubkit.test.progress")
    log.setLevel(logging.INFO)
    log.propagate = False
    h = logging.StreamHandler(p.stream)                    # give it one to replace
    log.addHandler(h)
    undo = p.attach(log)
    before = p.stream.getvalue()
    log.info("a.epub  ok")
    added = p.stream.getvalue()[len(before):]
    assert added.startswith("\x1b[2F\x1b[J") and "a.epub  ok" in added
    undo()
    assert h in log.handlers


# -- through the real pipeline --------------------------------------------

class Slow(Stage):
    name = "repair"

    def run(self, ctx):
        ctx.report("half way")
        return StageResult()


def test_the_pipeline_reports_every_book_and_stage(tmp_path):
    src, out = tmp_path / "in", tmp_path / "out"
    src.mkdir()
    make_epub(src / "one.epub")
    make_epub(src / "two.epub", title="Two")
    cfg = Config.load(None, {"source": src, "output": out, "run.workers": 1,
                             "recaps.enabled": False})
    seen = []

    class Spy(Progress):
        def book_start(self, key, name):
            seen.append(("start", name))
            super().book_start(key, name)

        def stage(self, key, stage, idx, n):
            seen.append(("stage", stage, idx, n))
            super().stage(key, stage, idx, n)

        def detail(self, key, text):
            seen.append(("detail", text))
            super().detail(key, text)

        def book_end(self, key, failed=False):
            seen.append(("end", failed))
            super().book_end(key, failed)

    p = Pipeline(cfg, logging.getLogger("epubkit.test"), progress=Spy())
    p.stages = [Slow()]
    state = p.run()
    assert state.counts() == {"done": 2}
    assert [s for s in seen if s[0] == "start"] == [("start", "one.epub"), ("start", "two.epub")]
    assert seen.count(("stage", "repair", 1, 1)) == 2
    assert seen.count(("detail", "half way")) == 2
    assert seen.count(("end", False)) == 2


def test_a_failing_book_is_reported_as_failed(tmp_path):
    src, out = tmp_path / "in", tmp_path / "out"
    src.mkdir()
    make_epub(src / "bad.epub")
    cfg = Config.load(None, {"source": src, "output": out, "run.workers": 1,
                             "recaps.enabled": False})

    class Boom(Stage):
        name = "repair"

        def run(self, ctx):
            return StageResult(ok=False, message="nope")

    ends = []

    class Spy(Progress):
        def book_end(self, key, failed=False):
            ends.append(failed)
            super().book_end(key, failed)

    p = Pipeline(cfg, logging.getLogger("epubkit.test"), progress=Spy())
    p.stages = [Boom()]
    p.run()
    assert ends == [True]


def test_a_terminal_that_reports_no_width_still_gets_a_readable_display(monkeypatch):
    """A pty can say 0 columns; every line then collapsed to an ellipsis."""
    import os

    from epubkit import progress
    monkeypatch.setattr(progress.shutil, "get_terminal_size",
                        lambda fallback=(80, 24): os.terminal_size((0, 0)))
    assert progress._terminal_width() == 80
    monkeypatch.setattr(progress.shutil, "get_terminal_size",
                        lambda fallback=(80, 24): os.terminal_size((132, 40)))
    assert progress._terminal_width() == 132
