"""The orchestrator: find the books, run the stages, record what happened.

One book's failure never ends the batch by default. Each book is independent,
its outcome is written the moment it is known, and the finished file only
reaches the output folder once every stage it ran has agreed to it.
"""
from __future__ import annotations

import shutil
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from . import legacy
from .config import Config
from .stages import report as report_stage
from .stages.base import Context
from .stages.convert import ConvertStage
from .stages.ingest import IngestStage
from .stages.kepub import KepubStage
from .stages.metadata import MetadataStage
from .stages.recaps import RecapStage
from .stages.repair import RepairStage
from .stages.validate import ValidateStage
from .state import STATE_NAME, RunState, fingerprint

STAGE_CLASSES = {
    "ingest": IngestStage,
    "convert": ConvertStage,
    "metadata": MetadataStage,
    "repair": RepairStage,
    "kepub": KepubStage,
    "validate": ValidateStage,
    "recaps": RecapStage,
}

BOOK_SUFFIXES = (".epub", ".pdf", ".mobi", ".azw", ".azw3", ".fb2", ".zip")


def find_books(root: Path, exclude=()) -> list[Path]:
    """Books under `root`, skipping anything inside an excluded directory.

    The exclusions matter more than they look. Putting the library inside the
    folder of downloads is an entirely reasonable thing to want, and without
    this the run walks into its own output, treats every finished book as a
    new arrival, and converts what it already converted - growing each time it
    runs.
    """
    blocked = []
    for d in exclude:
        if not d:
            continue
        try:
            blocked.append(Path(d).resolve())
        except OSError:
            continue

    def inside_excluded(path: Path) -> bool:
        try:
            resolved = path.resolve()
        except OSError:
            return False
        return any(resolved == b or b in resolved.parents for b in blocked)

    out = []
    for p in sorted(root.rglob("*")):
        if not p.is_file() or p.name.startswith("._") or p.name.startswith("."):
            continue
        if any(part.startswith(".") for part in p.relative_to(root).parts[:-1]):
            continue
        if not p.name.lower().endswith(BOOK_SUFFIXES):
            continue
        if inside_excluded(p):
            continue
        out.append(p)
    return out


class Pipeline:
    def __init__(self, config: Config, log, provider=None):
        self.config = config
        self.log = log
        self.provider = provider
        self.stages = [STAGE_CLASSES[n]() for n in config.run.stages
                       if n in STAGE_CLASSES]

    def run(self) -> RunState:
        cfg = self.config
        out = cfg.paths.output
        work_root = cfg.paths.work
        out.mkdir(parents=True, exist_ok=True)
        work_root.mkdir(parents=True, exist_ok=True)

        legacy.adopt_state(work_root, STATE_NAME)
        state = RunState(work_root / STATE_NAME)
        books = find_books(cfg.paths.source,
                           exclude=(out, work_root, cfg.paths.backup))
        self.log.info("%d book(s) found in %s", len(books), cfg.paths.source)
        if _is_inside(out, cfg.paths.source):
            self.log.info("(the library is inside the source folder, so what "
                          "is already in %s is left out)", out.name)

        todo = [b for b in books if not state.should_skip(b, cfg.run.resume)]
        todo_set = set(todo)
        for b in books:
            if b not in todo_set:
                state.record(b)
                state.skipped_now.add(str(b))
        if len(todo) != len(books):
            self.log.info("%d already done and unchanged, %d to process",
                          len(books) - len(todo), len(todo))

        started = time.time()
        if cfg.run.dry_run:
            for b in todo:
                self.log.info("would process %s", b.name)
            return state

        workers = max(1, cfg.run.workers)
        if workers == 1:
            for b in todo:
                self._one(b, state)
        else:
            with ThreadPoolExecutor(max_workers=workers) as pool:
                futures = {pool.submit(self._one, b, state): b for b in todo}
                for f in as_completed(futures):
                    exc = f.exception()
                    if exc and not cfg.run.continue_on_error:
                        raise exc

        state.meta["elapsed"] = time.time() - started
        state.save()
        path = report_stage.write(state, out, time.time() - started)
        self.log.info("report written to %s", path)
        return state

    # ------------------------------------------------------------------

    def _one(self, source: Path, state: RunState) -> None:
        cfg = self.config
        rec = state.record(source)
        rec.notes = []
        rec.stages = {}
        started = time.time()
        work = cfg.paths.work / _slug(source.name)

        try:
            work.mkdir(parents=True, exist_ok=True)
            ctx = Context(config=cfg, record=rec, path=source, work=work,
                          log=self.log, provider=self.provider)

            for stage in self.stages:
                if not stage.applies(ctx):
                    rec.mark(stage.name, "skipped")
                    continue
                result = stage.run(ctx)
                rec.mark(stage.name, "ok" if result.ok else "failed")
                if result.path:
                    ctx.path = result.path
                if not result.ok:
                    rec.status = "failed"
                    rec.message = result.message
                    self.log.warning("%-52s %s", source.name[:52], result.message)
                    return
                if result.stop:
                    rec.status = "skipped"
                    rec.message = result.message
                    return

            final = cfg.paths.output / ctx.path.name
            final.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(ctx.path, final)
            rec.output = str(final)
            rec.status = "done"
            rec.fingerprint = fingerprint(source)
            self.log.info("%-52s ok%s", source.name[:52],
                          "  (%s)" % rec.message if rec.message else "")
        except Exception as exc:                            # noqa: BLE001
            rec.status = "failed"
            rec.message = "%s: %s" % (type(exc).__name__, exc)
            self.log.warning("%-52s %s", source.name[:52], rec.message)
            if not cfg.run.continue_on_error:
                raise
        finally:
            rec.seconds = time.time() - started
            state.update(rec)
            if not cfg.run.verbose:
                shutil.rmtree(work, ignore_errors=True)


def _is_inside(inner: Path, outer: Path) -> bool:
    try:
        inner, outer = inner.resolve(), outer.resolve()
    except OSError:
        return False
    return outer in inner.parents


def _slug(name: str) -> str:
    import re
    return re.sub(r"[^A-Za-z0-9._-]+", "_", name)[:120]
