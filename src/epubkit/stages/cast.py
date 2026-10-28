"""The who's who, built from the whole book at once rather than passage by passage.

The passage-by-passage who's who asked the model, every few pages, for
everyone in the scene and a line on each. Over a long series that went wrong
in ways one prompt could not fix (Dungeon Crawler Carl 5, Oct 2026): Carl was
only ever "the main character"; Mordecai and Katia had the same line again and
again; "Katia Grim" and "Elle McGib" were names cut short; the Syndicate,
Borant and the Princess Posse were listed as people; Donut "conquered
Cleveland", which the book never says; and "not yet connected to Carl" was
shown to the reader as if it were a fact.

So this builds the cast first and writes about people second:

1. **Names come from the text.** Every capitalised name the book uses, found
   by searching it, so a name the model shortened or invented cannot appear.
2. **One question sorts them** into people, groups, places, things and
   real-world names the story only refers to, and says who the story treats
   as someone to keep track of. Only people are listed: everyone named who
   matters, and anyone met, or talked about often, even if never met.
3. **One entry per person, rewritten only when something lasting changes.**
   The first is written from where the book introduces them; after that, at
   each checkpoint, the model gets the entry so far and what the book has
   said about them since, and answers SAME unless who they are has moved on.
   One or two sentences. The main character gets a real entry, not a label.
4. **Every entry is checked in code before it is used:** it must quote the
   extracts it was written from, every proper name and number in it must be
   in those extracts or in what the earlier books said, it must not talk about
   "the extracts" or "the reader", and it must not be another person's entry.
   A failed entry is asked for once more with the problems listed, and then
   left out rather than shown.

Nothing here is ever told about a paragraph past the row it is filed at.
Rows have the same shape as before, so the Kobo side does not change.
"""
from __future__ import annotations

import difflib
import os
import re
import threading
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

# ------------------------------------------------------------------ prompts

SYSTEM = ("You write short, factual who's-who entries for readers of a novel. "
          "You only state what the extracts you are given say.")

SORT = """Below are names found in the book "{title}", each with a line where it appears.

For each name, answer on one line, in the same order:
Name | kind | matters

kind is one of:
- person: one individual in the story, including an animal, an AI, a god or a monster with a name
- group: a family, team, party, company, species, organisation, government or crowd
- place: a planet, country, city, building, floor, room or region
- thing: an object, item, spell, skill, title, show, ship, brand or event
- real: a real-world person the story only refers to (an author, an actor, a historical figure) who does not take part
- other: anything else, or not a name at all

matters is yes if the story treats them as someone the reader should keep track of, no if they are only a passing mention.

Names:
{names}"""

INTRO = """You are writing a who's-who entry for a reader of "{title}". The reader has read up to the end of the extracts below and nothing after them.
{about}
Person: {name}{also}
{known}
Extracts that mention them, in order (the last is where the reader is now):
{extracts}

Answer in exactly four lines:
KIND: person (anyone with a name who acts or speaks in the story: human, animal, monster, god, ghost, AI or game guide, whatever form they are in now), or not a person (a group, species, place, object, title, show, spell or move)
QUOTE: <copy a few words, exactly, from the extract that best shows who they are>
STATUS: met or heard (met = they have appeared in person; heard = so far they are only talked about)
ENTRY: <one or two short sentences saying who {name} is and why they matter to the story so far>

Rules for ENTRY:
- Only lasting facts that the extracts or the earlier books state outright: what they are, their role, and who they are to the people around them. Not where they are or what they are doing in this scene.
- When an extract is someone speaking, "I", "me" and "my" are the speaker, not {name}. Name a relationship (brother, owner, partner, boss) only if an extract says it in so many words.
- Leave out spells, skills, items and levels unless they are what the person is known for.
- Never write "the main character", "the narrator", "the speaker", "the protagonist", "currently", "not yet connected", or anything about "the extracts", "the passage" or "the reader".
- Do not start with their name."""

UPDATE = """You keep a who's-who entry up to date for a reader of "{title}". The reader has read up to the end of the extracts below and nothing after them.
{about}
Person: {name}{also}
{known}
Their entry so far:
{current}

What the book has said about them since, in order:
{extracts}

Has anything lasting changed about who {name} is: a new role, class or title, a new relationship, something revealed, a change in what they are, joining or leaving a group, or their death? A single scene is not a change; a death always is.

Answer in exactly three lines:
QUOTE: <copy a few words, exactly, from the extract that shows the change, or - if nothing changed>
STATUS: met or heard (met = they have appeared in person by now; heard = so far they are only talked about)
ENTRY: SAME if nothing lasting changed; otherwise the whole entry again, at most two short sentences: who they are and what they are to the others, with the change. Drop older details that matter less rather than adding to the list.

Rules for ENTRY:
- Only lasting facts that the extracts, the entry so far or the earlier books state outright. Not where they are or what they are doing in this scene.
- When an extract is someone speaking, "I", "me" and "my" are the speaker, not {name}.
- If the extracts show that something in the entry so far is wrong, write the whole entry again without it.
- Leave out spells, skills, items and levels unless they are what the person is known for.
- Never write "the main character", "the narrator", "the speaker", "the protagonist", "currently", "not yet connected", or anything about "the extracts", "the passage" or "the reader".
- Do not start with their name."""

AGAIN = """

Your last answer could not be used:
{problems}
Answer again in the same shape."""

SYSTEM_FR = ("Vous rédigez des fiches courtes et factuelles sur les personnages d'un roman. "
             "Vous n'affirmez que ce que disent les extraits fournis.")

SORT_FR = """Voici des noms trouvés dans le livre « {title} », chacun avec une ligne où il apparaît.

Pour chaque nom, répondez sur une ligne, dans le même ordre :
Nom | type | compte

type est l'un de :
- person : une personne de l'histoire, y compris un animal, une IA, un dieu ou un monstre qui a un nom
- group : une famille, une équipe, une entreprise, une espèce, une organisation, un gouvernement ou une foule
- place : une planète, un pays, une ville, un bâtiment, une pièce ou une région
- thing : un objet, un sort, un titre, une émission, un navire, une marque ou un événement
- real : une personne réelle que l'histoire ne fait que citer (un auteur, un acteur, un personnage historique)
- other : autre chose, ou pas un nom

compte vaut yes si l'histoire en fait quelqu'un que le lecteur doit suivre, no s'il n'est que cité en passant.

Noms :
{names}"""

INTRO_FR = """Vous rédigez une fiche de personnage pour un lecteur de « {title} ». Le lecteur a lu jusqu'à la fin des extraits ci-dessous, et rien au-delà.
{about}
Personnage : {name}{also}
{known}
Extraits qui le mentionnent, dans l'ordre (le dernier est l'endroit où en est le lecteur) :
{extracts}

Répondez en exactement quatre lignes :
KIND: person (quiconque a un nom et agit ou parle dans l'histoire : humain, animal, monstre, dieu, fantôme, IA), ou not a person (un groupe, une espèce, un lieu, un objet, un titre, une émission, un sort)
QUOTE: <recopiez exactement quelques mots de l'extrait qui montre le mieux qui c'est>
STATUS: met ou heard (met = le personnage est apparu en personne ; heard = on n'a fait qu'en parler)
ENTRY: <une ou deux phrases courtes, en français, qui disent qui est {name} et pourquoi il compte pour l'histoire jusqu'ici>

Règles pour ENTRY :
- Seulement ce que disent les extraits ou les livres précédents. Aucune supposition, rien de la suite.
- Seulement des faits durables, dits clairement : ce qu'il est, son rôle, ce qu'il est pour les autres. Pas où il est ni ce qu'il fait dans cette scène.
- Quand un extrait fait parler quelqu'un, « je » et « mon » sont celui qui parle, pas {name}. Ne citez un lien (frère, maître, associé) que si un extrait le dit en toutes lettres.
- N'écrivez jamais « le personnage principal », « le narrateur », « actuellement », « pas encore lié », ni rien sur « les extraits », « le passage » ou « le lecteur ».
- Ne commencez pas par son nom."""

UPDATE_FR = """Vous tenez à jour la fiche d'un personnage pour un lecteur de « {title} ». Le lecteur a lu jusqu'à la fin des extraits ci-dessous, et rien au-delà.
{about}
Personnage : {name}{also}
{known}
Sa fiche jusqu'ici :
{current}

Ce que le livre a dit de ce personnage depuis, dans l'ordre :
{extracts}

Quelque chose de durable a-t-il changé : un nouveau rôle, une nouvelle classe ou un nouveau titre, un nouveau lien, une révélation, un changement de nature, un départ ou une arrivée dans un groupe, ou sa mort ? Une seule scène n'est pas un changement ; une mort l'est toujours.

Répondez en exactement trois lignes :
QUOTE: <recopiez exactement quelques mots de l'extrait qui montre le changement, ou ->
STATUS: met ou heard
ENTRY: SAME si rien de durable n'a changé ; sinon toute la fiche, en deux phrases courtes au plus, en français : qui c'est et ce qu'il est pour les autres, avec le changement. Retirez les détails anciens moins importants plutôt que d'allonger la liste.

Règles pour ENTRY :
- Seulement des faits durables, dits clairement par les extraits, la fiche ou les livres précédents. Pas où il est ni ce qu'il fait dans cette scène.
- Si les extraits montrent qu'un point de la fiche est faux, réécrivez toute la fiche sans lui.
- N'écrivez jamais « le personnage principal », « le narrateur », « actuellement », « pas encore lié », ni rien sur « les extraits », « le passage » ou « le lecteur ».
- Ne commencez pas par son nom."""

AGAIN_FR = """

Votre réponse précédente n'est pas utilisable :
{problems}
Répondez de nouveau de la même façon."""

WORDS = {
    "en": {
        "system": SYSTEM, "sort": SORT, "intro": INTRO, "update": UPDATE, "again": AGAIN,
        "lead": "The story's main character is {hero}.",
        "first_person": "{hero} tells the story as \"I\": every \"I\" in the narration is {hero}.",
        "ensemble": "The story follows several main characters in turn.",
        "hero_own": "{name} is the story's main character: say who they are, not that.",
        "also": " (also called {forms})",
        "known": "What the earlier books in the series said about them: {line}",
        "unknown": "",
        "problems": {
            "shape": "it was not three lines starting QUOTE:, STATUS: and ENTRY:",
            "person": "{name} is named again and again in the book: answer KIND: person and write their entry",
            "quote": "the QUOTE is not words copied from the extracts",
            "leak": "the ENTRY talks about the extracts, the reader, or is a placeholder",
            "unbacked": "the ENTRY names {what}, which the extracts do not",
            "long": "the ENTRY is longer than two short sentences",
            "short": "the ENTRY is too short to say who they are",
            "theirs": "the ENTRY is the one written for {who}",
            "guess": "most of the ENTRY is not in the extracts",
            "relation": "the extracts never say {who} is anyone's {rel} near this person: "
                        "a relationship must be one the extracts state about {name}",
            "scene": "the ENTRY says where they are or what they are doing now; say who they are",
        },
    },
    "fr": {
        "system": SYSTEM_FR, "sort": SORT_FR, "intro": INTRO_FR, "update": UPDATE_FR,
        "again": AGAIN_FR,
        "lead": "Le personnage principal de l'histoire est {hero}.",
        "first_person": "{hero} raconte l'histoire à la première personne : chaque « je » est {hero}.",
        "ensemble": "L'histoire suit tour à tour plusieurs personnages principaux.",
        "hero_own": "{name} est le personnage principal : dites qui c'est, pas cela.",
        "also": " (aussi appelé {forms})",
        "known": "Ce que les livres précédents de la série disaient de ce personnage : {line}",
        "unknown": "",
        "problems": {
            "shape": "ce n'était pas trois lignes commençant par QUOTE:, STATUS: et ENTRY:",
            "person": "{name} est cité très souvent dans le livre : répondez KIND: person et écrivez sa fiche",
            "quote": "la QUOTE n'est pas recopiée des extraits",
            "leak": "l'ENTRY parle des extraits, du lecteur, ou ne dit rien",
            "unbacked": "l'ENTRY cite {what}, que les extraits ne citent pas",
            "long": "l'ENTRY dépasse deux phrases courtes",
            "short": "l'ENTRY est trop courte pour dire qui c'est",
            "theirs": "l'ENTRY est celle écrite pour {who}",
            "guess": "l'essentiel de l'ENTRY n'est pas dans les extraits",
            "relation": "les extraits ne disent jamais que {who} a ce lien ({rel}) ici : "
                        "un lien doit être dit par les extraits à propos de {name}",
            "scene": "l'ENTRY dit où il est ou ce qu'il fait maintenant ; dites qui c'est",
        },
    },
}

# ------------------------------------------------------------------ settings

#: how many checkpoints a book is split into at most for updating entries
CHECKPOINTS = 24
#: new mentions since the last look before an entry is looked at again
UPDATE_AFTER = 3
#: extracts given with one question, and the characters in each
EXTRACTS = 10
EXTRACT_CHARS = 420
#: names sorted in one question
SORT_BATCH = 30
#: a name used this often is a person, whatever one answer says
OFTEN = 10
#: at most this many words in an entry
MAX_WORDS = 40

# ------------------------------------------------------------------ names

_PARTICLES = {"of", "the", "de", "du", "la", "le", "van", "von", "der", "da", "del", "di"}
_TITLED = r"(?:Mr|Mrs|Ms|Dr|St|Mme|Mlle|M)\."
_CAP = r"[A-ZÀ-ÖØ-Þ][\w'’\-]*"
NAME = re.compile(
    r"(?<![\w'’\-])(?:%s\s+)?%s(?:\s+(?:(?:%s)\s+)?%s){0,3}" % (
        _TITLED, _CAP, "|".join(sorted(_PARTICLES)), _CAP))
# capitalised for grammar, not names
_COMMON = set("""
i i'm i'd i'll i've im id ok okay yes no oh ah uh um hey hi hello yeah yep nope well
the a an and but or so then now there here this that these those what who why how
where when which while if as at in on of to by for from with without into onto upon
he she it we they you me him her us them my your his its our their mine yours
mr mrs ms dr sir madam miss lord lady chapter part book prologue epilogue interlude
monday tuesday wednesday thursday friday saturday sunday january february march april
may june july august september october november december
god christ jesus heaven hell okay alright please thanks thank sorry wait look
""".split())
# French grammar words are grammar only in a book that uses them as words:
# DCC 5 lost Elle, named 105 times, to the pronoun "elle"
_COMMON_FR = set("""
le la les un une des et ou mais donc puis alors je tu il elle nous vous ils elles
mon ma mes ton ta tes son sa ses notre votre leur leurs ce cette ces qui que quoi
oui non bon bien ah oh eh chapitre partie livre monsieur madame mademoiselle
""".split())
_TITLES = {"mr", "mrs", "ms", "dr", "sir", "lady", "lord", "miss", "madam", "monsieur",
           "madame", "mademoiselle"}
_PRONOUNS = {"i", "i'm", "i’m", "i'd", "i’d", "i'll", "i’ll", "i've", "i’ve", "me", "my"}
_ENDS = re.compile(r"(?:['’]s|['’])$")
_START = re.compile(r"(?:^|[.!?…:;—–\"“”«»(\[]|\s-)\s*$")


def _clean_name(raw: str, common=None) -> str:
    common = _COMMON | _COMMON_FR if common is None else common
    name = _ENDS.sub("", raw.strip()).strip("-’'")
    words = name.split()
    # "The" or "Then" at the front is grammar; "Mr." and "Dr." are not
    while words and words[0].lower().rstrip(".") not in _TITLES and \
            words[0].lower() in common:
        words = words[1:]
    while words and words[-1].lower() in _PARTICLES:
        words = words[:-1]
    return " ".join(words)


def _caps_only(name: str) -> bool:
    letters = re.sub(r"[^\w]", "", name)
    return len(letters) > 1 and letters.isupper()


def find_names(docs) -> dict:
    """Every name-like run of capitals in the book.

    -> {name: {"n": times used, "mid": times not at a sentence start,
               "first": (order, para), "line": a line it is used in}}
    """
    found: dict = {}
    lower = Counter()
    for _order, _path, _label, paras in docs:
        for _n, text in paras:
            lower.update(w for w in re.findall(r"(?<![\w'’])[a-zà-ÿ][\w'’]*", text))
    common = _COMMON | {w for w in _COMMON_FR if lower[w] >= 3}
    for order, _path, _label, paras in docs:
        for n, text in paras:
            for m in NAME.finditer(text):
                name = _clean_name(m.group(0), common)
                if not name or _caps_only(name) or name.lower() in common:
                    continue
                if len(name) < 2 or any(w.lower() in _PRONOUNS for w in name.split()):
                    continue            # "Since I", "All I"
                # a name after "The", or of two words or more, is a name
                # even at the start of a sentence: "Katia Grimaldi stepped in"
                at_start = bool(_START.search(text[:m.start()])) and \
                    name.split()[0] == m.group(0).split()[0] and len(name.split()) == 1
                entry = found.setdefault(name, {"n": 0, "mid": 0, "first": (order, n),
                                                "line": ""})
                entry["n"] += 1
                if not at_start:
                    entry["mid"] += 1
                if not entry["line"] or (not at_start and entry["mid"] == 1):
                    entry["line"] = _around(text, m.start(), m.end(), 220)
    out = {}
    for name, e in found.items():
        one = name.split()
        if e["mid"] == 0:
            continue            # only ever the first word of a sentence
        if len(one) == 1 and lower[name.lower()] > 3 * e["mid"]:
            continue            # "Hope", "Will": an ordinary word, mostly
        out[name] = e
    return out


def _around(text: str, start: int, end: int, chars: int) -> str:
    if len(text) <= chars:
        return text.strip()
    half = max(0, (chars - (end - start)) // 2)
    a = max(0, start - half)
    b = min(len(text), end + half)
    a = text.rfind(" ", 0, a) + 1 if a else 0
    cut = text.find(" ", b)
    b = cut if cut != -1 else len(text)
    return ("…" if a else "") + text[a:b].strip() + ("…" if b < len(text) else "")


def _parse_sort(text: str) -> dict:
    out = {}
    for line in text.splitlines():
        parts = [p.strip(" *-\t`\"") for p in line.split("|")]
        if len(parts) < 2 or not parts[0]:
            continue
        kind = parts[1].lower().split()[0] if parts[1].split() else ""
        matters = len(parts) > 2 and parts[2].lower().startswith(("y", "o"))
        out[re.sub(r"^\d+[.)]\s*", "", parts[0])] = (kind, matters)
    return out


def sort_names(ask, names: dict, title: str, words: dict) -> dict:
    """name -> (kind, matters), from the model, a batch of names at a time."""
    todo = sorted(names, key=lambda n: names[n]["first"])
    got: dict = {}
    for size in (SORT_BATCH, 8):
        left = [n for n in todo if n not in got]
        for i in range(0, len(left), size):
            batch = left[i:i + size]
            listed = "\n".join("- %s: %s" % (n, names[n]["line"]) for n in batch)
            try:
                reply = ask(words["sort"].format(title=title, names=listed), words["system"],
                            lambda t: bool(_parse_sort(t)))
            except Exception:                               # noqa: BLE001
                continue
            said = _parse_sort(reply)
            low = {k.lower(): v for k, v in said.items()}
            for n in batch:
                v = said.get(n) or low.get(n.lower())
                if v:
                    got[n] = v
    return got


# ------------------------------------------------------------------ entries

ENTRY = re.compile(r"(?im)^\W*entry\W*:?\s*(.*)$")
QUOTE = re.compile(r"(?im)^\W*quote\W*:?\s*(.*)$")
STATUS = re.compile(r"(?im)^\W*status\W*:?\s*(\w+)")
LEAK = re.compile(
    r"(?i)\b(?:extracts?|the passages?|this passage|the reader|reader['’]s notes|"
    r"the text|not (?:yet )?(?:connected|linked|mentioned|named|known|stated|specified|clear)|"
    r"no (?:information|details?)|unknown|unclear|the main character|the protagonist|"
    r"main character of|les extraits?|le passage|le lecteur|pas encore li[ée]e?s?|"
    r"personnage principal|inconnue?|the narrator|le narrateur|la narratrice|the speaker|"
    r"le locuteur|la locutrice)\b")
# a scene, not who someone is: Carl was "in the vice principal's office with
# a companion" for the whole of DCC 5
SCENE = re.compile(r"(?i)\b(?:currently|right now|at the moment|at present|for now|"
                   r"in this scene|actuellement|en ce moment|pour l['’]instant)\b")
KIND = re.compile(r"(?im)^\W*kind\W*:?\s*(.*)$")
_SENTENCE = re.compile(r"(?<=[.!?])\s+(?=[A-ZÀ-Þ\"“«])")
_STOP = set("""
the and was that his her with for had not but you she they said have from this
were been their them what when who will would could there about which after into
than then also only more most some such very over just being because before while
les des une dans qui pour sur avec elle mais sont était ses leur aussi plus
""".split())


def not_a_person(text: str) -> bool:
    """The answer's KIND line says this name is not a person."""
    k = KIND.search(text or "")
    return bool(k and re.match(r"(?i)\W*(?:not|pas)\b", k.group(1)))


def _unnamed(entry: str, forms) -> str:
    """"Orren is a Valtay worm" -> "A Valtay worm": the device already shows
    the name in bold in front of the entry."""
    alts = "|".join(re.escape(f) for f in sorted(forms, key=len, reverse=True) if f)
    lead = r"(?:%s|he|she|they|il|elle)" % alts if alts else r"(?:he|she|they|il|elle)"
    m = re.match(r"(?i)^(?:%s)\s+(?:is|was|are|were|est|était)\s+(?=\w)" % lead, entry)
    if not m:
        return entry
    rest = entry[m.end():]
    return rest[:1].upper() + rest[1:]


def alias_of(entry: str, own_forms, owners: dict) -> str:
    """The person an entry says this name is: "Kiwi is the leader of the
    pack" written for "Mongoliensis Pack Leader", "The nickname for
    Tserendolgor" written for "Ren". owners: form -> shown name."""
    names = [f for f in owners if f not in own_forms and len(f) > 2]
    if not names:
        return ""
    alts = "|".join(re.escape(f) for f in sorted(names, key=len, reverse=True))
    m = re.match(r"\W*(%s)\s+(?:is|was|est|était)\b" % alts, entry) or \
        re.search(r"(?i:\b(?:nickname|another name|title|surnom|autre nom|titre)\s+"
                  r"(?:for|of|de|d['’])\s*)(%s)(?![\w])" % alts, entry)
    return owners[m.group(1)] if m else ""


def parse_entry(text: str) -> tuple[str, str, str] | None:
    """-> (quote, met|heard|"", entry), or None when it is not the answer asked for."""
    if not_a_person(text):
        return "", "", "-"
    e = ENTRY.search(text or "")
    if not e:
        return None
    q = QUOTE.search(text)
    s = STATUS.search(text)
    entry = e.group(1).strip().strip("\"'*` ")
    status = s.group(1).lower() if s else ""
    status = "met" if status.startswith(("met", "rencontr")) else \
             "heard" if status.startswith(("heard", "entendu")) else ""
    quote = q.group(1).strip().strip("\"'“”«»*` ") if q else ""
    return quote, status, entry


def _norm(s: str) -> str:
    s = s.replace("’", "'").replace("“", '"').replace("”", '"').replace("…", "...")
    return re.sub(r"\s+", " ", re.sub(r"[\"«»]", "", s)).strip().lower()


def _trim(entry: str) -> str:
    entry = re.sub(r"\s+", " ", entry).strip()
    sentences = _SENTENCE.split(entry)
    return " ".join(sentences[:2]).strip()


def problems(entry: str, quote: str, source: str, name_forms, others: dict,
             words: dict, same_ok: bool = False) -> list[str]:
    """What is wrong with an entry, as the reasons to give the model. [] = fine.

    source: the extracts and what the earlier books said - everything the
    entry may draw on. others: the entries other people already have.
    """
    say = words["problems"]
    if not entry:
        return [say["shape"]]
    if same_ok and _same(entry):
        return []
    out = []
    plain = _norm(source)
    q = _norm(quote).strip(". ")
    if len(q.split()) < 2 or q not in plain:
        # a quote stitched across a cut is still a quote if each piece of it
        # is there word for word
        pieces = [p for p in re.split(r"\.\.\.", q) if len(p.split()) >= 2]
        if not pieces or not all(p.strip(" .") in plain for p in pieces):
            out.append(say["quote"])
    if LEAK.search(entry):
        out.append(say["leak"])
    if SCENE.search(entry):
        out.append(say["scene"])
    n_words = len(entry.split())
    if n_words > MAX_WORDS or len(_SENTENCE.split(entry)) > 2:
        out.append(say["long"])
    if n_words < 4:
        out.append(say["short"])
    # every proper name and number must come from what it was written from
    have = set(re.findall(r"[\w'’\-]+", source))
    mine = {w for f in name_forms for w in re.findall(r"[\w'’\-]+", f)}
    missing = []
    for i, w in enumerate(re.findall(r"[\w'’\-]+", entry)):
        w = re.sub(r"['’]s$", "", w)
        if w in mine or w in have:
            continue
        if w.isdigit() or (w[:1].isupper() and i > 0 and not _starts_sentence(entry, w)):
            if w.lower() not in _COMMON and w.lower() not in _COMMON_FR and w not in missing:
                missing.append(w)
    if missing:
        out.append(say["unbacked"].format(what=", ".join(missing[:5])))
    # and most of what it says should be in there in some form
    content = [w.lower() for w in re.findall(r"[^\W\d_]{5,}", entry)
               if w.lower() not in _STOP and w not in mine]
    if len(content) >= 6:
        stems = {w.lower()[:5] for w in have}
        if sum(1 for w in content if w[:5] in stems) < 0.4 * len(content):
            out.append(say["guess"])
    for who, theirs in others.items():
        if theirs and difflib.SequenceMatcher(None, _norm(theirs), _norm(entry)).ratio() >= 0.85:
            out.append(say["theirs"].format(who=who))
            break
    return out


# family and ties: DCC 5 had Circe as "the mother of Donut" (she is Vrah's)
# and Li Na as "the sister of Zhang" (she is Li Jun's)
_RELATIONS = (r"mother|father|sister|brother|son|daughter|twin|wife|husband|"
              r"girlfriend|boyfriend|partner|cousin|aunt|uncle|niece|nephew|"
              r"grand(?:mother|father|son|daughter)|half-sister|half-brother|"
              r"mère|père|sœur|soeur|frère|fils|fille|jumelle?|femme|mari|cousine?|"
              r"tante|oncle")
_PARENT = ["mother", "father", "daughter", "son", "parent", "child", "mère", "père",
           "fille", "fils"]
_SIBLING = ["sister", "brother", "sibling", "twin", "sœur", "soeur", "frère", "jumel"]
_TIES = {**{w: _PARENT for w in _PARENT}, **{w: _SIBLING for w in _SIBLING},
         "half-sister": _SIBLING, "half-brother": _SIBLING,
         **{w: ["wife", "husband", "married", "femme", "mari"] for w in
            ("wife", "husband", "femme", "mari")},
         **{w: ["aunt", "uncle", "niece", "nephew", "tante", "oncle"] for w in
            ("aunt", "uncle", "niece", "nephew", "tante", "oncle")}}
_NAMEW = r"[A-ZÀ-ÖØ-Þ][\w'’\-]+(?:\s+[A-ZÀ-ÖØ-Þ][\w'’\-]+)?"
_REL_OF = re.compile(r"\b(%s)s?\s+(?:of|to|de|du|d['’])\s*(%s)" % (_RELATIONS, _NAMEW))
_REL_POSS = re.compile(r"(%s)['’]s\s+(?:\w+\s+)?(%s)\b" % (_NAMEW, _RELATIONS))


def unbacked_relations(entry: str, source: str) -> list[tuple[str, str]]:
    """[(who, relation)] the entry claims that no line of the source puts
    side by side: "mother of Donut" needs "mother" near "Donut" somewhere."""
    claims = [(m.group(2), m.group(1)) for m in _REL_OF.finditer(entry)] + \
             [(m.group(1), m.group(2)) for m in _REL_POSS.finditer(entry)]
    out = []
    for who, rel in claims:
        # "Vrah, her daughter" backs "Vrah's mother"
        stems = "|".join(re.escape(w) for w in _TIES.get(rel.lower(), [rel.lower()[:6]]))
        near = re.compile(r"(?is)%s.{0,60}(?:%s)|(?:%s).{0,60}%s" % (
            re.escape(who), stems, stems, re.escape(who)))
        if not any(near.search(line) for line in source.splitlines()):
            out.append((who, rel))
    return out


def _same(entry: str) -> bool:
    return bool(re.match(r"(?i)\W*(?:same|m[êe]me)\b", entry))


def _starts_sentence(entry: str, word: str) -> bool:
    return bool(re.search(r"(?:^|[.!?]\s+)%s\b" % re.escape(word), entry))


# ------------------------------------------------------------------ the book


def _pos(order: int, para: int) -> int:
    return order * 1_000_000 + para


def checkpoints(docs, most: int = CHECKPOINTS) -> list[tuple[int, str, int]]:
    """Where entries are looked at again: chapter ends, merged so a book has
    at most `most` of them. -> [(order, path, last paragraph)]"""
    sizes = [sum(len(t) for _n, t in paras) for _o, _p, _l, paras in docs]
    total = sum(sizes) or 1
    step = total / max(1, most)
    out, run = [], 0
    for (order, path, _label, paras), size in zip(docs, sizes, strict=True):
        run += size
        if run >= step or (order, path) == (docs[-1][0], docs[-1][1]):
            if paras:
                out.append((order, path, paras[-1][0]))
            run = 0
    return out


def _form_pattern(forms) -> re.Pattern:
    alts = sorted({f for f in forms if f}, key=len, reverse=True)
    return re.compile(r"(?<![\w'’\-])(?:%s)(?![\w\-])" % "|".join(re.escape(f) for f in alts))


def _mentions(docs, forms) -> list[tuple[int, str, int, str]]:
    """Every paragraph naming the person: [(order, path, para, extract)]."""
    pat = _form_pattern(forms)
    out = []
    for order, path, _label, paras in docs:
        for n, text in paras:
            m = pat.search(text)
            if m:
                out.append((order, path, n, _around(text, m.start(), m.end(), EXTRACT_CHARS)))
    return out


def _spread(items, k: int):
    """At most k items, spread out, always keeping the last."""
    if len(items) <= k:
        return list(items)
    step = (len(items) - 1) / (k - 1)
    return [items[round(i * step)] for i in range(k)]


def _following(docs, order: int, para: int, k: int = 2) -> list[str]:
    """The paragraphs right after a first mention, in the same chapter: how
    the book goes on to introduce someone."""
    for o, _p, _l, paras in docs:
        if o != order:
            continue
        idx = next((i for i, (n, _t) in enumerate(paras) if n == para), None)
        if idx is None:
            return []
        return [t[:EXTRACT_CHARS] for _n, t in paras[idx + 1: idx + 1 + k]]
    return []


class Person:
    def __init__(self, name: str, forms, matters: bool):
        self.name = name
        self.forms = sorted(set(forms) | {name}, key=len, reverse=True)
        self.matters = matters
        self.mentions: list = []
        self.rows: list = []            # (order, path, para, status, entry)
        self.entry = ""
        self.status = "heard"
        self.asked = 0
        self.retried = 0
        self.failed = 0
        self.not_person = False
        self.remembered = False
        self.spoke = None               # (order, path, para) first heard speaking
        self.alias_of = ""              # the entry said this is someone else


def build(ctx, docs, ask, *, hero: str = "", first_person: bool = False,
          lang: str = "en", view=None, title: str = "", workers: int = 1):
    """-> (people rows, report lines). Rows are
    (order, path, para, name, met|heard, times named so far, entry)."""
    words = WORDS.get(lang, WORDS["en"])
    report: list[str] = []
    ctx.report("who's who: finding names")
    names = find_names(docs)
    remembered = set()
    if view:
        for n in list(names):
            if view.name_of(n, True):
                remembered.add(n)
    # a name used once is a passing mention, unless the earlier books know it
    asked_about = {n: e for n, e in names.items()
                   if e["n"] >= 2 or n in remembered or (hero and n == hero)}
    ctx.report("who's who: sorting %d names" % len(asked_about))
    kinds = sort_names(ask, asked_about, title, words)
    by_kind = Counter(k for k, _m in kinds.values())
    report.append("%d names found; %d asked about: %s" % (
        len(names), len(asked_about),
        ", ".join("%d %s" % (c, k) for k, c in by_kind.most_common())))
    # keep everyone named who matters, anyone talked about often, anyone the
    # earlier books know; never a group, a place, a thing or a real author
    keep = []
    for n, e in asked_about.items():
        kind, matters = kinds.get(n, ("", False))
        if kind != "person":
            if kind == "" and (n in remembered or n == hero):
                keep.append(n)
            continue
        if matters or e["n"] >= 4 or n in remembered or n == hero:
            keep.append(n)
    set_aside = sorted((n for n in asked_about if kinds.get(n, ("",))[0] in
                        ("group", "real")), key=lambda n: -asked_about[n]["n"])
    if set_aside:
        report.append("not people: " + ", ".join(set_aside[:40]))

    # one person, however many names: "Harry", "Harry Potter", "Mr. Potter"
    # are not all one, but the first two are
    from ..tools import dedupe_cast
    order = sorted(keep, key=lambda n: _pos(*names[n]["first"]))
    try:
        ident = dedupe_cast.identities([(_pos(*names[n]["first"]), "", n, "") for n in order])
    except Exception:                                       # noqa: BLE001
        ident = {n: n for n in order}
    groups: dict[str, list[str]] = {}
    for n in order:
        shown = ident.get(n) or n
        if view:
            shown = view.name_of(shown, True) or view.name_of(n, True) or shown
        groups.setdefault(shown, []).append(n)
    groups = merge_names(groups, {n: e["n"] for n, e in names.items()}, docs,
                         keep_as=(lambda n: view.name_of(n, True)) if view else None,
                         hero=hero)
    people = []
    for shown, forms in groups.items():
        matters = any(kinds.get(f, ("", False))[1] for f in forms)
        p = Person(shown, forms, matters)
        p.mentions = _mentions(docs, p.forms)
        p.spoke = _first_spoken(docs, p.forms)
        p.remembered = bool(view and any(view.name_of(f, True) for f in p.forms))
        if p.mentions:
            people.append(p)
    people.sort(key=lambda p: -len(p.mentions))
    owners = {f: p.name for p in people for f in p.forms}
    counts = {p.name: len(p.mentions) for p in people}
    ctx.note("who's who: %d people from %d names" % (len(people), len(names)))

    about = []
    if hero:
        about.append(words["first_person" if first_person else "lead"].format(hero=hero))
    else:
        about.append(words["ensemble"])
    marks = checkpoints(docs)
    done = [0]
    lock = threading.Lock()
    current: dict[str, str] = {}

    def lead_of(p) -> bool:
        return bool(hero) and p.name == hero

    def known_line(p) -> str:
        line = view.tie(p.name) if view else ""
        return words["known"].format(line=line) if line else words["unknown"]

    def one_question(p, template, extracts, current_entry=""):
        known = known_line(p)
        others_forms = [f for f in p.forms if f != p.name]
        prompt = template.format(
            title=title, about="\n".join(about + ([words["hero_own"].format(name=p.name)]
                                                  if hero and p.name == hero else [])),
            name=p.name, also=words["also"].format(forms=", ".join(others_forms[:4]))
            if others_forms else "", known=known,
            extracts="\n".join("%d. %s" % (i + 1, x) for i, x in enumerate(extracts)),
            current=current_entry)
        source = "\n".join(extracts + [known, current_entry] + about)
        update = bool(current_entry)
        reason = []
        for attempt in range(2):
            text = prompt + (words["again"].format(problems="\n".join("- " + r for r in reason))
                             if attempt else "")
            p.asked += 1
            try:
                reply = ask(text, words["system"], lambda t: parse_entry(t) is not None)
            except Exception:                               # noqa: BLE001
                reply = ""
            got = parse_entry(reply)
            if not got:
                reason = [words["problems"]["shape"]]
                continue
            quote, status, entry = got
            if entry == "-" and not quote and not update:
                # Only a rare name is dropped on this say-so. Round two of
                # the DCC pilot lost Mordecai from books 3 and 4 this way
                # (he was in another body), and Sledge, Prepotente and the
                # gods with him: someone the book names again and again,
                # or the earlier books know, is a person.
                if len(p.mentions) < OFTEN and not p.remembered and not lead_of(p):
                    p.not_person = True
                    return "", None
                reason = [words["problems"]["person"].format(name=p.name)]
                continue
            # a rare name whose entry is about someone the book names more
            # is that person under a title or a nickname
            other = "" if update or len(p.mentions) >= OFTEN else \
                alias_of(entry, p.forms, owners)
            if other and counts.get(other, 0) > len(p.mentions):
                p.alias_of = other
                return "", None
            entry = _trim(_unnamed(entry, p.forms))
            with lock:
                others = {w: e for w, e in current.items() if w != p.name}
            reason = problems(entry, quote, source, p.forms, others, words, same_ok=update)
            if not (update and _same(entry)):
                for who, rel in unbacked_relations(entry, source):
                    reason.append(words["problems"]["relation"].format(
                        who=who, rel=rel, name=p.name))
                if attempt and reason and all(
                        r.startswith(words["problems"]["relation"][:20]) for r in reason):
                    # a good entry with one bad tie: keep it without that sentence
                    bad = [w for w, _r in unbacked_relations(entry, source)]
                    kept = " ".join(x for x in _SENTENCE.split(entry)
                                    if not any(w in x for w in bad))
                    if len(kept.split()) >= 4:
                        entry, reason = kept, []
            if not reason:
                if attempt:
                    p.retried += 1
                if update and _same(entry):
                    return status, ""
                return status, entry
        p.failed += 1
        ctx.log.debug("    who's who: %s left out: %s", p.name, "; ".join(reason))
        return "", None

    def write(p):
        first = p.mentions[0]
        f_order, f_path, f_para = first[0], first[1], first[2]
        extracts = [first[3]] + _following(docs, f_order, f_para)
        status, entry = one_question(p, words["intro"], extracts)
        if p.not_person or p.alias_of:
            with lock:
                done[0] += 1
            return
        last_seen = 0
        lead = bool(hero) and p.name == hero
        if entry:
            p.entry, p.status = entry, "met" if lead else (status or "heard")
            p.rows.append((f_order, f_path, f_para, p.status, entry))
            with lock:
                current[p.name] = entry
        # main and supporting people are looked at again as the book goes on
        regular = p.matters or len(p.mentions) >= 8 or lead
        for m_order, m_path, m_para in marks:
            upto = _pos(m_order, m_para)
            if upto < _pos(f_order, f_para):
                continue
            new = [x for x in p.mentions[last_seen:] if _pos(x[0], x[2]) <= upto
                   and _pos(x[0], x[2]) > _pos(f_order, f_para)]
            if not new:
                continue
            enough = len(new) >= (UPDATE_AFTER if regular else UPDATE_AFTER * 2)
            if not enough and p.entry:
                continue
            last_seen = p.mentions.index(new[-1]) + 1
            picked = [x[3] for x in _spread(new, EXTRACTS)]
            if p.entry:
                status, entry = one_question(p, words["update"], picked, p.entry)
            else:
                # the first entry failed its checks: try again from here
                status, entry = one_question(p, words["intro"], picked)
            if p.not_person or p.alias_of:
                break
            if entry is None:
                continue
            was_met = p.status == "met"
            if status == "met":
                p.status = "met"
            if entry:
                p.entry = entry
                with lock:
                    current[p.name] = entry
            # the lead is filed again at every checkpoint, so their count
            # (and their place at the top of the list) keeps up
            if entry or (p.entry and (lead or (p.status == "met" and not was_met))):
                p.rows.append((m_order, m_path, m_para, p.status, p.entry))
            if not regular and p.entry:
                break               # a minor figure gets one entry, maybe a second
        # one last look over everything since the entry last changed: in DCC 5
        # Odette's stopped at chapter 57 and Diwata's at 76, each checkpoint
        # having seen too little to call it a change
        if regular and p.entry and p.rows and marks and not p.not_person:
            last = _pos(p.rows[-1][0], p.rows[-1][2])
            late = [x for x in p.mentions if _pos(x[0], x[2]) > last]
            if len(late) >= UPDATE_AFTER * 3:
                status, entry = one_question(p, words["update"],
                                             [x[3] for x in _spread(late, EXTRACTS)], p.entry)
                if entry:
                    m_order, m_path, m_para = marks[-1]
                    p.entry = entry
                    if status == "met":
                        p.status = "met"
                    p.rows.append((m_order, m_path, m_para, p.status, entry))
        with lock:
            done[0] += 1
            ctx.report("who's who: %d/%d people" % (done[0], len(people)))

    ctx.report("who's who: 0/%d people" % len(people))
    if workers > 1:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            list(pool.map(write, people))
    else:
        for p in people:
            write(p)

    dropped = [p.name for p in people if p.not_person]
    if dropped:
        report.append("not people after all: " + ", ".join(dropped[:40]))
    same = ["%s (%s)" % (p.name, p.alias_of) for p in people if p.alias_of]
    if same:
        report.append("someone else's name: " + ", ".join(same[:40]))
    people = [p for p in people if not p.not_person and not p.alias_of]
    rows = _rows(people, hero)
    shown = [p for p in people if p.rows]
    report.append("%d people written, %d left out after failing the checks twice; "
                  "%d questions, %d answers fixed on a second try" % (
                      len(shown), sum(1 for p in people if not p.rows),
                      sum(p.asked for p in people), sum(p.retried for p in people)))
    lost = [p.name for p in people if not p.rows]
    if lost:
        report.append("left out: " + ", ".join(lost[:40]))
    return rows, report


_SPEECH = (r"said|says|asked|asks|replied|replies|shouted|yelled|whispered|growled|"
           r"muttered|snapped|added|cried|screamed|answered|dit|demanda|répondit|"
           r"cria|murmura")


def _first_spoken(docs, forms):
    """Where someone is first heard speaking ("“Late,” Mordecai said"): from
    there on the reader has met them, whatever the model said. DCC 5 had
    Donut and Mordecai as "only heard of" at their first rows."""
    alts = "|".join(re.escape(f) for f in sorted(set(forms), key=len, reverse=True) if f)
    if not alts:
        return None
    pat = re.compile(r"(?<![\w'’\-])(?:%s)(?![\w\-])\s*,?\s+(?:%s)\b|\b(?:%s)\s+(?:%s)(?![\w\-])"
                     % (alts, _SPEECH, _SPEECH, alts))
    for order, path, _label, paras in docs:
        for n, text in paras:
            if re.search("[\"“”«»]", text) and pat.search(text):
                return (order, path, n)
    return None


def _squash(name: str) -> str:
    return re.sub(r"[\s\-’'.]", "", name).lower()


_RANK_WORDS = {"princess", "prince", "queen", "king", "lord", "lady", "captain",
               "commandant", "duke", "duchess", "emperor", "empress", "sir", "crawler",
               "hunter", "princesse", "reine", "roi"}


def merge_names(groups: dict, used: dict, docs, keep_as=None, hero: str = "") -> dict:
    """Fold names that are one person into one entry, after the dedupe:
    "Li-Na" and "Li Na", "Jenn’ifer" and "Jennifer"; "Miriam" and "Miriam
    Dom"; "Princess" alone and the one Princess the book means by it;
    "Sledgie" for "Sledge", "Bea" for "Beatrice"; and "Sledge, also known
    as Very Sullen". groups: shown name -> its forms; used: form -> times
    the book uses it. A name the earlier books know is never folded away.
    """
    groups = {k: list(v) for k, v in groups.items()}
    uses = {k: sum(used.get(f, 0) for f in v) for k, v in groups.items()}

    def into(small, big):
        if small == big or small not in groups or big not in groups:
            return
        if keep_as and keep_as(small) and keep_as(small) != keep_as(big):
            return
        groups[big].extend(groups.pop(small))
        uses[big] = uses.get(big, 0) + uses.pop(small, 0)

    # spelled the same but for hyphens, apostrophes and spaces
    by: dict[str, list[str]] = {}
    for k in list(groups):
        by.setdefault(_squash(k), []).append(k)
    for ks in by.values():
        ks.sort(key=lambda k: -uses.get(k, 0))
        for k in ks[1:]:
            into(k, ks[0])
    # one word that begins exactly one longer name, or a rank on its own
    # that one person carries far more than anyone else with it
    for k in sorted(groups, key=lambda k: len(k.split())):
        if k not in groups or len(k.split()) != 1:
            continue
        low = k.lower()
        longer = [o for o in groups if o != k and len(o.split()) > 1
                  and o.split()[0].lower() == low]
        if len(longer) == 1 and low not in _RANK_WORDS:
            into(k, longer[0])
        elif longer and low in _RANK_WORDS:
            longer.sort(key=lambda o: -uses.get(o, 0))
            if len(longer) == 1 or uses.get(longer[0], 0) >= 5 * uses.get(longer[1], 0):
                into(k, longer[0])
    # a short form of a one-word name: "Bea", "Gus", "Sledgie"
    singles = sorted((k for k in groups if len(k.split()) == 1), key=len)
    for k in singles:
        if k not in groups or len(k) < 3 or k == hero:
            continue
        low = k.lower()
        hits = []
        for o in groups:
            ol = o.lower()
            if o == k or o == hero or len(o.split()) != 1:
                continue
            common = len(os.path.commonprefix([low, ol]))
            if (ol.startswith(low) and len(ol) > len(low)) or \
                    (common >= max(4, min(len(low), len(ol)) - 1) and abs(len(ol) - len(low)) <= 2):
                hits.append(o)
        if len(hits) == 1:
            a, b = k, hits[0]
            big, small = (a, b) if uses.get(a, 0) >= uses.get(b, 0) else (b, a)
            into(small, big)
    # "Miss Beatrice" is Bea when Beatrice is a first name; "Mrs. Weasley"
    # is nobody's when Weasley ends several names
    hon = re.compile(r"^(?:Mr|Mrs|Ms|Miss|Mme|Mlle|Madame|Monsieur)\.?\s+(\S+)$")
    ends = Counter(n.split()[-1] for n in used if len(n.split()) > 1 and not hon.match(n))
    for k in list(groups):
        m = hon.match(k)
        if k not in groups or not m or ends[m.group(1)] >= 2:
            continue
        # a bare "Weasley" entry is a family, not a first name
        owner = [o for o in groups if o != k and o != m.group(1) and m.group(1) in groups[o]]
        if len(owner) == 1:
            into(k, owner[0])
    # a rare longer form around a common name is that person under a title or
    # a stray word: "Even Donut", "Dungeon Princess Donut", "NW Princess
    # Donut the Queen". Never a family form: "Mrs. Weasley" is not Ron.
    for k in sorted(groups, key=lambda k: -len(k.split())):
        if k not in groups or len(k.split()) < 2 or uses.get(k, 0) > 3:
            continue
        if re.match(r"(?i)^(?:mr|mrs|ms|miss|aunt|uncle|madame?|monsieur|mme|mlle|"
                    r"tante|oncle)\b", k):
            continue
        # any form counts: Donut's entry may be shown as "Princess Donut"
        inside = [o for o in groups if o != k
                  and any(f != k and re.search(r"(?<![\w'’])%s(?![\w'’])" % re.escape(f), k)
                          for f in groups[o])
                  and uses.get(o, 0) >= 10 * max(1, uses.get(k, 0))]
        if inside:
            inside.sort(key=lambda o: (-len(o.split()), -uses.get(o, 0)))
            into(k, inside[0])
    # "Sledge, also known as Very Sullen"
    alts = {f: k for k, fs in groups.items() for f in fs}
    if len(alts) > 1:
        names_re = "|".join(re.escape(f) for f in sorted(alts, key=len, reverse=True))
        aka = re.compile(r"(?<![\w'’])(%s)(?![\w])[^.!?]{0,25}?\b(?:also known as|aka|a\.k\.a\.|"
                         r"otherwise known as|alias|dit|surnommée?)\b[\s,:\"“]*(%s)(?![\w])"
                         % (names_re, names_re))
        for _o, _p, _l, paras in docs:
            for _n, text in paras:
                for m in aka.finditer(text):
                    a, b = alts.get(m.group(1)), alts.get(m.group(2))
                    if a and b and a in groups and b in groups and a != b:
                        big, small = (a, b) if uses.get(a, 0) >= uses.get(b, 0) else (b, a)
                        into(small, big)
                        for f in groups[big]:
                            alts[f] = big
    return groups


def _rows(people, hero: str):
    """Rows for people.tsv, sorted by position, with the running count of
    times each person has been named by then. The main character's count is
    kept above everyone's, so the device lists them first."""
    events = []
    for p in people:
        rows = sorted(p.rows, key=lambda r: _pos(r[0], r[2]))
        if p.spoke and rows:
            s_pos = _pos(p.spoke[0], p.spoke[2])
            before = [r for r in rows if _pos(r[0], r[2]) <= s_pos]
            if before and before[-1][3] != "met":
                rows.append((p.spoke[0], p.spoke[1], p.spoke[2], "met", before[-1][4]))
            rows = [r if _pos(r[0], r[2]) < s_pos else r[:3] + ("met",) + r[4:]
                    for r in rows]
        for order, path, para, status, entry in rows:
            events.append((_pos(order, para), order, path, para, p, status, entry))
    events.sort(key=lambda e: (e[0], e[4].name))
    out = []
    top = 0
    for pos, order, path, para, p, status, entry in events:
        seen = sum(1 for m in p.mentions if _pos(m[0], m[2]) <= pos)
        top = max(top, seen)
        if hero and p.name == hero:
            seen = max(seen, top + 1)
        out.append((order, path, para, p.name, status, seen, entry))
    # met is for good once it is true
    met: set = set()
    fixed = []
    for r in out:
        if r[4] == "met":
            met.add(r[3])
        fixed.append(r if r[3] not in met else r[:4] + ("met",) + r[5:])
    # one row per person per place: the last one, which knows the most
    last = {(r[0], r[2], r[3]): i for i, r in enumerate(fixed)}
    return [r for i, r in enumerate(fixed) if last[(r[0], r[2], r[3])] == i]
