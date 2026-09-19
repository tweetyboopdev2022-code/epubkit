"""Work out what the book actually is, write it in, and rename from that.

Order inside this stage matters. Catalogue lookups run first and the model
only ever fills what they could not answer, because a catalogue can be checked
and a model cannot. A field the book already states is never overwritten.

The rename happens here, from the finished metadata, and never earlier. The
filename is what the reader sorts by, so it is built from the corrected title
and series rather than from whatever the download was called.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from .base import Context, Stage, StageResult, run_tool

#: Asking a model for facts invites invention, so the prompt is narrow and the
#: answer is constrained to JSON with an explicit "unknown" escape.
PROMPT = """Identify this book from its own front matter.

Answer ONLY with JSON in exactly this shape:
{{"title": "", "authors": [""], "series": "", "series_index": null}}

Rules:
- Use only what the text below states. If the text does not say, use null for
  series and series_index, and do not guess.
- "title" is the work's title, without the series name attached to it.
- "series_index" is a number, or null. Never invent an ordering.
- If you cannot identify the book at all, answer {{"title": null}}.

Filename: {filename}

First pages:
{excerpt}
"""


class MetadataStage(Stage):
    name = "metadata"

    def run(self, ctx: Context) -> StageResult:
        book = ctx.path
        plan_path = ctx.work / "plan.json"

        current = self._scan(ctx, book)
        ctx.meta["scanned"] = current

        needs = _missing(current, ctx.config.metadata.overwrite_existing)
        if needs:
            found = self._catalogue(ctx, book, current, needs)
            current.update({k: v for k, v in found.items() if v})
            needs = _missing(current, ctx.config.metadata.overwrite_existing)

        if needs and ctx.config.metadata.use_ai_fallback and ctx.provider:
            guessed = self._ask_model(ctx, book, current)
            # the model fills gaps; it never overrules the file or a catalogue
            for k, v in guessed.items():
                if v and not current.get(k):
                    current[k] = v
                    ctx.note("%s from model" % k)

        still = _missing(current, False)
        if still:
            ctx.note("unresolved: %s" % ", ".join(still))

        plan = {book.name: _to_plan_entry(current)}
        plan_path.write_text(json.dumps(plan, indent=1))

        out_dir = ctx.work / "meta"
        out_dir.mkdir(parents=True, exist_ok=True)
        r = run_tool("apply_metadata",
                     [str(book.parent), "--plan", str(plan_path),
                      "--out", str(out_dir)],
                     timeout=600)
        applied = out_dir / book.name
        if r.returncode != 0 or not applied.exists():
            # not fatal: a book with imperfect metadata still reads
            detail = (r.stderr or r.stdout or "").strip().splitlines()
            ctx.note("could not rewrite metadata (%s); keeping what the book "
                     "already stated" % (detail[-1][:90] if detail else "no output"))
            applied = book
        else:
            ctx.note("metadata written into the book")

        renamed = self._rename(ctx, applied, current)
        ctx.record.title = current.get("title")
        ctx.record.authors = ", ".join(current.get("authors") or []) or None
        ctx.record.series = current.get("series")
        ctx.record.series_index = current.get("series_index")
        return StageResult(path=renamed)

    # ------------------------------------------------------------------

    def _scan(self, ctx: Context, book: Path) -> dict:
        # The bundled tools take the folder, not the file. Every book has its
        # own work directory, so the folder here holds exactly this one book.
        r = run_tool("scan_epubs", [str(book.parent), "--json"], timeout=300)
        try:
            data = json.loads(r.stdout or "[]")
        except json.JSONDecodeError:
            ctx.note("could not read the book's own metadata")
            return {}
        # scan_epubs emits a list of rows, each carrying its own "file".
        rows = data if isinstance(data, list) else list(data.values())
        entry = next((row for row in rows
                      if isinstance(row, dict) and row.get("file") == book.name),
                     rows[0] if rows else {})
        if not isinstance(entry, dict):
            return {}
        # scan_epubs emits authors as [{name, file_as}]; the plan wants pairs.
        # These two schemas have silently mismatched before.
        authors = []
        for a in entry.get("authors") or []:
            authors.append(a.get("name") if isinstance(a, dict) else str(a))
        dates = entry.get("dates")
        return {
            "title": entry.get("title"),
            "authors": [a for a in authors if a],
            "series": entry.get("series"),
            "series_index": entry.get("series_index") or entry.get("position"),
            "description": entry.get("description"),
            "date": (dates[0] if isinstance(dates, list) and dates else dates),
        }

    def _catalogue(self, ctx: Context, book: Path, current: dict,
                   needs: list[str]) -> dict:
        tmp = ctx.work / "lookup.json"
        tmp.write_text(json.dumps({book.name: _to_plan_entry(current)}, indent=1))
        r = run_tool("research_kobo", [str(tmp)], timeout=300)
        if r.returncode != 0:
            ctx.note("catalogue lookup failed")
            return {}
        try:
            filled = json.loads(tmp.read_text()).get(book.name, {})
        except (OSError, json.JSONDecodeError):
            return {}
        got = {
            "title": filled.get("title"),
            "series": filled.get("series"),
            "series_index": filled.get("series_index"),
            "description": filled.get("description"),
            "date": filled.get("date"),
        }
        authors = filled.get("authors")
        if authors:
            got["authors"] = [a[0] if isinstance(a, (list, tuple)) else a
                              for a in authors]
        if any(got.values()):
            ctx.note("catalogue filled: %s"
                     % ", ".join(k for k, v in got.items() if v and k in needs))
        return got

    def _ask_model(self, ctx: Context, book: Path, current: dict) -> dict:
        excerpt = _first_pages(book)
        if not excerpt:
            return {}
        try:
            reply = ctx.provider.complete(
                PROMPT.format(filename=book.name, excerpt=excerpt[:6000]),
                system="You identify books. You never guess a series order.",
            )
        except Exception as exc:                            # noqa: BLE001
            ctx.note("model lookup failed: %s" % str(exc)[:120])
            return {}
        data = _json_from(reply.text)
        if not data or not data.get("title"):
            return {}
        idx = data.get("series_index")
        try:
            idx = float(idx) if idx is not None else None
        except (TypeError, ValueError):
            idx = None
        return {
            "title": data.get("title"),
            "authors": [a for a in (data.get("authors") or []) if a],
            "series": data.get("series") or None,
            "series_index": idx,
        }

    def _rename(self, ctx: Context, book: Path, meta: dict) -> Path:
        name = _filename_for(meta, ctx.config.metadata)
        if not name:
            return book
        target = book.with_name(name + book.suffix if not
                                book.name.endswith(".kepub.epub")
                                else name + ".kepub.epub")
        if target == book or target.exists():
            return book
        try:
            book.rename(target)
        except OSError:
            return book
        ctx.note("renamed to %s" % target.name)
        return target


# ---------------------------------------------------------------- helpers


def _missing(meta: dict, overwrite: bool) -> list[str]:
    if overwrite:
        return ["title", "authors", "series"]
    out = []
    if not meta.get("title"):
        out.append("title")
    if not meta.get("authors"):
        out.append("authors")
    # a missing series is normal for a standalone, so it is not "missing"
    return out


def _to_plan_entry(meta: dict) -> dict:
    authors = meta.get("authors") or []
    return {
        "title": meta.get("title"),
        "authors": [[a, _sort_name(a)] for a in authors],
        "series": meta.get("series"),
        "series_index": meta.get("series_index"),
        "description": meta.get("description"),
        "date": meta.get("date"),
    }


def _sort_name(name: str) -> str:
    """"Douglas Adams" -> "Adams, Douglas", leaving existing sort forms alone."""
    if "," in name:
        return name
    parts = name.split()
    if len(parts) < 2:
        return name
    return "%s, %s" % (parts[-1], " ".join(parts[:-1]))


#: Most filesystems cap a single name at 255 bytes. Room is left for the
#: ".kepub.epub" suffix and for a " (2)" collision suffix.
NAME_BYTES = 255 - len(".kepub.epub") - 4


def _filename_for(meta: dict, mcfg) -> str | None:
    title = (meta.get("title") or "").strip()
    if not title:
        return None
    authors = ", ".join(meta.get("authors") or []) or "Unknown"
    series = (meta.get("series") or "").strip()
    idx = meta.get("series_index")
    if series and idx is not None:
        try:
            name = mcfg.filename_template.format(
                series=series, index=int(float(idx)), title=title, authors=authors)
        except (ValueError, KeyError):
            name = "%s - %s - %s" % (series, title, authors)
    else:
        name = mcfg.standalone_template.format(title=title, authors=authors)

    name = _safe(name)
    if _bytes(name) <= NAME_BYTES:
        return name

    # Too long for the filesystem. The series name and the title are what the
    # reader sorts and searches by, so they are kept whole and the author list
    # gives way first - never the other way round, and never a chopped title.
    if series and idx is not None:
        shorter = _safe("%s %02d - %s" % (series, int(float(idx)), title))
    else:
        shorter = _safe(title)
    if _bytes(shorter) <= NAME_BYTES:
        return shorter
    # A title alone over 240 bytes is a broken metadata record, not a book.
    # Cut on a word boundary so what is left is still readable.
    return _trim_words(shorter, NAME_BYTES)


def _bytes(name: str) -> int:
    return len(name.encode("utf-8"))


def _trim_words(name: str, limit: int) -> str:
    words = name.split(" ")
    out = ""
    for w in words:
        candidate = (out + " " + w).strip()
        if _bytes(candidate) > limit:
            break
        out = candidate
    return out or name.encode("utf-8")[:limit].decode("utf-8", "ignore")


def _safe(name: str) -> str:
    """Filesystem-safe, and not abbreviated.

    The full series name and the full title stay, however long the result
    runs - only characters that cannot appear in a filename are replaced.
    Length is dealt with by _filename_for, and only when the filesystem
    genuinely cannot hold the name.
    """
    name = name.replace("/", "-").replace("\\", "-").replace(":", " -")
    name = re.sub(r'[<>"|?*\x00-\x1f]', "", name)
    return re.sub(r"\s+", " ", name).strip(" .")


def _first_pages(book: Path, limit: int = 8000) -> str:
    import zipfile
    try:
        with zipfile.ZipFile(book) as z:
            docs = [n for n in z.namelist()
                    if n.lower().endswith((".xhtml", ".html", ".htm"))]
            text = []
            for n in docs[:6]:
                raw = z.read(n).decode("utf-8", "ignore")
                raw = re.sub(r"<[^>]+>", " ", raw)
                text.append(re.sub(r"\s+", " ", raw))
                if sum(len(t) for t in text) > limit:
                    break
            return " ".join(text)[:limit]
    except (zipfile.BadZipFile, OSError, KeyError):
        return ""


def _json_from(text: str) -> dict:
    """Models wrap JSON in prose and fences however firmly you ask them not to.

    Scanning for the first brace and the last one looks obvious and is wrong:
    a reasoning model that talks about the answer before giving it, or any
    model that offers a second example, leaves braces on both sides and the
    span between them is not JSON. Each balanced object is tried instead,
    last first, since the model's final word is its answer.
    """
    if not text:
        return {}
    candidates = []
    depth = start = 0
    in_string = escaped = False
    for i, ch in enumerate(text):
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                candidates.append(text[start:i + 1])
            elif depth < 0:
                depth = 0
    for blob in reversed(candidates):
        try:
            data = json.loads(blob)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict) and "title" in data:
            return data
    return {}
