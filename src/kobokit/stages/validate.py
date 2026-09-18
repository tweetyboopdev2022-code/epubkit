"""Real EPUBCheck, plus the structural checks it does not make.

The two catch different things. EPUBCheck finds spec violations; verify_epubs
finds a book that came out with less than it went in with - a lost stylesheet,
a missing image, a table of contents that shrank. Neither replaces the other.

What this stage does NOT do is quietly ship a book it could not clean. A book
that still has errors is reported with its count, and `validate.require_clean`
decides whether that stops it reaching the output folder.
"""
from __future__ import annotations

from .base import Context, Stage, StageResult


class ValidateStage(Stage):
    name = "validate"

    def run(self, ctx: Context) -> StageResult:
        from ..tools import epubcheck_util as ec

        book = ctx.path
        if not ec.available():
            # A missing validator is a setup problem, not a broken book, and
            # must not fail every title in the batch.
            ctx.note("EPUBCheck unavailable, skipping validation")
            return StageResult(message="not checked")

        results = ec.run(str(book), timeout=ctx.config.validate.timeout)
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
