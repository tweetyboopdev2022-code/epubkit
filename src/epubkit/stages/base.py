"""What a stage is, and the shared context it operates on."""
from __future__ import annotations

import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..config import Config
from ..state import BookRecord


@dataclass
class Context:
    """Everything a stage needs, and the one book it is working on."""

    config: Config
    record: BookRecord
    #: the file as it currently stands - each stage may replace this
    path: Path
    work: Path
    log: Any
    provider: Any = None
    meta: dict[str, Any] = field(default_factory=dict)
    #: set by the pipeline so a long stage can say where it has got to
    step: Any = None
    #: set by the Workshop: a feed of what was just written, shown live
    said: Any = None

    def report(self, text: str) -> None:
        """Tell the live display what this stage is doing, e.g. 'chapter 4/22'."""
        if self.step:
            self.step(text)

    def note(self, text: str) -> None:
        self.record.notes.append(text)
        self.log.debug("  %s", text)
        self.tell(text)

    def tell(self, text: str) -> None:
        """Show a line in the Workshop's live feed; nothing anywhere else."""
        if self.said:
            self.said(text)


@dataclass
class StageResult:
    ok: bool = True
    #: set when the stage produced a new file for later stages to work on
    path: Path | None = None
    message: str = ""
    #: True when the stage decided this book should not continue, without
    #: that being an error - an unconvertible format, say
    stop: bool = False
    #: set by a stage that split this file into several books. The pipeline
    #: then runs each one through the stages that come after it.
    parts: list[Path] | None = None


class Stage:
    name = "stage"

    def applies(self, ctx: Context) -> bool:      # noqa: ARG002
        return True

    def run(self, ctx: Context) -> StageResult:
        raise NotImplementedError


def run_tool(module: str, args: list[str], timeout: int = 900,
             cwd: Path | None = None) -> subprocess.CompletedProcess:
    """Run one of the bundled tools as its own process.

    They are run rather than imported on purpose. Each was written as a
    standalone script, several use module-level state, and one book that
    crashes a tool should not take the rest of the batch with it. The cost is
    a process launch per step, which is nothing beside EPUBCheck.
    """
    cmd = [sys.executable, "-m", "epubkit.tools." + module, *args]
    return subprocess.run(cmd, capture_output=True, text=True,
                          timeout=timeout, cwd=str(cwd) if cwd else None)
