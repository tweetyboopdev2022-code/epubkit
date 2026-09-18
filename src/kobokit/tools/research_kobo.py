#!/usr/bin/env python3
"""Fill the blanks in plan.template.json from Open Library, in place.

Written against the schema pipeline.py actually emits: a dict keyed by
filename, authors as [display, sort] pairs, series_index rather than position.
The older research.py expects a list of {"file": ...} entries and a plain
author string, so it corrupts this plan rather than filling it.

Two rules, both from the skill: a field that already has a value is never
overwritten, and nothing is invented. A book the sources cannot answer keeps
its blanks and is named in the report.
"""
import json
import re
import sys
import time
import urllib.parse
import urllib.request

UA = "kobo-library/1.0 (personal library tool)"
SLEEP = 1.0
TIMEOUT = 20


def get(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    for attempt in (1, 2):
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                return json.loads(r.read().decode("utf-8", "replace"))
        except Exception as exc:
            if attempt == 2:
                print("  lookup failed: %s" % exc, flush=True)
                return None
            time.sleep(3)
    return None


# A surname can be more than the last word. Taking only the last word turns
# "Ursula K. Le Guin" into "Guin, Ursula K. Le", which sorts her under G.
# The particle stays attached to the surname rather than trailing it, so this
# gives "de Maupassant, Guy" where a French library would write
# "Maupassant, Guy de". That is a deliberate trade: keeping it attached is
# consistent across languages and never splits a compound surname, and the
# convention differs by language in a way the name alone does not reveal.
PARTICLES = {"de", "del", "della", "der", "den", "di", "da", "das", "dos", "du",
             "la", "le", "van", "von", "ten", "ter", "af", "av", "bin", "ibn",
             "al", "st", "saint"}


def sort_name(display):
    parts = display.split()
    if len(parts) < 2:
        return display
    cut = len(parts) - 1
    while cut > 1 and parts[cut - 1].lower().strip(".") in PARTICLES:
        cut -= 1
    return "%s, %s" % (" ".join(parts[cut:]), " ".join(parts[:cut]))


def series_from_filename(name):
    """Her filenames already encode series as "Name NN - Title - Author".

    Reading it back is preserving what is there, not inventing: Open Library's
    search rarely carries series at all, and an empty series field would let
    rename_files.py drop the ordering the filename already had.
    """
    m = re.match(r"^(.+?)\s+(\d{1,3})\s+-\s+", name)
    if m and not re.match(r"^\d", m.group(1)):
        return m.group(1).strip(), str(int(m.group(2)))
    return ("", "")


def parse_series(raw):
    for s in raw or []:
        m = re.match(r"^(.*?)\s*[;,]\s*(?:bk\.?|book|no\.?|#)?\s*(\d+)\s*$", s, re.I)
        if m:
            return m.group(1).strip(), m.group(2)
    return ("", "")


def describe(work_key):
    if not work_key:
        return ""
    d = get("https://openlibrary.org%s.json" % work_key)
    if not d:
        return ""
    desc = d.get("description")
    if isinstance(desc, dict):
        desc = desc.get("value", "")
    return (desc or "").strip()


def lookup(title, author):
    q = urllib.parse.urlencode({"q": ("%s %s" % (title, author)).strip(), "limit": 1})
    d = get("https://openlibrary.org/search.json?" + q)
    docs = (d or {}).get("docs") or []
    return docs[0] if docs else None


def main():
    path = sys.argv[1]
    plan = json.load(open(path, encoding="utf-8"))
    if not isinstance(plan, dict):
        sys.exit("expected the plan.template.json schema: a dict keyed by filename")

    filled, gaps, skipped = {}, {}, 0
    sorted_fixed = 0
    for name, entry in plan.items():
        # A converted book carries a creator with no opf:file-as, and
        # verify_epubs blocks the build over it. Deriving the sort form is a
        # transformation of what is already there, not a lookup, so it happens
        # before anything decides whether this entry needs the network at all.
        for pair in entry.get("authors") or []:
            if isinstance(pair, list) and len(pair) == 2 and pair[0] and not pair[1]:
                pair[1] = sort_name(pair[0])
                sorted_fixed += 1

        wanted = [k for k in ("title", "series", "series_index", "date", "isbn",
                              "description", "language") if not entry.get(k)]
        # The one field worth checking even when filled. A sideloaded book
        # usually carries its Calibre import stamp as dc:date, which the skill
        # warns about: a reprint's date standing in for first publication.
        # Only ever moved backwards, and only against a sourced year.
        if entry.get("date") and "date" not in wanted:
            wanted.append("date")
        if not entry.get("authors"):
            wanted.append("authors")
        if not wanted:
            skipped += 1
            continue

        fs_name, fs_idx = series_from_filename(name)
        if fs_name and not entry.get("series"):
            entry["series"] = fs_name
            entry["series_index"] = entry.get("series_index") or fs_idx
            wanted = [k for k in wanted if k not in ("series", "series_index")]
            if not wanted:
                skipped += 1
                continue

        title = entry.get("title") or re.sub(r"\.(kepub\.)?epub$", "", name)
        authors = entry.get("authors") or []
        author = authors[0][0] if authors and isinstance(authors[0], list) else ""
        print("looking up: %s" % title[:60], flush=True)

        doc = lookup(title, author)
        time.sleep(SLEEP)
        if not doc:
            gaps[name] = wanted
            continue

        got = []
        if "title" in wanted and doc.get("title"):
            entry["title"] = doc["title"]; got.append("title")
        if "authors" in wanted and doc.get("author_name"):
            entry["authors"] = [[a, sort_name(a)] for a in doc["author_name"]]
            got.append("authors")
        if "date" in wanted and doc.get("first_publish_year"):
            sourced = int(doc["first_publish_year"])
            have = 0
            try:
                have = int((entry.get("date") or "0")[:4])
            except ValueError:
                have = 0
            if not have:
                entry["date"] = "%d-01-01" % sourced; got.append("date")
            elif sourced < have - 1:
                print("  date %s looks like a reprint, using sourced %d"
                      % (entry["date"], sourced), flush=True)
                entry["date"] = "%d-01-01" % sourced; got.append("date")
        if "isbn" in wanted and doc.get("isbn"):
            entry["isbn"] = doc["isbn"][0]; got.append("isbn")
        langs = doc.get("language") or []
        if "language" in wanted and "eng" in langs:
            entry["language"] = "en"; got.append("language")
        if not entry.get("subjects") and doc.get("subject"):
            entry["subjects"] = doc["subject"][:8]
        s_name, s_idx = parse_series(doc.get("series"))
        if "series" in wanted and s_name:
            entry["series"] = s_name; got.append("series")
        if "series_index" in wanted and s_idx:
            entry["series_index"] = s_idx; got.append("series_index")
        if "description" in wanted:
            d = describe(doc.get("key"))
            time.sleep(SLEEP)
            if d:
                entry["description"] = d; got.append("description")

        if got:
            filled[name] = got
        still = [k for k in wanted if k not in got]
        if still:
            gaps[name] = still

    json.dump(plan, open(path, "w", encoding="utf-8"), indent=2, ensure_ascii=False)

    print()
    if sorted_fixed:
        print("derived %d author sort name(s) that were missing" % sorted_fixed)
    print("%d entries already complete, %d filled, %d still have gaps"
          % (skipped, len(filled), len(gaps)))
    if gaps:
        print("\nunfilled, left blank rather than guessed:")
        for name in sorted(gaps)[:40]:
            print("  %-58s %s" % (name[:58], ", ".join(gaps[name])))
    missing_series = [n for n, g in gaps.items() if "series" in g]
    if missing_series:
        print("\n%d books have no sourced series. Their filenames will not sort "
              "by series until that is filled by hand." % len(missing_series))


if __name__ == "__main__":
    main()
