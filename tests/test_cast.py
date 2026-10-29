"""The who's who from the whole book: names from the text, checked entries."""
from __future__ import annotations

import re

from test_recaps import FILLER, _set_series, build, rows, run

from epubkit.stages import cast as C
from epubkit.stages import recaps as R

BOOK = {"recaps.cast_method": "book", "ai.concurrency": 1}


class Model:
    """Sorts names by a table, and answers entries from a script.

    entries: name -> list of entries, one per question about them, the last
    repeated; an entry of None answers with no QUOTE line, "SAME" says nothing
    changed. A quote is always taken from the prompt's first extract.
    """

    def __init__(self, kinds, entries=None, hero="Carl", untrue=()):
        self.kinds = kinds
        self.untrue = untrue        # words that make the checking question answer NO
        self.entries = entries or {}
        self.hero = hero
        self.calls = []
        self.asked = {}

    def complete(self, prompt, system=None, **kw):
        self.calls.append(prompt)
        return type("R", (), {"text": self.answer(prompt)})()

    def answer(self, prompt):
        if "Who is its main character" in prompt:
            return self.hero
        if "Name | kind | matters" in prompt:
            out = []
            for name in re.findall(r"(?m)^- ([^:\n]+):", prompt.split("Names:", 1)[1]):
                kind, matters = self.kinds.get(name, ("other", "no"))
                out.append("%s | %s | %s" % (name, kind, matters))
            return "\n".join(out)
        if "Sentences to check:" in prompt:
            said = re.findall(r"(?m)^(\d+)\. (.+)$", prompt.split("Sentences to check:", 1)[1])
            return "\n".join("%s. %s" % (n, "NO" if any(w in x for w in self.untrue) else "YES")
                             for n, x in said)
        if "Write what happened" in prompt:
            return "Carl and Donut went down into the dungeon."
        m = re.search(r"(?m)^Person: (.+?)(?: \(also called .*\))?$", prompt)
        if m:
            name = m.group(1)
            n = self.asked.get(name, 0)
            self.asked[name] = n + 1
            # quote the extract that shows a change, else the first one
            first = re.search(r"(?m)^\d+\. …?(.*revealed.*)$", prompt) or \
                re.search(r"(?m)^1\. …?(.+)$", prompt)
            quote = " ".join(first.group(1).split()[:5])
            script = self.entries.get(name, ["Seen where %s." % quote])
            entry = script[min(n, len(script) - 1)]
            if callable(entry):
                entry = entry(prompt)
            if entry is None:
                return "STATUS: met\nENTRY: A person."
            return "QUOTE: %s\nSTATUS: met\nENTRY: %s" % (quote, entry)
        # a passage
        return "SUMMARY: things happened.\nPEOPLE:\n"


def dungeon(tmp_path, chapters):
    book = build(tmp_path / "Dungeon Crawler Carl 05 - The Butcher's Masquerade.kepub.epub",
                 chapters)
    return book


CH1 = [
    "Carl looked at the door. Mordecai waited inside the guild hall with a "
    "potion in his hand. %s" % FILLER,
    "“You are late,” Mordecai said to Carl. Donut jumped onto the table. %s" % FILLER,
    "The Syndicate had sent a message. The Syndicate would be watching. "
    "Carl thought of the books by Louis L'Amour his grandmother read. %s" % FILLER,
    "NEW ACHIEVEMENT! Carl looked at the screen. Then he looked at Donut. %s" % FILLER,
]
CH2 = [
    "Katia Grimaldi stepped into the room. Katia smiled at Donut. %s" % FILLER,
    "Mordecai mixed another potion while Carl and Katia talked. %s" % FILLER,
    "Donut said that Mordecai was the best game guide in the dungeon. %s" % FILLER,
    "Carl and Mordecai and Donut and Katia went down the stairs. %s" % FILLER,
]

KINDS = {"Carl": ("person", "yes"), "Mordecai": ("person", "yes"),
         "Donut": ("person", "yes"), "Katia Grimaldi": ("person", "yes"),
         "Katia": ("person", "yes"), "Syndicate": ("group", "yes"),
         "Louis L'Amour": ("real", "no")}


def people_of(book):
    return [r for r in rows(book, "people.tsv")]


# -- names come from the text ------------------------------------------------

def test_names_are_found_in_the_text_not_taken_from_the_model():
    docs = [(1, "a.xhtml", "Chapter 1", list(enumerate(CH1))),
            (2, "b.xhtml", "Chapter 2", list(enumerate(CH2)))]
    names = C.find_names(docs)
    assert {"Carl", "Mordecai", "Donut", "Katia Grimaldi", "Syndicate"} <= set(names)
    # all-capitals system messages and grammar words are not names
    assert "NEW ACHIEVEMENT" not in names and "Then" not in names
    assert names["Mordecai"]["first"] == (1, 0)
    # only ever whole: never a name cut short
    assert "Katia Grim" not in names


def test_a_word_used_only_to_start_sentences_is_not_a_name():
    docs = [(1, "a.xhtml", "", [(0, "Suddenly the rain came. Suddenly it stopped. "
                                    "Wren saw it and Wren laughed.")])]
    names = C.find_names(docs)
    assert "Suddenly" not in names and "Wren" in names


def test_groups_places_and_real_people_are_not_listed(tmp_path):
    book = dungeon(tmp_path, [CH1, CH2])
    run(tmp_path, book, Model(KINDS), **BOOK)
    names = {r[3] for r in people_of(book)}
    assert {"Carl", "Mordecai", "Donut"} <= names
    assert "Syndicate" not in names and "Louis L'Amour" not in names
    # Katia and Katia Grimaldi are one person
    assert len([n for n in names if n.startswith("Katia")]) == 1


# -- every entry is checked ------------------------------------------------------

def test_an_entry_naming_what_the_book_never_said_is_asked_again(tmp_path):
    book = dungeon(tmp_path, [CH1, CH2])
    model = Model(KINDS, {"Donut": ["A cat who conquered Cleveland with Carl.",
                                    "A cat who jumps onto tables near Carl."]})
    run(tmp_path, book, model, **BOOK)
    said = [r[6] for r in people_of(book) if r[3] == "Donut"]
    assert said and all("Cleveland" not in s for s in said)
    asked = [c for c in model.calls if re.search(r"(?m)^Person: Donut", c)]
    assert any("Cleveland" in c and "could not be used" in c for c in asked)


def test_an_entry_that_fails_twice_is_left_out_not_shown(tmp_path):
    book = dungeon(tmp_path, [CH1, CH2])
    model = Model(KINDS, {"Mordecai": ["Not yet connected to Carl."]})
    run(tmp_path, book, model, **BOOK)
    assert not [r for r in people_of(book) if "connected" in r[6]]
    assert "Mordecai" not in {r[3] for r in people_of(book)}


def test_a_reply_without_a_quote_is_not_used(tmp_path):
    book = dungeon(tmp_path, [CH1, CH2])
    run(tmp_path, book, Model(KINDS, {"Mordecai": [None]}), **BOOK)
    assert "Mordecai" not in {r[3] for r in people_of(book)}


def test_problems_catches_each_kind_of_bad_entry():
    words = C.WORDS["en"]
    src = "Mordecai waited inside the guild hall with a potion in his hand. Carl"
    ok = C.problems("A game guide who waits in the guild hall with a potion.",
                    "waited inside the guild hall", src, ["Mordecai"], {}, words)
    assert ok == []
    assert C.problems("A game guide.", "waited outside the castle", src,
                      ["Mordecai"], {}, words)            # not a real quote
    assert C.problems("The main character's guide in the guild hall.",
                      "waited inside the guild hall", src, ["Mordecai"], {}, words)
    assert C.problems("A guide who once ruled Valtay from the guild hall.",
                      "waited inside the guild hall", src, ["Mordecai"], {}, words)
    same = C.problems("A game guide who waits in the guild hall with a potion.",
                      "waited inside the guild hall", src, ["Mordecai"],
                      {"Katia": "A game guide who waits in the guild hall with a potion."},
                      words)
    assert same and "Katia" in same[0]
    # nothing changed is an answer in itself
    assert C.problems("SAME", "-", src, ["Mordecai"], {}, words, same_ok=True) == []


# -- one entry per person, moved on only when something changes -------------------

def test_the_lead_gets_a_real_entry_and_stays_top(tmp_path):
    book = dungeon(tmp_path, [CH1, CH2])
    model = Model(KINDS, {"Carl": ["A crawler who looks after Donut in the dungeon."]})
    run(tmp_path, book, model, **BOOK)
    rs = people_of(book)
    carl = [r for r in rs if r[3] == "Carl"]
    assert carl and all(r[6] != "the main character" for r in carl)
    assert all(r[4] == "met" for r in carl)
    # at Carl's last row his count is above everyone's so far
    last = carl[-1]
    pos = R._pos(int(last[0]), int(last[2]))
    others = [int(r[5]) for r in rs if r[3] != "Carl" and R._pos(int(r[0]), int(r[2])) <= pos]
    assert int(last[5]) > max(others)


def test_same_adds_no_row_and_a_change_is_filed_where_it_was_learned(tmp_path):
    chapters = [CH1, CH2] + [["Mordecai said little. %s" % FILLER] * 3 for _ in range(4)]
    chapters.append(["Mordecai revealed he had once been a crawler himself. %s" % FILLER,
                     "Mordecai looked at Carl. %s" % FILLER,
                     "Mordecai sighed. %s" % FILLER])
    book = dungeon(tmp_path, chapters)
    def later(prompt):
        if "revealed he had once been" in prompt:
            return "A game guide who was once a crawler himself."
        return "SAME"
    model = Model(KINDS, {"Mordecai": ["A game guide who waits in the guild hall.", later]})
    run(tmp_path, book, model, **BOOK)
    mor = [r for r in people_of(book) if r[3] == "Mordecai"]
    texts = [r[6] for r in mor]
    assert texts[0] == "A game guide who waits in the guild hall."
    assert texts[-1] == "A game guide who was once a crawler himself."
    assert len(set(texts)) == len(texts), "an unchanged entry is not filed again"
    # the change is never filed before the paragraph that tells it
    last = mor[-1]
    assert int(last[0]) == max(int(r[0]) for r in mor)
    import zipfile

    from test_recaps import para_of
    with zipfile.ZipFile(book) as z:
        member = [n for n in z.namelist() if n.endswith("ch%d.xhtml" % len(chapters))][0]
    assert int(last[2]) >= para_of(book, member, "revealed he had once been")


def test_the_first_entry_sees_nothing_past_the_first_mention_chapter(tmp_path):
    book = dungeon(tmp_path, [CH1, CH2])
    model = Model(KINDS)
    run(tmp_path, book, model, **BOOK)
    first = next(c for c in model.calls if re.search(r"(?m)^Person: Mordecai", c))
    assert "Katia" not in first.split("Extracts that mention them", 1)[1]


# -- characters only -----------------------------------------------------------

def test_characters_only_keeps_recaps_and_reads_no_passages(tmp_path):
    book = dungeon(tmp_path, [CH1, CH2])
    run(tmp_path, book, Model(KINDS), **{"ai.concurrency": 1})
    before = rows(book, "parts.tsv")
    model = Model(KINDS)
    run(tmp_path, book, model, **BOOK, **{"recaps.cast_only": True})
    assert rows(book, "parts.tsv") == before
    assert not [c for c in model.calls if "Passage:" in c]
    meta = dict((r[0], r[1]) for r in rows(book, "meta.tsv"))
    assert meta.get("people_method") == "book"
    assert {"Carl", "Mordecai"} <= {r[3] for r in people_of(book)}


def test_the_series_memory_gets_each_persons_last_entry(tmp_path):
    book = dungeon(tmp_path, [CH1, CH2])
    _set_series(book, "Dungeon Crawler Carl")
    model = Model(KINDS, {"Mordecai": ["A game guide who waits in the guild hall."]})
    run(tmp_path, book, model, **BOOK, **{"recaps.main_character": "Carl",
                                         "recaps.series_dir": str(tmp_path / "s")})
    import json
    data = json.loads(next((tmp_path / "s").glob("*.json")).read_text())
    by = {n: e["by_book"] for n, e in data["people"].items()}
    assert list(by["Mordecai"].values()) == ["A game guide who waits in the guild hall."]
    assert "Carl" in by


def test_a_report_lists_what_was_set_aside(tmp_path):
    book = dungeon(tmp_path, [CH1, CH2])
    run(tmp_path, book, Model(KINDS), **BOOK,
        **{"recaps.cast_report_dir": str(tmp_path / "rep")})
    text = next((tmp_path / "rep").glob("*.cast.txt")).read_text()
    assert "Syndicate" in text.split("not people:", 1)[1].splitlines()[0]


# -- second round, after the DCC 5 pilot ------------------------------------------

def test_names_that_are_one_person_are_folded_together():
    groups = {n: [n] for n in ["Princess", "Princess Donut", "Princess D’nadia", "Miriam",
                               "Miriam Dom", "Li-Na", "Li Na", "Li Jun", "Sledge", "Sledgie",
                               "Very Sullen", "Bea", "Beatrice", "Carl", "Carla"]}
    used = {n: 5 for n in groups}
    used.update({"Princess Donut": 1200, "Princess D’nadia": 24, "Sledge": 20})
    docs = [(1, "a", "", [(0, "Sledge, also known as Very Sullen, waved.")])]
    got = C.merge_names(groups, used, docs, hero="Carl")
    assert set(got["Princess Donut"]) == {"Princess", "Princess Donut"}
    assert "Miriam" in got["Miriam Dom"]
    assert len([k for k in got if C._squash(k) == "lina"]) == 1
    assert "Li Jun" in got                       # Li is not folded into either
    assert {"Sledgie", "Very Sullen"} <= set(got["Sledge"])
    assert "Bea" in got.get("Beatrice", []) or "Beatrice" in got.get("Bea", [])
    assert "Carl" in got and "Carla" in got      # never the lead


def test_the_entry_does_not_repeat_the_name():
    assert C._unnamed("Orren is a Valtay worm.", ["Orren"]) == "A Valtay worm."
    assert C._unnamed("She is a vampire.", ["Miriam"]) == "A vampire."
    assert C._unnamed("A cat who is Carl's partner.", ["Donut"]) == "A cat who is Carl's partner."


def test_a_scene_is_not_who_someone_is():
    words = C.WORDS["en"]
    src = "Carl sat in the vice principal's office with Donut. A former Coast Guard tech."
    bad = C.problems("A former Coast Guard tech who is currently in the vice principal's office.",
                     "sat in the vice principal's office", src, ["Carl"], {}, words)
    assert words["problems"]["scene"] in bad


def test_a_name_the_model_says_is_not_a_person_is_dropped(tmp_path):
    book = dungeon(tmp_path, [CH1, CH2])

    class Kinds(Model):
        def answer(self, prompt):
            if re.search(r"(?m)^Person: Mongo", prompt):
                return "KIND: not a person (a show)\nQUOTE: -\nSTATUS: heard\nENTRY: -"
            return super().answer(prompt)
    kinds = dict(KINDS, Mongo=("person", "yes"))
    ch = [CH1 + ["Mongo ran. Mongo roared. Mongo slept. Mongo ate."], CH2]
    book = dungeon(tmp_path, ch)
    run(tmp_path, book, Kinds(kinds), **BOOK)
    assert "Mongo" not in {r[3] for r in people_of(book)}


def test_someone_heard_speaking_has_been_met_from_there(tmp_path):
    book = dungeon(tmp_path, [CH1, CH2])

    class Heard(Model):
        def answer(self, prompt):
            return super().answer(prompt).replace("STATUS: met", "STATUS: heard")
    run(tmp_path, book, Heard(KINDS), **BOOK)
    mor = [r for r in people_of(book) if r[3] == "Mordecai"]
    # “You are late,” Mordecai said: the second paragraph of chapter 1
    assert any(r[4] == "met" and int(r[0]) == int(mor[0][0]) for r in mor)
    assert mor[0][4] == "heard"


def test_pronouns_do_not_make_a_name():
    docs = [(1, "a", "", [(0, "Since I left, nobody came. Since I came back, Wren left. "
                              "They said All I wanted. Wren nodded.")])]
    names = C.find_names(docs)
    assert "Since I" not in names and "All I" not in names


def test_a_name_the_book_uses_often_is_never_dropped_as_not_a_person(tmp_path):
    class Kinds(Model):
        def answer(self, prompt):
            if re.search(r"(?m)^Person: Mordecai", prompt) and "Their entry so far" not in prompt \
                    and "answer KIND: person" not in prompt:
                return "KIND: not a person (a pocket kuma)\nQUOTE: -\nSTATUS: heard\nENTRY: -"
            return super().answer(prompt)
    ch = [CH1 + ["Mordecai poured a drink. %s" % FILLER] * 12, CH2]
    book = dungeon(tmp_path, ch)
    run(tmp_path, book, Kinds(KINDS), **BOOK)
    assert "Mordecai" in {r[3] for r in people_of(book)}


def test_a_title_around_a_name_is_that_person():
    groups = {n: [n] for n in ["Donut", "Even Donut", "Dungeon Princess Donut",
                               "NW Princess Donut the Queen", "Mrs. Weasley", "Weasley"]}
    used = {"Donut": 900, "Even Donut": 1, "Dungeon Princess Donut": 2,
            "NW Princess Donut the Queen": 1, "Mrs. Weasley": 2, "Weasley": 60}
    got = C.merge_names(groups, used, [], hero="Carl")
    assert set(got) == {"Donut", "Mrs. Weasley", "Weasley"}


def test_a_title_around_a_short_form_joins_the_full_name():
    groups = {"Princess Donut": ["Princess Donut", "Donut"], "Even Donut": ["Even Donut"],
              "Goddamnit Donut": ["Goddamnit Donut"]}
    used = {"Princess Donut": 80, "Donut": 800, "Even Donut": 1, "Goddamnit Donut": 2}
    got = C.merge_names(groups, used, [], hero="Carl")
    assert set(got) == {"Princess Donut"}


def test_a_name_that_is_a_french_word_is_a_name_in_an_english_book():
    docs = [(1, "a", "", [(0, "Katia waved at Elle. Elle laughed, and Carl saw Elle wave back. "
                              "Then Elle left.")])]
    assert "Elle" in C.find_names(docs)
    fr = [(1, "a", "", [(0, "Elle partit. Puis elle revint, et elle rit avec Jeanne. "
                            "Mais elle dit non, et Jeanne vit Elle.")])]
    assert "Elle" not in C.find_names(fr)


def test_an_entry_about_someone_else_names_that_someone():
    owners = {"Kiwi": "Kiwi", "Tserendolgor": "Tserendolgor", "Carl": "Carl"}
    assert C.alias_of("Kiwi is the Level 60 leader of the pack.",
                      ["Mongoliensis Pack Leader"], owners) == "Kiwi"
    assert C.alias_of("The nickname for Tserendolgor, ranked number 38.",
                      ["Ren"], owners) == "Tserendolgor"
    assert C.alias_of("A crawler who fights beside Carl.", ["Tran"], owners) == ""


def test_miss_and_a_first_name_is_that_person_but_mrs_and_a_family_name_is_not():
    groups = {"Bea": ["Bea", "Beatrice"], "Miss Beatrice": ["Miss Beatrice"],
              "Ron Weasley": ["Ron Weasley", "Ron"], "Fred Weasley": ["Fred Weasley"],
              "Mrs. Weasley": ["Mrs. Weasley"], "Weasley": ["Weasley"]}
    used = {"Bea": 30, "Beatrice": 10, "Miss Beatrice": 21, "Ron Weasley": 40, "Ron": 400,
            "Fred Weasley": 5, "Mrs. Weasley": 30, "Weasley": 60}
    got = C.merge_names(groups, used, [], hero="Harry")
    assert "Miss Beatrice" not in got and "Miss Beatrice" in got["Bea"]
    assert "Mrs. Weasley" in got


def test_one_row_per_person_per_place():
    p = C.Person("Maxim Popov", ["Maxim Popov"], True)
    p.mentions = [(17, "a", 90, "x")]
    p.rows = [(17, "a", 90, "heard", "A crawler."), (17, "a", 90, "met", "A crawler.")]
    got = C._rows([p], "Carl")
    assert len(got) == 1 and got[0][4] == "met"


def test_a_tie_the_book_never_states_is_caught():
    source = ("1. Circe Took rose from her chair. Vrah, her daughter, waited.\n"
              "2. Donut glared at Circe across the table.")
    assert C.unbacked_relations("She is the mother of Donut.", source) == [("Donut", "mother")]
    assert C.unbacked_relations("A hive queen and Vrah's mother.", source) == []
    assert C.unbacked_relations("A hive queen who owns a park.", source) == []


def test_a_last_look_covers_everything_since_the_entry_last_changed(tmp_path):
    chapters = [CH1, CH2] + [["Mordecai said little in chapter %d. %s" % (i, FILLER)] * 3
                             for i in range(3, 9)]
    book = dungeon(tmp_path, chapters)
    seen = []
    def later(prompt):
        seen.append(prompt)
        return "SAME"
    model = Model(KINDS, {"Mordecai": ["A game guide who waits in the guild hall.", later]})
    run(tmp_path, book, model, **BOOK)
    final = seen[-1]
    # the last question writes the entry afresh, from the early and the late
    # chapters together, not from the old draft
    assert "since, in order" not in final
    assert "chapter 3" in final and "chapter 8" in final


def test_a_wrong_first_entry_does_not_last_the_book(tmp_path):
    chapters = [CH1, CH2] + [["Mordecai said little in chapter %d. %s" % (i, FILLER)] * 3
                             for i in range(3, 9)]
    book = dungeon(tmp_path, chapters)
    model = Model(KINDS, {"Mordecai": ["A game guide who waits in the guild hall.", "SAME",
                                       "A game guide who trains Carl in the guild hall."]})
    run(tmp_path, book, model, **BOOK)
    mine = [r for r in people_of(book) if r[3] == "Mordecai"]
    assert mine[-1][-1] == "A game guide who trains Carl in the guild hall."


def test_titles_and_family_names_join_the_one_person_they_mean():
    groups = {"Harry": ["Harry", "Harry Potter"], "Lily Potter": ["Lily Potter"],
              "Potter": ["Potter"], "Severus": ["Severus", "Severus Snape"],
              "Professor Snape": ["Professor Snape"], "Neville": ["Neville", "Neville Longbottom"],
              "Longbottom": ["Longbottom"], "Ron": ["Ron", "Ron Weasley", "Ronald Weasley"],
              "Mr. Ronald Weasley": ["Mr. Ronald Weasley"], "Fred Weasley": ["Fred Weasley"],
              "Mrs. Weasley": ["Mrs. Weasley"]}
    used = {"Harry": 2000, "Harry Potter": 100, "Lily Potter": 5, "Potter": 80,
            "Severus": 20, "Severus Snape": 10, "Professor Snape": 40, "Neville": 200,
            "Neville Longbottom": 10, "Longbottom": 12, "Ron": 900, "Ron Weasley": 30,
            "Ronald Weasley": 3, "Mr. Ronald Weasley": 1, "Fred Weasley": 20, "Mrs. Weasley": 60}
    got = C.merge_names(groups, used, [], hero="Harry")
    assert "Professor Snape" in got["Severus"] and "Longbottom" in got["Neville"]
    assert "Mr. Ronald Weasley" in got["Ron"] and "Potter" in got["Harry"]
    assert "Mrs. Weasley" in got


def test_mum_and_a_bare_title_are_not_names():
    docs = [(1, "a", "", [(0, "Ron said Mum would be cross. Then Mum wrote. The Professor "
                              "nodded; Professor Snape and the Professor left with Ron.")])]
    names = C.find_names(docs)
    assert "Mum" not in names and "Professor" not in names and "Professor Snape" in names


def test_a_full_name_ties_a_first_name_to_a_family_name():
    groups = {"Minerva": ["Minerva"], "Professor McGonagall": ["Professor McGonagall"],
              "Remus": ["Remus"], "Lupin": ["Lupin", "Professor Lupin"],
              "Molly": ["Molly"], "Mrs. Weasley": ["Mrs. Weasley"], "Ron": ["Ron", "Ron Weasley"]}
    used = {"Minerva": 4, "Professor McGonagall": 80, "Minerva McGonagall": 2, "Remus": 10,
            "Lupin": 200, "Professor Lupin": 40, "Remus Lupin": 3, "Molly": 5,
            "Mrs. Weasley": 60, "Molly Weasley": 1, "Ron": 900, "Ron Weasley": 30}
    got = C.merge_names(groups, used, [], hero="Harry")
    assert "Minerva" in got["Professor McGonagall"] and "Remus" in got["Lupin"]
    # Weasley is several people's name: Molly is not joined by it
    assert "Molly" in got and "Mrs. Weasley" in got


def test_a_name_the_earlier_books_know_can_join_one_they_do_not():
    groups = {"Seamus": ["Seamus"], "Seamus Finnigan": ["Seamus Finnigan"]}
    used = {"Seamus": 40, "Seamus Finnigan": 3}
    got = C.merge_names(groups, used, [], keep_as=lambda n: n if n == "Seamus" else "")
    assert list(got) == ["Seamus"] and "Seamus Finnigan" in got["Seamus"]


def test_where_someone_was_last_named_is_the_books_own_sentence(tmp_path):
    book = dungeon(tmp_path, [CH1, CH2])
    run(tmp_path, book, Model(KINDS), **BOOK)
    seen = rows(book, "seen.tsv")
    mor = [r for r in seen if r[3] == "Mordecai"]
    assert mor and all(len(r) == 5 for r in seen)
    assert mor[0][4] == "Mordecai waited inside the guild hall with a potion in his hand."
    # the lead is named everywhere and gets none
    assert not [r for r in seen if r[3] == "Carl"]
    # nobody who is not on the list
    assert {r[3] for r in seen} <= {r[3] for r in people_of(book)}


def test_a_long_sentence_is_cut_around_the_name():
    pat = C._form_pattern(["Wren"])
    long = "It was a very long day " * 20 + "and Wren came home at last " + "slowly " * 30 + "."
    got = C._sentence_with(long, pat)
    assert "Wren" in got and len(got) <= C.SEEN_CHARS + 2


# -- a names list for the series ---------------------------------------------------

NAMES_TSV = (
    "# from the fan wiki: names and kinds only\n"
    "Mordecai\tPocket Kuma; Mordy\tperson\t1\n"
    "Syndicate\tthe Syndicate\tgroup\t1\n"
    "Katia Grimaldi\tKatia\tcrawler\t2\n"
    "Donut\tPrincess Donut\tpet\t1\n"
    "Katia's Secret\tDonut\tperson\t7\n"
)


def test_a_names_list_from_later_books_is_never_read(tmp_path):
    (tmp_path / "Dungeon Crawler Carl.names.tsv").write_text(NAMES_TSV)
    from epubkit import series_memory as S
    five = S.Names.open(tmp_path, "Dungeon Crawler Carl", 5)
    assert five.of("pocket kuma") == "Mordecai" and five.of("Donut") == "Donut"
    assert five.is_person("Syndicate") is False and five.is_person("Katia") is True
    assert five.is_person("Zev") is None
    # book 7's row would make Donut someone else; book 5 never sees it
    assert S.Names.open(tmp_path, "Dungeon Crawler Carl", 7).of("Donut") == ""
    assert not S.Names.open(tmp_path / "none", "Dungeon Crawler Carl", 5)


def test_a_names_list_joins_nicknames_and_settles_what_is_a_person(tmp_path):
    (tmp_path / "s").mkdir()
    (tmp_path / "s" / "Dungeon Crawler Carl.names.tsv").write_text(NAMES_TSV)
    ch3 = ["Pocket Kuma waved from the shelf. Carl waved back. %s" % FILLER,
           "Pocket Kuma said nothing more. %s" % FILLER]
    book = dungeon(tmp_path, [CH1, CH2, ch3])
    _set_series(book, "Dungeon Crawler Carl")
    # the model is wrong about the Syndicate; the list wins and nobody asks it
    model = Model(dict(KINDS, Syndicate=("person", "yes")))
    run(tmp_path, book, model, **BOOK, **{"recaps.series_dir": str(tmp_path / "s")})
    names = {r[3] for r in people_of(book)}
    assert "Mordecai" in names and "Pocket Kuma" not in names
    assert "Syndicate" not in names
    sorts = [c for c in model.calls if "Name | kind | matters" in c]
    assert not any(re.search(r"(?m)^- (Syndicate|Mordecai|Pocket Kuma):", c) for c in sorts)


# -- every sentence backed by the book --------------------------------------------

def test_a_sentence_the_book_does_not_back_is_taken_out(tmp_path):
    book = dungeon(tmp_path, [CH1, CH2])
    model = Model(KINDS, {"Mordecai": ["A game guide who waits in the guild hall. "
                                       "He gives Carl a potion in his hand."]},
                  untrue=("gives Carl",))
    run(tmp_path, book, model, **BOOK)
    said = [r[6] for r in people_of(book) if r[3] == "Mordecai"]
    assert said and all("gives Carl" not in s for s in said)
    assert any(s.startswith("A game guide who waits") for s in said)


def test_an_entry_the_book_does_not_back_at_all_is_asked_again(tmp_path):
    book = dungeon(tmp_path, [CH1, CH2])
    model = Model(KINDS, {"Mordecai": ["A wizard who rules Valtay.",
                                       "A game guide who waits in the guild hall."]},
                  untrue=("wizard",))
    run(tmp_path, book, model, **BOOK)
    said = [r[6] for r in people_of(book) if r[3] == "Mordecai"]
    assert said and said[0] == "A game guide who waits in the guild hall."


def test_joining_or_leaving_needs_a_quote_that_shows_it():
    words = C.WORDS["en"]
    assert C.event_problems("A crawler who rejoined the party.", "Katia walked away",
                            "A crawler in Carl's party.", "left", words)
    assert not C.event_problems("A crawler who left the party.", "Katia left the party for good",
                                "A crawler in Carl's party.", "left", words)
    # said already: not news, no quote needed
    assert not C.event_problems("A crawler who left the party.", "-",
                                "A crawler who left the party.", "none", words)


def test_a_half_name_from_the_names_list_is_allowed():
    words = C.WORDS["en"]
    src = "Cici waved at Lucia. Cici sat down."
    entry = "A crawler who sits with Lucia Mar."
    assert C.problems(entry, "Cici waved at Lucia", src, ["Cici"], {}, words)
    assert C.problems(entry, "Cici waved at Lucia", src, ["Cici"], {}, words,
                      allowed={"Lucia", "Mar"}) == []


def test_one_bad_sentence_does_not_lose_the_whole_entry():
    words = C.WORDS["en"]
    src = "Cici waved at Carl. Cici sat down at the table with Donut."
    entry = "A crawler who sits with Donut at the table. She once ruled Valtay."
    reason = C.problems(entry, "Cici sat down", src, ["Cici"], {}, words)
    assert reason
    kept, left = C._repaired(entry, "Cici sat down", src, ["Cici"], {}, words, "", "", set(), reason)
    assert kept == "A crawler who sits with Donut at the table." and left == []


# -- the series facts list catches what the wiki says is wrong ----------------------

FACTS_TSV = (
    "# from the fan wiki: basic facts, each with the book it is first known in\n"
    "Florin\tspecies\tcrocodilian\t3\n"
    "Katia Grimaldi\tspecies\tdoppelganger\t2\n"
    "Katia Grimaldi\talias\tKatia\t2\n"
    "Carl\tspecies\thuman\t1\n"
    "Bea\tex-girlfriend\tCarl\t1\n"
    "Li Na\tsister\tLi Jun\t4\n"
    "Li Na\tsister\tZhang\t9\n"
    "Diwata\tnot\tCirce Took\t5\n"
    "Circe Took\tmother\tVrah\t5\n"
)


def _facts(tmp_path, upto=5):
    from epubkit import series_memory as S
    (tmp_path / "Dungeon Crawler Carl.facts.tsv").write_text(FACTS_TSV)
    return S.Facts.open(tmp_path, "Dungeon Crawler Carl", upto)


def test_the_facts_list_catches_a_wrong_species(tmp_path):
    facts, words = _facts(tmp_path), C.WORDS["en"]
    got = C.wiki_problems("A human crawler who travels with Carl.", "Florin", facts, words)
    assert got and "crocodilian" in got[0][1]
    got = C.wiki_problems("A human crawler, and Donut's friend.", "Katia", facts, words)
    assert got and "doppelganger" in got[0][1]
    # a species named later in the sentence is someone else's
    assert C.wiki_problems("A crocodilian who guards the human Carl.", "Florin",
                           facts, words) == []
    assert C.wiki_problems("A human crawler.", "Carl", facts, words) == []


def test_the_facts_list_catches_a_wrong_identity_and_a_wrong_tie(tmp_path):
    facts, words = _facts(tmp_path), C.WORDS["en"]
    got = C.wiki_problems("A queen of the Hive. She is actually Circe Took.", "Diwata",
                          facts, words)
    assert [x for x, _r in got] == ["She is actually Circe Took."]
    assert C.wiki_problems("A crawler, and the sister of Vrah.", "Circe Took", facts, words)


def test_the_facts_list_backs_a_tie_from_an_earlier_book(tmp_path):
    facts = _facts(tmp_path)
    claims = C.unbacked_relations("Carl's girlfriend, now a crawler.", "Bea waved at Carl.")
    assert claims and C.wiki_backed(claims, "Bea", facts) == []
    # book 9's tie is not known in book 5, so it backs nothing
    claims = C.unbacked_relations("The sister of Zhang.", "Li Na waved.")
    assert C.wiki_backed(claims, "Li Na", facts) == claims
    assert C.wiki_backed(claims, "Li Na", _facts(tmp_path, 9)) == []


def test_a_wrong_species_is_asked_again_and_never_written(tmp_path):
    (tmp_path / "s").mkdir()
    (tmp_path / "s" / "Dungeon Crawler Carl.facts.tsv").write_text(FACTS_TSV)
    book = dungeon(tmp_path, [CH1, CH2])
    _set_series(book, "Dungeon Crawler Carl")
    seen = []

    def second(prompt):
        seen.append(prompt)
        return "A crawler who smiled at Donut in the room."
    model = Model(KINDS, {"Katia": ["A human crawler who smiled at Donut.", second]})
    run(tmp_path, book, model, **BOOK, **{"recaps.series_dir": str(tmp_path / "s")})
    said = [r[6] for r in people_of(book) if r[3] == "Katia"]
    assert said and not any("human" in s for s in said)
    # the second question says what was wrong, but the entry comes from the model
    assert seen and "not human" in seen[0]


def test_known_mistakes_can_be_put_to_the_model_without_the_book(tmp_path):
    from epubkit.tools import cast_mistakes as M
    facts = _facts(tmp_path)
    cases = [{"name": "Katia", "extracts": ["Katia smiled at Donut in the room."],
              "must_not_say": "human", "should_say": ["Donut"]},
             {"name": "Mordecai", "extracts": ["Mordecai waited in the guild hall."],
              "must_not_say": ["killed"]}]
    model = Model(KINDS, {"Katia": ["A human crawler who smiled at Donut.",
                                    "A crawler who smiled at Donut in the room."],
                          "Mordecai": ["A game guide who waits in the guild hall."]})

    def ask(prompt, system, usable):
        return model.complete(prompt, system).text
    got, _r, _s = C.build(M._Ctx(), [], ask, facts=facts, probe=cases)
    assert [M.score(c, e)[0] for c, e in zip(cases, got)] == ["right", "right"]
    assert M.score(cases[0], "A human crawler.")[0] == "wrong"
    assert M.score(cases[0], None)[0] == "lost"


def test_the_facts_list_catches_a_hidden_identity_however_it_is_put(tmp_path):
    facts, words = _facts(tmp_path), C.WORDS["en"]
    for wrong in ("A hive queen. She is actually a mantis named Circe Took.",
                  "A crawler; she is revealed to be the human Circe Took.",
                  "A crawler who turns out to be Circe Took."):
        assert C.wiki_problems(wrong, "Diwata", facts, words), wrong
    for fine in ("A crawler whose sponsor is Circe Took.",
                 "A crawler. She is angry at Circe Took."):
        assert not C.wiki_problems(fine, "Diwata", facts, words), fine


def test_an_update_that_gives_back_the_entry_so_far_means_nothing_changed():
    from epubkit.tools import cast_mistakes as M
    old = "A crawler and Hekla's friend who has left the party."
    cases = [{"name": "Katia", "extracts": ["Katia walked out with Hekla's crew."],
              "current_entry": old}]
    model = Model(KINDS, {"Katia": [old]})

    def ask(prompt, system, usable):
        reply = model.complete(prompt, system).text
        return reply.replace("QUOTE: ", "QUOTE: made up words ")   # a quote not in the extracts
    got, _r, _s = C.build(M._Ctx(), [], ask, probe=cases)
    assert got == [""]


def test_a_sentence_about_the_narrator_is_dropped_not_the_whole_entry():
    from epubkit.tools import cast_mistakes as M
    cases = [{"name": "Mordecai", "extracts": ["Mordecai waited in the guild hall with a potion."]}]
    model = Model(KINDS, {"Mordecai": ["A game guide who waits in the guild hall. "
                                       "The narrator visits him."]})

    def ask(prompt, system, usable):
        return model.complete(prompt, system).text
    got, _r, _s = C.build(M._Ctx(), [], ask, probe=cases)
    assert got == ["A game guide who waits in the guild hall."]


def test_nothing_changed_will_not_do_when_the_entry_so_far_gets_a_fact_wrong(tmp_path):
    from epubkit.tools import cast_mistakes as M
    cases = [{"name": "Katia", "extracts": ["Katia smiled at Donut in the room."],
              "current_entry": "A human crawler who smiled at Donut."}]
    seen = []

    def fixed(prompt):
        seen.append(prompt)
        return "A crawler who smiled at Donut in the room."
    model = Model(KINDS, {"Katia": ["SAME", fixed]})

    def ask(prompt, system, usable):
        return model.complete(prompt, system).text
    got, _r, _s = C.build(M._Ctx(), [], ask, facts=_facts(tmp_path), probe=cases)
    assert got == ["A crawler who smiled at Donut in the room."]
    assert "not human" in seen[0]


def test_a_wrong_species_is_caught_in_an_entry_of_several_sentences(tmp_path):
    facts, words = _facts(tmp_path), C.WORDS["en"]
    assert C.wiki_problems("A human Crawler who joined. She left the party.", "Katia",
                           facts, words)


def test_a_wrong_identity_is_cut_off_and_the_rest_kept():
    assert C._before_identity("A deity associated with a desecrated temple who is "
                              "actually a baby mantis named Circe Took.") == \
        "A deity associated with a desecrated temple."
    assert C._before_identity("She is Circe Took.") == ""


def test_an_empty_quote_line_never_takes_the_next_line():
    assert C.parse_entry("CHANGE: other\nQUOTE:\nSTATUS: met\nENTRY: A doppelganger.") == \
        ("", "met", "A doppelganger.")


def test_a_sentence_kept_from_the_entry_so_far_is_not_checked_again():
    from epubkit.tools import cast_mistakes as M
    old = "A crawler and Hekla's friend. She has left the party."
    cases = [{"name": "Katia", "extracts": ["Katia smiled at Donut in the room."],
              "current_entry": old}]
    model = Model(KINDS, {"Katia": ["A crawler who smiled at Donut in the room. "
                                    "She has left the party."]}, untrue=("left the party",))

    def ask(prompt, system, usable):
        return model.complete(prompt, system).text
    got, _r, _s = C.build(M._Ctx(), [], ask, probe=cases)
    assert got and "left the party" in got[0]
