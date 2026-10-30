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

import difflib
import os
import re
from collections import Counter

from . import cast as C

FACTS = """Below is part of a chapter of the book "{title}".{about}

People named in it, with what the reader already knows about each:
{people}

Text:
{text}

For each person above that this text tells the reader something new and lasting about, write one line:
Name | kind | fact | quote

- kind: who (what they are: species, job, role, title, what they can do), tie (how they are tied to someone: family, partner, friend, enemy, ally, boss, sponsor, guide), or change (a big change: they join or leave someone, die, change form or body, are revealed to be someone or something else).
- fact: one short sentence of that kind. Not where they are or what they do, say or feel in this scene, and nothing the reader already knows.
- Say only what the text says plainly, about that person and not someone else in the scene.
- quote: a few words copied exactly from the text that show the fact.
- Use each name exactly as listed. If nothing new and lasting is said, write NONE."""

COMPOSE = """Write the who's-who entry for {name} in "{title}", for a reader at this point in the book.{about}

Who {name} is:
{who}
{changes}
Write one or two short sentences, at most 35 words. First say who {name} is: what they are and how they are tied to the people around them{hero_tie}. Then, if there are changes, what is true about them now. Never leave out who they are for something smaller. Use only the lines above. Do not start with the name. Answer with the entry only."""

SYSTEM = ("You keep a who's who for readers of a novel. You only state what the text "
          "you are given says plainly.")

#: characters of chapter text per question; the model has 32k tokens
PART_CHARS = 18000
#: people per question
PART_PEOPLE = 20
#: facts shown when writing an entry: the first few and the latest
FIRST_FACTS, LAST_FACTS = 4, 8
#: an unlisted name is put to the model only if the book uses it this often
UNLISTED_OFTEN = 2
MAX_FACT_WORDS = 30
MAX_ENTRY_WORDS = 40

_FACT_LINE = re.compile(r"^\W*([^|\n]{1,60}?)\s*\|\s*([^|\n]+?)\s*\|\s*(.+?)\s*$", re.M)
KINDS = ("who", "tie", "change")


def _bare(s: str) -> str:
    return " ".join(re.findall(r"[\w'\-]+", C._norm(s)))


def _words(text: str) -> set[str]:
    """Every word, and every word without its "'s": "Carl's" backs "Carl"."""
    found = set(re.findall(r"[\w'’\-]+", text))
    return found | {re.sub(r"['’]s$", "", w) for w in found}


def parse_facts(text: str) -> list[tuple[str, str, str, str]]:
    """[(name, kind, fact, quote)]; a line without a kind is a "who"."""
    out = []
    for m in _FACT_LINE.finditer(text or ""):
        cells = [x.strip(" *`\"“”") for x in m.group(0).strip().split("|")]
        cells = [re.sub(r"^\W*", "", cells[0])] + cells[1:]
        if len(cells) >= 4:
            name, kind, fact, quote = cells[0], cells[1].lower(), cells[2], "|".join(cells[3:])
        elif len(cells) == 3:
            name, kind, fact, quote = cells[0], "who", cells[1], cells[2]
        else:
            continue
        if name.lower() in ("name", "none") or fact.lower() in ("fact", "none", "-", ""):
            continue
        out.append((name, kind.split()[0] if kind.split() else "who", fact, quote.strip()))
    return out


def quoted(quote: str, text: str) -> bool:
    """The quote is in the text, give or take a word or a mark: the model
    copies "GRYFFINDOR!" as "Gryffindor", or drops an "and"."""
    q, t = _bare(quote), _bare(text)
    if len(q.split()) < 3:
        return False
    if q in t:
        return True
    qw, tw = q.split(), t.split()
    starts = {i for i, w in enumerate(tw) if w in qw[:2] or w == qw[-1]}
    for i in starts:
        for a in (i, i - len(qw) + 1):
            win = tw[max(0, a):max(0, a) + len(qw) + 2]
            sm = difflib.SequenceMatcher(None, win, qw, autojunk=False)
            if sum(b.size for b in sm.get_matching_blocks()) >= 0.8 * len(qw):
                return True
    return False


def fact_problems(fact: str, quote: str, text: str, forms, known: str, allowed=(),
                  facts=None, name: str = "") -> list[str]:
    """Why a fact can't be used, [] if it can. text: the part it came from;
    known: what the reader was told about this person already."""
    out = []
    if not quoted(quote, text):
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
    __slots__ = ("pos", "order", "path", "para", "text", "earlier", "kind")

    def __init__(self, pos, order, path, para, text, earlier=False, kind="who"):
        self.pos, self.order, self.path, self.para = pos, order, path, para
        self.text, self.earlier, self.kind = text, earlier, kind


def _shown_facts(facts: list) -> tuple[list, list]:
    """(who they are, what changed): every "who" and "tie" fact, the first
    few and the latest, and the latest changes. Round 17 showed the latest
    facts only, and Dumbledore ended as "watches the Quidditch match"."""
    who = [f for f in facts if f.kind != "change"]
    if len(who) > FIRST_FACTS + LAST_FACTS:
        who = who[:FIRST_FACTS] + who[-LAST_FACTS:]
    changes = [f for f in facts if f.kind == "change"][-LAST_FACTS // 2:]
    return who, changes


def _fallback(facts: list) -> str:
    """An entry without the model: the first fact, and the latest change if
    there is one, else the latest fact."""
    first = facts[0].text.rstrip(". ") + "."
    changes = [f for f in facts if f.kind == "change"]
    last = (changes or facts)[-1].text.rstrip(". ") + "."
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
        elif e["n"] >= UNLISTED_OFTEN and not _family_only(n, names_list):
            unlisted.append(n)
    if unlisted:
        kinds = C.sort_names(ask, {n: names[n] for n in unlisted}, title, words)
        took = [n for n in unlisted if kinds.get(n, ("",))[0] == "person"
                and (kinds[n][1] or names[n]["n"] >= 4)]
        for n in took:
            groups.setdefault(n, []).append(n)
        report.append("not on the names list, sorted by the model: %d, %d people: %s" % (
            len(unlisted), len(took), ", ".join(took[:30])))

    def keep_as(n):
        return (view.name_of(n, True) if view else "") or \
            (names_list.of(n) if names_list else "")
    used = {n: e["n"] for n, e in names.items()}
    groups = C.merge_names(groups, used, docs, keep_as=keep_as, hero=hero)
    # "Crawler Carl", "Dungeon Princess Donut": a word or two in front of
    # someone else's name, that are nobody's name themselves
    owner = {f: k for k, fs in groups.items() for f in fs + [k]}
    for k in sorted(groups, key=lambda k: -len(k.split())):
        w = k.split()
        if k not in groups or len(w) < 2:
            continue
        for cut in range(1, len(w)):
            rest, front = " ".join(w[cut:]), w[:cut]
            other = owner.get(rest)
            if other and other != k and other in groups and \
                    not any(owner.get(x) and x.lower() not in C._RANK_WORDS for x in front):
                groups[other].extend(groups.pop(k))
                for f in groups[other]:
                    owner[f] = other
                break
    # "Theobold" for "Theobald": one letter apart in a long name is a typo
    # (the book's or the wiki's), not a second person
    keys = sorted(groups, key=lambda k: -sum(used.get(f, 0) for f in groups[k]))
    for i, a in enumerate(keys):
        for b in keys[i + 1:]:
            if a in groups and b in groups and _one_letter(a, b):
                groups[a].extend(groups.pop(b))
    # shown as the book names them by the time they come in, not by the
    # wiki's title for them ("Tom Riddle" for Voldemort is a later book's)
    out = {}
    for _k, forms in groups.items():
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


_HONOR = re.compile(r"^(?:Mr|Mrs|Ms|Miss|Master|Madam|Madame|Mister)\.?\s+")


def _family_only(name: str, names_list) -> bool:
    """"Dursley", "Mr. Potter", "Mrs. Weasley": a family name several listed
    people share, which the model would make a person of its own."""
    rest = _HONOR.sub("", name)
    if " " in rest:
        return False
    tail = rest.lower()
    owners = {n for n, kind in names_list.kind.items()
              if kind == "person" and len(n.split()) > 1 and n.split()[-1].lower() == tail}
    return len(owners) >= 2


def _one_letter(a: str, b: str) -> bool:
    a, b = C._squash(a), C._squash(b)
    if a == b:
        return True
    if min(len(a), len(b)) < 6 or abs(len(a) - len(b)) > 1 or a[0] != b[0]:
        return False
    if len(a) == len(b):
        return sum(x != y for x, y in zip(a, b, strict=True)) == 1
    short, long_ = sorted((a, b), key=len)
    return any(long_[:i] + long_[i + 1:] == short for i in range(len(long_)))


def match(who: str, batch):
    """The person in batch a fact's name means, or None: the name as given,
    any of their names, "Carl's" for Carl, "Crawler Carl" or "Carl Johnson"
    for the one Carl asked about."""
    w = re.sub(r"['’]s$", "", (who or "").strip(" *`\"“”")).strip()
    low = w.lower()
    for p in batch:
        if any(low == f.lower() for f in p.forms):
            return p
    words = set(re.findall(r"[\w'’\-]+", low))
    hits = [p for p in batch if any(set(f.lower().split()) <= words for f in p.forms)]
    if len(hits) == 1:
        return hits[0]
    hits = [p for p in batch if any(low in f.lower().split() for f in p.forms)]
    return hits[0] if len(hits) == 1 else None


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
            # what the earlier books said is checked like any fact: rounds 16
            # and 17 carried an old "human" Florin and Li Na "sister of Zhang"
            # forward word for word, against the facts list
            bad = {x for x, _r in C.wiki_problems(line, shown, facts, words)} if line else set()
            line = " ".join(x for x in C.sentences(line) if x not in bad)
            if line and not C.LEAK.search(line):
                m = p.mentions[0]
                p.facts.append(Fact(C._pos(m[0], m[2]), m[0], m[1], m[2], line, earlier=True))
            people.append(p)
    report.append("%d people from %d names" % (len(people), len(names)))
    ctx.note("who's who: %d people from %d names" % (len(people), len(names)))
    about = ""
    if hero:
        about = " " + words["first_person" if first_person else "lead"].format(hero=hero)
    dropped = Counter()
    kept = [0]
    asked = [0]

    def known_of(p):
        return p.entry or "; ".join(f.text for f in p.facts) or "nothing yet"

    def compose(p, upto_facts):
        who, changes = _shown_facts(upto_facts)
        facts_text = "\n".join("- " + f.text for f in who + changes)
        prompt = COMPOSE.format(
            name=p.name, title=title, about=about,
            who="\n".join("- " + f.text for f in who) or "- (nothing yet)",
            changes=("\nWhat changed, in order:\n" + "\n".join("- " + f.text for f in changes)
                     + "\n") if changes else "",
            hero_tie=(", above all to %s" % hero) if hero and p.name != hero else "")
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
    for i, (order, path, _label, paras) in enumerate(docs):
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
                for who, kind, fact, quote in parse_facts(reply):
                    if kind not in KINDS:
                        dropped["scene"] += 1
                        continue
                    p = match(who, batch)
                    if p is None:
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
                    para = next((n for n, t in part if quoted(quote, t)), None)
                    if para is None:
                        para = max(m[2] for m in p.mentions
                                   if m[0] == order and part[0][0] <= m[2] <= part[-1][0])
                    f = Fact(C._pos(order, para), order, path, para, fact.rstrip(". ") + ".",
                             kind=kind)
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
    # someone named but never told about yet gets one question of their own,
    # on where the book brings them in (round 16 left 31 people in DCC 5 out)
    for p in [p for p in people if not p.rows and not p.facts]:
        first = p.mentions[0]
        bits = [(first[0], first[1], first[2])] + \
            [(m[0], m[1], m[2]) for m in p.mentions[1:4]]
        texts = []
        for order, path, para in bits:
            for o, _p, _l, paras in docs:
                if o == order:
                    texts += [(order, path, n, t) for n, t in paras if para <= n <= para + 1]
        seen, part = set(), []
        for x in texts:
            if (x[0], x[2]) not in seen:
                seen.add((x[0], x[2]))
                part.append(x)
        text = "\n\n".join(t for *_k, t in part)
        asked[0] += 1
        try:
            reply = ask(FACTS.format(title=title, about=about, text=text,
                                     people="- %s: nothing yet" % p.name),
                        SYSTEM, lambda t: bool(parse_facts(t)) or "NONE" in t.upper())
        except Exception:                                   # noqa: BLE001
            continue
        for who, kind, fact, quote in parse_facts(reply):
            if kind not in KINDS or match(who, [p]) is None or \
                    fact_problems(fact, quote, text, p.forms, "", allowed, facts, p.name):
                continue
            at = next((x for x in part if quoted(quote, x[3])), part[0])
            p.facts.append(Fact(C._pos(at[0], at[2]), at[0], at[1], at[2],
                                fact.rstrip(". ") + ".", kind=kind))
            kept[0] += 1
            break
        if p.facts:
            f = p.facts[0]
            entry = compose(p, p.facts)
            p.rows.append((f.order, f.path, f.para, "heard", entry))
            p.entry = entry
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
    if os.environ.get("CAST_HISTORY"):
        for p in people:
            if p.history:
                report.append("history %s:" % p.name)
                report.extend("  " + h for h in p.history)
    lost = [p.name for p in people if not p.rows]
    if lost:
        report.append("nothing said about: " + ", ".join(lost[:40]))
    return rows, report, C.seen_rows(shown, hero)
