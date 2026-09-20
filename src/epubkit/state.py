"""Per-book run state, written to disk as it goes.

A library run is long. It gets interrupted - a laptop sleeps, a shell call is
reaped, a model goes away mid-batch. So every book's outcome is recorded the
moment it is known, and re-running the same command picks up where it stopped
instead of starting over.

State is keyed by the source file's path and content fingerprint. If the
source file changes, its recorded result no longer applies and the book is
processed again; if it has not changed, a finished book is skipped.
"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

STATE_NAME = "epubkit-state.json"
SCHEMA = 1


def fingerprint(path: Path) -> str:
    """Cheap but honest: size plus a hash of the head and tail.

    Hashing a 79 MB comic in full, for every book, on every resume, costs more
    than it is worth. Size plus both ends catches a re-download, a truncation
    and an in-place edit, which is what this needs to detect.
    """
    st = path.stat()
    h = hashlib.sha256()
    h.update(str(st.st_size).encode())
    with open(path, "rb") as fh:
        h.update(fh.read(65536))
        if st.st_size > 131072:
            fh.seek(-65536, os.SEEK_END)
            h.update(fh.read(65536))
    return h.hexdigest()[:32]


@dataclass
class BookRecord:
    source: str
    fingerprint: str = ""
    status: str = "pending"      # pending|done|skipped|failed
    output: str | None = None
    title: str | None = None
    authors: str | None = None
    series: str | None = None
    series_index: float | None = None
    format_in: str | None = None
    stages: dict[str, str] = field(default_factory=dict)
    errors_before: int | None = None
    errors_after: int | None = None
    recaps: int | None = None
    notes: list[str] = field(default_factory=list)
    #: structural problems verify_epubs found, separate from EPUBCheck's
    structural: list[str] = field(default_factory=list)
    #: titles of the books inside, when this file holds more than one
    omnibus: list[str] = field(default_factory=list)
    #: tool -> ran | skipped: <why it did not apply>. A step that quietly
    #: does nothing is the failure mode this exists to make visible.
    tools: dict[str, str] = field(default_factory=dict)
    message: str = ""
    seconds: float = 0.0

    def mark(self, stage: str, outcome: str) -> None:
        self.stages[stage] = outcome

    def ran(self, tool: str, detail: str = "ran") -> None:
        self.tools[tool] = detail

    def not_applicable(self, tool: str, why: str) -> None:
        self.tools[tool] = "n/a: " + why


class RunState:
    """A json file of BookRecords, safe to write from several threads."""

    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.Lock()
        self.started = time.time()
        self.books: dict[str, BookRecord] = {}
        #: books this run left alone because they were already finished. Kept
        #: out of BookRecord.status on purpose: overwriting a book's recorded
        #: "done" with "skipped" would make the NEXT resume reprocess it.
        self.skipped_now: set[str] = set()
        self.meta: dict[str, Any] = {"schema": SCHEMA}
        if path.exists():
            self._load()

    def _load(self) -> None:
        try:
            raw = json.loads(self.path.read_text())
        except (OSError, json.JSONDecodeError):
            # A corrupt state file must not end the run - the worst case is
            # that finished work is redone, which is safe.
            return
        if raw.get("schema") != SCHEMA:
            return
        self.meta = {k: v for k, v in raw.items() if k != "books"}
        for key, rec in (raw.get("books") or {}).items():
            known = BookRecord.__dataclass_fields__                 # type: ignore[attr-defined]
            self.books[key] = BookRecord(**{k: v for k, v in rec.items() if k in known})

    def save(self) -> None:
        with self._lock:
            self._save_locked()

    def _save_locked(self) -> None:
        payload = dict(self.meta)
        payload["schema"] = SCHEMA
        payload["updated"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        payload["books"] = {k: asdict(v) for k, v in self.books.items()}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # written via a temp file in the same directory so an interrupted save
        # cannot leave a half-written state behind
        fd, tmp = tempfile.mkstemp(dir=str(self.path.parent), suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as fh:
                json.dump(payload, fh, indent=1)
            os.replace(tmp, self.path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    # -- per book --------------------------------------------------------

    def record(self, source: Path) -> BookRecord:
        key = str(source)
        rec = self.books.get(key)
        if rec is None:
            rec = BookRecord(source=key)
            self.books[key] = rec
        return rec

    def should_skip(self, source: Path, resume: bool) -> bool:
        if not resume:
            return False
        rec = self.books.get(str(source))
        if not rec or rec.status != "done":
            return False
        try:
            return rec.fingerprint == fingerprint(source)
        except OSError:
            return False

    def update(self, rec: BookRecord) -> None:
        with self._lock:
            self.books[rec.source] = rec
            self._save_locked()

    # -- summary ---------------------------------------------------------

    def counts(self) -> dict[str, int]:
        """What this run did, not what the state file has accumulated."""
        out: dict[str, int] = {}
        for key, rec in self.books.items():
            status = "skipped" if key in self.skipped_now else rec.status
            out[status] = out.get(status, 0) + 1
        return out

    def failed(self) -> list[BookRecord]:
        return [r for r in self.books.values() if r.status == "failed"]
