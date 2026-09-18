"""Say what was actually wrong, per book.

Not "42 files processed". The point of a report is that someone can check the
work: the miscredited author, the series position that would have sorted a
book after its own sequel, the placeholder standing in for a cover. And it
separates what the pipeline fixed from what arrived broken, so a book that
came in without its artwork does not read as though this tool stripped it.
"""
from __future__ import annotations

import time
from pathlib import Path

from ..state import RunState


def write(state: RunState, out_dir: Path, elapsed: float) -> Path:
    path = out_dir / "REPORT.md"
    books = sorted(state.books.values(), key=lambda r: (r.status, r.source))
    left_alone = state.skipped_now
    done = [b for b in books if b.status == "done" and b.source not in left_alone]
    failed = [b for b in books if b.status == "failed"]
    skipped = [b for b in books
               if b.status == "skipped" or b.source in left_alone]

    fixed_errors = sum((b.errors_before or 0) - (b.errors_after or 0)
                       for b in done
                       if b.errors_before is not None and b.errors_after is not None)
    remaining = sum(b.errors_after or 0 for b in done)
    recaps = sum(b.recaps or 0 for b in done)

    L: list[str] = []
    L.append("# kobokit run")
    L.append("")
    L.append("%s, %d books in %s." % (time.strftime("%d %B %Y"), len(books),
                                      _duration(elapsed)))
    L.append("")
    L.append("| outcome | books |")
    L.append("|---|---|")
    L.append("| written to the library | %d |" % len(done))
    L.append("| left alone | %d |" % len(skipped))
    L.append("| failed | %d |" % len(failed))
    L.append("")
    if fixed_errors:
        L.append("%d EPUBCheck errors removed; %d remain across the finished "
                 "books." % (fixed_errors, remaining))
    if recaps:
        L.append("%d chapter recaps written." % recaps)
    L.append("")

    if done:
        L.append("## Written")
        L.append("")
        for b in done:
            L.append("**%s**" % (b.title or Path(b.source).name))
            bits = []
            if b.authors:
                bits.append(b.authors)
            if b.series:
                bits.append("%s%s" % (b.series,
                                      " #%g" % b.series_index
                                      if b.series_index is not None else ""))
            if b.format_in and b.format_in not in ("epub", "kepub"):
                bits.append("converted from %s" % b.format_in)
            if bits:
                L.append("  " + " — ".join(bits))
            if b.errors_before:
                L.append("  EPUBCheck %d → %d" % (b.errors_before,
                                                  b.errors_after or 0))
            if b.recaps:
                L.append("  %d recaps" % b.recaps)
            for n in b.notes:
                L.append("  - %s" % n)
            L.append("")

    if failed:
        L.append("## Not written")
        L.append("")
        L.append("These were left where they were. Nothing was overwritten.")
        L.append("")
        for b in failed:
            L.append("- **%s** — %s" % (Path(b.source).name, b.message or "failed"))
        L.append("")

    if skipped:
        L.append("## Already done")
        L.append("")
        L.append("%d book(s) unchanged since the last run." % len(skipped))
        L.append("")

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(L))
    return path


def _duration(seconds: float) -> str:
    seconds = int(seconds)
    if seconds < 90:
        return "%d seconds" % seconds
    if seconds < 5400:
        return "%d minutes" % round(seconds / 60)
    return "%.1f hours" % (seconds / 3600.0)
