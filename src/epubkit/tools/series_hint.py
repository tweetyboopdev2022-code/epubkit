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
import threading
import zipfile

#: Three books are worked on at once, and each may add to the shared cache of
#: series lists. Without this, two writers each read the file, add their own
#: author and write it back, and one author's lists silently vanish - which is
#: how Heaven's River missed the Bobiverse list another book had just found.
_CACHE_LOCK = threading.Lock()

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
    # "qntm;" - a separator left on the end of the only name
    cleaned = []
    for a in authors:
        for part in re.split(r"\s*;\s*", a or ""):
            part = part.strip(" ,")
            if part and part not in cleaned:
                cleaned.append(part)
    if cleaned != [a for a in authors if a]:
        notes.append("stray punctuation taken off the author")
    fixed = [_uninvert(a) for a in cleaned]
    if fixed != cleaned:
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
#: "ALSO BY MARTHA WELLS" / "Books by Dennis E. Taylor" - the top of the page,
#: not a series of its own.
BYLINE = re.compile(r"^(?:also|other\s+(?:books|titles|works)|books|titles|novels)\s+(?:also\s+)?by\b", re.I)
#: Section headings on an "Also by" page that are not a series: the list
#: under them is a mixed bag, never a reading order.
NOT_A_SERIES = re.compile(
    r"\b(?:novels?|novellas?|standalones?|stand-alones?|short\s+(?:fiction|stories)|"
    r"stories|collections?|antholog\w*|non-?fiction|tie-ins?|young\s+adult|ya|"
    r"for\s+(?:younger|young)\s+readers|picture\s+books|poetry|essays|edited|"
    r"other\s+(?:books|works|titles)|graphic\s+novels?|audio|contents|"
    r"table\s+of\s+contents)\b", re.I)
#: "(coming soon)", "(short fiction)", "(with Aaron de Orive)" after a title
TRAILING_NOTE = re.compile(r"\s*[(\[][^()\[\]]*[)\]]\s*$")


def _is_caps_heading(t: str) -> bool:
    """An all-capitals line of a few words: "THE MURDERBOT DIARIES"."""
    letters = re.sub(r"[^A-Za-z]", "", t)
    return (len(letters) >= 3 and t.upper() == t and len(t.split()) <= 8
            and not re.search(r"[.!?]$", t))


INLINE = re.compile(r"</?(?:span|i|b|em|strong|a|small|sup|sub|u|abbr)\b[^>]*>", re.I)


def _text(fragment: str) -> str:
    # inline tags join (small caps are often "<span>D</span>ETECTIVE"); block
    # tags separate
    fragment = INLINE.sub("", fragment)
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", fragment))).strip()


SMALL_WORDS = {"of", "the", "and", "a", "an", "in", "on", "at", "for", "to", "or", "by"}


def _caps(name: str) -> str:
    """"THE FALL OF ILE-RIEN TRILOGY" -> "The Fall of Ile-Rien Trilogy"."""
    if not name.isupper():
        return name
    out = []
    for i, w in enumerate(name.split()):
        if w in ("DI", "DCI", "DS", "FBI", "CIA", "NYPD"):
            out.append(w)
        elif i and w.lower() in SMALL_WORDS:
            out.append(w.lower())
        else:
            out.append("-".join(p.capitalize() for p in w.split("-")))
    return " ".join(out)


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
            named_series = re.search(r">\s*[^<]{0,60}\bseries\b:?\s*<", raw, re.I)
            # Macmillan's "ALSO BY MARTHA WELLS" page never says "series": each
            # series is just an all-capitals heading over its titles.
            byline_page = re.search(r"\b(?:also|books|titles)\s+by\b",
                                    _text(raw.split("<body", 1)[-1]), re.I)
            if not named_series and not byline_page:
                continue
            body = raw.split("<body", 1)[-1]
            for group in BREAK.split(body):
                lines = [t for t in (_text(inner) for _tag, inner in BLOCK.findall(group)) if t]
                # all-capitals headings only mean something on a page whose
                # titles are not themselves in capitals
                caps_ok = bool(byline_page) and lines and \
                    sum(_is_caps_heading(t) for t in lines) * 2 < len(lines)
                current, titles = None, []

                def close():
                    if current and titles:
                        out.append((current, titles))

                for t in lines:
                    if BYLINE.match(t):
                        close()
                        current, titles = None, []
                        continue
                    h = HEADING.match(t)
                    caps = caps_ok and _is_caps_heading(t)
                    if h or caps:
                        close()
                        name = (h.group("name") or h.group("name2")) if h else t
                        if NOT_A_SERIES.search(name):
                            current, titles = None, []
                        else:
                            current, titles = _caps(_series_name(name)), []
                        continue
                    if current is not None:
                        if len(t) > 90 or re.search(r"available in|audio|sign up|listeners", t, re.I):
                            close()
                            current, titles = None, []
                            continue
                        # an unmarked subheading inside a list ("Other Novels",
                        # "Short Stories") ends it; so does a web address
                        if (len(t.split()) <= 4 and NOT_A_SERIES.search(t)
                                and not re.search(r"\d", t)) or re.search(r"\.(com|net|org)\b|www\.", t, re.I):
                            close()
                            current, titles = None, []
                            continue
                        titles.append(TRAILING_NOTE.sub("", t).strip() or t)
                close()
    # the same list printed twice (ebook, then audio) counts once
    seen, uniq = set(), []
    for s, ts in out:
        key = (norm(s), tuple(norm(t) for t in ts))
        if key not in seen:
            seen.add(key)
            uniq.append((s, ts))
    return uniq


def _lead_named(path, series: str, times: int = 2) -> bool:
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


def title_heads(title: str) -> list:
    """The title, then the title without what follows its first separator.

    "Artificial Condition--The Murderbot Diaries" and "Rogue Protocol: The
    Murderbot Diaries" are listed by their author simply as "Artificial
    Condition" and "Rogue Protocol"."""
    out = [title]
    for sep in ("--", ": ", " - ", " – ", " — "):
        if sep in title:
            head = title.split(sep, 1)[0].strip()
            if head and head not in out:
                out.append(head)
    return out


def _place(title: str, lists):
    best = None
    cands = []
    for t in title_heads(title):
        for c in (t, TRAILING_NOTE.sub("", t).strip()):
            if c and c not in cands:
                cands.append(c)
    for cand in cands:
        want = norm(cand)
        for series, titles in lists:
            names = [norm(TRAILING_NOTE.sub("", t)) for t in titles]
            if want in names:
                i = names.index(want) + 1
                # the longest list wins: a newer book's list is the fuller one
                if not best or len(titles) > best[2]:
                    best = (series, i, len(titles))
        if best:
            break
    return best


def strip_series_suffix(title: str, series: str) -> str:
    """"Rogue Protocol: The Murderbot Diaries" -> "Rogue Protocol" once the
    book is known to be in The Murderbot Diaries. A subtitle that is anything
    other than the series name is left alone."""
    if not title or not series:
        return title
    for sep in ("--", ": ", " - ", " – ", " — "):
        if sep in title:
            head, tail = title.split(sep, 1)
            if head.strip() and norm(tail) == norm(series):
                return head.strip()
    return title


def _remember(cache, author: str, lists) -> dict:
    """Add these lists to the cache file, merging with what other workers
    wrote meanwhile. Returns the cache as it now stands."""
    with _CACHE_LOCK:
        store = _load(cache)
        if lists and author:
            mine = store.setdefault(author, [])
            changed = False
            for s, ts in lists:
                if [s, list(ts)] not in mine:
                    mine.append([s, list(ts)])
                    changed = True
            if changed:
                try:
                    os.makedirs(os.path.dirname(str(cache)) or ".", exist_ok=True)
                    tmp = str(cache) + ".tmp"
                    with open(tmp, "w") as f:
                        json.dump(store, f, indent=1)
                    os.replace(tmp, str(cache))
                except OSError:
                    pass
        return store


def _opf_authors(path) -> list:
    try:
        with zipfile.ZipFile(path) as z:
            opf = next((n for n in z.namelist() if n.endswith(".opf")), None)
            if not opf:
                return []
            text = z.read(opf).decode("utf-8", "ignore")
    except (zipfile.BadZipFile, OSError):
        return []
    names = [html.unescape(re.sub(r"<[^>]+>", "", m)).strip(" ;,")
             for m in re.findall(r"<dc:creator\b[^>]*>(.*?)</dc:creator>", text, re.S)]
    return [_uninvert(n) for n in names if n]


def _stated_series(path):
    """The series name a book states for itself - its calibre:series or
    EPUB 3 collection, or the one written into its title ("All These Worlds
    (Bobiverse Book 3)")."""
    try:
        with zipfile.ZipFile(path) as z:
            opf = next((n for n in z.namelist() if n.endswith(".opf")), None)
            text = z.read(opf).decode("utf-8", "ignore") if opf else ""
    except (zipfile.BadZipFile, OSError):
        return None
    m = re.search(r'name="calibre:series"\s+content="([^"]+)"', text) or \
        re.search(r'content="([^"]+)"\s+name="calibre:series"', text) or \
        re.search(r'property="belongs-to-collection"[^>]*>([^<]+)<', text)
    if m:
        return _series_name(html.unescape(m.group(1)))
    t = re.search(r"<dc:title\b[^>]*>([^<]+)<", text)
    if t:
        meta, _ = tidy({"title": html.unescape(t.group(1)), "authors": []})
        return meta.get("series")
    return None


def _remember_name(cache, name: str) -> None:
    with _CACHE_LOCK:
        store = _load(cache)
        names = store.setdefault("_series_names", {})
        if norm(name) in names:
            return
        names[norm(name)] = name
        try:
            tmp = str(cache) + ".tmp"
            with open(tmp, "w") as f:
                json.dump(store, f, indent=1)
            os.replace(tmp, str(cache))
        except OSError:
            pass


def harvest(paths, cache) -> int:
    """Read the "Also by" lists of every book in the batch before any is
    worked on, so a book can be placed from a list that only a later book in
    the batch carries. Returns how many lists were found."""
    found = 0
    for p in paths:
        p = str(p)
        if not p.lower().endswith(".epub"):
            continue
        stated = _stated_series(p)
        if stated:
            _remember_name(cache, stated)
        lists = book_lists(p)
        if not lists:
            continue
        author = norm(", ".join(_opf_authors(p)))
        if author:
            _remember(cache, author, lists)
            found += len(lists)
    return found


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
    store = _remember(cache, author, lists) if cache else {}
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
    series, idx, n_listed = found
    if meta.get("series") and norm(meta["series"]) != norm(series):
        return None, None, "lists call the series %r, the book %r" % (series, meta["series"])
    # One series, one spelling. The ad page says "The Bobiverse", the books
    # themselves say "Bobiverse" - and two spellings are two series on the
    # Kobo. A name some book in the batch states for itself wins.
    series = meta.get("series") or \
        (store.get("_series_names") or {}).get(norm(series)) or series
    # The failure this guards against is a standalone printed straight after
    # a series list and read as its next book - so the last place in a list
    # needs the series named more than a few times. Anywhere else, the book
    # naming its series at all is enough: a Murderbot novella says
    # "Murderbot" only half a dozen times (the old bar of 30 placed none).
    if not _lead_named(path, series, 5 if idx == n_listed else 2):
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
