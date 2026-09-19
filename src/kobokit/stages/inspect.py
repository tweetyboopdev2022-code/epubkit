"""Checks that only look, and only say what they found.

Anything here can be wrong without costing a book: it reports, the report
carries it, and the reader decides. Splitting a box set or trusting a guessed
boundary is not something to do on a hunch at three in the morning halfway
through a batch.
"""
from __future__ import annotations

import re
import zipfile
from pathlib import Path

from .base import Context, run_tool

#: words a book uses about itself when it is more than one book
BUNDLE = re.compile(r"\b(omnibus|anthology|collection|trilogy|quartet|"
                    r"box set|boxed set|complete series|books? one|"
                    r"\d\s*-\s*book|\d\s*book bundle)\b", re.I)

#: below this there is nothing to split; running the detector costs seconds
MIN_SPINE = 40


def _spine_length(book: Path) -> int:
    try:
        with zipfile.ZipFile(book) as z:
            opf = next((n for n in z.namelist() if n.endswith(".opf")), None)
            if not opf:
                return 0
            return len(re.findall(r"<itemref",
                                  z.read(opf).decode("utf-8", "ignore")))
    except (zipfile.BadZipFile, OSError):
        return 0


def looks_like_a_bundle(ctx: Context, book: Path) -> bool:
    """Does the book describe itself as more than one book?"""
    haystack = " ".join(filter(None, [
        book.name, ctx.record.title or "", ctx.record.series or ""]))
    if BUNDLE.search(haystack):
        return True
    try:
        with zipfile.ZipFile(book) as z:
            opf = next((n for n in z.namelist() if n.endswith(".opf")), None)
            if opf:
                head = z.read(opf).decode("utf-8", "ignore")[:4000]
                return bool(BUNDLE.search(re.sub(r"<[^>]+>", " ", head)))
    except (zipfile.BadZipFile, OSError):
        pass
    return False


def detect_omnibus(ctx: Context, book: Path) -> None:
    """Say so in the report; never split. Splitting stays a decision."""
    if not ctx.config.convert.detect_omnibus:
        ctx.record.not_applicable("plan_omnibus", "turned off")
        return
    spine = _spine_length(book)
    if spine < MIN_SPINE:
        ctx.record.not_applicable(
            "plan_omnibus", "%d spine items, too short to be a bundle" % spine)
        return
    if not looks_like_a_bundle(ctx, book):
        ctx.record.not_applicable(
            "plan_omnibus", "nothing about it says omnibus or collection")
        return

    plan = ctx.work / "omnibus.draft.json"
    r = run_tool("plan_omnibus", [str(book), "--out", str(plan)], timeout=600)
    out = (r.stdout or "") + (r.stderr or "")
    if "no omnibus structure found" in out or r.returncode != 0:
        ctx.record.ran("plan_omnibus", "looked, found one book")
        return

    titles = re.findall(r"^\s+\d+\.\s+\[\s*\d+-\s*\d+\]\s+\d+ pages\s+(.*)$",
                        out, re.M)
    ctx.record.omnibus = [t.strip() for t in titles if t.strip()]
    if ctx.record.omnibus:
        ctx.note("this looks like %d books in one file: %s"
                 % (len(ctx.record.omnibus),
                    "; ".join(ctx.record.omnibus[:4])))
        ctx.note("left whole. `kobokit split` on this file splits it.")
        ctx.record.ran("plan_omnibus",
                       "%d books detected" % len(ctx.record.omnibus))
    else:
        ctx.record.ran("plan_omnibus", "looked, found one book")
