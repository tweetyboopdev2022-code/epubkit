"""Per-chapter recaps and a spoiler-safe cast list, written into the book.

Two rules make this feature work rather than ruin books, and both were learned
the hard way.

**Never recap the chapter the reader is inside.** The recap for the current
chapter describes the whole of it, including the end they have not reached. An
early version cheerfully told someone in the middle of Goblet of Fire chapter
31 that the Triwizard Cup was a Portkey. Recaps are therefore keyed to the
chapter *behind* the reader, which is what `lag_chapters` means.

**A cast entry is withheld until the chapter that character appears in**, and
describes only who they are at that point - never what they turn out to be.

Everything is keyed to spine position, never to pages. A page depends on font
size and margins; a chapter does not, and Nickel already numbers every spine
document.
"""
from __future__ import annotations

import html
import re
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .base import Context, Stage, StageResult

SYSTEM = (
    "You write spoiler-safe recaps. You use only what the given chapter "
    "states, you never draw on other knowledge of the book, and you never "
    "say what a character turns out to be."
)

PROMPT = """Recap this chapter for a reader who has just finished it and read nothing beyond it.

Rules:
- Use ONLY what this chapter states. If you happen to know this book, ignore that.
- Never say what becomes of anyone: no deaths, betrayals, reveals, or fates.
- Describe characters only as this chapter presents them.
- CHARACTERS lists named individual people only. Not species, groups, houses,
  organisations, places, objects or concepts.
- Write in the same language as the chapter.
- Plain text. No markdown, no asterisks.

Answer in exactly this shape:
SUMMARY: <2-3 sentences, past tense>
CHARACTERS:
<name> - <who they are in the story, one line>

Chapter:
{chapter}
"""


class RecapStage(Stage):
    name = "recaps"

    def applies(self, ctx: Context) -> bool:
        return bool(ctx.config.recaps.enabled and ctx.provider)

    def run(self, ctx: Context) -> StageResult:
        book = ctx.path
        chapters = _chapters(book, ctx.config.recaps.min_chars)
        if not chapters:
            ctx.note("no chapters long enough to recap")
            return StageResult(message="no recaps")

        lag = max(0, ctx.config.recaps.lag_chapters)
        cfg = ctx.config.ai
        results: dict[int, tuple[str, str, str]] = {}

        def one(item):
            idx, href, text = item
            try:
                reply = ctx.provider.complete(
                    PROMPT.format(chapter=text[:12000]), system=SYSTEM)
            except Exception as exc:                        # noqa: BLE001
                ctx.log.debug("    recap %d failed: %s", idx, exc)
                return None
            summary, cast = _parse(reply.text)
            return (idx, href, summary, cast) if summary else None

        workers = max(1, min(cfg.concurrency, len(chapters)))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            for got in pool.map(one, chapters):
                if got:
                    idx, href, summary, cast = got
                    results[idx] = (href, summary, cast)

        if not results:
            ctx.note("recap generation produced nothing")
            return StageResult(message="no recaps")

        # shift by the lag so a recap only ever describes what is behind
        rows = []
        for idx in sorted(results):
            href, summary, _ = results[idx]
            shown_at = idx + lag
            rows.append((shown_at, href, summary))

        cast_rows = _cast_rows(results) if ctx.config.recaps.cast else []
        if cast_rows:
            cast_rows = _collapse(ctx, cast_rows)
        _embed(book, rows, cast_rows)
        ctx.record.recaps = len(rows)
        ctx.note("%d recaps, %d cast entries" % (len(rows), len(cast_rows)))
        return StageResult(message="%d recaps" % len(rows))


# ---------------------------------------------------------------- helpers


def _chapters(book: Path, min_chars: int) -> list[tuple[int, str, str]]:
    """Spine documents with enough prose to be a chapter, as (index, href, text).

    The spine is what Nickel itself reads, so the index matches what the device
    stores in ChapterIDBookmarked. A filename glob would not.
    """
    out: list[tuple[int, str, str]] = []
    try:
        with zipfile.ZipFile(book) as z:
            opf_name = next((n for n in z.namelist() if n.endswith(".opf")), None)
            if not opf_name:
                return []
            opf = z.read(opf_name).decode("utf-8", "ignore")
            base = opf_name.rsplit("/", 1)[0] if "/" in opf_name else ""
            manifest = _manifest(opf)
            hrefs = []
            for idref in re.findall(r'<itemref\b[^>]*idref="([^"]+)"', opf):
                href = manifest.get(idref)
                if href:
                    hrefs.append(href)
            for i, href in enumerate(hrefs):
                full = (base + "/" + href) if base else href
                try:
                    raw = z.read(full).decode("utf-8", "ignore")
                except KeyError:
                    continue
                text = _plain(raw)
                if len(text) >= min_chars:
                    out.append((i, href, text))
    except (zipfile.BadZipFile, OSError):
        return []
    return out


def _manifest(opf: str) -> dict[str, str]:
    """id -> href, without assuming which attribute comes first.

    Attribute order carries no meaning in XML, and a regex that expects
    id before href silently drops every item written the other way round -
    which drops chapters from the recap set with no error anywhere.
    """
    out: dict[str, str] = {}
    for tag in re.findall(r"<item\b[^>]*/?>", opf):
        attrs = dict(re.findall(r'([A-Za-z_:][-\w:.]*)\s*=\s*"([^"]*)"', tag))
        if attrs.get("id") and attrs.get("href"):
            out[attrs["id"]] = attrs["href"]
    return out


def _plain(raw: str) -> str:
    raw = re.sub(r"(?is)<(script|style)\b.*?</\1>", " ", raw)
    raw = re.sub(r"<[^>]+>", " ", raw)
    return re.sub(r"\s+", " ", html.unescape(raw)).strip()


def _parse(text: str) -> tuple[str, str]:
    if not text:
        return "", ""
    summary, cast = "", []
    m = re.search(r"SUMMARY:\s*(.+?)(?:\nCHARACTERS:|\Z)", text, re.S | re.I)
    if m:
        summary = re.sub(r"\s+", " ", m.group(1)).strip()
    m = re.search(r"CHARACTERS:\s*(.+)\Z", text, re.S | re.I)
    if m:
        for line in m.group(1).splitlines():
            line = line.strip(" -*\t")
            if line and " - " in line:
                cast.append(line)
    return summary, "\n".join(cast)


def _collapse(ctx: Context, rows: list) -> list:
    """One person, one entry.

    A model calls the same character Harry, Harry Potter and Potter across
    three chapters, and an uncollapsed cast list reads like a crowd scene.
    """
    try:
        from ..tools import dedupe_cast
    except ImportError:                                     # pragma: no cover
        return rows
    try:
        # dedupe_cast works on (order, file, name, description) rows
        shaped = [(i, "", name, desc) for i, name, desc in rows]
        out = dedupe_cast.collapse(shaped)
    except Exception as exc:                                # noqa: BLE001
        ctx.log.debug("    cast dedupe failed: %s", exc)
        ctx.record.ran("dedupe_cast", "failed, keeping the raw list")
        return rows
    collapsed = [(i, name, desc) for i, _f, name, desc in out]
    if len(collapsed) < len(rows):
        ctx.note("cast: %d entries collapsed to %d people"
                 % (len(rows), len(collapsed)))
    ctx.record.ran("dedupe_cast", "%d -> %d" % (len(rows), len(collapsed)))
    return collapsed


def _cast_rows(results: dict) -> list[tuple[int, str, str]]:
    """First appearance wins, so nobody is introduced before the reader meets them."""
    seen: dict[str, tuple[int, str]] = {}
    for idx in sorted(results):
        _, _, cast = results[idx]
        for line in (cast or "").splitlines():
            if " - " not in line:
                continue
            name, _, desc = line.partition(" - ")
            key = name.strip().lower()
            if key and key not in seen:
                seen[key] = (idx, "%s\t%s" % (name.strip(), desc.strip()))
    return [(idx, *row.split("\t", 1)) for idx, row in
            sorted(seen.values(), key=lambda r: r[0])]


def _embed(book: Path, rows, cast_rows) -> None:
    """Write the TSVs into the book, declared in the manifest, never the spine.

    Declared so EPUBCheck does not call them undeclared resources; out of the
    spine so the reader never turns a page into them.
    """
    recaps = "\n".join("%d\t%s\t%s" % (i, h, s) for i, h, s in rows)
    cast = "\n".join("%d\t%s\t%s" % (i, n, d) for i, n, d in cast_rows)

    import shutil
    import tempfile

    fd, tmp = tempfile.mkstemp(suffix=".epub", dir=str(book.parent))
    import os
    os.close(fd)
    with zipfile.ZipFile(book) as zin:
        names = zin.namelist()
        opf_name = next((n for n in names if n.endswith(".opf")), None)
        base = (opf_name.rsplit("/", 1)[0] + "/") if opf_name and "/" in opf_name else ""
        want = {base + "recaps.tsv": recaps, base + "cast.tsv": cast}
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zout:
            if "mimetype" in names:
                zi = zipfile.ZipInfo("mimetype")
                zi.compress_type = zipfile.ZIP_STORED
                zout.writestr(zi, zin.read("mimetype"))
            for n in names:
                if n == "mimetype" or n in want:
                    continue
                data = zin.read(n)
                if n == opf_name:
                    data = _declare(data.decode("utf-8", "ignore")).encode("utf-8")
                zout.writestr(n, data)
            for n, body in want.items():
                if body:
                    zout.writestr(n, body)
    shutil.move(tmp, str(book))


def _declare(opf: str) -> str:
    add = ""
    for ident, href, mt in (("recaps-tsv", "recaps.tsv", "text/plain"),
                            ("cast-tsv", "cast.tsv", "text/plain")):
        if 'href="%s"' % href not in opf:
            add += '<item id="%s" href="%s" media-type="%s"/>' % (ident, href, mt)
    if add and "</manifest>" in opf:
        opf = opf.replace("</manifest>", add + "</manifest>", 1)
    return opf
