"""The who's who from facts gathered chapter by chapter (cast_method = "facts")."""
from __future__ import annotations

import logging
import re

from epubkit import series_memory as S
from epubkit.stages import cast_facts as F


class Ctx:
    log = logging.getLogger("test")

    def report(self, _t):
        pass

    def note(self, _t):
        pass

    def tell(self, _t):
        pass


DOCS = [
    (1, "c1.xhtml", "Chapter 1", [
        (0, "Carl walked into the guild hall. Mordecai, the game guide, was mixing potions."),
        (1, "“Sit down,” Mordecai said. Rambo would have run, Carl thought."),
        (2, "Donut, a cat who was Carl's ex-girlfriend's pet, jumped onto the table."),
    ]),
    (2, "c2.xhtml", "Chapter 2", [
        (0, "Katia Grimaldi came in. Katia is a doppelganger who can change her face."),
        (1, "Carl watched Mordecai. Mordecai had once been a crawler himself."),
        (2, "Then the trap fell and Mordecai's old friend Iota died in the fire. Iota was gone."),
        (3, "Carl, Donut and Katia went down. Rambo, Rambo, Rambo, Carl sang. Rambo! Rambo!"),
    ]),
]

LIST = S.Names([("Carl", [], "person"), ("Mordecai", ["Mordy"], "person"),
                ("Princess Donut", ["Donut"], "person"),
                ("Katia Grimaldi", ["Katia"], "person"), ("Iota", [], "person")])


class Model:
    """Answers the facts question from a table of (person, fact, quote) per
    chapter text, and writes an entry from the facts it is shown."""

    def __init__(self, facts, bad_entry=False):
        self.facts = facts
        self.bad_entry = bad_entry
        self.prompts = []

    def __call__(self, prompt, system, usable):
        self.prompts.append(prompt)
        if "Name | kind | matters" in prompt:
            names = re.findall(r"(?m)^- ([^:\n]+):", prompt.split("Names:", 1)[1])
            return "\n".join("%s | %s | no" % (n, "other") for n in names)
        if "Name | fact | quote" in prompt:
            text = prompt.split("Text:", 1)[1]
            out = ["%s | %s | %s" % row for key, row in self.facts if key in text]
            return "\n".join(out) or "NONE"
        if "Write the who's-who entry" in prompt:
            if self.bad_entry:
                return "A wizard from Narnia who hates Zorblax."
            lines = re.findall(r"(?m)^- (.+)$", prompt.split("in order", 1)[1])
            return " ".join(lines[-2:])
        return ""


FACTS = [
    ("Mordecai, the game guide", ("Mordecai", "A game guide who mixes potions", "Mordecai, the game guide")),
    ("Donut, a cat", ("Donut", "A cat who was Carl's ex-girlfriend's pet", "a cat who was Carl's")),
    ("Katia is a doppelganger", ("Katia Grimaldi", "A doppelganger who can change her face",
                                 "Katia is a doppelganger")),
    ("Iota died", ("Iota", "Mordecai's old friend, who died in the fire",
                   "old friend Iota died in the fire")),
    # a fact whose quote is not in the text, and one about someone not asked
    ("Mordecai had once", ("Mordecai", "Was once a crawler", "he was a crawler long ago")),
    ("Mordecai had once", ("Zorblax", "A wizard", "Mordecai had once been")),
]


def run(model, **kw):
    return F.build(Ctx(), DOCS, model, hero="Carl", title="Dungeon", names_list=LIST, **kw)


def test_only_listed_people_get_an_entry_and_names_join():
    rows, report, _ = run(Model(FACTS))
    who = {r[3] for r in rows}
    assert {"Mordecai", "Donut", "Katia", "Iota"} <= who
    assert "Rambo" not in who                 # the model sorted it: not a person
    assert not any(r[3] == "Katia Grimaldi" for r in rows) or "Katia" not in who


def test_an_entry_comes_from_checked_facts_filed_where_they_are_told():
    rows, report, _ = run(Model(FACTS))
    iota = [r for r in rows if r[3] == "Iota"]
    assert iota and iota[0][:3] == (2, "c2.xhtml", 2)     # not before the reader is there
    assert "died" in iota[-1][6]
    mordecai = " ".join(r[6] for r in rows if r[3] == "Mordecai")
    assert "game guide" in mordecai and "long ago" not in mordecai
    assert any("quote" in line for line in report)


def test_an_entry_that_strays_from_its_facts_is_not_used():
    rows, _report, _ = run(Model(FACTS, bad_entry=True))
    text = " ".join(r[6] for r in rows)
    assert "Narnia" not in text and "game guide" in text


def test_a_fact_is_checked_against_its_text():
    text = "Katia is a doppelganger who can change her face."
    assert F.fact_problems("A doppelganger who can change her face", "Katia is a doppelganger",
                           text, ["Katia"], "") == []
    assert "quote" in F.fact_problems("A doppelganger", "Katia is a human", text, ["Katia"], "")
    assert any(b.startswith("name") for b in F.fact_problems(
        "A doppelganger sent by Zorblax", "Katia is a doppelganger", text, ["Katia"], ""))
    assert any(b.startswith("tie") for b in F.fact_problems(
        "The sister of Donut, a doppelganger", "Katia is a doppelganger", text + " Donut ran.",
        ["Katia"], ""))
