"""Classify the incoming file and put the original somewhere safe.

The original is copied before anything reads it, not after. Every later stage
may rewrite the book in place, and the copy is what makes that reversible.
"""
from __future__ import annotations

import shutil
import zipfile
from pathlib import Path

from .base import Context, Stage, StageResult

EPUB = {".epub"}
CONVERTIBLE = {".pdf", ".mobi", ".azw", ".azw3", ".fb2", ".fb2.zip", ".zip"}
KNOWN = EPUB | CONVERTIBLE


def classify(path: Path) -> str:
    """Return epub | kepub | pdf | mobi | fb2 | unknown.

    Extension first, then content, because a .epub that is really a zip of
    something else and a .zip that is really an FB2 both turn up in practice.
    """
    name = path.name.lower()
    if name.endswith(".kepub.epub"):
        return "kepub"
    suffix = path.suffix.lower()
    if suffix == ".epub":
        return "kepub" if _is_kepub(path) else "epub"
    if suffix == ".pdf":
        return "pdf"
    if suffix in (".mobi", ".azw", ".azw3"):
        return "mobi"
    if suffix == ".fb2" or name.endswith(".fb2.zip"):
        return "fb2"
    if suffix == ".zip":
        return "fb2" if _zip_holds_fb2(path) else "unknown"
    return "unknown"


def _is_kepub(path: Path) -> bool:
    """A kepub is an EPUB whose text is already wrapped in koboSpans.

    Several documents are checked, not just the first. The first document in
    almost every book is a cover page with no prose in it and therefore no
    spans, so stopping there calls most kepubs plain EPUBs.
    """
    try:
        with zipfile.ZipFile(path) as z:
            docs = [n for n in z.namelist()
                    if n.lower().endswith((".xhtml", ".html", ".htm"))]
            for n in docs[:12]:
                if b"koboSpan" in z.read(n)[:200000]:
                    return True
    except (zipfile.BadZipFile, OSError, KeyError):
        return False
    return False


def _zip_holds_fb2(path: Path) -> bool:
    try:
        with zipfile.ZipFile(path) as z:
            return any(n.lower().endswith(".fb2") for n in z.namelist())
    except (zipfile.BadZipFile, OSError):
        return False


class IngestStage(Stage):
    name = "ingest"

    def run(self, ctx: Context) -> StageResult:
        src = ctx.path
        kind = classify(src)
        ctx.record.format_in = kind
        ctx.meta["format_in"] = kind

        if kind == "unknown":
            # Named, not silently dropped: books have gone missing from
            # batches exactly this way.
            return StageResult(ok=False, stop=True,
                               message="unrecognised format %s" % src.suffix)

        # zip integrity up front, so a corrupt archive is reported as corrupt
        # rather than as a mysterious failure five stages later
        if kind in ("epub", "kepub"):
            try:
                with zipfile.ZipFile(src) as z:
                    bad = z.testzip()
                if bad:
                    return StageResult(ok=False, stop=True,
                                       message="corrupt archive (bad CRC: %s)" % bad)
            except zipfile.BadZipFile as exc:
                return StageResult(ok=False, stop=True,
                                   message="not a readable zip: %s" % exc)

        if ctx.config.paths.backup:
            backup = ctx.config.paths.backup / src.name
            if not backup.exists():
                backup.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, backup)
                ctx.note("original kept at %s" % backup)

        staged = ctx.work / src.name
        staged.parent.mkdir(parents=True, exist_ok=True)
        if not staged.exists():
            shutil.copy2(src, staged)
        ctx.note("ingested as %s" % kind)
        return StageResult(path=staged, message=kind)
