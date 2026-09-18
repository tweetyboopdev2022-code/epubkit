#!/usr/bin/env python3
"""Collapse raw cast rows so each person is one identity across the book.

Rows in and out are (order, filename, name, description). One row is kept per
person per chapter they appear in, so the reader's position selects how much of
that person's role has been revealed.
"""
import re
import sys

TITLES = r"^(?:professor|prof\.?|mr\.?|mrs\.?|miss|ms\.?|dr\.?|uncle|aunt|madame|madam|mme\.?|sir|lady|lord)\s+"
# Mr/Mrs/Miss/Aunt/Uncle mark a DIFFERENT member of the same family, so
# "Mrs. Finnigan" must not collapse into "Seamus Finnigan". Professor and
# Dr are roles the same person also answers to, so those still collapse.
FAMILY = r"^(?:mr\.?|mrs\.?|miss|ms\.?|uncle|aunt)\s+"

GENERIC = {
    "dementor", "muggle", "wizard", "witch", "goblin", "elf", "house-elf",
    "centaur", "thestral", "dragon", "owl", "cat", "giant", "ghost",
    "portrait", "prefect", "auror", "student", "teacher", "professor",
    "villager", "guard", "boy", "girl", "man", "woman", "narrator",
    "death eater", "ministry", "order", "mother", "father", "sister",
    "brother", "aunt", "uncle", "cousin", "friend", "neighbour", "neighbor",
}


def split_people(name):
    return [p.strip() for p in re.split(r"\s+and\s+|\s*&\s*", name) if p.strip()]


def canon(name):
    n = name.strip().rstrip(".,;:")
    return re.sub(TITLES, "", n, flags=re.I).strip()


def singular(tok):
    if len(tok) > 4 and tok.endswith("ies"):
        return tok[:-3] + "y"
    if len(tok) > 3 and tok.endswith("es") and tok[-3] in "sxz":
        return tok[:-2]
    if len(tok) > 3 and tok.endswith("s") and not tok.endswith("ss"):
        return tok[:-1]
    return tok


def fold(name):
    return " ".join(singular(t) for t in canon(name).lower().split())


def key(name):
    toks = [singular(t) for t in re.split(r"\s+", canon(name).lower()) if t]
    if not toks:
        return ""
    return toks[0] if len(toks) == 1 else toks[0] + " " + toks[-1]


def is_person(name):
    n = canon(name)
    low = name.lower()
    if not n or not n[0].isupper():
        return False
    if low.endswith("s") and "'" in low:
        return False
    if "'s " in low or chr(8217) + "s " in low:
        return False
    if len(n) > 45 or len(n.split()) > 5:
        return False
    return fold(name) not in GENERIC


def collapse(rows):
    split = []
    for order, fname, name, desc in rows:
        for person in split_people(name):
            if is_person(person):
                split.append((order, fname, person.strip().rstrip(".,;:"), desc))

    full = {}
    for _, _, name, _ in split:
        toks = [singular(t) for t in canon(name).lower().split() if t]
        if len(toks) > 1:
            for t in (toks[0], toks[-1]):
                full.setdefault(t, set()).add(key(name))

# If the model listed the short form and a full name as two entries in the
# SAME chapter, it meant two people: Mrs. Finnigan and Seamus Finnigan.
# Forms that never co-occur are the one person under two names.
    by_chapter = {}
    for order, _, name, _ in split:
        by_chapter.setdefault(order, set()).add(name)

    rivals = set()
    for names in by_chapter.values():
        keys = {}
        for n in names:
            keys.setdefault(key(n), []).append(n)
        shorts = [n for n in names
                  if len([t for t in canon(n).lower().split() if t]) == 1]
        for sh in shorts:
            tok = singular(canon(sh).lower().split()[0])
            for other in names:
                if other == sh:
                    continue
                toks = [singular(t) for t in canon(other).lower().split() if t]
                if len(toks) > 1 and tok in (toks[0], toks[-1]):
                    rivals.add((key(sh), key(other)))

    def resolve(name):
        # A lone token is the same person as a full name containing it, but
        # only when exactly one full name does; otherwise it splits in two.
        toks = [singular(t) for t in canon(name).lower().split() if t]
        if len(toks) == 1 and len(full.get(toks[0], ())) == 1:
            target = next(iter(full[toks[0]]))
            if (key(name), target) not in rivals:
                return target
        return key(name)

    display = {}
    for order, _, name, _ in split:
        k = resolve(name)
        # The shown name is the earliest form. Preferring the longest imported
        # later revelations backwards: chapter 1 showed "Arabella Doreen Figg"
        # when the reader only knows "Mrs. Figg".
        if k and (k not in display or order < display[k][0]):
            display[k] = (order, name)

    per_chapter = {}
    for order, fname, name, desc in split:
        k = resolve(name)
        if k:
            per_chapter[(k, order)] = (fname, desc)

    out = [(order, fname, display[k][1], desc)
           for (k, order), (fname, desc) in per_chapter.items()]
    return sorted(out, key=lambda r: (r[0], r[2]))


def read_rows(path):
    rows = []
    for line in open(path, encoding="utf-8"):
        parts = line.rstrip("\n").split("\t")
        if len(parts) >= 4:
            rows.append((int(parts[0]), parts[1], parts[2], parts[3]))
    return rows


def write_rows(path, rows):
    with open(path, "w", encoding="utf-8") as fh:
        for order, fname, name, desc in rows:
            fh.write("%d\t%s\t%s\t%s\n" % (order, fname, name, desc))


if __name__ == "__main__":
    incoming = read_rows(sys.argv[1])
    result = collapse(incoming)
    write_rows(sys.argv[2], result)
    print("in %d rows -> out %d rows, %d people"
          % (len(incoming), len(result), len({r[2] for r in result})))
