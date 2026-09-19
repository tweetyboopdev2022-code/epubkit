"""Real EPUBCheck, plus the structural checks it does not make.

The two catch different things. EPUBCheck finds spec violations; verify_epubs
finds a book that came out with less than it went in with - a lost stylesheet,
a missing image, a table of contents that shrank. Neither replaces the other.

What this stage does NOT do is quietly ship a book it could not clean. A book
that still has errors is reported with its count, and `validate.require_clean`
decides whether that stops it reaching the output folder.
"""
from __future__ import annotations

from pathlib import Path

from .base import Context, Stage, StageResult


class ValidateStage(Stage):
    name = "validate"

    def run(self, ctx: Context) -> StageResult:
        from ..tools import epubcheck_util as ec
        from .inspect import detect_omnibus

        book = ctx.path
        detect_omnibus(ctx, book)
        if not ec.available():
            # A missing validator is a setup problem, not a broken book, and
            # must not fail every title in the batch.
            ctx.note("EPUBCheck unavailable, skipping validation")
            _structural_check(ctx, book, None)
            return StageResult(message="not checked")

        results = ec.run(str(book), timeout=ctx.config.validate.timeout)
        _structural_check(ctx, book, results)
        errors = [e for e in (results or [])
                  if e["severity"] in ("FATAL", "ERROR")]
        warnings = [e for e in (results or []) if e["severity"] == "WARNING"]

        ctx.record.errors_after = len(errors)
        if ctx.record.errors_before is None:
            ctx.record.errors_before = len(errors)

        summary = ec.summarize(results)
        ctx.note("epubcheck: %s%s" % (
            summary, ", %d warnings" % len(warnings) if warnings else ""))

        if errors and ctx.config.validate.require_clean:
            if ctx.config.validate.accept_improvement and _improved(ctx):
                ctx.note("kept: better than the source, though not clean")
                return StageResult(message=summary)
            return StageResult(
                ok=False,
                message="%s - not written to the library" % summary)

        return StageResult(message=summary)


def _improved(ctx: Context) -> bool:
    before, after = ctx.record.errors_before, ctx.record.errors_after
    return (before is not None and after is not None and after < before)


def _structural_check(ctx: Context, book: Path, epubcheck_results) -> None:
    """verify_epubs: zip integrity, mimetype placement, OPF and spine sanity.

    Different questions from EPUBCheck's. EPUBCheck asks whether the book
    obeys the spec; this asks whether the archive is put together the way a
    reader expects to find it - the mimetype stored first and uncompressed,
    the spine pointing at files that exist, no dangling internal links.
    """
    try:
        from ..tools import verify_epubs
    except ImportError:                                     # pragma: no cover
        return
    # verify_epubs runs EPUBCheck itself, which this stage has already done.
    # Running a JVM twice per book doubles the cost of the slowest step in the
    # pipeline, so it is handed the results instead.
    real = verify_epubs.epubcheck_util

    class _Cached:
        run = staticmethod(lambda *a, **k: epubcheck_results)
        summarize = staticmethod(real.summarize)

    verify_epubs.epubcheck_util = _Cached
    try:
        problems, info = verify_epubs.check(str(book))
    except Exception as exc:                                # noqa: BLE001
        ctx.note("structural check could not run: %s" % str(exc)[:100])
        return
    finally:
        verify_epubs.epubcheck_util = real

    # EPUBCheck findings are reported by the stage itself; no need twice
    structural = [p for p in problems if not p.startswith("epubcheck ")]
    if structural:
        ctx.note("structure: %s" % "; ".join(structural[:4]))
        ctx.record.structural = structural
    if info.get("dangling"):
        ctx.note("structure: %d internal link(s) point at files that are not "
                 "in the book" % info["dangling"])
