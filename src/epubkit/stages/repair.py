"""Structural repair, then EPUB 3 conformance.

The dead-reference pass runs before the table of contents is rebuilt, because
rebuild_toc refuses to replace a TOC unless the new one has more entries, and
it cannot judge that against a spine full of entries pointing at files that are
not in the archive.

The EPUB3 pass is the one that does the heavy lifting: most books declare
EPUB 2.0 while their content is EPUB 3, and EPUBCheck validates content against
whatever the package claims. Promoting the declaration and finishing the job
removes that whole class rather than papering over it.
"""
from __future__ import annotations

import shutil
from pathlib import Path

from .base import Context, Stage, StageResult, run_tool


class RepairStage(Stage):
    name = "repair"

    def run(self, ctx: Context) -> StageResult:
        book = ctx.path

        # These take a folder; each book has a work directory to itself.
        folder = str(book.parent)
        r = run_tool("prune_dead_refs", [folder, "--apply"], timeout=300)
        if r.returncode == 0 and "removed" in (r.stdout or ""):
            ctx.note("pruned dead references")

        r = run_tool("rebuild_toc", [folder], timeout=300)
        if r.returncode == 0 and "rebuilt" in (r.stdout or "").lower():
            ctx.note("rebuilt table of contents")

        # cover art comes from inside the book; a generated placeholder is
        # never passed off as the real jacket
        run_tool("check_covers", [folder, "--fix"], timeout=300)

        fixed = self._to_epub3(ctx, book)
        self._slim(ctx, fixed)
        return StageResult(path=fixed)

    def _slim(self, ctx: Context, book: Path) -> None:
        """Recompress a book that is bigger than the screen can use.

        A 60 MB comic whose pages are 3000px wide shows exactly the same thing
        on a 1072px panel as one a third the size, but takes three times as
        long to open and turn. Nothing the device can display is lost.
        """
        limit = ctx.config.convert.slim_above_mb
        if not limit:
            ctx.record.not_applicable("slim", "turned off")
            return
        mb = book.stat().st_size / 1e6
        if mb < limit:
            ctx.record.not_applicable("slim", "%.1f MB, under the %.0f MB "
                                      "threshold" % (mb, limit))
            return
        r = run_tool("slim", [str(book.parent), "--apply",
                              "--panel", ctx.config.convert.panel,
                              "--quality", str(ctx.config.convert.jpeg_quality)],
                     timeout=1800)
        after = book.stat().st_size / 1e6 if book.exists() else mb
        if r.returncode == 0 and after < mb - 0.5:
            ctx.note("slimmed %.1f MB to %.1f MB" % (mb, after))
            ctx.record.ran("slim", "%.1f -> %.1f MB" % (mb, after))
        else:
            ctx.record.ran("slim", "no worthwhile saving")

    def _to_epub3(self, ctx: Context, book: Path) -> Path:
        from ..tools import fix_epub3, verify_fix

        candidate = ctx.work / ("epub3-" + book.name)
        try:
            info = fix_epub3.fix_book(str(book), str(candidate))
        except Exception as exc:                            # noqa: BLE001
            ctx.note("epub3 rewrite failed: %s" % str(exc)[:140])
            return book

        ok, problems, before, after = verify_fix.compare(str(book), str(candidate))
        if not ok:
            # Content preservation is absolute. A book that lost a koboSpan,
            # a document, an image or its words is discarded no matter how
            # clean it validates - a reading position points straight at a
            # koboSpan, and losing one moves where the reader resumes.
            ctx.note("epub3 rewrite discarded, content check failed: %s"
                     % "; ".join(problems))
            try:
                candidate.unlink()
            except OSError:
                pass
            return book

        notes = info.get("notes") or []
        if notes:
            ctx.note("epub3: %s" % ", ".join(notes[:8]))
        shutil.move(str(candidate), str(book))
        ctx.meta["epub3"] = True
        return book
