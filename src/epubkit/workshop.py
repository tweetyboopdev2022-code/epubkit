"""What the Grimmory Workshop needs from a run: to see it and to steer it.

The Workshop runs epubkit on one book at a time, on the same PC people game
on, and shows what is happening on a page. Two small files connect them:

- the status file, written by epubkit, read by the Workshop: which step,
  how far through it, what was just written, all as JSON, rewritten whole
  on each change so a reader never sees half of it;
- the control file, written by the Workshop, read by epubkit before every
  model call and between steps::

      {"state": "run" | "pause" | "stop", "skip": "recaps"}

  "pause" holds the run before its next model call, however long it takes,
  and "run" lets it go on from exactly there. "stop" ends it; what the model
  already answered is kept, so the next run picks up where this one stopped.
  "skip" names a step to give up on now, or before it starts.

Neither file is needed: without them a run behaves as it always has.
"""
from __future__ import annotations

import json
import os
import re
import tempfile
import threading
import time
from pathlib import Path

from .progress import Progress

FEED = 30                           # lines of "what just happened" kept
WAIT = 2.0                          # seconds between looks while paused


class Stopped(BaseException):
    """The Workshop asked the run to stop. A BaseException, so the many
    `except Exception` guards around model calls let it through."""


class StepSkipped(BaseException):
    """The Workshop asked to skip the step being worked on."""

    def __init__(self, step: str):
        super().__init__(step)
        self.step = step


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False)
    os.replace(tmp, path)


_PART = re.compile(r"(?:passage|chapter) (\d+)/(\d+)")
_WHO = re.compile(r"who's who: (\d+)/(\d+)")


class Status(Progress):
    """The live display, written to a file instead of a terminal.

    It takes the pipeline's reports as the terminal display does, so it can
    stand in for it, and adds what a page wants: a feed of recent lines, the
    part reached within a step (passage 41 of 90), and whether it is paused.
    """

    def __init__(self, path, now=time.time):
        super().__init__(enabled=False, now=now)
        self.path = Path(path)
        self._wall = now
        self._data = {"state": "running", "started": now(), "updated": now(),
                      "books": {}, "feed": []}
        self._write_lock = threading.Lock()
        self._save()

    def _save(self) -> None:
        with self._write_lock:
            self._data["updated"] = self._wall()
            _write_json(self.path, self._data)

    def _book(self, key):
        return self._data["books"].setdefault(key, {"name": Path(key).name, "steps": []})

    def book_start(self, key, name):
        super().book_start(key, name)
        with self._lock:
            b = self._book(key)
            b.update(name=name, state="working", started=self._wall(), step="", detail="")
        self._save()

    def stage(self, key, stage, idx, n):
        super().stage(key, stage, idx, n)
        with self._lock:
            b = self._book(key)
            now = self._wall()
            for s in b["steps"]:
                if s["state"] == "working":
                    s.update(state="done", ended=now)
            b["steps"].append({"name": stage, "state": "working", "started": now,
                               "idx": idx, "of": n})
            b.update(step=stage, detail="", part=None)
        self._save()

    def step_result(self, key, stage, state, note=""):
        """How a step ended: done, skipped or failed, with one line on it."""
        with self._lock:
            b = self._book(key)
            for s in reversed(b["steps"]):
                if s["name"] == stage:
                    s.update(state=state, ended=self._wall())
                    if note:
                        s["note"] = note
                    break
            else:
                b["steps"].append({"name": stage, "state": state, "ended": self._wall(),
                                   "note": note})
        self._save()

    def detail(self, key, text):
        super().detail(key, text)
        with self._lock:
            b = self._book(key)
            b["detail"] = text
            m = _PART.search(text) or _WHO.search(text)
            if m:
                b["part"] = [int(m.group(1)), int(m.group(2))]
        self._save()

    def feed(self, text: str, key: str = "") -> None:
        with self._lock:
            self._data["feed"].append({"at": self._wall(), "book": Path(key).name if key else "",
                                       "text": text[:300]})
            del self._data["feed"][:-FEED]
        self._save()

    def book_end(self, key, failed=False):
        super().book_end(key, failed)
        with self._lock:
            b = self._book(key)
            for s in b["steps"]:
                if s["state"] == "working":
                    s.update(state="failed" if failed else "done", ended=self._wall())
            b.update(state="failed" if failed else "done", ended=self._wall())
        self._save()

    def set_state(self, state: str) -> None:
        if self._data["state"] != state:
            self._data["state"] = state
            self._save()

    def end(self):
        super().end()
        if self._data["state"] not in ("stopped",):
            self.set_state("finished")


class Control:
    """Reads the Workshop's control file. No file: always run, skip nothing."""

    def __init__(self, path=None, status: Status | None = None, sleep=time.sleep):
        self.path = Path(path) if path else None
        self.status = status
        self.sleep = sleep
        self.step = ""                  # the step being worked on, for "skip"

    def read(self) -> dict:
        if not self.path:
            return {}
        try:
            got = json.loads(self.path.read_text(encoding="utf-8"))
            return got if isinstance(got, dict) else {}
        except (OSError, ValueError):
            return {}

    def skips(self, step: str) -> bool:
        skip = self.read().get("skip") or ()
        return step in ([skip] if isinstance(skip, str) else skip)

    def check(self) -> None:
        """Before a model call: wait out a pause, stop, or skip this step."""
        paused = False
        while True:
            c = self.read()
            state = c.get("state", "run")
            if state == "stop":
                if self.status:
                    self.status.set_state("stopped")
                raise Stopped()
            if self.step and self.skips(self.step):
                raise StepSkipped(self.step)
            if state != "pause":
                break
            if not paused and self.status:
                self.status.set_state("paused")
            paused = True
            self.sleep(WAIT)
        if paused and self.status:
            self.status.set_state("running")


class Gated:
    """A provider that asks the control file before every model call."""

    def __init__(self, provider, control: Control):
        self._provider = provider
        self._control = control

    def complete(self, *a, **kw):
        self._control.check()
        return self._provider.complete(*a, **kw)

    def __getattr__(self, name):
        return getattr(self._provider, name)
