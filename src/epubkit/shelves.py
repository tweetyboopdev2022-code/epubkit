"""Decide which library a finished book belongs in: Kids, Teens or Adult.

This is the last step of `epubkit run` and the whole of `epubkit shelves`. It
looks at every finished book in the library folder, so a book left alone by
this run is still rated, and the answer goes into REPORT.md.

Accuracy comes from agreement between independent evidence, not from one guess:

  1. what the book itself says (subjects, blurb),
  2. what catalogues say (Open Library subjects, Google Books categories and
     maturity rating), found by title and author,
  3. what the text is like (length and reading level of the actual prose),
  4. what a model knows about the book, shown all of the above.

Explicit audience wording ("juvenile fiction", "young adult") decides on its
own. Otherwise the model decides, and its answer is checked against the text:
a short, easy book the model calls Adult (or a long, dense one it calls Kids)
is flagged and put on the safer side. Kids is only ever "high" confidence when
the sources agree, because a wrong Kids shelf is the mistake that matters.
"""
from __future__ import annotations

import json
import re
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

from .library_hint import ADULT, KIDS, TEENS, Suggestion, recommend

RULES_VERSION = 3          # bump when the rules change, so cached answers refresh

_DC = "{http://purl.org/dc/elements/1.1/}"
_OPF = "{http://www.idpf.org/2007/opf}"
_UA = {"User-Agent": "epubkit/1.0 (personal library tool)"}


# --------------------------------------------------------------- the book

def read_book(path: Path) -> dict:
    """title, authors, series, subjects, description, isbn - from the OPF."""
    with zipfile.ZipFile(path) as z:
        opf = next(n for n in z.namelist() if n.endswith(".opf"))
        md = ET.fromstring(z.read(opf)).find(_OPF + "metadata")
    text = lambda tag: [(e.text or "").strip() for e in md.findall(_DC + tag)]  # noqa: E731
    metas = {m.get("name"): m.get("content") for m in md.findall(_OPF + "meta")
             if m.get("name")}
    isbn = next((re.sub(r"[^0-9Xx]", "", i) for i in text("identifier")
                 if re.sub(r"[^0-9Xx]", "", i) and
                 len(re.sub(r"[^0-9Xx]", "", i)) in (10, 13)), None)
    return {"title": (text("title") or [""])[0],
            "authors": [a for a in text("creator") if a],
            "series": metas.get("calibre:series"),
            "subjects": [s for s in text("subject") if s],
            "description": (text("description") or [""])[0] or None,
            "isbn": isbn}


_TAG = re.compile(r"<[^>]+>")
_WORD = re.compile(r"[A-Za-z']+")


def _syllables(w: str) -> int:
    w = w.lower().strip("'")
    n = len(re.findall(r"[aeiouy]+", w))
    if w.endswith("e") and n > 1 and not w.endswith("le"):
        n -= 1
    return max(1, n)


def text_stats(path: Path, sample_words: int = 6000) -> dict:
    """Length and reading level of the book's prose (chapter text only)."""
    total = 0
    sample: list[str] = []
    with zipfile.ZipFile(path) as z:
        for n in z.namelist():
            if not n.lower().endswith((".xhtml", ".html", ".htm")):
                continue
            body = _TAG.sub(" ", re.sub(r"(?is)<(script|style|head).*?</\1>", " ",
                                        z.read(n).decode("utf-8", "ignore")))
            words = _WORD.findall(body)
            total += len(words)
            if len(words) > 150 and len(sample) < sample_words:
                sample.extend(words[:sample_words - len(sample)])
    out = {"words": total, "grade": None, "sample": ""}
    if len(sample) >= 300:
        out["grade"] = _grade_from(path, len(sample))
        out["sample"] = " ".join(sample[:220])
    return out


def _grade_from(path: Path, n_words: int) -> float | None:
    """Flesch-Kincaid grade on the first substantial chapters."""
    words = syl = sents = 0
    with zipfile.ZipFile(path) as z:
        for n in z.namelist():
            if not n.lower().endswith((".xhtml", ".html", ".htm")):
                continue
            body = re.sub(r"\s+", " ", _TAG.sub(" ", re.sub(
                r"(?is)<(script|style|head).*?</\1>", " ",
                z.read(n).decode("utf-8", "ignore"))))
            w = _WORD.findall(body)
            if len(w) < 150:
                continue
            words += len(w)
            syl += sum(_syllables(x) for x in w)
            sents += max(1, len(re.findall(r"[.!?][\"')\]]*(?:\s|$)", body)))
            if words >= n_words:
                break
    if words < 300:
        return None
    return round(0.39 * words / sents + 11.8 * syl / words - 15.59, 1)


# ------------------------------------------------------------ catalogues

def _get(url: str, timeout: float = 8.0) -> dict:
    req = urllib.request.Request(url, headers=_UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:             # noqa: S310
        return json.loads(r.read().decode("utf-8", "ignore"))


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


def _same_title(a: str, b: str) -> bool:
    a, b = _norm(a), _norm(b)
    return bool(a and b and (a == b or a in b or b in a))


def lookup(book: dict, fetch=_get) -> dict:
    """Catalogue evidence for a book: subjects, categories, maturity rating.

    A result only counts when its title matches the book's, so a catalogue's
    answer about a different book never leaks in. Any failure is just "no
    evidence" - the network is never required.
    """
    ev = {"subjects": [], "maturity": None, "sources": []}
    title = book.get("title") or ""
    if not title:
        return ev
    author = (book.get("authors") or [""])[0]
    # Google Books
    try:
        q = ("isbn:" + book["isbn"]) if book.get("isbn") else \
            "intitle:%s+inauthor:%s" % (urllib.parse.quote(title), urllib.parse.quote(author))
        data = fetch("https://www.googleapis.com/books/v1/volumes?q=%s&maxResults=5" % q)
        for it in data.get("items") or []:
            v = it.get("volumeInfo") or {}
            if not _same_title(title, v.get("title", "")) and not book.get("isbn"):
                continue
            ev["subjects"] += [c for c in (v.get("categories") or [])]
            if v.get("maturityRating") == "MATURE":
                ev["maturity"] = "MATURE"
            if v.get("categories"):
                ev["sources"].append("Google Books")
                break
    except Exception:                                                   # noqa: BLE001
        pass
    # Open Library
    try:
        url = "https://openlibrary.org/search.json?title=%s&author=%s&limit=3&fields=title,subject" % (
            urllib.parse.quote(title), urllib.parse.quote(author))
        for d in fetch(url).get("docs") or []:
            if _same_title(title, d.get("title", "")) and d.get("subject"):
                ev["subjects"] += list(d["subject"])[:40]
                ev["sources"].append("Open Library")
                break
    except Exception:                                                   # noqa: BLE001
        pass
    ev["subjects"] = list(dict.fromkeys(ev["subjects"]))
    return ev


# -------------------------------------------------------------- the model

PROMPT = """Decide which library shelf this book belongs on. Use everything below.

Answer ONLY with JSON in exactly this shape:
{{"library": "Kids" | "Teens" | "Adult", "folder": "Fiction" | "Non-fiction" | "",
  "confidence": "high" | "medium" | "low", "reason": ""}}

Meaning:
- Kids: written for children up to about 12 (picture books, early readers,
  chapter books, middle grade).
- Teens: young adult, written for readers about 13-17.
- Adult: everything else. "folder" is Fiction or Non-fiction (also for Kids/Teens).
Rules:
- Judge by who the book is written FOR, not who appears in it.
- Reading level matters: grade 2-8 prose and under ~45,000 words points to Kids;
  grade 7-9 prose to Teens; long, dense prose to Adult.
- If you do not recognise the book and the evidence is thin, answer "low".
- "reason" is one short sentence.

Title: {title}
Author: {authors}
Series: {series}
Subjects in the file: {subjects}
Catalogue categories ({sources}): {catalogue}
Description: {description}
Length: {words} words; reading level: {grade}
Opening text: {sample}
"""

_SYSTEM = "You are a careful children's and adult librarian. You never guess about a book you do not know."


def _ask(provider, book, ev, st) -> Suggestion | None:
    prompt = PROMPT.format(
        title=book["title"] or "unknown", authors=", ".join(book["authors"]) or "unknown",
        series=book["series"] or "none",
        subjects="; ".join(book["subjects"]) or "none",
        sources=", ".join(ev["sources"]) or "none found",
        catalogue="; ".join(ev["subjects"][:25]) or "none",
        description=(book["description"] or "none")[:900],
        words=st["words"], grade=st["grade"] if st["grade"] is not None else "unknown",
        sample=st["sample"] or "none")
    data = json_from(provider.complete(prompt, system=_SYSTEM).text)
    lib = str(data.get("library") or "").strip().title()
    if lib not in (KIDS, TEENS, ADULT):
        return None
    folder = {"non-fiction": "Non-fiction", "nonfiction": "Non-fiction",
              "fiction": "Fiction"}.get(str(data.get("folder") or "").strip().lower(), "")
    conf = str(data.get("confidence") or "").lower()
    conf = conf if conf in ("high", "medium", "low") else "medium"
    return Suggestion(lib, folder, conf,
                      ["model: " + (str(data.get("reason") or "").strip()[:160] or "best guess")])


def json_from(text: str) -> dict:
    m = re.search(r"\{.*\}", text or "", re.S)
    if not m:
        return {}
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError:
        return {}


# ---------------------------------------------------------------- deciding

def _band(st: dict) -> str | None:
    """What the prose alone suggests: kids | adult | None (can't tell)."""
    w, g = st["words"], st["grade"]
    if g is None or w < 500:
        return None
    if w < 45000 and g <= 8.5:
        return KIDS
    if w > 90000 or g >= 10.0:
        return ADULT
    return None


def decide(book: dict, ev: dict, st: dict, provider=None) -> Suggestion:
    subjects = list(book["subjects"]) + ev["subjects"]
    marker = recommend(subjects, book["description"], book["title"], book["series"])
    band = _band(st)
    facts = ("%d words" % st["words"]) + (", grade %s" % st["grade"] if st["grade"] is not None else "")

    if ev["maturity"] == "MATURE":
        return Suggestion(ADULT, marker.folder, "high", ["Google Books rates it mature"])

    # 1. explicit audience wording in the file or a catalogue decides
    if marker.library != ADULT:
        s = marker
        where = "the book" if recommend(book["subjects"], book["description"]).library != ADULT \
            else "a catalogue"
        s.reasons = ["%s %s" % (where, s.reasons[0])]
        if band == ADULT and s.library == KIDS:
            s.confidence = "low"
            s.reasons.append("but the text is long and dense (%s)" % facts)
        elif band == KIDS and s.library == KIDS and s.confidence == "medium":
            s.confidence = "high"
            s.reasons.append("and the text is short and easy (%s)" % facts)
        return s

    # 2. nothing explicit: ask the model with everything, then cross-check
    guess = None
    if provider:
        try:
            guess = _ask(provider, book, ev, st)
        except Exception as exc:                                        # noqa: BLE001
            print("  model failed for %s: %s" % (book["title"], str(exc)[:100]))
    if guess:
        if guess.library == ADULT and band == KIDS:
            return Suggestion(KIDS, guess.folder, "low",
                              ["the model said Adult, but the text is short and easy (%s); "
                               "check it" % facts] + guess.reasons)
        if guess.library == KIDS and band == ADULT:
            return Suggestion(ADULT, guess.folder, "low",
                              ["the model said Kids, but the text is long and dense (%s); "
                               "check it" % facts] + guess.reasons)
        agrees = band in (None, guess.library) and band is not None
        if guess.confidence == "high" and (agrees or guess.library == ADULT and band is None):
            guess.confidence = "high"
        elif guess.confidence == "low":
            guess.confidence = "low"
        else:
            guess.confidence = "medium"
        if guess.library == ADULT and not guess.folder:
            guess.folder = marker.folder
        if agrees:
            guess.reasons.append("text agrees (%s)" % facts)
        return guess

    # 3. no model: markers and the prose only
    if band == KIDS:
        return Suggestion(KIDS, marker.folder, "low",
                          ["short and easy to read (%s); nothing else says who it is for" % facts])
    reasons = list(marker.reasons)
    if band == ADULT:
        reasons.append("long, dense text (%s)" % facts)
    return Suggestion(ADULT, marker.folder, marker.confidence if band == ADULT else "low", reasons)


def assess(path: Path, provider=None, lookup_online: bool = True, fetch=_get) -> tuple[dict, Suggestion]:
    book = read_book(path)
    ev = lookup(book, fetch) if lookup_online else {"subjects": [], "maturity": None, "sources": []}
    return book, decide(book, ev, text_stats(path), provider)


# ------------------------------------------------------------- the library

def _sig(p: Path, provider, lookup_online: bool) -> str:
    st = p.stat()
    return "%d:%d:%s:%s:%s" % (st.st_size, int(st.st_mtime), RULES_VERSION,
                               type(provider).__name__ if provider else "-",
                               getattr(getattr(provider, "cfg", None), "model", "") or "")


def classify_library(library: Path, provider=None, cache_path: Path | None = None,
                     lookup_online: bool = True, refresh: bool = False,
                     log=lambda *_: None, fetch=_get) -> list[dict]:
    """Rate every finished book in `library`. Returns rows for the report."""
    cache: dict = {}
    if cache_path and cache_path.exists() and not refresh:
        try:
            cache = json.loads(cache_path.read_text())
        except (OSError, ValueError):
            cache = {}
    rows, fresh = [], {}
    for p in sorted(x for x in library.glob("*.epub") if x.is_file()):
        sig = _sig(p, provider, lookup_online)
        hit = cache.get(p.name)
        if hit and hit.get("sig") == sig:
            row = hit["row"]
        else:
            try:
                book, s = assess(p, provider, lookup_online, fetch)
            except Exception as exc:                                    # noqa: BLE001
                log("skipped %s: %s" % (p.name, str(exc)[:80]))
                continue
            row = {"name": p.name, "title": book["title"],
                   "authors": ", ".join(book["authors"]), "series": book["series"] or "",
                   "shelf": s.label(),
                   "confidence": s.confidence, "why": "; ".join(s.reasons)}
            log("%-18s %-6s %s" % (s.label(), s.confidence, book["title"][:50]))
        fresh[p.name] = {"sig": sig, "row": row}
        rows.append(row)
    rows = series_consensus(rows)
    if cache_path:
        try:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            cache_path.write_text(json.dumps(fresh, indent=1))
        except OSError:
            pass
    return rows


def series_consensus(rows: list[dict]) -> list[dict]:
    """Books of one series share an audience, so they share a shelf.

    The most common library among a series' books wins (a tie goes to the
    older shelf). A book moved this way is marked for a check, since it was
    settled by its siblings rather than by its own evidence.
    """
    order = {KIDS: 0, TEENS: 1, ADULT: 2}
    by: dict[str, list[dict]] = {}
    for r in rows:
        if r.get("series"):
            by.setdefault(r["series"].lower(), []).append(r)
    out = {r["name"]: dict(r) for r in rows}
    for group in by.values():
        if len(group) < 2:
            continue
        votes: dict[str, int] = {}
        for r in group:
            lib = r["shelf"].split(" › ")[0]
            votes[lib] = votes.get(lib, 0) + (2 if r["confidence"] == "high" else 1)
        win = max(votes, key=lambda k: (votes[k], order[k]))
        for r in group:
            lib = r["shelf"].split(" › ")[0]
            if lib != win:
                o = out[r["name"]]
                folder = r["shelf"].split(" › ")[1] if " › " in r["shelf"] else ""
                o["shelf"] = win + (" › " + folder if folder and win == ADULT else "")
                o["confidence"] = "low"
                o["why"] = "the rest of %s is %s; %s" % (r["series"], win, r["why"])
    return [out[r["name"]] for r in rows]


def section(rows: list[dict]) -> list[str]:
    """The 'Suggested libraries' part of REPORT.md."""
    if not rows:
        return []
    L = ["## Suggested libraries", "",
         "Decided from the book's own subjects, catalogue records, how long and "
         "how hard the text is, and a model that sees all of that. Anything "
         "marked *check* is a guess or the sources disagreed - look before "
         "adding it, above all before anything goes into Kids.", ""]
    for lib in (KIDS, TEENS, ADULT):
        group = [r for r in rows if r["shelf"].split(" › ")[0] == lib]
        if not group:
            continue
        L.append("**%s** (%d)" % (lib, len(group)))
        for r in group:
            place = r["shelf"].split(" › ")[1] if " › " in r["shelf"] else ""
            bits = [x for x in (place, r["why"]) if x]
            flag = " *check*" if r["confidence"] == "low" or \
                (lib != ADULT and r["confidence"] != "high") else ""
            L.append("- %s%s%s%s" % (r["title"] or r["name"],
                                     " — %s" % r["authors"] if r["authors"] else "",
                                     " (%s)" % "; ".join(bits) if bits else "", flag))
        L.append("")
    return L


def run(library: Path, provider, log=print, refresh: bool = True,
        lookup_online: bool = True) -> int:
    """`epubkit shelves`: the same step, on demand, plus a SHELVES.md."""
    rows = classify_library(library, provider, library / ".epubkit-work" / "shelves-cache.json",
                            lookup_online, refresh, log)
    if not rows:
        log("no finished books in %s" % library)
        return 1
    (library / "SHELVES.md").write_text("\n".join(section(rows)))
    log("\nwritten to %s" % (library / "SHELVES.md"))
    return 0
