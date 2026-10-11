"""The orchestrator: find the books, run the stages, record what happened.

One book's failure never ends the batch by default. Each book is independent,
its outcome is written the moment it is known, and the finished file only
reaches the output folder once every stage it ran has agreed to it.
"""
from __future__ import annotations

import os
import shutil
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from . import legacy
from .config import Config
from .progress import Progress
from .stages import report as report_stage
from .stages.base import Context
from .stages.convert import ConvertStage
from .stages.ingest import IngestStage
from .stages.kepub import KepubStage
from .stages.metadata import MetadataStage
from .stages.recaps import RecapStage
from .stages.repair import RepairStage
from .stages.split import SplitStage
from .stages.validate import ValidateStage
from .state import STATE_NAME, RunState, fingerprint

STAGE_CLASSES = {
    "ingest": IngestStage,
    "convert": ConvertStage,
    "split": SplitStage,
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
    def __init__(self, config: Config, log, provider=None, progress=None):
        self.config = config
        self.log = log
        self.provider = provider
        self.progress = progress or Progress()        # disabled unless given a live one
        names = list(config.run.stages)
        # A config written before splitting existed lists no "split" stage.
        # The decision "one book or several?" belongs ahead of everything that
        # works per book, so it is put there unless splitting is switched off.
        if ("split" not in names and "metadata" in names
                and config.convert.split_omnibus != "off"):
            names.insert(names.index("metadata"), "split")
        self.stages = [STAGE_CLASSES[n]() for n in names if n in STAGE_CLASSES]

    def run(self) -> RunState:
        cfg = self.config
        out = cfg.paths.output
        work_root = cfg.paths.work
        out.mkdir(parents=True, exist_ok=True)
        work_root.mkdir(parents=True, exist_ok=True)

        legacy.adopt_state(work_root, STATE_NAME)
        state = RunState(work_root / STATE_NAME)
        try:
            os.listdir(cfg.paths.source)
        except PermissionError:
            # macOS gives each app its own permission for Downloads, Documents
            # and Desktop. Without it the folder simply looks empty, and the
            # run used to report "0 books" as if there were nothing to do.
            self.log.error(
                "cannot read %s - this app is not allowed into that folder. "
                "On a Mac: System Settings > Privacy & Security > Files and "
                "Folders (or Full Disk Access), and allow the app you ran "
                "epubkit from (Terminal, iTerm, or sshd for a remote session)."
                % cfg.paths.source)
            return RunState(work_root / STATE_NAME)
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

        if "metadata" in [s.name for s in self.stages] and todo:
            # Read every book's "Also by" page first, so a book can be placed
            # in its series from a list printed in a later book of the batch.
            from .tools import series_hint
            try:
                series_hint.harvest(todo, work_root / "series-lists.json")
            except Exception as exc:                        # noqa: BLE001
                self.log.debug("series lists not read up front: %s", exc)

        started = time.time()
        if cfg.run.dry_run:
            for b in todo:
                self.log.info("would process %s", b.name)
            return state

        workers = max(1, cfg.run.workers)
        self.progress.begin(len(todo), workers)
        try:
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
        finally:
            self.progress.end()

        state.meta["elapsed"] = time.time() - started
        state.save()
        rows = self._shelves(out, work_root)
        path = report_stage.write(state, out, time.time() - started, rows)
        self.log.info("report written to %s", path)
        return state

    def _shelves(self, out: Path, work_root: Path) -> list[dict]:
        """Last step: rate every finished book Kids / Teens / Adult / Cookbooks,
        then move each into the folder of that library."""
        if not self.config.metadata.suggest_shelves:
            return []
        from . import shelves
        self.log.info("choosing a library for each finished book...")
        try:
            rows = shelves.classify_library(
                out, self.provider, work_root / "shelves-cache.json",
                self.config.metadata.shelf_lookup, log=lambda m: self.log.info("  %s", m))
            if self.config.metadata.sort_into_libraries:
                rows = shelves.sort_library(out, rows, log=lambda m: self.log.info("  %s", m))
            return rows
        except Exception as exc:                                    # noqa: BLE001
            self.log.warning("could not suggest libraries: %s", str(exc)[:120])
            return []

    # ------------------------------------------------------------------

    def _one(self, source: Path, state: RunState) -> None:
        cfg = self.config
        rec = state.record(source)
        rec.notes = []
        rec.stages = {}
        rec.split_into = 0
        rec.omnibus = []
        # a retry starts clean: "85 errors - not written" from the last run
        # was printed next to "ok" when the retry succeeded
        rec.message = ""
        rec.errors_before = rec.errors_after = None
        started = time.time()
        work = cfg.paths.work / _slug(source.name)
        key = str(source)
        self.progress.book_start(key, source.name)

        try:
            # a work folder left by a run that was stopped part way would
            # otherwise feed stale files into this one
            if work.exists():
                shutil.rmtree(work, ignore_errors=True)
            work.mkdir(parents=True, exist_ok=True)
            ctx = Context(config=cfg, record=rec, path=source, work=work,
                          log=self.log, provider=self.provider,
                          step=lambda text: self.progress.detail(key, text))
            outcome = self._stages(ctx, source.name, key, 0)
            if outcome == "done":
                self._finish(ctx, rec)
                rec.fingerprint = fingerprint(source)
                self.log.info("%-52s ok%s", source.name[:52],
                              "  (%s)" % rec.message if rec.message else "")
            elif outcome == "split":
                self._parts(ctx, rec, state, source)
        except Exception as exc:                            # noqa: BLE001
            rec.status = "failed"
            rec.message = "%s: %s" % (type(exc).__name__, exc)
            self.log.warning("%-52s %s", source.name[:52], rec.message)
            if not cfg.run.continue_on_error:
                raise
        finally:
            self.progress.book_end(key, failed=rec.status == "failed")
            rec.seconds = time.time() - started
            state.update(rec)
            if not cfg.run.verbose:
                shutil.rmtree(work, ignore_errors=True)

    def _stages(self, ctx: Context, name: str, key: str, start: int) -> str:
        """Run the stages from `start`. done | failed | skipped | split."""
        rec = ctx.record
        for i, stage in enumerate(self.stages, 1):
            if i <= start:
                continue
            self.progress.stage(key, stage.name, i, len(self.stages))
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
                self.log.warning("%-52s %s", name[:52], result.message)
                return "failed"
            if result.parts:
                ctx.meta["parts"] = result.parts
                ctx.meta["resume_after"] = i
                return "split"
            if result.stop:
                rec.status = "skipped"
                rec.message = result.message
                return "skipped"
        return "done"

    def _finish(self, ctx: Context, rec) -> None:
        final = self.config.paths.output / ctx.path.name
        final.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ctx.path, final)
        rec.output = str(final)
        rec.status = "done"

    def _parts(self, ctx: Context, rec, state: RunState, source: Path) -> None:
        """The file held several books: each goes through the rest of the pipeline alone.

        Run one after another inside this book's worker, so its working
        folder outlives all of them.
        """
        parts = ctx.meta["parts"]
        after = ctx.meta["resume_after"]
        self.progress.extend(len(parts))
        bad = 0
        for n, part in enumerate(parts, 1):
            prec = state.record(part)
            prec.notes, prec.stages, prec.omnibus = [], {}, []
            prec.parent, prec.status, prec.message = str(source), "pending", ""
            key = str(part)
            started = time.time()
            self.progress.book_start(key, part.name)
            try:
                pctx = Context(config=self.config, record=prec, path=part,
                               work=ctx.work / ("part%02d" % n), log=self.log,
                               provider=self.provider,
                               step=lambda text, k=key: self.progress.detail(k, text))
                pctx.work.mkdir(parents=True, exist_ok=True)
                if self._stages(pctx, part.name, key, after) == "done":
                    self._finish(pctx, prec)
                    self.log.info("%-52s ok  (part %d of %d)", part.name[:52],
                                  n, len(parts))
            except Exception as exc:                        # noqa: BLE001
                prec.status = "failed"
                prec.message = "%s: %s" % (type(exc).__name__, exc)
                self.log.warning("%-52s %s", part.name[:52], prec.message)
            finally:
                bad += prec.status != "done"
                self.progress.book_end(key, failed=prec.status == "failed")
                prec.seconds = time.time() - started
                state.update(prec)
        if bad:
            rec.status, rec.message = "failed", "%d of %d parts were not written" % (bad, len(parts))
            self.log.warning("%-52s %s", source.name[:52], rec.message)
        else:
            rec.status, rec.output = "done", None
            rec.message = "split into %d books" % len(parts)
            rec.fingerprint = fingerprint(source)
            self.log.info("%-52s split into %d books", source.name[:52], len(parts))


def _is_inside(inner: Path, outer: Path) -> bool:
    try:
        inner, outer = inner.resolve(), outer.resolve()
    except OSError:
        return False
    return outer in inner.parents


def _slug(name: str) -> str:
    import re
    return re.sub(r"[^A-Za-z0-9._-]+", "_", name)[:120]
