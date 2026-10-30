"""What a reader of a series already knows when they open the next book.

Recaps were written one passage at a time, told to use nothing but that
passage. In book 3 of Dungeon Crawler Carl that meant the model did not know
Donut was Carl's cat or who Mordecai was: everyone long-running came out
"not yet connected to Carl", or described from one stray line.

So each series gets a memory file: who everyone is to the main character,
what the series' own words mean, and the story so far, stored book by book.
Book N is recapped with the memory as it stood at the END of book N-1, which
the reader of book N has by definition read. Nothing from book N or later is
ever shown to book N, so a rerun of book 3 after book 5 is done is as
spoiler-safe as the first run.

The file is plain JSON, meant to be opened and corrected by hand::

    {
      "series": "Dungeon Crawler Carl",
      "lead": "Carl",
      "first_person": true,
      "ensemble": null,                     <- true: no single lead (told by turns)
      "books": {"1": {"title": "...", "story": "..."}},
      "people": {
        "Donut": {
          "aliases": ["Princess Donut"],
          "fixed": "",                      <- write here to override
          "by_book": {"1": "Carl's cat; his partner in the dungeon"}
        }
      },
      "terms": {"the crawl": "..."}         <- hand-written; always shown
    }

A non-empty ``fixed`` wins over everything the model wrote for that person,
in every book. ``terms`` is only ever filled by hand.
"""
from __future__ import annotations

import html
import json
import os
import re
import tempfile
import zipfile
from pathlib import Path

#: what the memory may add to one request; gemma2 reads 8192 tokens in all,
#: and the passage, the rules and the reply need most of them
BUDGET = 4500


def default_dir() -> Path:
    root = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(root) / "epubkit" / "series"


def slug(series: str) -> str:
    s = re.sub(r"[^\w\s'-]", "", series or "", flags=re.U).strip()
    return re.sub(r"\s+", " ", s)[:120] or "untitled"


def _key(n) -> str:
    """Book numbers as keys: 3 -> "3", 2.5 -> "2.5"."""
    f = float(n)
    return str(int(f)) if f == int(f) else str(f)


def _num(k: str) -> float:
    try:
        return float(k)
    except (TypeError, ValueError):
        return float("inf")


# ------------------------------------------------------------ which book


def series_of(book: Path, record_series=None, record_index=None) -> tuple[str, float | None]:
    """(series, number in it) for a book, from its OPF or its file name.

    Some libraries name files "Series NN - Title - Author", which is more
    reliable than the series tags Grimmory sometimes writes, so the file
    name decides the number when it has one.
    """
    series, index = record_series or "", record_index
    opf = ""
    try:
        with zipfile.ZipFile(book) as z:
            name = next((n for n in z.namelist() if n.endswith(".opf")), None)
            opf = z.read(name).decode("utf-8", "ignore") if name else ""
    except (OSError, zipfile.BadZipFile):
        pass
    if not series:
        m = re.search(r'name="calibre:series"\s+content="([^"]+)"', opf) or \
            re.search(r'content="([^"]+)"\s+name="calibre:series"', opf) or \
            re.search(r'property="belongs-to-collection"[^>]*>([^<]+)<', opf)
        series = html.unescape(m.group(1)).strip() if m else ""
    stem = Path(book).name
    stem = re.sub(r"(\.kepub)?\.epub$", "", stem, flags=re.I)
    if series:
        m = re.match(r"\s*%s\s*(?:#|book|vol\.?)?\s*(\d+(?:\.\d+)?)\s*[-–—:]"
                     % re.escape(series), stem, re.I)
        if m:
            index = float(m.group(1))
    if index is None:
        m = re.search(r'name="calibre:series_index"\s+content="([\d.]+)"', opf) or \
            re.search(r'content="([\d.]+)"\s+name="calibre:series_index"', opf) or \
            re.search(r'property="group-position"[^>]*>\s*([\d.]+)\s*<', opf)
        if m:
            try:
                index = float(m.group(1))
            except ValueError:
                index = None
    if index is not None and float(index) == int(float(index)):
        index = int(float(index))
    return series, index


# ------------------------------------------------------------ who is who


def _tokens(name: str) -> list[str]:
    # not made singular: "Crawlers" (everyone) is not "Crawler Forkith"
    from .tools.dedupe_cast import canon
    return [t.strip(".,;:") for t in canon(name or "").lower().split() if t.strip(".,;:")]


def same_person(a: str, b: str) -> bool:
    """Could two names be one person? "Miss Beatrice" and "Beatrice",
    "Elle" and "Elle McGib", "Imani C" and "Imani Carter" (chat handles in
    Dungeon Crawler Carl are cut short). The first names must agree; then
    either one name is the first name alone, or one surname begins the other.
    """
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb or ta[0] != tb[0]:
        return False
    if ta == tb or len(ta) == 1 or len(tb) == 1:
        return True
    return ta[-1].startswith(tb[-1]) or tb[-1].startswith(ta[-1])


def _one_match(name: str, people: dict) -> str:
    """The one remembered person `name` could be, or "" when none or several."""
    hits = {who for who, entry in people.items()
            for n in [who] + list(entry.get("aliases") or [])
            if same_person(name, n)}
    return next(iter(hits)) if len(hits) == 1 else ""


# ------------------------------------------------------------ the memory


class Memory:
    def __init__(self, path: Path, series: str):
        self.path = Path(path)
        self.data = {"series": series, "lead": "", "first_person": None, "ensemble": None,
                     "books": {}, "people": {}, "terms": {}}
        try:
            got = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(got, dict):
                self.data.update(got)
        except (OSError, ValueError):
            pass

    def reload(self) -> None:
        """Read the file again. A book takes most of an hour, and a hand fix
        made meanwhile ("fixed", aliases, terms) must survive the save at the
        end of it - book 4 of Dungeon Crawler Carl wrote back the copy it had
        read at its start and undid two fixes."""
        fresh = Memory(self.path, self.data.get("series", ""))
        self.data = fresh.data

    @classmethod
    def open(cls, folder, series: str) -> Memory:
        folder = Path(folder) if folder else default_dir()
        return cls(folder / (slug(series) + ".json"), series)

    # -- reading, as of a point in the series

    def before(self, index) -> View:
        """What a reader starting book `index` knows: books strictly before it."""
        return View(self.data, _num(_key(index)) if index is not None else float("inf"))

    # -- writing, after a book is done

    def record_book(self, index, title: str, story: str, people: dict,
                    lead: str = "", first_person: bool | None = None,
                    ensemble: bool = False) -> None:
        """people: {name: description as of the end of this book}.

        ensemble: the book follows several people, with no single lead. The
        series is then marked so, and any lead it had is dropped.
        """
        k = _key(index)
        self.data["books"][k] = {"title": title, "story": story.strip()}
        if ensemble:
            self.data["ensemble"] = True
            self.data["lead"] = ""
        elif lead and not self.data.get("lead") and not self.data.get("ensemble"):
            self.data["lead"] = lead
        if first_person is not None and self.data.get("first_person") is None:
            self.data["first_person"] = bool(first_person)
        ppl = self.data["people"]
        for name, desc in people.items():
            if not desc:
                continue
            entry = ppl.get(name)
            if entry is None:
                entry = self._alias_of(name)
                if entry is not None:
                    # the person's own name already said who they are here
                    entry.setdefault("by_book", {}).setdefault(k, desc)
                    continue
                entry = ppl[name] = {"aliases": [], "fixed": "", "by_book": {}}
            entry.setdefault("by_book", {})[k] = desc
        self.data["books"] = dict(sorted(self.data["books"].items(), key=lambda kv: _num(kv[0])))

    def _alias_of(self, name: str):
        low = name.lower()
        for entry in self.data["people"].values():
            if low in (a.lower() for a in entry.get("aliases", [])):
                return entry
        # "Miss Beatrice" for Beatrice, "Elle McGib" for Elle
        who = _one_match(name, self.data["people"])
        if who:
            entry = self.data["people"][who]
            entry.setdefault("aliases", []).append(name)
            return entry
        # "Bea" for "Beatrice": a one-word name that begins exactly one
        # person's first name becomes their alias
        if len(name.split()) == 1 and len(low) >= 3:
            hits = [(who, e) for who, e in self.data["people"].items()
                    if who.split() and who.split()[0].lower() != low
                    and who.split()[0].lower().startswith(low)]
            if len(hits) == 1:
                hits[0][1].setdefault("aliases", []).append(name)
                return hits[0][1]
        return None

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(self.data, fh, ensure_ascii=False, indent=2)
            fh.write("\n")
        os.replace(tmp, self.path)


class View:
    """The memory as of the start of one book."""

    def __init__(self, data: dict, upto: float):
        self.lead = data.get("lead", "")
        self.ensemble = bool(data.get("ensemble"))
        self.first_person = data.get("first_person")
        self.terms = dict(data.get("terms") or {})
        self.stories = [(k, b.get("title", ""), b.get("story", ""))
                        for k, b in (data.get("books") or {}).items()
                        if _num(k) < upto and b.get("story")]
        self.stories.sort(key=lambda s: _num(s[0]))
        self.people = {}            # name -> (description, aliases, books seen)
        self._entries = {}
        for name, entry in (data.get("people") or {}).items():
            fixed = (entry.get("fixed") or "").strip()
            books = sorted((k for k in (entry.get("by_book") or {}) if _num(k) < upto),
                           key=_num)
            if not books and not fixed:
                continue
            desc = fixed or entry["by_book"][books[-1]]
            self.people[name] = (desc, list(entry.get("aliases") or []), len(books))
            self._entries[name] = entry

    def __bool__(self) -> bool:
        return bool(self.people or self.stories or self.terms)

    def name_of(self, name: str, exact: bool = False) -> str:
        """The name the earlier books knew this person by, so one person
        keeps one name from book to book; "" when nobody or several fit.
        exact: only a remembered name or alias, never a likely variant."""
        low = name.lower().strip()
        for who, (_d, aliases, _n) in self.people.items():
            if low in [who.lower()] + [a.lower() for a in aliases]:
                return who
        return "" if exact else _one_match(name, self._entries)

    def lines(self) -> dict[str, str]:
        """Each remembered description -> whose it is."""
        return {d: who for who, (d, _a, _n) in self.people.items() if d}

    def tie(self, name: str) -> str:
        """The remembered description of someone, by name or alias."""
        low = name.lower().strip()
        if not low:
            return ""
        known = self.name_of(name)
        if known:
            return self.people[known][0]
        loose = ""
        for who, (desc, aliases, _n) in self.people.items():
            for n in [who] + aliases:
                n = n.lower().strip()
                if not n:
                    continue
                if low == n:
                    return desc
                # "Mordecai" for "Mordecai the Game Guide": the first words
                # agree and one of the two is that word alone
                if low.split()[0] == n.split()[0] and (
                        len(low.split()) == 1 or len(n.split()) == 1):
                    loose = loose or desc
        return loose

    def notes(self, passage: str, words: dict, budget: int = BUDGET,
              stories: bool = True) -> str:
        """The block put in front of a passage: the people it names and the
        series' main cast first, then the story so far, newest book first."""
        out = []
        named, cast = [], []
        for who, (desc, aliases, seen) in self.people.items():
            hit = any(re.search(r"(?<![\w])%s(?![\w])" % re.escape(n.split()[0]), passage)
                      for n in [who] + aliases if n.split())
            (named if hit else cast).append((seen, who, desc))
        named.sort(key=lambda r: -r[0])
        cast.sort(key=lambda r: -r[0])
        lines = ["- %s: %s" % (w, d) for _s, w, d in named]
        lines += ["- %s: %s" % (w, d) for s, w, d in cast[:8] if s >= 2]
        size = 0
        if lines:
            out.append(words["series_people"])
            for ln in lines:
                if size + len(ln) > budget * 0.55:
                    break
                out.append(ln)
                size += len(ln)
        if self.terms:
            out.append(words["series_terms"])
            for t, d in self.terms.items():
                ln = "- %s: %s" % (t, d)
                if size + len(ln) > budget * 0.7:
                    break
                out.append(ln)
                size += len(ln)
        if stories and self.stories:
            out.append(words["series_story"])
            told = []
            for k, title, story in reversed(self.stories):
                ln = "- %s %s%s: %s" % (words["book"], k, (" (%s)" % title) if title else "",
                                       story)
                if size + len(ln) > budget:
                    ln = ln[:max(0, budget - size)].rsplit(" ", 1)[0]
                    if len(ln) < 80:
                        break
                told.append(ln)
                size += len(ln)
            out.extend(reversed(told))
        return "\n".join(out)


# ------------------------------------------------------------ names list


_TITLE_WORDS = {"princess", "prince", "queen", "king", "lord", "lady", "sir", "dame",
                "duke", "duchess", "count", "countess", "baron", "baroness", "emperor",
                "empress", "captain", "general", "doctor", "professor", "father", "mother",
                "brother", "sister", "saint", "master", "mister", "miss", "madam"}


_LINK_WORDS = {"the", "of", "de", "du", "la", "le", "van", "von", "der", "da", "del", "di"}


class Names:
    """A series' names list, from a fan wiki or written by hand: who goes by
    which names, and which names are not people at all. It never says who
    anyone is, so no description can come from it, only which names to join
    and which to leave out of the who's who.

    The file sits next to the memory as "<series>.names.tsv", one row each::

        Mordecai<TAB>Pocket Kuma; Mordy<TAB>person<TAB>1
        Desperado Club<TAB><TAB>place<TAB>3

    name, other names (";" between them), kind, and the book it first comes
    up in. A row from a later book than the one being read is skipped, so a
    name the reader hasn't met yet can't join or split anyone. Lines
    starting with "#" are notes.
    """

    PEOPLE = {"person", "crawler", "npc", "god", "ai", "pet", "character"}

    def __init__(self, rows=()):
        self.owner: dict[str, str] = {}     # lower-case name -> list name
        self.kind: dict[str, str] = {}      # list name -> its kind
        self.spelled: set[str] = set()      # every name and other name, as written
        for name, others, kind in rows:
            name = name.strip()
            if not name:
                continue
            self.spelled.update(n.strip() for n in [name] + others if n.strip())
            kind = (kind or "").strip().lower()
            self.kind[name] = "person" if kind in self.PEOPLE else (kind or "")
            for n in [name] + others:
                low = n.strip().lower()
                if not low:
                    continue
                # a name two rows both claim joins neither
                if self.owner.get(low, name) != name:
                    self.owner[low] = ""
                else:
                    self.owner[low] = name

    @classmethod
    def open(cls, folder, series: str, upto=None) -> Names:
        folder = Path(folder) if folder else default_dir()
        path = folder / (slug(series) + ".names.tsv")
        limit = _num(_key(upto)) if upto is not None else float("inf")
        rows = []
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            text = ""
        for line in text.splitlines():
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            cells = line.split("\t") + ["", "", "", ""]
            first = cells[3].strip()
            if first and _num(first) > limit:
                continue
            rows.append((cells[0], [o for o in cells[1].split(";") if o.strip()], cells[2]))
        return cls(rows)

    def __bool__(self) -> bool:
        return bool(self.owner)

    def of(self, name: str) -> str:
        """The list's name for this one, or "" when it isn't there."""
        return self.owner.get((name or "").strip().lower(), "")

    def _firsts(self) -> dict[str, str]:
        if not hasattr(self, "_first_of"):
            seen: dict[str, str] = {}
            for n, kind in self.kind.items():
                # "Potions Club girl", "Popov Brothers (Maxim & Dmitri)": a
                # description, not a name; "Princess Chonkalot": a title first
                if kind != "person" or len(n.split()) < 2 or re.search(r"[(&,]", n) or \
                        any(w[:1].islower() and w not in _LINK_WORDS for w in n.split()[1:]) or \
                        n.split()[0].lower() in _TITLE_WORDS:
                    continue
                f = n.split()[0].lower()
                seen[f] = "" if f in seen and seen[f] != n else n
            self._first_of = {f: n for f, n in seen.items() if n}
        return self._first_of

    def words(self) -> set[str]:
        """Every word of every name on the list: an entry may use them even when
        the extracts it was written from give only part of a name ("Mar" of
        "Lucia Mar" cost Cici her entry in DCC 5)."""
        return {w for n in self.spelled for w in re.findall(r"[\w'’\-]+", n)}

    def is_person(self, name: str, text: str = ""):
        """True, False, or None when the list doesn't say. A first name alone
        is a person when it begins one person's name on the list and no one
        else's, and the book (text) gives that whole name: "Lavender" when
        the book has "Brown, Lavender". Round 14 lost her to the model; round
        15, without the last condition, let in "Don", "Potions" and "Judge".
        It never joins them: two Dennises may share it."""
        who = self.of(name)
        low = (name or "").strip().lower()
        full = self._firsts().get(low) if low and " " not in low else None
        if not who and full and text:
            first, last = full.split()[0], full.split()[-1]
            if re.search(r"(?<![\w'’])(?:%s|%s,\s+%s)(?![\w'’])" % (
                    re.escape(full), re.escape(last), re.escape(first)), text):
                return True
        if not who or not self.kind.get(who):
            return None
        return self.kind[who] == "person"


# ------------------------------------------------------------ facts list


class Facts:
    """Basic facts about a series' people, from a fan wiki, used only to
    catch an entry that gets them wrong: what someone is, who they are kin
    to, and who they are not. Nothing in it is ever written into an entry.

    The file sits next to the memory as "<series>.facts.tsv", one row each::

        Florin<TAB>species<TAB>crocodilian<TAB>2
        Bea<TAB>ex-girlfriend<TAB>Carl<TAB>1
        Diwata<TAB>not<TAB>Circe<TAB>5
        Mordecai<TAB>alias<TAB>Pocket Kuma<TAB>4

    name, fact, value, and the book it first comes up in. A fact from a
    later book than the one being read is skipped, so nothing can give away
    what the reader hasn't reached. Lines starting with "#" are notes.
    """

    def __init__(self, rows=()):
        self.owner: dict[str, str] = {}             # lower-case name or alias -> name
        self.species: dict[str, set[str]] = {}      # name -> what they are
        self.ties: dict[str, list[tuple[str, str]]] = {}    # name -> [(tie, other)]
        self.nots: dict[str, set[str]] = {}         # name -> people they are not
        rows = [(n.strip(), f.strip().lower(), v.strip()) for n, f, v in rows if n.strip()]
        for name, fact, value in rows:
            self.owner.setdefault(name.lower(), name)
            if fact == "alias" and value:
                self.owner.setdefault(value.lower(), name)
        for name, fact, value in rows:
            if value and fact not in ("alias", "species"):
                self.owner.setdefault(value.lower(), value)     # kin with no row of their own
        for name, fact, value in rows:
            if not value:
                continue
            if fact == "species":
                self.species.setdefault(name, set()).add(value.lower())
            elif fact == "not":
                self.nots.setdefault(name, set()).add(self.of(value) or value)
            elif fact != "alias":
                self.ties.setdefault(name, []).append((fact, self.of(value) or value))

    @classmethod
    def open(cls, folder, series: str, upto=None) -> Facts:
        folder = Path(folder) if folder else default_dir()
        path = folder / (slug(series) + ".facts.tsv")
        limit = _num(_key(upto)) if upto is not None else float("inf")
        rows = []
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            text = ""
        for line in text.splitlines():
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            cells = line.split("\t") + ["", "", "", ""]
            first = cells[3].strip()
            if first and _num(first) > limit:
                continue
            rows.append((cells[0], cells[1], cells[2]))
        return cls(rows)

    def __bool__(self) -> bool:
        return bool(self.owner)

    def of(self, name: str) -> str:
        return self.owner.get((name or "").strip().lower(), "")

    def kinds(self) -> set[str]:
        """Every species the list knows, for spotting one in an entry."""
        return {s for v in self.species.values() for s in v}

    def tie(self, name: str, other: str) -> list[str]:
        """The ties the list gives between two people, either way round."""
        a, b = self.of(name), self.of(other)
        if not a or not b:
            return []
        return [t for t, o in self.ties.get(a, []) if o == b] + \
               [t for t, o in self.ties.get(b, []) if o == a]
