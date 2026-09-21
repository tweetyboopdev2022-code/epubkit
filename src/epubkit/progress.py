"""A live display for `epubkit run`, so a long batch visibly is not stuck.

One summary line (bar, books finished, time, estimate) and one line per book
being worked on right now, naming the stage it is in and how long it has been
there. The slow stages - EPUBCheck, and above all recaps, which ask a model
about every chapter - show a spinner and a stage clock that keep moving, and
recaps also report which chapter they have reached.

Only drawn on a real terminal. Piped, redirected, `TERM=dumb`, or `--plain`
gets the ordinary one-line-per-book log, unchanged, so nothing that reads the
output has to care that this exists.
"""
from __future__ import annotations

import logging
import os
import shutil
import sys
import threading
import time
from contextlib import contextmanager

FRAMES_UNICODE = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
FRAMES_ASCII = "|/-\\"
MAX_ROWS = 6                     # book lines shown at once; the rest are counted


def wanted(stream, plain: bool = False, env=None) -> bool:
    """Draw only for a person looking at a terminal."""
    env = os.environ if env is None else env
    if plain or env.get("TERM") == "dumb":
        return False
    try:
        return bool(stream.isatty())
    except (AttributeError, ValueError):
        return False


def clock(seconds: float) -> str:
    s = max(0, int(seconds))
    h, rem = divmod(s, 3600)
    m, s = divmod(rem, 60)
    return "%d:%02d:%02d" % (h, m, s) if h else "%d:%02d" % (m, s)


def _terminal_width() -> int:
    """The terminal's width, or 80 when it will not say (some report 0)."""
    cols = shutil.get_terminal_size((80, 24)).columns
    return cols if cols >= 20 else 80


def _fit(text: str, width: int) -> str:
    return text if len(text) <= width else text[:max(0, width - 1)] + "…"


class Progress:
    """Thread-safe: the pipeline's worker threads report, one thread draws."""

    def __init__(self, stream=None, enabled: bool = False, interval: float = 0.1,
                 now=time.monotonic, unicode: bool | None = None):
        self.stream = stream or sys.stderr
        self.enabled = enabled
        self.interval = interval
        self._now = now
        if unicode is None:
            enc = (getattr(self.stream, "encoding", None) or "").lower()
            unicode = "utf" in enc
        self._frames = FRAMES_UNICODE if unicode else FRAMES_ASCII
        self._bar = ("█", "░") if unicode else ("#", "-")
        self._lock = threading.RLock()
        self._active: dict[str, dict] = {}
        self._total = 0
        self._finished = 0
        self._failed = 0
        self._durations: list[float] = []
        self._workers = 1
        self._t0 = 0.0
        self._drawn = 0                     # lines of our block currently on screen
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    @classmethod
    def for_stream(cls, stream, plain: bool = False) -> Progress:
        return cls(stream, enabled=wanted(stream, plain))

    # -- reported by the pipeline -----------------------------------------

    def begin(self, total: int, workers: int = 1) -> None:
        if not self.enabled:
            return
        with self._lock:
            self._total, self._workers = total, max(1, workers)
            self._t0 = self._now()
        self.stream.write("\x1b[?25l")                     # hide the cursor
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def book_start(self, key: str, name: str) -> None:
        with self._lock:
            self._active[key] = {"name": name, "stage": "starting", "idx": 0,
                                 "n": 0, "detail": "", "t0": self._now(),
                                 "s0": self._now()}

    def stage(self, key: str, stage: str, idx: int, n: int) -> None:
        with self._lock:
            a = self._active.get(key)
            if a:
                a.update(stage=stage, idx=idx, n=n, detail="", s0=self._now())

    def detail(self, key: str, text: str) -> None:
        with self._lock:
            a = self._active.get(key)
            if a:
                a["detail"] = text

    def book_end(self, key: str, failed: bool = False) -> None:
        with self._lock:
            a = self._active.pop(key, None)
            if a is None:
                return
            self._finished += 1
            self._failed += bool(failed)
            self._durations.append(self._now() - a["t0"])

    def end(self) -> None:
        if not self.enabled:
            return
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)
        with self._lock:
            self._erase()
        self.stream.write("\x1b[?25h")                     # cursor back
        self.stream.flush()

    # -- drawing ----------------------------------------------------------

    def lines(self, width: int) -> list[str]:
        """The block as text. Pure given the state, so it can be tested."""
        with self._lock:
            now = self._now()
            total = max(self._total, 1)
            filled = int(20 * self._finished / total)
            bar = self._bar[0] * filled + self._bar[1] * (20 - filled)
            head = "%s %d/%d books" % (bar, self._finished, self._total)
            if self._active:
                head += "  %d running" % len(self._active)
            if self._failed:
                head += "  %d failed" % self._failed
            head += "  %s" % clock(now - self._t0)
            if self._durations and self._finished < self._total:
                each = sum(self._durations) / len(self._durations)
                left = each * (self._total - self._finished) / self._workers
                head += "  ~%s left" % clock(left)
            out = [_fit(head, width - 1)]

            frame = self._frames[int(now / self.interval) % len(self._frames)]
            rows = sorted(self._active.values(), key=lambda a: a["t0"])
            for a in rows[:MAX_ROWS]:
                where = a["stage"]
                if a["n"]:
                    where += " (%d/%d)" % (a["idx"], a["n"])
                if a["detail"]:
                    where += "  " + a["detail"]
                tail = "  %s" % clock(now - a["s0"])
                room = max(10, width - 1 - len(where) - len(tail) - 4)
                out.append(_fit("  %s %-*s  %s%s" % (frame, min(room, 46),
                                                     _fit(a["name"], room),
                                                     where, tail), width - 1))
            if len(rows) > MAX_ROWS:
                out.append("  … and %d more" % (len(rows) - MAX_ROWS))
            return out

    def _erase(self) -> None:
        if self._drawn:
            self.stream.write("\x1b[%dF\x1b[J" % self._drawn)
            self._drawn = 0

    def _draw(self) -> None:
        block = self.lines(_terminal_width())
        with self._lock:
            self._erase()
            self.stream.write("\n".join(block) + "\n")
            self.stream.flush()
            self._drawn = len(block)

    def _loop(self) -> None:
        while not self._stop.wait(self.interval):
            try:
                self._draw()
            except (OSError, ValueError):
                return                     # the terminal went away; stop quietly

    @contextmanager
    def paused(self):
        """Clear the block while something else prints, so lines never tangle."""
        with self._lock:
            if self.enabled:
                self._erase()
            yield

    def attach(self, log: logging.Logger):
        """Route the logger through `paused`, and hand back an undo."""
        if not self.enabled:
            return lambda: None
        old = [h for h in log.handlers if isinstance(h, logging.StreamHandler)]
        for h in old:
            log.removeHandler(h)
        new = _PausedHandler(self, self.stream)
        new.setFormatter(old[0].formatter if old else logging.Formatter("%(message)s"))
        log.addHandler(new)

        def undo():
            log.removeHandler(new)
            for h in old:
                log.addHandler(h)
        return undo


class _PausedHandler(logging.StreamHandler):
    def __init__(self, progress: Progress, stream):
        super().__init__(stream)
        self._progress = progress

    def emit(self, record):
        with self._progress.paused():
            super().emit(record)
