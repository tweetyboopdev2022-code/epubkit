"""Turn a finished EPUB into a kepub, which is what a Kobo reads best.

A kepub is an EPUB whose text is wrapped in `koboSpan` elements. Nickel uses
those spans for page numbers, reading statistics and — the part that matters
most — for remembering where the reader stopped: `ChapterIDBookmarked` in the
device database points straight at one. A plain EPUB sideloaded onto a Kobo
gets none of that; it gets an estimated position and no statistics.

A book that arrived as a kepub is repaired rather than re-wrapped. Running the
span transform over existing spans renumbers them, and a renumbered span is a
reading position that has silently moved.
"""
from __future__ import annotations

import shutil
import zipfile
from pathlib import Path

from .base import Context, Stage, StageResult, run_tool


def _span_count(path: Path) -> int:
    n = 0
    try:
        with zipfile.ZipFile(path) as z:
            for name in z.namelist():
                if name.lower().endswith((".xhtml", ".html", ".htm")):
                    n += z.read(name).count(b"koboSpan")
    except (zipfile.BadZipFile, OSError):
        return 0
    return n


def _kepub_named(ctx: Context, book: Path) -> Path | None:
    """Give a book that is a kepub inside a `.kepub.epub` name.

    A Kobo goes by the name: `x.epub` is opened as a plain EPUB even when its
    text is full of koboSpans. That happens to anything cut from a kepub - a
    split omnibus, say - so a book that already is one gets the name it needs.
    Only the name changes; the bytes are the ones repaired above.
    """
    if book.name.lower().endswith(".kepub.epub") or not book.name.lower().endswith(".epub"):
        return None
    target = book.with_name(book.name[:-len(".epub")] + ".kepub.epub")
    if target.exists():
        ctx.note("left the name alone: %s already exists" % target.name)
        return None
    book.rename(target)
    ctx.note("renamed to %s so the Kobo opens it as a kepub" % target.name)
    return target


class KepubStage(Stage):
    name = "kepub"

    def applies(self, ctx: Context) -> bool:
        if not ctx.config.convert.kepub:
            return False
        # a fixed-layout book out of pdf2kepub is already one, and has no
        # flowing text to wrap
        return not ctx.meta.get("fixed_layout")

    def run(self, ctx: Context) -> StageResult:
        book = ctx.path
        if ctx.meta.get("format_in") == "kepub" or _span_count(book):
            return self._repair(ctx, book)
        return self._convert(ctx, book)

    # ------------------------------------------------------------------

    def _repair(self, ctx: Context, book: Path) -> StageResult:
        before = _span_count(book)
        r = run_tool("repair_kepub", [str(book.parent)], timeout=600)
        after = _span_count(book)
        if after < before:
            # Should never happen, but a lost span is a lost reading position,
            # so it is worth stating rather than assuming.
            ctx.note("kepub repair lost spans (%d -> %d); keeping the original"
                     % (before, after))
            return StageResult(ok=False,
                               message="kepub repair lost %d koboSpans"
                                       % (before - after))
        ctx.note("already a kepub; repaired in place (%d koboSpans intact)"
                 % after)
        if r.returncode != 0:
            ctx.note("repair_kepub exited %d" % r.returncode)
        return StageResult(path=_kepub_named(ctx, book))

    def _convert(self, ctx: Context, book: Path) -> StageResult:
        out_dir = ctx.work / "kepub"
        out_dir.mkdir(parents=True, exist_ok=True)
        r = run_tool("kepubify", [str(book), str(out_dir), "--verify"],
                     timeout=900)

        produced = sorted(out_dir.glob("*.kepub.epub"))
        if r.returncode != 0 or not produced:
            # Not fatal. A plain EPUB reads on a Kobo; it just reads with less
            # of the device's own machinery behind it.
            ctx.note("kepub conversion failed, shipping the EPUB as it is: %s"
                     % (r.stderr or r.stdout or "no output").strip()
                     .splitlines()[-1][:120])
            return StageResult(message="kepub skipped")

        made = produced[0]
        spans = _span_count(made)
        if not spans:
            ctx.note("kepub came out with no koboSpans; shipping the EPUB")
            return StageResult(message="kepub skipped")

        final = book.parent / (book.name[:-len(".epub")] + ".kepub.epub"
                               if book.name.endswith(".epub") else
                               book.name + ".kepub.epub")
        shutil.move(str(made), str(final))
        if final != book:
            try:
                book.unlink()
            except OSError:
                pass
        ctx.note("converted to kepub (%d koboSpans, so the Kobo can track "
                 "pages and position)" % spans)
        return StageResult(path=final)
