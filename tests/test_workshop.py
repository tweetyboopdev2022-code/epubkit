"""What the Grimmory Workshop sees of a run, and how it steers one."""
from __future__ import annotations

import json
import logging

import pytest
from test_cast import BOOK, CH1, CH2, KINDS, Model, dungeon
from test_pipeline import cfg_for

from epubkit.pipeline import Pipeline
from epubkit.stages.base import Stage, StageResult
from epubkit.workshop import Control, Gated, Status, StepSkipped, Stopped


def status_of(path):
    return json.loads(path.read_text())


class Slow(Stage):
    """A step that asks a model three times, as recaps do many times."""
    name = "slow"

    def run(self, ctx):
        for i in range(3):
            ctx.report("passage %d/3" % (i + 1))
            ctx.provider.complete("hello %d" % i)
            ctx.tell("wrote passage %d" % (i + 1))
        return StageResult()


class Echo:
    """Counts the step's own questions; the shelf question at the end is not one."""

    def __init__(self):
        self.calls = 0

    def complete(self, prompt, system=None, **kw):
        self.calls += prompt.startswith("hello")
        return type("R", (), {"text": "ok"})()


def test_the_status_file_shows_each_step_and_how_far_it_got(library, tmp_path):
    st = tmp_path / "status.json"
    status = Status(st)
    cfg = cfg_for(library, tmp_path / "out", **{"run.stages": ("ingest", "repair")})
    p = Pipeline(cfg, logging.getLogger("t"), Echo(), status)
    p.stages.append(Slow())
    p.run()
    got = status_of(st)
    assert got["state"] == "finished"
    book = next(iter(got["books"].values()))
    assert [s["name"] for s in book["steps"]] == ["ingest", "repair", "slow"]
    assert all(s["state"] == "done" for s in book["steps"]) and book["state"] == "done"
    assert book["part"] == [3, 3]
    assert "wrote passage 3" in [f["text"] for f in got["feed"]]


def test_a_pause_holds_the_next_model_call_until_run(tmp_path):
    ctl = tmp_path / "control.json"
    ctl.write_text('{"state": "pause"}')
    st = Status(tmp_path / "status.json")
    seen = []

    def sleep(_s):
        # the Workshop looks at the page while it waits, then presses Resume
        seen.append(status_of(tmp_path / "status.json")["state"])
        ctl.write_text('{"state": "run"}')

    model = Echo()
    gated = Gated(model, Control(ctl, st, sleep=sleep))
    gated.complete("hello")
    assert seen == ["paused"] and model.calls == 1
    assert status_of(tmp_path / "status.json")["state"] == "running"


def test_stop_ends_the_run_and_no_more_is_asked(tmp_path):
    ctl = tmp_path / "control.json"
    ctl.write_text('{"state": "stop"}')
    model = Echo()
    with pytest.raises(Stopped):
        Gated(model, Control(ctl)).complete("hello")
    assert model.calls == 0


def test_a_step_can_be_skipped_before_it_starts_or_while_it_runs(library, tmp_path):
    ctl = tmp_path / "control.json"
    ctl.write_text('{"state": "run", "skip": ["repair"]}')
    st = tmp_path / "status.json"
    cfg = cfg_for(library, tmp_path / "out", **{"run.stages": ("ingest", "repair")})
    class SkipMidway(Echo):
        def complete(self, prompt, system=None, **kw):
            super().complete(prompt)
            if prompt.startswith("hello"):
                    ctl.write_text('{"state": "run", "skip": ["repair", "slow"]}')
            return type("R", (), {"text": "ok"})()

    model = SkipMidway()
    p = Pipeline(cfg, logging.getLogger("t"), model, Status(st), control=Control(ctl))
    p.stages.append(Slow())
    state = p.run()
    assert state.counts().get("done") == 2          # the books still go through
    book = next(iter(status_of(st)["books"].values()))
    by = {s["name"]: s["state"] for s in book["steps"]}
    assert by == {"ingest": "done", "repair": "skipped", "slow": "skipped"}
    assert model.calls == 1                         # the first book's first passage only


def test_without_files_nothing_changes(tmp_path):
    c = Control()
    c.step = "recaps"
    c.check()
    assert not c.skips("recaps")
    with pytest.raises(StepSkipped):
        bad = tmp_path / "c.json"
        bad.write_text('{"skip": "recaps"}')
        c2 = Control(bad)
        c2.step = "recaps"
        c2.check()


def test_the_feed_shows_each_person_as_their_entry_is_written(tmp_path):
    from test_recaps import run
    book = dungeon(tmp_path, [CH1, CH2])
    feed = []
    import epubkit.stages.base as B
    orig = B.Context.tell
    B.Context.tell = lambda self, text: feed.append(text)
    try:
        run(tmp_path, book, Model(KINDS, {"Mordecai": ["A game guide in the guild hall."]}), **BOOK)
    finally:
        B.Context.tell = orig
    assert "Mordecai: A game guide in the guild hall." in feed
