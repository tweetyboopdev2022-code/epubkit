"""Suggest which library a finished book belongs in: Kids, Teens or Adult.

A suggestion for the report, never an action. The reason it errs the way it
does: a picture book in the adult library is an annoyance, but an adult
thriller in the kids' library is in front of a child whose Kobo is limited to
that library. So the default is Adult, Kids and Teens need something the book
or a catalogue actually said, and every suggestion carries the words that led
to it, so it can be checked in a glance.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

KIDS, TEENS, ADULT = "Kids", "Teens", "Adult"

# what a catalogue or a publisher writes for a younger reader
_TEENS = re.compile(r"\b(young adult|y\.?a\.? fiction|teen(?:s|age|agers)?|"
                    r"ages? (?:1[2-8])\s*(?:and up|\+|[-–]\s*1[2-8]))\b", re.I)
_KIDS = re.compile(r"\b(picture books?|early readers?|chapter books?|middle grade|"
                   r"juvenile (?:fiction|literature|nonfiction)|children'?s|"
                   r"board books?|graphic novels? for (?:kids|children)|"
                   r"ages? (?:[3-9]|1[01])\s*(?:and up|\+|[-–]\s*(?:[4-9]|1[0-2]))|"
                   r"grades? (?:k|[1-6])\b)", re.I)
_NONFICTION = re.compile(r"\b(non-?fiction|biograph\w*|memoirs?|autobiograph\w*|"
                         r"self-help|history|science|essays|true crime|cooking|"
                         r"business|psychology|politics)\b", re.I)
_FICTION = re.compile(r"\bfiction\b", re.I)

# A blurb is prose, and prose about an adult novel says "a teenage girl" and "a
# children's author" all the time. So descriptions only count for wording that
# names the reader, never one that merely mentions a young person. The broad
# patterns above are for subject headings, which classify the book itself.
_TEENS_BLURB = re.compile(r"\b(young adult (?:novel|fiction|series|book|readers?)|"
                          r"for (?:young adults|teens|teen readers)|"
                          r"ages? 1[2-8]\s*(?:and up|\+|[-–]\s*1[2-8]))\b", re.I)
_KIDS_BLURB = re.compile(r"\b(picture books?|early readers?|middle[- ]grade|"
                         r"for (?:young readers|kids|children)|"
                         r"ages? (?:[3-9]|1[01])\s*(?:and up|\+|[-–]\s*(?:[4-9]|1[0-2]))|"
                         r"grades? (?:k|[1-6])\b)", re.I)


@dataclass
class Suggestion:
    library: str
    folder: str = ""                 # for Adult: Fiction or Non-fiction, when clear
    confidence: str = "low"          # high | medium | low
    reasons: list[str] = field(default_factory=list)

    def label(self) -> str:
        return self.library + (" › %s" % self.folder if self.folder else "")


def _hits(rx: re.Pattern, texts: list[str]) -> list[str]:
    out: list[str] = []
    for t in texts:
        for m in rx.finditer(t):
            w = m.group(0).strip().lower()
            if w not in out:
                out.append(w)
    return out


def recommend(subjects: list[str] | None, description: str | None = None,
              title: str | None = None, series: str | None = None) -> Suggestion:
    subs = [s for s in (subjects or []) if s]
    blurb = [description] if description else []

    teens = _hits(_TEENS, subs) + _hits(_TEENS_BLURB, blurb)
    kids = _hits(_KIDS, subs) + _hits(_KIDS_BLURB, blurb)
    teens, kids = list(dict.fromkeys(teens)), list(dict.fromkeys(kids))

    if teens and not kids:
        # a YA label is explicit; two independent phrases make it firm
        return Suggestion(TEENS, confidence="high" if len(teens) > 1 else "medium",
                          reasons=["says: " + ", ".join(teens[:3])])
    if kids and not teens:
        subject_hit = bool(_hits(_KIDS, subs))
        return Suggestion(KIDS, confidence="high" if len(kids) > 1 or subject_hit else "medium",
                          reasons=["says: " + ", ".join(kids[:3])])
    if kids and teens:
        # both younger labels at once: the older shelf is the safer one
        return Suggestion(TEENS, confidence="low",
                          reasons=["says both %s and %s; chose the older shelf, check it"
                                   % (kids[0], teens[0])])

    folder = ""
    if _hits(_NONFICTION, subs) and not _hits(_FICTION, [s for s in subs
                                                       if not re.search(r"non-?fiction", s, re.I)]):
        folder = "Non-fiction"
    elif _hits(_FICTION, subs):
        folder = "Fiction"
    if subs or description:
        return Suggestion(ADULT, folder, "medium" if subs else "low",
                          ["nothing marks it as written for younger readers"])
    return Suggestion(ADULT, folder, "low",
                      ["no subjects or description to go on; Adult is the default"])
