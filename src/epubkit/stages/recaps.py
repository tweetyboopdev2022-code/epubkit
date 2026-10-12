"""Recaps and a spoiler-safe who's who, written into the book, down to the page.

Three rules make this feature work rather than ruin books, and all of them were
learned the hard way.

**Never recap anything ahead of the reader.** A chapter recap describes the
whole chapter, including the end they have not reached. An early version told
someone in the middle of Goblet of Fire chapter 31 that the Triwizard Cup was a
Portkey. So a book is recapped in *passages* - runs of paragraphs a few pages
long - and each passage is filed against the koboSpan paragraph it ENDS on. The
device shows a passage only once the reader's bookmark is past that paragraph,
which is as close to "up to this page" as a reflowable book allows.

**Everyone the reader knows about, from the moment they know about them.** Not
just who walks on stage: the dead father, the king in the story told at dinner,
the aunt a letter mentions. Each person is filed against the exact paragraph
that first names them, found by searching the text rather than trusted to the
model, so nobody is introduced a page early. Whether the reader has *met* them
or only *heard of* them is kept, and upgrades when they turn up in person.

**Describe people as they stand at that point, never as they turn out.** A
first mention carries only how the text introduces them; what they are to the
main character is filed at the end of the passage it was learned in, and moves
on as the book does.

Everything is keyed to spine document and koboSpan paragraph number, never to
page numbers: a page depends on font size and margins, and the bookmark Nickel
stores (``ChapterIDBookmarked``, ``OEBPS/ch.xhtml#kobo.N.M``) names the
paragraph directly.

Files written beside the OPF, in the manifest and never the spine:

- ``recaps/parts.tsv``  order, path, end paragraph, chapter label, passage recap
- ``recaps/people.tsv`` order, path, paragraph, name, met|heard, times seen, description
- ``recaps/spine.tsv``  order, path of every spine document
- ``recaps/meta.tsv``   format, passage_words, model, main_character
- ``recaps/recaps.tsv`` order, path, chapter label, chapter recap   (older readers)
- ``recaps/cast.tsv``   order, path, name, description              (older readers)
"""
from __future__ import annotations

import hashlib
import html
import json
import os
import posixpath
import re
import shutil
import tempfile
import threading
import urllib.parse
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .base import Context, Stage, StageResult

RECAP_DIR = "recaps"
#: bumped whenever what is written changes shape; `audit` reports it
FORMAT = 3
# a document this short is a title page or a divider, whatever the TOC says
FLOOR_CHARS = 300

SYSTEM = (
    "You write spoiler-safe recaps and character lists for readers of a novel. "
    "You use only what the given passage states, you never draw on other "
    "knowledge of the book, and you never say what anyone turns out to be."
)

HERO_PROMPT = """Below are extracts from across a whole book. Who is its main character?

The names that turn up in the most chapters are: {names}

A book can open on someone else entirely - a prologue, a villain, a
politician - so judge from all the extracts, not the first. Answer with one
name exactly as written above, and nothing else. If the story is told by an
"I" whose name is not in the list, answer: the narrator

{text}
"""

PROMPT = """The reader has read this book up to the END of the passage below, and nothing after it.
The main character of the whole book is {hero}, including in passages where
{hero} does not appear. Nobody else is ever "the main character".

Rules:
- Use ONLY what this passage states. If you happen to know this book, ignore that.
- Never say what becomes of anyone: no deaths, betrayals, reveals or fates
  beyond what the passage itself shows.
- Write in the same language as the passage. Plain text, no markdown.
- Never remark on who is absent ("Harry is not in this passage").

Answer in exactly this shape:
SUMMARY: <2-4 sentences, past tense, what happened in this passage, ending
with the situation at its final lines - who is there and what is about to
happen - so a reader knows exactly where they left off.>
PEOPLE:
<Name> | <met or heard> | <how the passage first introduces them> | <their relationship to {hero} and their role>

PEOPLE must list EVERY individual the passage names or refers to, not only
those on the page. Include people who are only mentioned in passing, talked
about, remembered, written to or about, or described in a story, rumour,
legend or piece of history that someone tells - including the dead and the
absent. Leave out groups, species, places, organisations and objects.
- Name: as the text calls them. A title used as a name counts ("The Duke").
- met: they are physically present or speak in this passage. heard: only
  talked about, remembered, or named - including anyone dead or far away.
- The third field is how they are first presented, before anything later in
  the passage adds to it. 4 to 12 words.
- The fourth field says who this person IS, not what they did in one scene.
  State a connection to {hero} ONLY if this passage itself shows or says it.
  Never infer family ties or relationships from anything outside the
  passage; if the passage does not connect them to {hero}, begin with
  "not yet connected to {hero}" and then give their role. Otherwise:
  their relationship to {hero} (family, friend, enemy, teacher, stranger,
  someone {hero} has only heard of...) and their role (a teacher, a
  government minister, a servant...), as of the end of this passage.
  4 to 14 words. Examples: "Harry's best friend, a fellow student";
  "not yet connected to Wren; the town's magistrate".
  For {hero} themself write: the main character. For anyone not yet
  connected to {hero}, say so plainly ("not yet connected to {hero}").

{before}Passage:
{text}
"""

BEFORE = """Just before this passage (context only - do not summarise it, and
list nobody from it who is not also in the passage):
{text}

"""

# ------------------------------------------------------------ language
#
# A French book gets French prompts and French fixed wording, so nothing in its
# recaps or who's who is English. Asking an English prompt to "write in the
# language of the passage" was not enough: gemma drifted into English, and the
# lines epubkit writes itself ("not yet connected to ...") were English anyway.

SYSTEM_FR = (
    "Vous rédigez des résumés sans divulgâchage et des listes de personnages "
    "pour les lecteurs d'un roman. Vous n'utilisez que ce que dit le passage "
    "fourni, vous ne puisez jamais dans d'autres connaissances du livre, et "
    "vous ne dites jamais ce que quelqu'un s'avère être. Vous écrivez "
    "toujours en français."
)

HERO_PROMPT_FR = """Voici des extraits pris dans tout un livre. Qui en est le personnage principal ?

Les noms qui reviennent dans le plus de chapitres sont : {names}

Un livre peut s'ouvrir sur quelqu'un d'autre - un prologue, un méchant, un
homme politique - alors jugez d'après tous les extraits, pas seulement le
premier. Répondez par un seul nom, écrit exactement comme dans la liste
ci-dessus, et rien d'autre. Si l'histoire est racontée par un « je » dont le
nom n'est pas dans la liste, répondez : le narrateur

{text}
"""

PROMPT_FR = """Le lecteur a lu ce livre jusqu'à la FIN du passage ci-dessous, et rien au-delà.
Le personnage principal de tout le livre est {hero}, y compris dans les passages
où {hero} n'apparaît pas. Personne d'autre n'est jamais « le personnage principal ».

Règles :
- N'utilisez QUE ce que dit ce passage. Si vous connaissez ce livre, n'en tenez pas compte.
- Ne dites jamais ce que devient quelqu'un : aucune mort, trahison, révélation
  ou destinée au-delà de ce que montre le passage lui-même.
- Écrivez tout en français, y compris les descriptions des personnages.
  Texte brut, sans markdown.
- Ne faites jamais de remarque sur les absents (« Harry n'est pas dans ce passage »).

Répondez exactement sous cette forme, en gardant tels quels les mots RÉSUMÉ,
PERSONNAGES, rencontré et évoqué :
RÉSUMÉ : <2 à 4 phrases au passé : ce qui s'est passé dans ce passage, en
terminant par la situation à ses dernières lignes - qui est là et ce qui va
arriver - pour que le lecteur sache exactement où il s'est arrêté.>
PERSONNAGES :
<Nom> | <rencontré ou évoqué> | <comment le passage le présente d'abord> | <son lien avec {hero} et son rôle>

PERSONNAGES doit lister CHAQUE individu que le passage nomme ou évoque, pas
seulement ceux qui sont présents. Incluez les personnes seulement mentionnées
en passant, dont on parle, dont on se souvient, à qui ou sur qui l'on écrit, ou
qui figurent dans une histoire, une rumeur, une légende ou un récit historique
que quelqu'un raconte - y compris les morts et les absents. Laissez de côté les
groupes, les espèces, les lieux, les organisations et les objets.
- Nom : tel que le texte l'appelle. Un titre employé comme nom compte (« Le Duc »).
- rencontré : la personne est physiquement présente ou parle dans ce passage.
  évoqué : on en parle seulement, on s'en souvient ou on la nomme - y compris
  toute personne morte ou lointaine.
- Le troisième champ dit comment la personne est présentée la première fois,
  avant que la suite du passage n'y ajoute quoi que ce soit. De 4 à 12 mots.
- Le quatrième champ dit qui EST cette personne, pas ce qu'elle a fait dans
  une scène. N'indiquez un lien avec {hero} QUE si ce passage le montre ou le
  dit lui-même. Ne déduisez jamais de liens familiaux ou de relations de quoi
  que ce soit hors du passage ; si le passage ne la relie pas à {hero},
  commencez par « pas encore lié à {hero} » puis donnez son rôle. Sinon :
  son lien avec {hero} (famille, ami, ennemi, professeur, inconnu, quelqu'un
  dont {hero} a seulement entendu parler...) et son rôle (un professeur, une
  ministre, un domestique...), tels qu'à la fin de ce passage. De 4 à 14 mots.
  Exemples : « meilleur ami de Harry, un camarade d'école » ;
  « pas encore lié à Wren ; le magistrat de la ville ».
  Pour {hero} lui-même, écrivez : le personnage principal.

{before}Passage :
{text}
"""

BEFORE_FR = """Juste avant ce passage (pour le contexte seulement - ne le résumez pas,
et ne listez personne qui s'y trouve sans être aussi dans le passage) :
{text}

"""

LEAN = """
The reader already knows these people, as of the passages before this one:
{known}

This overrides "every individual" above: in PEOPLE list ONLY
- people who are not in that list,
- people from that list who are met in person for the first time in this
  passage (they are marked heard),
- people from that list whose relationship to {hero} or whose role changes
  in this passage.
Leave everyone else out, even when the passage names them.
"""

LEAN_FR = """
Le lecteur connaît déjà ces personnes, d'après les passages précédents :
{known}

Ceci remplace « chaque individu » ci-dessus : dans PERSONNAGES, listez SEULEMENT
- les personnes absentes de cette liste,
- les personnes de cette liste rencontrées en personne pour la première fois
  dans ce passage (elles sont marquées évoqué),
- les personnes de cette liste dont le lien avec {hero} ou le rôle change
  dans ce passage.
Laissez de côté tous les autres, même si le passage les nomme.
"""

#: everything a recap run says in the book's own language
TEXT = {
    "en": {"system": SYSTEM, "hero_prompt": HERO_PROMPT, "prompt": PROMPT,
           "before": BEFORE, "lean": LEAN, "met": "met", "heard": "heard",
           "narrator": "the narrator",
           "main": "the main character", "unlinked": "not yet connected to %s"},
    "fr": {"system": SYSTEM_FR, "hero_prompt": HERO_PROMPT_FR, "prompt": PROMPT_FR,
           "before": BEFORE_FR, "lean": LEAN_FR, "met": "rencontré",
           "heard": "évoqué", "narrator": "le narrateur",
           "main": "le personnage principal", "unlinked": "pas encore lié à %s"},
}
NARRATORS = {"the narrator", "le narrateur", "la narratrice"}
UNLINKED = re.compile(r"(?i)^(not yet connected|pas encore li[ée])")

_EN_WORDS = set("the and was that his her with for had not but you she they said "
                "have this from what were would there could into been him which "
                "when of to".split())
# words Spanish, Italian and Portuguese do not share, so those are not French
_FR_WORDS = set("les des et est une dans qui pas pour sur avec il elle "
                "du au mais nous vous était avait plus ses lui je".split())


def _text_language(text: str) -> str:
    """'en' or 'fr' from the commonest words, or '' when it is not clear."""
    words = re.findall(r"[^\W\d_]+", text.lower())
    en = sum(1 for w in words if w in _EN_WORDS)
    fr = sum(1 for w in words if w in _FR_WORDS)
    if en + fr < 40:
        return ""
    if fr > en * 2:
        return "fr"
    if en > fr * 2:
        return "en"
    return ""


def _declared_language(opf: str) -> str:
    m = re.search(r"<dc:language[^>]*>\s*([^<\s]+)", opf or "", re.I)
    code = (m.group(1) if m else "").lower()
    if code.startswith(("fr", "fre", "fra")):
        return "fr"
    return "en" if code.startswith("en") else ""


def _language(setting: str, text: str, opf: str) -> str:
    """The language a book's recaps are written in: 'en' or 'fr'.

    The text decides before dc:language does: a converted book is often
    labelled "en" whatever it is written in. A book in neither language gets
    the English prompts, which ask for the passage's own language.
    """
    setting = (setting or "auto").strip().lower()[:2]
    if setting in TEXT:
        return setting
    m = re.search(r"<dc:language[^>]*>\s*([^<\s]+)", opf or "", re.I)
    declared = (m.group(1) if m else "").lower()
    if declared and not declared.startswith(("en", "fr")):
        return "en"
    return _text_language(text) or _declared_language(opf) or "en"


def book_language(book: Path, setting: str = "auto") -> str:
    """The recap language for a book on disk, from a sample of its text."""
    try:
        with zipfile.ZipFile(book) as z:
            opf_name = _opf_name(z)
            opf = z.read(opf_name).decode("utf-8", "ignore") if opf_name else ""
            base = posixpath.dirname(opf_name) if opf_name else ""
            manifest = _manifest(opf)
            spine = [_join(base, manifest[r]) for r in
                     re.findall(r'<itemref\b[^>]*\bidref="([^"]+)"', opf) if r in manifest]
            text = ""
            for full in spine[len(spine) // 4:] + spine[:len(spine) // 4]:
                try:
                    text += " " + _plain(z.read(full).decode("utf-8", "ignore"))[:6000]
                except KeyError:
                    continue
                if len(text) > 20000:
                    break
    except (zipfile.BadZipFile, OSError, KeyError):
        return "en"
    return _language(setting, text, opf)


FRONT = ("praise for", "copyright", "all rights reserved", "table of contents",
         "also by", "about the author", "dedication", "acknowledgment",
         "acknowledgement", "a note on the type", "first published",
         "reading group guide", "discussion questions",
         "du même auteur", "des mêmes auteurs", "tous droits réservés",
         "table des matières", "remerciements", "à propos de l'auteur",
         "dépôt légal", "achevé d'imprimer", "dans la même collection")

SCENE_BREAK = re.compile(r"^[\s*#~•·◆◇✦❖§—–-]{0,20}$")
SPAN = re.compile(
    r'<span\b[^>]*?\bid="kobo\.(\d+)\.\d+"[^>]*>(.*?)</span>', re.S)
HERO_WORDS = ("the main character", "main character", "protagonist")


class RecapStage(Stage):
    name = "recaps"

    def applies(self, ctx: Context) -> bool:
        # a picture book has no prose to recap, and a comic that only looks
        # like prose to the spine (Kindle panel view) is one of those
        if ctx.meta.get("fixed_layout"):
            return False
        return bool(ctx.config.recaps.enabled and ctx.provider)

    def run(self, ctx: Context) -> StageResult:
        rc = ctx.config.recaps
        docs = _documents(ctx.path, rc.min_chars)
        if not docs:
            ctx.note("no chapters long enough to recap")
            return StageResult(message="no recaps")

        target = max(2000, int(rc.passage_words) * 6)
        passages = []                         # (order, path, label, paras)
        for order, path, label, paras in docs:
            for chunk in _segments(paras, target):
                passages.append((order, path, label, chunk))
        # the closing lines of the passage before, so "he" and "the letter"
        # at the top of a passage mean something to the model
        tails = [""] + [_tail(p[3]) for p in passages[:-1]]
        lookup = {id(p): t for p, t in zip(passages, tails)}  # noqa: B905

        lang = _language(getattr(rc, "language", "auto"),
                         " ".join(t for _o, _p, _l, ps in docs[:8] for _n, t in ps)[:40000],
                         _opf_text(ctx.path))
        words = TEXT[lang]
        if lang != "en":
            ctx.note("recaps written in %s" % {"fr": "French"}.get(lang, lang))
        answers = _Answers(ctx.path, getattr(ctx.provider, "model", ""),
                           enabled=getattr(rc, "resume", True))
        if answers.got:
            ctx.note("resuming: %d answers kept from an earlier run" % len(answers.got))
        ctx.report("finding the main character (%d passages to go)" % len(passages))
        hero = _hero(ctx, docs, lang, answers)
        ctx.note("main character: %s" % hero)
        lean = bool(getattr(rc, "lean_cast", False)) and rc.cast

        def one(item, known=None):
            order, path, label, paras = item
            text = "\n\n".join(t for _n, t in paras)
            tail = lookup.get(id(item), "")
            before = words["before"].format(text=tail) if tail else ""
            if known:
                listed = "\n".join("- %s (%s): %s" % (n, words[st], rel or "?")
                                    for n, (st, rel) in list(known.items())[-80:])
                before = words["lean"].format(known=listed, hero=hero).lstrip() + "\n" + before
            prompt = words["prompt"].format(hero=hero, text=text[:16000], before=before)
            try:
                reply = answers.ask(ctx.provider, prompt, words["system"],
                                    usable=lambda t: any(_parse(t)))
            except Exception as exc:                        # noqa: BLE001
                ctx.log.debug("    recap %s:%d failed: %s", path, paras[0][0], exc)
                return None
            summary, people = _parse(reply)
            if not (summary or people):
                unread.append(reply)
                return None
            if known:
                people = _with_known(people, known, paras)
            return (item, summary, people)

        unread: list[str] = []
        workers = max(1, min(ctx.config.ai.concurrency, len(passages)))
        results: list = [None] * len(passages)
        known: dict = {}
        done = 0
        ctx.report("passage 0/%d" % len(passages))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            # In lean mode each batch is told who the batches before it met,
            # so a batch of `workers` passages still runs at once.
            step = workers if lean else len(passages)
            for start in range(0, len(passages), step):
                batch = range(start, min(start + step, len(passages)))
                snapshot = dict(known) if lean else None
                answered = pool.map(one, [passages[i] for i in batch],
                                    [snapshot] * len(batch))
                for i, res in zip(batch, answered, strict=True):
                    done += 1
                    ctx.report("passage %d/%d" % (done, len(passages)))
                    results[i] = res
                    if lean and res:
                        _learn(known, res[2])
        # A passage that timed out or came back garbled gets one more try,
        # rather than leaving a hole in the recaps.
        missed = [i for i, r in enumerate(results) if r is None]
        if missed and len(missed) < len(passages):
            ctx.note("retrying %d passage(s) that failed" % len(missed))
            unread.clear()
            for i in missed:
                results[i] = one(passages[i], dict(known) if lean else None)
        got = [r for r in results if r]
        if unread:
            ctx.note("%d of %d replies were not in the expected shape; the first began: %r"
                     % (len(unread), len(passages), unread[0][:300]))
        if not any(s for _i, s, _p in got):
            ctx.note("recap generation produced nothing")
            return StageResult(ok=not unread, message="no recaps")

        parts = [(it[0], it[1], it[3][-1][0], it[2], s)
                 for it, s, _p in got if s]
        people = _people_rows(got, hero, ctx, lang) if rc.cast else []
        legacy = bool(getattr(rc, "legacy_files", False))
        chapters = _chapter_rows(parts)
        cast = _legacy_cast(people) if legacy else []
        meta = [("format", str(FORMAT)), ("passage_words", str(rc.passage_words)),
                ("model", str(getattr(ctx.provider, "model", "") or "")),
                ("main_character", hero), ("language", lang)]
        _embed(ctx.path, parts, people, chapters if legacy else [], cast,
               _spine(ctx.path), meta)
        answers.forget()

        n_people = len({r[3] for r in people})
        heard = len({r[3] for r in people} - {r[3] for r in people if r[4] == "met"})
        ctx.record.recaps = len(chapters)
        ctx.note("%d passages over %d chapters; %d people (%d only heard of)"
                 % (len(parts), len(chapters), n_people, heard))
        return StageResult(message="%d recaps, %d people" % (len(chapters), n_people))


# ------------------------------------------------------------ resuming


class _Answers:
    """The model's replies for one book, kept on disk until the book is written.

    A book is only written once every passage is done, so a run stopped at
    passage 150 of 200 used to throw all 150 away. Each usable reply is now
    appended as it arrives, keyed by the exact request, and a rerun asks the
    model only for what is missing. The file goes once the book is written.
    """

    def __init__(self, book: Path, model: str, enabled: bool = True):
        root = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache")
        name = hashlib.sha1(str(Path(book).resolve()).encode("utf-8")).hexdigest()[:20]
        self.path = root / "epubkit" / "recaps" / (name + ".jsonl")
        self.model = model or ""
        self.enabled = enabled
        self.lock = threading.Lock()
        self.got: dict[str, str] = {}
        if not enabled:
            return
        try:
            with open(self.path, encoding="utf-8") as fh:
                for line in fh:
                    try:
                        d = json.loads(line)
                        self.got[d["k"]] = d["text"]
                    except (ValueError, KeyError, TypeError):
                        continue            # a line cut short by a crash
        except OSError:
            pass

    def _key(self, prompt: str, system: str) -> str:
        raw = "\0".join((self.model, system or "", prompt))
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def ask(self, provider, prompt: str, system: str, usable=None) -> str:
        key = self._key(prompt, system)
        if key in self.got:
            return self.got[key]
        text = provider.complete(prompt, system=system).text
        if self.enabled and (usable is None or usable(text)):
            with self.lock:
                self.got[key] = text
                try:
                    self.path.parent.mkdir(parents=True, exist_ok=True)
                    with open(self.path, "a", encoding="utf-8") as fh:
                        fh.write(json.dumps({"k": key, "text": text}) + "\n")
                except OSError:
                    pass                    # resuming is a bonus, not a need
        return text

    def forget(self) -> None:
        try:
            self.path.unlink()
        except OSError:
            pass


def _learn(known: dict, people) -> None:
    """Fold one passage's people into who the reader knows: met is for good."""
    for name, status, _intro, rel in people:
        was = known.pop(name, None)
        if was and was[0] == "met":
            status = "met"
        known[name] = (status, rel or (was[1] if was else ""))


def _with_known(people, known: dict, paras) -> list:
    """Lean replies leave out people already known and unchanged. Put back
    everyone known whom this passage names, as they stood, so the who's who
    still counts how often each person turns up."""
    listed = {n.lower() for n, *_ in people}
    out = list(people)
    for name, (status, rel) in known.items():
        if name.lower() in listed:
            continue
        if _all_words_present(name, paras) and _first_mention(name, paras) is not None:
            out.append((name, status, "", rel))
    return out


# ------------------------------------------------------------ the book


def _documents(book: Path, min_chars: int):
    """Spine documents worth reading, as (order, zip path, label, paragraphs).

    `order` is the spine position, and the path is the full path inside the
    zip - the same string Nickel stores in ChapterIDBookmarked, which is what
    the device matches on. Short documents are kept when the table of contents
    names them, so a two-page prologue or a letter between chapters still
    contributes its people; front and back matter are dropped by content.
    """
    out = []
    try:
        with zipfile.ZipFile(book) as z:
            opf_name = _opf_name(z)
            if not opf_name:
                return []
            opf = z.read(opf_name).decode("utf-8", "ignore")
            base = posixpath.dirname(opf_name)
            manifest = _manifest(opf)
            spine = []
            for idref in re.findall(r'<itemref\b[^>]*\bidref="([^"]+)"', opf):
                href = manifest.get(idref)
                if href:
                    spine.append(_join(base, href))
            titles = _toc_titles(z, opf, base, manifest)
            for order, full in enumerate(spine):
                try:
                    raw = z.read(full)
                except KeyError:
                    continue
                paras = _paragraphs(raw)
                size = sum(len(t) for _n, t in paras)
                if size < FLOOR_CHARS:
                    continue
                label = titles.get(full) or ""
                if _is_navigation(raw, paras, label):
                    # A contents page is a list of chapter titles. Given one,
                    # the model "summarised" Half-Blood Prince from what it
                    # knew of the book - Horcruxes, the cave - and filed it
                    # before chapter 1. Nothing here is story.
                    continue
                head = " ".join(t for _n, t in paras)[:1200].lower()
                if any(m in head for m in FRONT) and size < min_chars * 4:
                    continue
                if size < min_chars and not label:
                    continue
                if not label:
                    m = re.match(r"(?:unnamed\s+)?((?:chapter|chapitre)\s+\w+)", head, re.I)
                    label = m.group(1).title() if m else "Section %d" % order
                out.append((order, full, label, paras))
    except (zipfile.BadZipFile, OSError):
        return []
    return out


def _opf_text(book: Path) -> str:
    try:
        with zipfile.ZipFile(book) as z:
            name = _opf_name(z)
            return z.read(name).decode("utf-8", "ignore") if name else ""
    except (zipfile.BadZipFile, OSError, KeyError):
        return ""


def _spine(book: Path) -> list[tuple[int, str]]:
    """Every spine document as (order, zip path), recapped or not.

    The device needs this to place a bookmark that sits in a document with no
    recap of its own - a title page, an interlude too short to summarise - and
    a shell script has no business parsing an OPF.
    """
    try:
        with zipfile.ZipFile(book) as z:
            opf_name = _opf_name(z)
            if not opf_name:
                return []
            opf = z.read(opf_name).decode("utf-8", "ignore")
    except (zipfile.BadZipFile, OSError, KeyError):
        return []
    base = posixpath.dirname(opf_name)
    manifest = _manifest(opf)
    ids = re.findall(r'<itemref\b[^>]*\bidref="([^"]+)"', opf)
    return [(i, _join(base, manifest[r])) for i, r in enumerate(ids) if r in manifest]


_NAV_LABEL = re.compile(r"^\s*(table of )?contents?\s*$|^\s*(index|toc)\s*$|"
                        r"^\s*(table des mati[eè]res|sommaire)\s*$", re.I)


def _is_navigation(raw: bytes, paras, label: str) -> bool:
    """A contents page, index or other list of links rather than prose."""
    if _NAV_LABEL.match(label or ""):
        return True
    text = raw.decode("utf-8", "ignore")
    if re.search(r'<nav\b[^>]*epub:type="toc"', text):
        return True
    links = len(re.findall(r"<a\b[^>]*\bhref=", text))
    if len(paras) >= 5 and links >= 0.6 * len(paras):
        return True
    return False


def _opf_name(z: zipfile.ZipFile) -> str | None:
    try:
        c = z.read("META-INF/container.xml").decode("utf-8", "ignore")
        m = re.search(r'full-path="([^"]+)"', c)
        if m and m.group(1) in z.namelist():
            return m.group(1)
    except KeyError:
        pass
    return next((n for n in z.namelist() if n.endswith(".opf")), None)


def _join(base: str, href: str) -> str:
    href = urllib.parse.unquote(href.split("#")[0])
    return posixpath.normpath(posixpath.join(base, href)) if base else href


def _manifest(opf: str) -> dict[str, str]:
    """id -> href, without assuming which attribute comes first.

    Attribute order carries no meaning in XML, and a regex that expects
    id before href silently drops every item written the other way round -
    which drops chapters from the recap set with no error anywhere.
    """
    out: dict[str, str] = {}
    for tag in re.findall(r"<item\b[^>]*/?>", opf):
        attrs = dict(re.findall(r'([A-Za-z_:][-\w:.]*)\s*=\s*"([^"]*)"', tag))
        if attrs.get("id") and attrs.get("href"):
            out[attrs["id"]] = attrs["href"]
    return out


def _toc_titles(z, opf, base, manifest) -> dict[str, str]:
    """Spine path -> first TOC label pointing into it (NCX or EPUB 3 nav)."""
    cands = [h for h in manifest.values()
             if h.endswith(".ncx") or re.search(r"nav\.x?html?$", h, re.I)]
    for href in cands:
        full = _join(base, href)
        try:
            raw = z.read(full).decode("utf-8", "ignore")
        except KeyError:
            continue
        here = posixpath.dirname(full)
        found: dict[str, str] = {}
        if href.endswith(".ncx"):
            for label, src in re.findall(
                    r"<navLabel>\s*<text>(.*?)</text>\s*</navLabel>\s*"
                    r'<content\b[^>]*\bsrc="([^"]+)"', raw, re.S):
                found.setdefault(_join(here, src), _plain(label)[:60])
        else:
            for src, label in re.findall(r'<a\b[^>]*\bhref="([^"]+)"[^>]*>(.*?)</a>',
                                         raw, re.S):
                found.setdefault(_join(here, src), _plain(label)[:60])
        if found:
            return found
    return {}


def _paragraphs(raw: bytes) -> list[tuple[int, str]]:
    """(koboSpan paragraph number, text) for one content document.

    A kepub already carries the numbers. A plain EPUB is numbered in memory by
    the same transform `kepubify` applies, so the numbers match the kepub this
    book will become.
    """
    text = raw.decode("utf-8", "ignore")
    if 'id="kobo.' not in text:
        try:
            from ..tools import kepubify
            done = kepubify.convert_doc(raw)
        except Exception:                                   # noqa: BLE001
            done = None
        if done:
            text = done.decode("utf-8", "ignore")
    body = text.split("<body", 1)[-1]
    paras: dict[int, list[str]] = {}
    order: list[int] = []
    for n, inner in SPAN.findall(body):
        n = int(n)
        if n not in paras:
            paras[n] = []
            order.append(n)
        paras[n].append(inner)
    out = []
    for n in order:
        t = _plain("".join(paras[n]))
        if t:
            out.append((n, t))
    if not out:                   # no spans at all: one paragraph, number 0
        t = _plain(body)
        if t:
            out.append((0, t))
    return out


def _plain(raw: str) -> str:
    raw = re.sub(r"(?is)<(script|style)\b.*?</\1>", " ", raw)
    raw = re.sub(r"<[^>]+>", " ", raw)
    return re.sub(r"\s+", " ", html.unescape(raw)).strip()


def _segments(paras, target: int):
    """Cut a chapter into passages of about `target` characters.

    A scene break past the halfway mark ends a passage early, so passages
    follow the book's own seams where it has them; a chapter shorter than the
    target stays whole.
    """
    out, cur, size = [], [], 0
    for n, t in paras:
        if cur and SCENE_BREAK.match(t) and size >= target // 2:
            out.append(cur)
            cur, size = [], 0
        cur.append((n, t))
        size += len(t)
        if size >= target:
            out.append(cur)
            cur, size = [], 0
    if cur:
        # a stub of a few lines is folded into the passage before it
        if out and size < target // 4:
            out[-1].extend(cur)
        else:
            out.append(cur)
    return out


def _tail(paras, chars: int = 900) -> str:
    """The last few paragraphs of a passage, about `chars` long."""
    out, size = [], 0
    for _n, t in reversed(paras):
        out.append(t)
        size += len(t)
        if size >= chars:
            break
    text = "\n\n".join(reversed(out))
    return text[-chars * 2:]


# ------------------------------------------------------------ the model


def _hero(ctx: Context, docs, lang: str = "en", answers=None) -> str:
    """The main character, judged from the whole book rather than its opening.

    Asking about the opening alone named "The Prime Minister" as the lead of
    Harry Potter and the Half-Blood Prince, whose first chapter is his, and
    every relationship in the book was then described relative to him. So
    the names that recur across the most chapters are counted first, and the
    model chooses among them from extracts spread through the book. An answer
    outside that list falls back to the most widespread name.
    """
    fixed = (getattr(ctx.config.recaps, "main_character", "") or "").strip()
    if fixed:
        return fixed
    words = TEXT[lang]
    candidates = _recurring_names(docs)
    if not candidates:
        return words["narrator"]
    # A series named after its lead ("Detective Gina Harte") settles it. A
    # crime novel's victims often get the opening chapters, and the model
    # then picked Annabel, Holly or "the narrator" for Gina Harte books.
    lead = _series_lead(ctx, candidates)
    if lead:
        return lead
    picks = [docs[int(i * (len(docs) - 1) / 4)] for i in range(5)] if len(docs) > 4 else docs
    extracts = "\n\n".join(
        "From %s:\n%s" % (label, " ".join(t for _n, t in paras)[:1600])
        for _o, _p, label, paras in picks)
    try:
        prompt = words["hero_prompt"].format(names=", ".join(candidates), text=extracts)
        reply = (answers.ask(ctx.provider, prompt, words["system"]) if answers
                 else ctx.provider.complete(prompt, system=words["system"]).text)
        name = reply.strip().splitlines()[0].strip(" .\"'*")
    except Exception:                                       # noqa: BLE001
        name = ""
    if name.lower() in NARRATORS:
        return words["narrator"]
    for c in candidates:
        if name.lower() == c.lower() or (name and name.lower() in c.lower().split()):
            return c
    return candidates[0]


def _series_lead(ctx: Context, candidates) -> str:
    series = getattr(ctx.record, "series", None) or \
        (ctx.meta.get("scanned") or {}).get("series") or ""
    if not series:                  # `epubkit recaps` on a finished book
        try:
            with zipfile.ZipFile(ctx.path) as z:
                opf = next(z.read(n).decode("utf-8", "ignore")
                           for n in z.namelist() if n.endswith(".opf"))
            m = re.search(r'name="calibre:series" content="([^"]*)"', opf) or \
                re.search(r'property="belongs-to-collection"[^>]*>([^<]*)<', opf)
            series = html.unescape(m.group(1)) if m else ""
        except (OSError, zipfile.BadZipFile, StopIteration):
            series = ""
    words = _series_words(series)
    if not words:
        return ""
    for c in candidates:
        parts = [p.lower() for p in c.split()]
        if parts and parts[0] in words:
            return c
    return ""


#: words in a series title that are never the lead's name. "The" was the one
#: that mattered: "The Chronicles of Narnia" shares it with "The White Witch",
#: and every Narnia, Twilight and Hunger Games book was flagged as written
#: around the wrong main character.
_SERIES_FILLER = {"the", "and", "les", "des", "saga", "series", "book", "books",
                  "chronicles", "trilogy", "cycle", "sequence", "quartet", "sextet",
                  "tales", "adventures", "adventure", "guide", "collection", "novels",
                  "novel", "mysteries", "mystery", "files", "legacy", "world"}


def _series_words(series: str) -> set[str]:
    """Words of a series title that could be a person's name."""
    return {w.lower() for w in re.findall(r"[^\W\d_][\w'’]{2,}", series or "")
            if w.lower() not in _SERIES_FILLER and w.lower() not in _NOT_A_NAME}


def _first_name(name: str) -> str:
    """First word of a name, past any article or title: "The White Witch" -> white."""
    words = [w.lower().strip(".,") for w in (name or "").split()]
    words = [w for w in words if w not in _SMALL and w not in _NOT_A_NAME]
    return words[0] if words else ""


_NOT_NAMES = {
    "the", "a", "an", "he", "she", "it", "i", "in", "on", "at", "but", "and", "when",
    "then", "they", "we", "you", "his", "her", "what", "where", "why", "how",
    "chapter", "there", "this", "that", "mr", "mrs", "ms", "miss", "dr", "not",
    "no", "yes", "oh", "well", "sir", "lady", "lord", "professor", "uncle", "aunt",
    "god", "english", "french", "monday", "tuesday", "wednesday", "thursday",
    "friday", "saturday", "sunday", "january", "february", "march", "april", "may",
    "june", "july", "august", "september", "october", "november", "december",
    "christmas", "ministry", "minister", "prime", "king", "queen", "prince", "dark",
    # French: titles and forms of address, which are capitalised mid-sentence
    "monsieur", "madame", "mademoiselle", "mme", "mlle", "maître", "docteur",
    "père", "mère", "oncle", "tante", "dieu", "seigneur", "sire", "roi", "reine",
    "princesse", "saint", "sainte", "capitaine", "professeur", "majesté", "altesse",
    "noël", "français", "française", "anglais", "anglaise",
}


def _recurring_names(docs, keep: int = 8) -> list[str]:
    """Capitalised words mid-sentence, ranked by how many chapters use them.

    A walk-on can dominate a chapter; only the lead turns up in nearly all of
    them. Counting words that are capitalised away from the start of a
    sentence keeps "The" and "When" out without a dictionary.
    """
    from collections import Counter

    spread, total = Counter(), Counter()
    # accented letters count: "Hélène" and "Émile" are names too
    pat = re.compile(r"(?<=[a-zà-öø-ÿœ,;:\u2019'] )([A-ZÀ-ÖØ-ÞŒ][a-zà-öø-ÿœ\u2019'-]{2,})")
    for _o, _p, _l, paras in docs:
        seen = set()
        for _n, t in paras:
            for w in pat.findall(t):
                w = re.sub(r"[\u2019']s$", "", w)
                if w.lower() in _NOT_NAMES or "'" in w or "\u2019" in w:
                    continue                  # I'm, I'd, I've are not people
                total[w] += 1
                seen.add(w)
        spread.update(seen)
    ranked = sorted(spread, key=lambda w: (-spread[w], -total[w]))
    return ranked[:keep]


def _parse(text: str) -> tuple[str, list[tuple[str, str, str, str]]]:
    """-> (summary, [(name, met|heard, introduced as, relation)])."""
    if not text:
        return "", []
    text = re.sub(r"\*+", "", text)
    summary = ""
    m = re.search(r"(?:SUMMARY|R[ÉE]SUM[ÉE])\s*:\s*(.+?)"
                  r"(?:\n\s*(?:PEOPLE|CHARACTERS|PERSONNAGES|PERSONNES)\s*:|\Z)",
                  text, re.S | re.I)
    if m:
        summary = re.sub(r"\s+", " ", m.group(1)).strip()
    people = []
    m = re.search(r"(?:PEOPLE|CHARACTERS|PERSONNAGES|PERSONNES)\s*:\s*(.+)\Z",
                  text, re.S | re.I)
    if m:
        for line in m.group(1).splitlines():
            line = line.strip().lstrip("-•0123456789. ").strip()
            bits = [b.strip() for b in line.split("|")]
            if len(bits) < 4 or not bits[0]:
                continue
            name, status, intro, rel = bits[0], bits[1].lower(), bits[2], bits[3]
            name = re.sub(r"\s*\(.*?\)\s*", " ", name).strip(" .,:;\"'«»\u00a0\u202f")
            if not (1 < len(name) < 45) or \
                    not re.sub(r"^[dl][’']", "", name, flags=re.I)[:1].isupper():
                continue
            # the French prompt asks for "rencontré" / "évoqué"
            status = "met" if status.startswith(("met", "rencontr")) else "heard"
            people.append((name, status, _tidy(intro), _tidy(rel)))
    return summary, people


def _tidy(s: str, words: int = 16) -> str:
    """One short line: a model asked for 12 words sometimes writes 40."""
    s = re.sub(r"\s+", " ", s).strip(" .;")
    w = s.split(" ")
    if len(w) <= words:
        return s
    cut = " ".join(w[:words])
    # end on the last clause boundary if there is a sensible one
    p = max(cut.rfind(", "), cut.rfind("; "))
    return (cut[:p] if p > len(cut) // 2 else cut).rstrip(" ,;")


_GROUPS = {"family", "families", "staff", "office", "ministry", "department",
           "misinformation", "headquarters", "bank", "hospital", "newspaper",
           "council", "society", "team", "people", "crowd", "government",
           "builders", "eaters", "obliviators", "aurors", "dementors", "muggles",
           "students", "teachers", "guards", "twins", "parents", "children",
           "brothers", "sisters", "villagers", "soldiers", "police", "army",
           "committee", "board", "house", "school", "class", "gang", "club",
           "famille", "familles", "ministère", "gouvernement", "armée", "foule",
           "élèves", "professeurs", "gardes", "soldats", "villageois", "enfants",
           "frères", "sœurs", "jumeaux", "jumelles", "conseil", "équipe", "école",
           "classe", "bande", "gens", "habitants", "policiers"}
_SMALL = {"the", "of", "de", "la", "le", "les", "des", "van", "von", "der", "da", "du",
          "del", "al", "bin"}


def _individual(name: str) -> bool:
    """A person's name or a title used as one - not a group or a description.

    "The builders", "The Hogwarts staff", "The Gaunt family" and "The ugly
    little man" were all listed as people. A name is capitalised word by word
    (bar small particles), does not end in a group noun, and is not a word
    broken off mid-speech ("Lord Vol-").
    """
    words = name.split()
    if not words or name.endswith(("-", "\u2014", "\u2013")):
        return False
    if any(w.lower().strip(".,") in _GROUPS for w in words):
        return False              # "The Ministry of Magic", "Office of Misinformation"
    for w in words:
        if w.lower() in _SMALL:
            continue
        # "d'Artagnan", "l'Inconnu"
        if not re.sub(r"^[dl][’']", "", w, flags=re.I)[:1].isupper():
            return False
    return True


def _is_hero(name: str, hero: str) -> bool:
    if not hero or hero in NARRATORS:
        return False
    a = {w.lower() for w in name.split() if w.lower() not in _SMALL}
    b = {w.lower() for w in hero.split() if w.lower() not in _SMALL}
    return bool(a & b) and (name.lower() == hero.lower() or len(b) == 1 or a == b
                            or a <= b or b <= a)


def _not_the_lead(rel: str, intro: str) -> str:
    """Only the lead is the main character; strip the claim from anyone else."""
    if "main character" not in rel.lower() and "personnage principal" not in rel.lower():
        return rel
    cleaned = re.sub(r"(?i)\b(the )?main character('s)?( of the (story|book))?[, ]*", "", rel)
    cleaned = re.sub(r"(?i)\b(le |du )?personnage principal"
                     r"( de l'histoire| du livre| du roman)?[, ]*", "", cleaned)
    cleaned = cleaned.strip(" ,;")
    return cleaned if len(cleaned.split()) >= 3 else intro


# ------------------------------------------------------------ the cast


def _first_mention(name: str, paras) -> int | None:
    """Paragraph number of the first paragraph that names this person.

    Tries the whole name (without a leading "The"), then its last and first
    words. The model is not trusted with positions, or with names: None means
    the passage never names this person, and the caller leaves them out.
    """
    bare = _bare(name)
    words = [w for w in re.findall(r"[\w'’.-]+", bare) if w[:1].isupper()]
    tries = [bare] + ([words[-1], words[0]] if len(words) > 1 else [])
    tries = [t for t in tries if len(t) >= 3 and t.lower() not in _NOT_A_NAME]
    for probe in tries:
        pat = re.compile(r"(?<![\w])%s(?![\w])" % re.escape(probe))
        for n, t in paras:
            if pat.search(t):
                return n
    return None


def _bare(name: str) -> str:
    """The name without a leading article, which the text lowercases mid-sentence."""
    return re.sub(r"^(?i:(?:the|le|la|les)\s+|l['’])", "", name)


def _cut_off(name: str, paras) -> bool:
    """A name the text only ever breaks off mid-word: "Lord Vol-"."""
    last = name.split()[-1].rstrip(".")
    text = " ".join(t for _n, t in paras)
    hits = re.findall(r"(?<![\w])%s(?![\w])(.?)" % re.escape(last), text)
    return bool(hits) and all(h in "-\u2014\u2013" and h for h in hits)


def _unclaim(rel: str, hero: str, lang: str = "en") -> str:
    """Remove a tie to the lead that a passage without the lead cannot show.

    Gemma called Bellatrix "Harry's acquaintance" and Fudge "Harry's friend"
    in chapters Harry is not in.
    """
    words = [w for w in hero.split() if w[:1].isupper()]
    if not words or not re.search(r"\b(%s)\b" % "|".join(map(re.escape, words)), rel):
        return rel
    if UNLINKED.match(rel):
        return rel
    rest = re.split(r"\s*[;,]\s*", rel, maxsplit=1)
    role = rest[1] if len(rest) > 1 and not re.search(
        r"\b(%s)\b" % "|".join(map(re.escape, words)), rest[1]) else ""
    sep = " ; " if lang == "fr" else "; "
    return TEXT[lang]["unlinked"] % hero + (sep + role if role else "")


def _all_words_present(name: str, paras) -> bool:
    """Every capitalised word of the name occurs somewhere in the passage.

    The surname alone matched "Serious Black", a slip of the model's for
    Sirius; an invented first name slips through the same way.
    """
    text = " ".join(t for _n, t in paras)
    for w in re.findall(r"[\w'’.-]+", _bare(name)):
        if w[:1].isupper() and w.lower() not in _NOT_A_NAME and len(w) > 1:
            if not re.search(r"(?<![\w])%s(?![\w])" % re.escape(w.rstrip(".")), text):
                return False
    return True


_NOT_A_NAME = {"the", "mr", "mr.", "mrs", "mrs.", "miss", "ms", "ms.", "dr", "dr.",
               "sir", "lady", "lord", "king", "queen", "prince", "princess",
               "uncle", "aunt", "professor", "captain", "old", "young", "saint",
               "le", "la", "les", "m.", "monsieur", "madame", "mademoiselle", "mme",
               "mlle", "docteur", "maître", "roi", "reine", "princesse", "oncle",
               "tante", "professeur", "capitaine", "sainte", "père", "mère"}


def _people_rows(got, hero, ctx, lang: str = "en"):
    """-> [(order, path, para, name, met|heard, description)], sorted by position.

    Two rows per person per passage at most:
    - where they are first named, how they were introduced (their first ever
      passage only), and
    - at the passage's last paragraph, who they are to the main character by
      then. A later passage's row supersedes it only once the reader is past
      that passage too.
    """
    raw = []   # (pos, (path, para, status, kind, text), name)
    from ..tools.dedupe_cast import split_people
    for (order, path, _label, paras), _s, listed in got:
        end = paras[-1][0]
        # a passage that never names the lead cannot show anyone's tie to them
        hero_here = hero in NARRATORS or any(
            _all_words_present(w, paras) for w in hero.split() if w[:1].isupper())
        people = [(one, st, i, r) for name, st, i, r in listed
                  for one in split_people(name)]
        for name, status, intro, rel in people:
            if not _individual(name):
                continue
            at = _first_mention(name, paras) if _all_words_present(name, paras) else None
            if at is None:
                # The passage never names them. That is the model filling in
                # from what it knows of the book - Grindelwald turned up in
                # Half-Blood Prince this way - so they are left out.
                continue
            if _cut_off(name, paras):
                continue
            if _is_hero(name, hero):
                rel = TEXT[lang]["main"]
            else:
                rel = _not_the_lead(rel, intro)
                if not hero_here:
                    rel = _unclaim(rel, hero, lang)
            raw.append((_pos(order, at), (order, path, at, status, "intro", intro), name))
            raw.append((_pos(order, end), (order, path, end, status, "rel", rel), name))

    names = _identities([(pos, name) for pos, _d, name in raw], ctx)

    rows, seen, met, said = [], set(), set(), {}
    for _p, (order, path, para, status, kind, text), name in sorted(raw, key=lambda r: r[0]):
        who = names.get(name)
        if not who or not text:
            continue
        if kind == "intro" and who in seen:
            # introduced long ago; but if this is where the reader first meets
            # someone they had only heard of, say so from this paragraph on,
            # keeping what was already known about them
            if status == "met" and who not in met and who in said:
                rows.append((order, path, para, who, "met", said[who]))
                met.add(who)
            continue
        seen.add(who)
        said[who] = text
        if status == "met":
            met.add(who)
        rows.append((order, path, para, who, status, text))

    # heard -> met is one way: once met, a later passage that only mentions
    # them does not demote them
    met_at: dict[str, int] = {}
    for order, _p, para, who, status, _t in rows:
        if status == "met":
            met_at.setdefault(who, _pos(order, para))
    fixed = []
    for order, path, para, who, status, text in rows:
        if who in met_at and _pos(order, para) >= met_at[who]:
            status = "met"
        fixed.append((order, path, para, who, status, text))
    # one row per person per position: the last word wins
    dedup = {}
    for r in fixed:
        dedup[(r[0], r[2], r[3])] = r
    return _thin(sorted(dedup.values(), key=lambda r: (_pos(r[0], r[2]), r[3])))


def _thin(rows):
    """Drop rows that would not change what the reader sees, and count.

    Most of the who's who is the same person described the same way passage
    after passage; keeping a row only when the status flips or the wording
    really changes cuts people.tsv to a fraction. The running count of
    passages each person has turned up in rides along on the rows that are
    kept, so the device can still rank the leads first.
    -> [(order, path, para, name, met|heard, seen so far, description)]
    """
    import difflib

    out, count, last = [], {}, {}
    for order, path, para, who, status, text in rows:
        count[who] = count.get(who, 0) + 1
        prev = last.get(who)
        if prev and prev[0] == status and (
                prev[1] == text or
                difflib.SequenceMatcher(None, prev[1].lower(), text.lower()).ratio() >= 0.75):
            continue
        last[who] = (status, text)
        out.append((order, path, para, who, status, count[who], text))
    return out


def _pos(order: int, para: int) -> int:
    return order * 1_000_000 + para


def _identities(named, ctx) -> dict[str, str]:
    """Raw name -> the one display name for that person.

    "Harry", "Harry Potter" and "Potter" are one person; "Mrs. Finnigan" and
    "Seamus Finnigan" are two. The shown name is the earliest form the reader
    met, so a full name learned later is not imported backwards.
    """
    try:
        from ..tools import dedupe_cast
        return dedupe_cast.identities([(pos, "", name, "") for pos, name in named])
    except Exception as exc:                                # noqa: BLE001
        ctx.log.debug("    cast dedupe failed: %s", exc)
        return {n: n for _p, n in named}


# ------------------------------------------------------------ outputs


def _chapter_rows(parts):
    """Chapter-level rows for readers that predate passages."""
    by = {}
    for order, path, _end, label, summary in parts:
        by.setdefault((order, path, label), []).append(summary)
    return [(o, p, lab, " ".join(s)) for (o, p, lab), s in sorted(by.items())]


def _legacy_cast(people):
    last = {}
    for order, path, _para, who, _st, _n, text in people:
        last[(order, who)] = (order, path, who, text)
    return sorted(last.values(), key=lambda r: (r[0], r[2]))


def _clean(s: str) -> str:
    return re.sub(r"[\t\r\n]+", " ", str(s)).strip()


def _embed(book: Path, parts, people, chapters, cast, spine=(), meta=()) -> None:
    """Write the TSVs into the book, in the manifest and never the spine.

    Declared so EPUBCheck does not call them undeclared resources; out of the
    spine so the reader never turns a page into them. Files an earlier version
    of this stage wrote beside the OPF are replaced, not left to disagree.
    """
    files = {
        "parts.tsv": "\n".join("%d\t%s\t%d\t%s\t%s" % (o, p, e, _clean(lab), _clean(s))
                               for o, p, e, lab, s in parts),
        "people.tsv": "\n".join("%d\t%s\t%d\t%s\t%s\t%d\t%s" % (o, p, n, _clean(w), st, c, _clean(t))
                                for o, p, n, w, st, c, t in people),
        "recaps.tsv": "\n".join("%d\t%s\t%s\t%s" % (o, p, _clean(lab), _clean(s))
                                for o, p, lab, s in chapters),
        "spine.tsv": "\n".join("%d\t%s" % (o, p) for o, p in spine),
        "meta.tsv": "\n".join("%s\t%s" % (k, _clean(v)) for k, v in meta),
        "cast.tsv": "\n".join("%d\t%s\t%s\t%s" % (o, p, _clean(w), _clean(t))
                              for o, p, w, t in cast),
    }
    # built in the system temp folder, never beside the book: a library
    # server watching that folder would import a stray .epub as a new book
    fd, tmp = tempfile.mkstemp(suffix=".epubkit-tmp")
    os.close(fd)
    try:
        with zipfile.ZipFile(book) as zin:
            names = zin.namelist()
            opf_name = _opf_name(zin)
            base = posixpath.dirname(opf_name) if opf_name else ""
            here = posixpath.join(base, RECAP_DIR) if base else RECAP_DIR
            want = {here + "/" + k: v for k, v in files.items() if v}
            with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zout:
                if "mimetype" in names:
                    zi = zipfile.ZipInfo("mimetype")
                    zi.compress_type = zipfile.ZIP_STORED
                    zout.writestr(zi, zin.read("mimetype"))
                for n in names:
                    if n == "mimetype" or _is_ours(n, base):
                        continue
                    info = zin.getinfo(n)
                    data = zin.read(n)
                    if n == opf_name:
                        data = _declare(data.decode("utf-8", "ignore"),
                                        [k for k in files if files[k]]).encode("utf-8")
                    # the member's own name, date and compression, so nothing
                    # but the recaps differs from the book that came in
                    zout.writestr(info, data)
                for n, body in want.items():
                    zout.writestr(n, body + "\n", compress_type=zipfile.ZIP_DEFLATED,
                                  compresslevel=9)
        problem = _changed_besides_recaps(book, tmp)
        if problem:
            raise RuntimeError("refusing to write %s: %s" % (book.name, problem))
        # Overwrite the file in place rather than renaming a new one over it:
        # same path, same inode, so a library server watching the folder sees
        # its book modified, not one book deleted and another added.
        shutil.copyfile(tmp, str(book))
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


_OURS = ("recaps", "cast", "parts", "people", "spine", "meta")


def _is_ours(name: str, base: str) -> bool:
    """A recap file this stage (or an earlier version of it) wrote."""
    rel = name[len(base) + 1:] if base and name.startswith(base + "/") else name
    if rel.startswith(RECAP_DIR + "/"):
        return rel[len(RECAP_DIR) + 1:].split(".")[0] in _OURS and rel.endswith(".tsv")
    return rel in ("recaps.tsv", "cast.tsv")          # the old flat layout


def _changed_besides_recaps(old: Path, new: str) -> str:
    """'' when the only differences are recap files and their manifest lines.

    This is the guarantee behind rewriting books that are already in a
    library: text, images, styles, metadata and every koboSpan come out
    byte-for-byte as they went in.
    """
    with zipfile.ZipFile(old) as a, zipfile.ZipFile(new) as b:
        if b.testzip() is not None:
            return "the new file fails its own CRC check"
        opf_name = _opf_name(a)
        base = posixpath.dirname(opf_name) if opf_name else ""
        keep_a = [n for n in a.namelist() if not _is_ours(n, base)]
        keep_b = [n for n in b.namelist() if not _is_ours(n, base)]
        if sorted(keep_a) != sorted(keep_b):
            return "the list of files inside it changed"
        for n in keep_a:
            x, y = a.read(n), b.read(n)
            if n == opf_name:
                x, y = _strip_recap_items(x), _strip_recap_items(y)
            if x != y:
                return "%s changed" % n
    return ""


def _strip_recap_items(opf: bytes) -> bytes:
    return re.sub(rb'<item\b[^>]*\bhref="(?:%s/)?(?:%s)\.tsv"[^>]*/>'
                  % (RECAP_DIR.encode(), "|".join(_OURS).encode()), b"", opf)


def _declare(opf: str, present) -> str:
    # drop declarations of the old flat files and of any recap file we are
    # rewriting, then declare what is actually there
    opf = _strip_recap_items(opf.encode("utf-8")).decode("utf-8")
    ids = set(re.findall(r'<item\b[^>]*\bid="([^"]+)"', opf))
    add = ""
    for name in present:
        ident = "recap-" + name.replace(".", "-")
        while ident in ids:
            ident += "x"
        add += ('<item id="%s" href="%s/%s" media-type="text/tab-separated-values"/>'
                % (ident, RECAP_DIR, name))
    if add and "</manifest>" in opf:
        opf = opf.replace("</manifest>", add + "</manifest>", 1)
    return opf


# ------------------------------------------------------------ audit


def audit(book: Path) -> dict:
    """What recap data a book carries, without a model and without changing it.

    kind is one of:
      none      - no recap data
      flat      - recaps.tsv beside the OPF, from epubkit before passages.
                  recap.sh never looked there, so these never showed on a Kobo
      chapters  - recaps/recaps.tsv only: whole chapters, from gen_recaps.py
      passages  - recaps/parts.tsv: up to the page, with the who's who
    """
    out = {"book": book.name, "kind": "none", "format": 0, "passages": 0,
           "chapters": 0, "people": 0, "heard_of": 0, "recap_kb": 0.0,
           "book_kb": round(book.stat().st_size / 1024, 1), "kepub": False,
           "passage_words": "", "model": "", "main_character": "",
           "lead_off": False, "language": "", "lang_off": False, "error": ""}
    try:
        with zipfile.ZipFile(book) as z:
            names = z.namelist()
            opf_name = _opf_name(z)
            base = posixpath.dirname(opf_name) if opf_name else ""
            here = (base + "/" if base else "") + RECAP_DIR + "/"
            ours = [n for n in names if _is_ours(n, base)]
            out["recap_kb"] = round(sum(z.getinfo(n).compress_size for n in ours) / 1024, 1)
            out["kepub"] = any(b"koboSpan" in z.read(n) for n in names
                               if n.lower().endswith((".xhtml", ".html", ".htm")))

            def rows(name):
                return [r.split("\t") for r in
                        z.read(here + name).decode("utf-8", "ignore").splitlines() if r]

            if here + "parts.tsv" in names:
                out["kind"] = "passages"
                parts = rows("parts.tsv")
                out["passages"] = len(parts)
                out["chapters"] = len({p[0] for p in parts})
                if here + "people.tsv" in names:
                    people = rows("people.tsv")
                    who = {r[3] for r in people if len(r) > 4}
                    met = {r[3] for r in people if len(r) > 4 and r[4] == "met"}
                    out["people"], out["heard_of"] = len(who), len(who - met)
                if here + "meta.tsv" in names:
                    meta = dict(r[:2] for r in rows("meta.tsv") if len(r) >= 2)
                    out["format"] = int(meta.get("format", 0) or 0)
                    out["passage_words"] = meta.get("passage_words", "")
                    out["model"] = meta.get("model", "")
                    out["main_character"] = meta.get("main_character", "")
                    out["language"] = meta.get("language", "")
                    # a series named after a person in the book ("Detective
                    # Gina Harte") whose recaps were written around someone
                    # else: done before the series-lead rule, redo them
                    series = ""
                    if opf_name:
                        opf = z.read(opf_name).decode("utf-8", "ignore")
                        m = re.search(r'name="calibre:series" content="([^"]*)"', opf) or \
                            re.search(r'property="belongs-to-collection"[^>]*>([^<]*)<', opf)
                        series = html.unescape(m.group(1)) if m else ""
                    if series and here + "people.tsv" in names:
                        firsts = {_first_name(r[3]) for r in people if len(r) > 4}
                        words = _series_words(series)
                        lead = _first_name(out["main_character"])
                        named = words & firsts
                        out["lead_off"] = bool(named) and lead not in words
            elif here + "recaps.tsv" in names:
                out["kind"] = "chapters"
                out["chapters"] = len(rows("recaps.tsv"))
                if here + "cast.tsv" in names:
                    out["people"] = len({r[2] for r in rows("cast.tsv") if len(r) > 2})
            elif any(n.endswith("recaps.tsv") for n in ours):
                out["kind"] = "flat"
                flat = next(n for n in ours if n.endswith("recaps.tsv"))
                out["chapters"] = len([r for r in z.read(flat).decode(
                    "utf-8", "ignore").splitlines() if r])
    except (zipfile.BadZipFile, OSError, KeyError, ValueError) as exc:
        out["error"] = str(exc)
    if out["kind"] == "passages" and not out["error"]:
        # recaps from before French support, or in the wrong language: redo.
        # Books from then carry no language and were all written as English.
        out["lang_off"] = book_language(book) != (out["language"] or "en")
    return out
