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
        if "Name | kind | fact | quote" in prompt:
            text = prompt.split("Text:", 1)[1]
            out = ["%s | %s | %s | %s" % ((row[0], "change" if "died" in row[1] else "who") + row[1:])
                   for key, row in self.facts if key in text]
            return "\n".join(out) or "NONE"
        if "Write the who's-who entry" in prompt:
            if self.bad_entry:
                return "A wizard from Narnia who hates Zorblax."
            lines = re.findall(r"(?m)^- (.+)$", prompt.split("Who ", 1)[1])
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


def test_a_fact_under_a_slightly_different_name_finds_its_person():
    carl = type("P", (), {"forms": ["Carl"]})()
    katia = type("P", (), {"forms": ["Katia", "Katia Grimaldi"]})()
    batch = [carl, katia]
    assert F.match("Carl's", batch) is carl and F.match("Crawler Carl", batch) is carl
    assert F.match("Katia Grimaldi", batch) is katia and F.match("katia", batch) is katia
    assert F.match("Zorblax", batch) is None


def test_a_family_name_several_people_share_is_nobody():
    names = S.Names([("Vernon Dursley", [], "person"), ("Petunia Dursley", [], "person"),
                     ("Neville Longbottom", [], "person")])
    assert F._family_only("Mr. Dursley", names) and F._family_only("Dursley", names)
    assert not F._family_only("Longbottom", names) and not F._family_only("Mr. Ollivander", names)


def test_someone_never_told_about_gets_a_question_of_their_own():
    class Shy(Model):
        """Says nothing about Katia unless she is asked about alone."""
        def __call__(self, prompt, system, usable):
            reply = super().__call__(prompt, system, usable)
            if "Name | kind | fact | quote" in prompt and "- Katia: nothing yet" not in prompt:
                reply = "\n".join(x for x in reply.splitlines()
                                   if not x.startswith("Katia")) or "NONE"
            return reply
    model = Shy(FACTS)
    rows, _report, _ = run(model)
    katia = [r for r in rows if r[3] == "Katia"]
    assert katia and "doppelganger" in katia[0][6]


def test_a_fact_line_with_or_without_its_kind():
    got = F.parse_facts("Albus Dumbledore | who | Headmaster of Hogwarts | the headmaster of Hogwarts\n"
                        "Hagrid | Keeper of Keys | Keeper of Keys and Grounds at Hogwarts\nNONE")
    assert got[0] == ("Albus Dumbledore", "who", "Headmaster of Hogwarts", "the headmaster of Hogwarts")
    assert got[1][:3] == ("Hagrid", "who", "Keeper of Keys")


def test_a_quote_may_differ_by_a_word_or_a_mark():
    text = 'The hat shouted "GRYFFINDOR!" and Neville ran off still wearing it to the table.'
    assert F.quoted("shouted Gryffindor and Neville ran", text)
    assert F.quoted("Neville ran off wearing it to the table", text)
    assert not F.quoted("Neville is a Slytherin prefect", text)


def test_who_they_are_is_never_pushed_out_by_the_latest_facts():
    facts = [F.Fact(i, 1, "a", i, "fact %d" % i, kind="who" if i < 2 else "change")
             for i in range(12)]
    who, changes = F._shown_facts(facts)
    assert [f.text for f in who] == ["fact 0", "fact 1"]
    assert changes[-1].text == "fact 11" and len(changes) <= F.LAST_FACTS
