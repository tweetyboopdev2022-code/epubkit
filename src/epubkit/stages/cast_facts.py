"""The who's who from facts, chapter by chapter (recaps.cast_method = "facts").

The whole-book who's who (cast.py) asked the model for a finished entry per
person and then checked it, line by line, against extracts. Each fix to
those checks broke something else (Oct 2026, rounds 13-15 on Dungeon
Crawler Carl 5 and Harry Potter 1): capitalised words became people (Rambo,
Skull Empire, "Don" from "Don't"), true lines were thrown out with false
ones, and deaths and twists late in a book went missing. So this splits the
work differently:

1. **The series' names list decides who is listed.** Only names the fan
   wiki's list calls a person, the book's lead, the people the earlier books
   know, and names the book uses often that the model sorts as a person. The
   list's other names join their owner.
2. **The book decides what is said, a chapter at a time.** For each part of
   each chapter, the model is shown the text and the people in it with what
   the reader knows so far, and lists only what is new and lasting about
   them, each fact with the words it came from.
3. **Each fact is checked in code.** Its quote must be in the text, a name
   in it must be in the text or already known, a tie must be stated near
   that person, and the wiki's facts list may veto it (it never adds).
4. **Entries are written from the facts so far,** never from the book
   directly, and filed where the latest fact they use is told: nothing is
   said before the reader has read it.

Rows have the same shape as cast.build's, so the Kobo side does not change.
"""
from __future__ import annotations

import re
from collections import Counter

from . import cast as C

FACTS = """Below is part of a chapter of the book "{title}".{about}

People named in it, with what the reader already knows about each:
{people}

Text:
{text}

For each person above that this text tells the reader something new and lasting about, write one line:
Name | fact | quote

- fact: one short sentence about who they are: what they are (species, job, role, title), how they are tied to someone (family, partner, friend, enemy, ally, boss, sponsor), or a big change (they join or leave someone, die, change form or body, are revealed to be someone or something else).
- Not small actions, not what they say or feel in a scene, and nothing the reader already knows.
- Say only what the text says plainly, about that person and not someone else in the scene.
- quote: a few words copied exactly from the text that show the fact.
- Use each name exactly as listed. If nothing new and lasting is said, write NONE."""

COMPOSE = """Write the who's-who entry for {name} in "{title}", for a reader at this point in the book.{about}

What the reader has been told about {name}, in order (a later line can change an earlier one):
{facts}

Write one or two short sentences, at most 35 words: who {name} is and how they fit into the story, then what is true about them now if something changed (they died, left, were revealed to be someone else). Use only the lines above and keep the most important ones. Do not start with the name. Answer with the entry only."""

SYSTEM = ("You keep a who's who for readers of a novel. You only state what the text "
          "you are given says plainly.")

#: characters of chapter text per question; the model has 32k tokens
PART_CHARS = 18000
#: people per question
PART_PEOPLE = 20
#: facts shown when writing an entry: the first few and the latest
FIRST_FACTS, LAST_FACTS = 4, 8
#: an unlisted name is put to the model only if the book uses it this often
UNLISTED_OFTEN = 6
MAX_FACT_WORDS = 30
MAX_ENTRY_WORDS = 40

_FACT_LINE = re.compile(r"^\W*([^|\n]{1,60}?)\s*\|\s*([^|\n]+?)\s*\|\s*(.+?)\s*$", re.M)


def _bare(s: str) -> str:
    return " ".join(re.findall(r"[\w'\-]+", C._norm(s)))


def _words(text: str) -> set[str]:
    """Every word, and every word without its "'s": "Carl's" backs "Carl"."""
    found = set(re.findall(r"[\w'’\-]+", text))
    return found | {re.sub(r"['’]s$", "", w) for w in found}


def parse_facts(text: str) -> list[tuple[str, str, str]]:
    out = []
    for m in _FACT_LINE.finditer(text or ""):
        name, fact, quote = (x.strip(" *`\"“”") for x in m.groups())
        if name.lower() in ("name", "none") or fact.lower() in ("fact", "none", "-", ""):
            continue
        out.append((name, fact, quote))
    return out


def fact_problems(fact: str, quote: str, text: str, forms, known: str, allowed=(),
                  facts=None, name: str = "") -> list[str]:
    """Why a fact can't be used, [] if it can. text: the part it came from;
    known: what the reader was told about this person already."""
    out = []
    q = _bare(quote)
    if len(q.split()) < 3 or q not in _bare(text):
        out.append("quote")
    words = fact.split()
    if len(words) > MAX_FACT_WORDS or len(words) < 3:
        out.append("length")
    if C.LEAK.search(fact):
        out.append("leak")
    have = _words(text + "\n" + known) | set(allowed)
    mine = {w for f in forms for w in re.findall(r"[\w'’\-]+", f)}
    for i, w in enumerate(re.findall(r"[\w'’\-]+", fact)):
        w = re.sub(r"['’]s$", "", w)
        if w in mine or w in have or not (w[:1].isupper() or w.isdigit()):
            continue
        if i == 0 or C._starts_sentence(fact, w) or w.lower() in C._COMMON:
            continue
        out.append("name " + w)
        break
    rels = C.wiki_backed(C.unbacked_relations(fact, text + "\n" + known), name, facts)
    if rels:
        out.append("tie %s %s" % rels[0])
    if facts is not None and C.wiki_problems(fact, name, facts, C.WORDS["en"]):
        out.append("wiki")
    return out


def entry_problems(entry: str, facts_text: str, forms) -> list[str]:
    """Why a written entry can't be used: it must keep to its facts."""
    if not entry:
        return ["empty"]
    say = []
    n = len(entry.split())
    if n > MAX_ENTRY_WORDS or len(C.sentences(entry)) > 2:
        say.append("it is longer than two short sentences")
    if n < 4:
        say.append("it is too short to say who they are")
    if C.LEAK.search(entry) or C.SCENE.search(entry):
        say.append('it talks about "the reader", "the text" or the scene, not who they are')
    have = _words(facts_text)
    mine = {w for f in forms for w in re.findall(r"[\w'’\-]+", f)}
    extra = []
    for i, w in enumerate(re.findall(r"[\w'’\-]+", entry)):
        w = re.sub(r"['’]s$", "", w)
        if w in mine or w in have or not (w[:1].isupper() or w.isdigit()):
            continue
        if i == 0 or C._starts_sentence(entry, w) or w.lower() in C._COMMON:
            continue
        extra.append(w)
    if extra:
        say.append("it names %s, which the lines above don't" % ", ".join(extra[:3]))
    content = [w.lower() for w in re.findall(r"[^\W\d_]{5,}", entry)
               if w.lower() not in C._STOP and w not in mine]
    if len(content) >= 5:
        stems = {w.lower()[:5] for w in re.findall(r"[^\W\d_]{5,}", facts_text)}
        if sum(1 for w in content if w[:5] in stems) < 0.6 * len(content):
            say.append("most of it is not in the lines above")
    return say


class Fact:
    __slots__ = ("pos", "order", "path", "para", "text", "earlier")

    def __init__(self, pos, order, path, para, text, earlier=False):
        self.pos, self.order, self.path, self.para = pos, order, path, para
        self.text, self.earlier = text, earlier


def _shown_facts(facts: list) -> list:
    if len(facts) <= FIRST_FACTS + LAST_FACTS:
        return list(facts)
    return facts[:FIRST_FACTS] + facts[-LAST_FACTS:]


def _fallback(facts: list) -> str:
    """An entry without the model: the first fact, and the latest if it
    says something else."""
    first = facts[0].text.rstrip(". ") + "."
    last = facts[-1].text.rstrip(". ") + "."
    return first if len(facts) == 1 or last == first else first + " " + last


def roster(ctx, docs, ask, names, names_list, view, hero, title, words, report):
    """-> {shown name: [forms]}: who gets an entry."""
    whole = "\n".join(t for _o, _p, _l, paras in docs for _n, t in paras)
    groups: dict[str, list[str]] = {}
    unlisted = []
    for n, e in names.items():
        known = view.name_of(n, True) if view else ""
        who = names_list.of(n) if names_list else ""
        if known:
            groups.setdefault(known, []).append(n)
        elif who and names_list.kind.get(who) == "person":
            groups.setdefault(who, []).append(n)
        elif who:
            continue                    # the list says: not a person
        elif names_list and names_list.is_person(n, whole):
            groups.setdefault(n, []).append(n)
        elif n == hero:
            groups.setdefault(n, []).append(n)
        elif e["n"] >= UNLISTED_OFTEN:
            unlisted.append(n)
    if unlisted:
        kinds = C.sort_names(ask, {n: names[n] for n in unlisted}, title, words)
        took = [n for n in unlisted if kinds.get(n, ("",))[0] == "person"]
        for n in took:
            groups.setdefault(n, []).append(n)
        report.append("not on the names list, sorted by the model: %d, %d people: %s" % (
            len(unlisted), len(took), ", ".join(took[:30])))

    def keep_as(n):
        return (view.name_of(n, True) if view else "") or \
            (names_list.of(n) if names_list else "")
    used = {n: e["n"] for n, e in names.items()}
    groups = C.merge_names(groups, used, docs, keep_as=keep_as, hero=hero)
    # shown as the book names them by the time they come in, not by the
    # wiki's title for them ("Tom Riddle" for Voldemort is a later book's)
    out = {}
    for k, forms in groups.items():
        forms = [f for f in forms if f in names] or forms
        if hero and hero in forms:
            shown = hero
        else:
            first = min(C._pos(*names[f]["first"]) for f in forms if f in names) \
                if any(f in names for f in forms) else 0
            early = [f for f in forms if f in names and
                     C._pos(*names[f]["first"]) // 1_000_000 <= first // 1_000_000]
            shown = max(early or forms, key=lambda f: names.get(f, {}).get("n", 0))
        if sum(names.get(f, {}).get("n", 0) for f in forms) >= 2 or shown == hero:
            out[shown] = sorted(set(forms) | {shown})
    return out


def _parts(paras):
    """A chapter cut into parts of at most PART_CHARS: [(paras)]."""
    out, cur, size = [], [], 0
    for n, t in paras:
        if cur and size + len(t) > PART_CHARS:
            out.append(cur)
            cur, size = [], 0
        cur.append((n, t))
        size += len(t)
    if cur:
        out.append(cur)
    return out


def build(ctx, docs, ask, *, hero: str = "", first_person: bool = False,
          lang: str = "en", view=None, title: str = "", workers: int = 1, names_list=None,
          facts=None, probe=None):
    """Same contract as cast.build. Without a names list, or for a book not
    in English, it is cast.build."""
    if not names_list or lang != "en" or probe is not None:
        return C.build(ctx, docs, ask, hero=hero, first_person=first_person, lang=lang,
                       view=view, title=title, workers=workers, names_list=names_list,
                       facts=facts, probe=probe)
    words = C.WORDS["en"]
    report: list[str] = []
    allowed = names_list.words()
    ctx.report("who's who: finding names")
    names = C.find_names(docs)
    groups = roster(ctx, docs, ask, names, names_list, view, hero, title, words, report)
    people = []
    for shown, forms in groups.items():
        p = C.Person(shown, forms, True)
        p.mentions = C._mentions(docs, p.forms)
        p.spoke = C._first_spoken(docs, p.forms)
        p.facts = []
        if p.mentions:
            line = view.tie(shown) if view else ""
            if line and not C.LEAK.search(line):
                m = p.mentions[0]
                p.facts.append(Fact(C._pos(m[0], m[2]), m[0], m[1], m[2], line, earlier=True))
            people.append(p)
    report.append("%d people from %d names" % (len(people), len(names)))
    ctx.note("who's who: %d people from %d names" % (len(people), len(names)))
    about = ""
    if hero:
        about = " " + words["first_person" if first_person else "lead"].format(hero=hero)
    by_form = {}
    for p in people:
        for f in p.forms:
            by_form.setdefault(f, p)
            by_form.setdefault(f.lower(), p)
    dropped = Counter()
    kept = [0]
    asked = [0]

    def known_of(p):
        return p.entry or "; ".join(f.text for f in p.facts) or "nothing yet"

    def compose(p, upto_facts):
        shown = _shown_facts(upto_facts)
        facts_text = "\n".join("- " + f.text for f in shown)
        prompt = COMPOSE.format(name=p.name, title=title, about=about, facts=facts_text)
        reason = []
        for attempt in range(2):
            text = prompt + (words["again"].format(problems="\n".join("- " + r for r in reason))
                             if attempt else "")
            asked[0] += 1
            try:
                reply = ask(text, SYSTEM, lambda t: bool(t.strip()))
            except Exception:                               # noqa: BLE001
                reply = ""
            entry = C._trim(C._unnamed(re.sub(r"(?im)^\W*entry\W*:\s*", "", reply or "").strip(),
                                       p.forms))
            reason = entry_problems(entry, facts_text + "\n" + p.name, p.forms)
            if not reason:
                return entry
            p.history.append("entry: %s\n    why: %s" % (entry, "; ".join(reason)))
        dropped["entry written from the facts themselves"] += 1
        return _fallback(upto_facts)

    total = len(docs)
    for i, (order, path, label, paras) in enumerate(docs):
        ctx.report("who's who: chapter %d/%d" % (i + 1, total))
        for part in _parts(paras):
            text = "\n\n".join(t for _n, t in part)
            here = [p for p in people
                    if any(m[0] == order and part[0][0] <= m[2] <= part[-1][0]
                           for m in p.mentions)]
            if not here:
                continue
            here.sort(key=lambda p: -len(p.mentions))
            new: dict = {}
            for j in range(0, len(here), PART_PEOPLE):
                batch = here[j:j + PART_PEOPLE]
                listed = "\n".join("- %s%s: %s" % (
                    p.name, (" (also called %s)" % ", ".join(f for f in p.forms if f != p.name)[:80])
                    if len(p.forms) > 1 else "", known_of(p)) for p in batch)
                asked[0] += 1
                try:
                    reply = ask(FACTS.format(title=title, about=about, people=listed, text=text),
                                SYSTEM, lambda t: bool(parse_facts(t)) or "NONE" in t.upper())
                except Exception:                           # noqa: BLE001
                    continue
                for who, fact, quote in parse_facts(reply):
                    p = by_form.get(who) or by_form.get(who.lower())
                    if p is None or p not in batch:
                        dropped["not one of the people asked about"] += 1
                        continue
                    bad = fact_problems(fact, quote, text, p.forms, known_of(p), allowed,
                                        facts, p.name)
                    if bad:
                        dropped[bad[0].split()[0]] += 1
                        p.history.append("fact turned down (%s): %s | %s" % (
                            ", ".join(bad), fact, quote))
                        continue
                    if any(C._plain(fact) == C._plain(f.text) for f in p.facts):
                        continue
                    q = _bare(quote)
                    para = next((n for n, t in part if q in _bare(t)), None)
                    if para is None:
                        para = max(m[2] for m in p.mentions
                                   if m[0] == order and part[0][0] <= m[2] <= part[-1][0])
                    f = Fact(C._pos(order, para), order, path, para, fact.rstrip(". ") + ".")
                    new.setdefault(p.name, []).append(f)
                    kept[0] += 1
            for p in here:
                got = sorted(new.get(p.name, []), key=lambda f: f.pos)
                if not got:
                    continue
                # someone new is described where the book first tells about
                # them, and again at the last new fact
                if not p.rows and len(got) > 1:
                    p.facts.append(got[0])
                    entry = compose(p, p.facts)
                    p.rows.append((got[0].order, got[0].path, got[0].para,
                                   "met" if p.name == hero else "heard", entry))
                    p.entry = entry
                    got = got[1:]
                p.facts.extend(got)
                entry = compose(p, p.facts)
                if entry != p.entry:
                    last = got[-1]
                    p.rows.append((last.order, last.path, last.para,
                                   "met" if p.name == hero else "heard", entry))
                    p.entry = entry
                    ctx.tell("%s: %s" % (p.name, entry))
    # someone the book names but never says anything lasting about still
    # gets what the earlier books said, where they first come in
    for p in people:
        if not p.rows and p.facts:
            f = p.facts[0]
            p.rows.append((f.order, f.path, f.para, "heard", C._trim(f.text)))
    shown = [p for p in people if p.rows]
    rows = C._rows(shown, hero)
    report.append("%d people written, %d with nothing lasting said about them; %d questions; "
                  "%d facts kept" % (len(shown), len(people) - len(shown), asked[0], kept[0]))
    if dropped:
        report.append("facts turned down: " + ", ".join(
            "%d %s" % (n, why) for why, n in dropped.most_common()))
    lost = [p.name for p in people if not p.rows]
    if lost:
        report.append("nothing said about: " + ", ".join(lost[:40]))
    return rows, report, C.seen_rows(shown, hero)
