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


# ------------------------------------------------------------ the memory


class Memory:
    def __init__(self, path: Path, series: str):
        self.path = Path(path)
        self.data = {"series": series, "lead": "", "first_person": None,
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
                    lead: str = "", first_person: bool | None = None) -> None:
        """people: {name: description as of the end of this book}."""
        k = _key(index)
        self.data["books"][k] = {"title": title, "story": story.strip()}
        if lead and not self.data.get("lead"):
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
        self.first_person = data.get("first_person")
        self.terms = dict(data.get("terms") or {})
        self.stories = [(k, b.get("title", ""), b.get("story", ""))
                        for k, b in (data.get("books") or {}).items()
                        if _num(k) < upto and b.get("story")]
        self.stories.sort(key=lambda s: _num(s[0]))
        self.people = {}            # name -> (description, aliases, books seen)
        for name, entry in (data.get("people") or {}).items():
            fixed = (entry.get("fixed") or "").strip()
            books = sorted((k for k in (entry.get("by_book") or {}) if _num(k) < upto),
                           key=_num)
            if not books and not fixed:
                continue
            desc = fixed or entry["by_book"][books[-1]]
            self.people[name] = (desc, list(entry.get("aliases") or []), len(books))

    def __bool__(self) -> bool:
        return bool(self.people or self.stories or self.terms)

    def tie(self, name: str) -> str:
        """The remembered description of someone, by name or alias."""
        low = name.lower().strip()
        if not low:
            return ""
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
