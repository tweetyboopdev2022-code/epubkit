"""Clean a book's title and find its series from the book itself.

Store copies stuff the title with marketing and the series:

    Her Dark Heart: A totally gripping crime thriller (Detective Gina Harte Book 5)
    Her Last Mistake - Detective Gina Harte Series 06 (2020)
    Detective Gina Harte 11: One Girl Missing

`tidy` takes the series out of the title (keeping its number), drops a
marketing subtitle ("A totally gripping crime thriller") and turns an author
stored surname-first ("Kovach, Carla") back into a name.

`from_book` reads the book's own "Books by ..." / "Also by ..." page. Those
list a series under its own heading, in reading order, so a title's place in
that list is its number. A list is only believed for a book whose text keeps
naming the series' lead - a standalone printed after the series list would
otherwise be counted as the next book in it. Lists seen in one book are kept
(`cache`), so a book that carries no list of its own - or is too new for the
lists it does carry - can still be placed from another book by the same
author.
"""
from __future__ import annotations

import html
import json
import os
import re
import zipfile

MARKETING = re.compile(
    r"gripping|unputdownable|page[- ]?turner|addictive|nail[- ]biting|"
    r"heart[- ]stopping|jaw[- ]dropping|edge[- ]of[- ]your[- ]seat|must[- ]read|"
    r"compulsive|spellbinding|breathtaking|heart[- ]pounding|twist|suspense|"
    r"thriller|page[- ]turning|chilling|unmissable|captivating|pulse[- ]pounding",
    re.I)
PLAIN_SUBTITLES = {"a novel", "a thriller", "a mystery", "a novella",
                   "a psychological thriller", "a crime thriller"}

# "Title (Series Book 5)", "Title (Series, #5)", "Title [Series 5]"
IN_BRACKETS = re.compile(
    r"^(?P<title>.+?)\s*[(\[](?P<series>[^()\[\]]*?[A-Za-z][^()\[\]]*?),?\s+"
    r"(?:Book|Bk\.?|#|No\.?|Number|Volume|Vol\.?|Series)?\s*(?P<idx>\d+(?:\.\d+)?)\s*[)\]]\s*$",
    re.I)
# "Title - Series Series 06 (2020)", "Title - Series Book 6", "Title - Series #6"
AFTER_DASH = re.compile(
    r"^(?P<title>.+?)\s+[-–—]\s+(?P<series>.+?)\s+"
    r"(?:Series|Book|#)\s*(?P<idx>\d+(?:\.\d+)?)(?:\s*\(\d{4}\))?\s*$", re.I)
# "Series 11: Title"
LEADING = re.compile(
    r"^(?P<series>[A-Z][\w'’.]*(?:\s+[A-Z][\w'’.]*)+)\s+(?:#|Book\s+)?"
    r"(?P<idx>\d+(?:\.\d+)?)\s*[:_]\s+(?P<title>.+)$")

GENERIC = {"detective", "di", "dci", "ds", "inspector", "series", "the", "a", "an",
           "mystery", "mysteries", "trilogy", "saga", "chronicles", "book", "books",
           "novel", "novels", "thriller", "thrillers", "and", "of", "cycle",
           "sequence", "files", "case", "cases", "adventure", "adventures", "dr",
           "sergeant", "chief", "constable", "agent", "captain", "professor"}


def _series_name(s: str) -> str:
    s = re.sub(r"\s+", " ", s or "").strip(" :,-–—")
    s = re.sub(r"\s+(?:series|books?)$", "", s, flags=re.I).strip(" :,")
    return s


def _index(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return int(f) if f == int(f) else f


def norm(t: str) -> str:
    t = html.unescape(t or "").replace("’", "'").replace("‘", "'")
    t = re.sub(r"[^\w' ]+", " ", t.lower())
    t = re.sub(r"^(the|a|an) ", "", re.sub(r"\s+", " ", t).strip())
    return t


def _strip_marketing(title: str):
    for sep in (": ", " - ", " – ", " — "):
        if sep in title:
            head, sub = title.split(sep, 1)
            s = sub.strip().lower()
            if s in PLAIN_SUBTITLES or (re.match(r"(an?|the)\b", s) and MARKETING.search(s)):
                return head.strip(), sub.strip()
    return title, None


def _uninvert(name: str) -> str:
    """"Kovach, Carla" -> "Carla Kovach". "Smith, Jr." and firms are left."""
    if name.count(",") != 1:
        return name
    last, first = (p.strip() for p in name.split(","))
    if not last or not first or re.search(r"\b(jr|sr|ii|iii|iv|inc|ltd|llc)\b\.?", first, re.I):
        return name
    if len(last.split()) > 3 or len(first.split()) > 3:
        return name
    return "%s %s" % (first, last)


def tidy(meta: dict):
    """Returns (meta, notes). Never invents a series number: every number
    comes out of the title it was written into."""
    meta = dict(meta)
    notes = []
    title = (meta.get("title") or "").strip()
    series = _series_name(meta.get("series") or "")
    idx = _index(meta.get("series_index"))

    for rx in (IN_BRACKETS, AFTER_DASH):
        m = rx.match(title)
        if m and not re.fullmatch(r"\d+", m.group("series").strip()):
            s = _series_name(m.group("series"))
            if s and (not series or norm(s) == norm(series)):
                title, series, idx = m.group("title").strip(" :-"), s, idx or _index(m.group("idx"))
                notes.append("series taken out of the title")
                break
    m = LEADING.match(title)
    if m:
        s = _series_name(m.group("series"))
        if (series and norm(s) == norm(series)) or (not series and len(s.split()) >= 2):
            title, series = m.group("title").strip(), s
            idx = idx or _index(m.group("idx"))
            notes.append("series taken out of the title")

    short, sub = _strip_marketing(title)
    if sub:
        title = short
        notes.append("marketing subtitle dropped (%s)" % sub[:60])

    authors = meta.get("authors") or []
    fixed = [_uninvert(a) for a in authors]
    if fixed != list(authors):
        notes.append("author name put first-name first")
    meta["authors"] = fixed
    meta["title"] = title or meta.get("title")
    if series:
        meta["series"] = series
        meta["series_index"] = idx
    return meta, notes


# -- the book's own list of the author's books --------------------------

BREAK = re.compile(r"<hr\b[^>]*>|</(?:div|section|ul|ol)>\s*<(?:div|section|ul|ol)\b", re.I)
BLOCK = re.compile(r"<(p|li|h[1-6])\b[^>]*>(.*?)</\1>", re.I | re.S)
HEADING = re.compile(r"^(?P<name>.{3,60}?)\s+series:?$|^series:?\s*(?P<name2>.{3,60})$", re.I)


INLINE = re.compile(r"</?(?:span|i|b|em|strong|a|small|sup|sub|u|abbr)\b[^>]*>", re.I)


def _text(fragment: str) -> str:
    # inline tags join (small caps are often "<span>D</span>ETECTIVE"); block
    # tags separate
    fragment = INLINE.sub("", fragment)
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", fragment))).strip()


def _caps(name: str) -> str:
    if name.isupper():
        return " ".join(w if w in ("DI", "DCI", "DS", "FBI", "CIA", "NYPD")
                        else w.capitalize() for w in name.split())
    return name


def book_lists(path) -> list:
    """[(series, [title, ...])] from the book's "Books by" pages."""
    out = []
    try:
        z = zipfile.ZipFile(path)
    except (zipfile.BadZipFile, OSError):
        return out
    with z:
        for n in z.namelist():
            if not n.lower().endswith((".xhtml", ".html", ".htm")):
                continue
            raw = z.read(n).decode("utf-8", "ignore")
            if len(raw) > 200000 or not re.search(r"(books|also|titles) by|published-works|series", raw, re.I):
                continue
            if not re.search(r">\s*[^<]{0,60}\bseries\b:?\s*<", raw, re.I):
                continue
            body = raw.split("<body", 1)[-1]
            for group in BREAK.split(body):
                current, titles = None, []
                for _tag, inner in BLOCK.findall(group):
                    t = _text(inner)
                    if not t:
                        continue
                    h = HEADING.match(t)
                    if h:
                        if current and titles:
                            out.append((current, titles))
                        current, titles = _caps(_series_name(h.group("name") or h.group("name2"))), []
                        continue
                    if current is not None:
                        if len(t) > 90 or re.search(r"available in|audio|sign up|listeners", t, re.I):
                            if titles:
                                out.append((current, titles))
                            current, titles = None, []
                            continue
                        titles.append(t)
                if current and titles:
                    out.append((current, titles))
    # the same list printed twice (ebook, then audio) counts once
    seen, uniq = set(), []
    for s, ts in out:
        key = (norm(s), tuple(norm(t) for t in ts))
        if key not in seen:
            seen.add(key)
            uniq.append((s, ts))
    return uniq


def _lead_named(path, series: str, times: int = 30) -> bool:
    words = [w for w in re.findall(r"[A-Za-z][\w'’]{2,}", series) if w.lower() not in GENERIC]
    if not words:
        return True
    try:
        with zipfile.ZipFile(path) as z:
            text = " ".join(z.read(n).decode("utf-8", "ignore") for n in z.namelist()
                            if n.lower().endswith((".xhtml", ".html", ".htm")))
    except (zipfile.BadZipFile, OSError):
        return False
    return any(len(re.findall(r"\b%s\b" % re.escape(w), text, re.I)) >= times for w in words)


def _place(title: str, lists):
    want = norm(title)
    best = None
    for series, titles in lists:
        names = [norm(t) for t in titles]
        if want in names:
            i = names.index(want) + 1
            # the longest list wins: a newer book's list is the fuller one
            if not best or len(titles) > best[2]:
                best = (series, i, len(titles))
    return best


def _load(cache):
    try:
        with open(cache) as f:
            return json.load(f)
    except (OSError, ValueError, TypeError):
        return {}


def from_book(path, meta: dict, cache=None):
    """(series, index, how) or (None, None, why-not). Also remembers the
    lists this book carries, for other books by the same author."""
    title = meta.get("title") or ""
    author = norm(", ".join(meta.get("authors") or []))
    lists = book_lists(path)
    store = _load(cache) if cache else {}
    if cache and lists and author:
        mine = store.setdefault(author, [])
        for s, ts in lists:
            if [s, ts] not in mine:
                mine.append([s, ts])
        try:
            os.makedirs(os.path.dirname(str(cache)) or ".", exist_ok=True)
            with open(cache, "w") as f:
                json.dump(store, f, indent=1)
        except OSError:
            pass
    found, how = _place(title, lists), "the book's own list of the author's books"
    other = _place(title, [tuple(x) for x in store.get(author, [])])
    if other and (not found or other[2] > found[2]):
        # another book by the same author lists more of the series
        if not found or (norm(other[0]) == norm(found[0]) and other[1] == found[1]):
            found, how = other, "another of the author's books"
        elif not found:
            found, how = other, "another of the author's books"
    if not found:
        return None, None, "not in any list of the author's books"
    series, idx, _n = found
    if meta.get("series") and norm(meta["series"]) != norm(series):
        return None, None, "lists call the series %r, the book %r" % (series, meta["series"])
    series = meta.get("series") or series
    if not _lead_named(path, series):
        return None, None, "listed under %s, but the book barely names its lead" % series
    return series, idx, how


def from_filename(name: str, title: str):
    """(series, index) from what the download was called, e.g.
    "Dog Man_ From the Creator of Captain Underpants (Dog Man #1) -- Dav
    Pilkey -- ...". Only believed when that name starts with the book's own
    title, so a wrongly named file cannot put a book in someone else's series."""
    stem = re.sub(r"(\.kepub)?\.(epub|mobi|azw3?|pdf|fb2(\.zip)?)$", "", name, flags=re.I)
    stem = stem.split(" -- ")[0].replace("_ ", ": ").replace("_", " ").strip()
    m = IN_BRACKETS.match(stem) or AFTER_DASH.match(stem)
    if not m or not title:
        return None, None
    if not norm(m.group("title")).startswith(norm(title)):
        return None, None
    s = _series_name(m.group("series"))
    if not s or re.fullmatch(r"\d+", s):
        return None, None
    return s, _index(m.group("idx"))
