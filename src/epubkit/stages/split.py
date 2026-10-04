"""Split a box set into its books, before anything else is done to them.

This runs right after conversion and ahead of metadata, repair, kepub and
recaps on purpose. Everything after it is per book: a title, a series number,
a cover, a shelf, a recap for every chapter. Done on a 13-book file those are
all wrong at once - one title for thirteen novels, thirteen books' chapters
recapped as one - so the decision "is this one book or several?" has to come
first, and every part then goes through the whole pipeline as a book of its own.

The detector (plan_omnibus) does the judging and is deliberately conservative:
it wants boundaries read from the table of contents, at least two runs, and
most of them titled like books rather than chapters. Anything short of that
leaves the file whole and says so in the report.
"""
from __future__ import annotations

import json
import re

from .base import Context, Stage, StageResult, run_tool
from .inspect import MIN_SPINE, _spine_length
from .validate import is_epub

_LABEL = re.compile(
    r"^(?P<series>.+?)[\s,]*(?:#|no\.?\s*|book\s+|vol(?:ume)?\.?\s*)?"
    r"(?P<n>\d{1,3})\s*[:.\-–—]\s*(?P<title>\S.*)$", re.I)


def derive_titles(labels: list[str]):
    """Series, and (number, title) per part, when the labels spell them out.

    "A Series of Unfortunate Events #1: The Bad Beginning" carries all three.
    Only when every label agrees on the series and the numbers rise in order is
    that trusted; otherwise each label is simply the title and there is no
    series to invent.
    """
    parsed = [_LABEL.match(label.strip()) for label in labels]
    if parsed and all(parsed):
        names = {m["series"].strip(" -–:,").lower() for m in parsed}
        nums = [int(m["n"]) for m in parsed]
        if len(names) == 1 and nums == sorted(set(nums)):
            series = parsed[0]["series"].strip(" -–:,")
            return series, [(str(int(m["n"])), m["title"].strip()) for m in parsed]
    return "", [("", label.strip()) for label in labels]


def _safe(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r'[\\/:*?"<>|]', "", text)).strip(" .-")


def fill_plan(data: dict, author: str) -> dict:
    """Give each part in a draft plan its title, series, number, author and file name."""
    books = data.get("books") or []
    series, parts = derive_titles([b["metadata"].get("title") or ("Book %d" % (i + 1))
                                   for i, b in enumerate(books)])
    for i, (b, (num, title)) in enumerate(zip(books, parts, strict=True), 1):
        md = b["metadata"]
        md["title"] = title
        md["authors"] = [[author, ""]] if author else [["", ""]]
        md["series"] = series
        md["series_index"] = num or ("" if not series else str(i))
        stem = " - ".join(x for x in (
            ("%s %02d" % (series, int(num))) if series and num else "", title, author) if x)
        b["out"] = _safe(stem or "Book %d" % i) + ".epub"
    return data


class SplitStage(Stage):
    name = "split"

    def applies(self, ctx: Context) -> bool:
        return is_epub(ctx.path)

    def run(self, ctx: Context) -> StageResult:
        mode = ctx.config.convert.split_omnibus
        book = ctx.path
        if mode == "off":
            ctx.record.not_applicable("split", "turned off")
            return StageResult()
        spine = _spine_length(book)
        if spine < MIN_SPINE:
            ctx.record.not_applicable(
                "split", "%d spine items, too short to be a bundle" % spine)
            return StageResult()

        plan = ctx.work / "omnibus.plan.json"
        r = run_tool("plan_omnibus", [str(book), "--out", str(plan)], timeout=600)
        out = (r.stdout or "") + (r.stderr or "")
        if "no omnibus structure found" in out or r.returncode != 0 or not plan.exists():
            ctx.record.ran("split", "looked, found one book")
            return StageResult()

        labels = [t.strip() for t in re.findall(
            r"^\s+\d+\.\s+\[\s*\d+-\s*\d+\]\s+\d+ pages\s+(.*)$", out, re.M)]
        ctx.record.omnibus = labels
        from_toc = "boundaries from table of contents" in out
        if len(labels) < 2:
            ctx.record.ran("split", "looked, found one book")
            return StageResult()
        ctx.note("this looks like %d books in one file: %s"
                 % (len(labels), "; ".join(labels[:4])))
        if mode != "auto" or not from_toc:
            why = ("splitting is set to report only" if mode != "auto" else
                   "the boundaries were guessed, not read from the table of contents")
            ctx.note("left whole: %s. `epubkit split` on this file splits it." % why)
            ctx.record.ran("split", "%d books detected, left whole" % len(labels))
            return StageResult()

        from ..shelves import read_book
        try:
            authors = read_book(book)["authors"]
        except Exception:                                   # noqa: BLE001
            authors = []
        data = fill_plan(json.loads(plan.read_text()), authors[0] if authors else "")
        plan.write_text(json.dumps(data, indent=1, ensure_ascii=False))

        parts_dir = ctx.work / "parts"
        parts_dir.mkdir(parents=True, exist_ok=True)
        r = run_tool("split_omnibus",
                     [str(book), "--plan", str(plan), "--out", str(parts_dir)],
                     timeout=1800)
        parts = [parts_dir / b["out"] for b in data["books"]]
        if r.returncode != 0 or not all(p.is_file() for p in parts):
            ctx.note("could not split it (%s); left whole"
                     % ((r.stderr or r.stdout or "no output").strip().splitlines() or [""])[-1][:100])
            ctx.record.ran("split", "split failed, left whole")
            return StageResult()

        ctx.record.split_into = len(parts)
        ctx.record.ran("split", "split into %d books" % len(parts))
        ctx.note("split into %d books, each now goes through the rest of the "
                 "pipeline on its own" % len(parts))
        return StageResult(parts=parts, message="split into %d books" % len(parts))
