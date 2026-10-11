import zipfile
from pathlib import Path

from epubkit.shelves import (ADULT, KIDS, TEENS, assess, classify_library, decide,
                             json_from, lookup, section)

OPF = """<?xml version="1.0"?><package xmlns="http://www.idpf.org/2007/opf" version="3.0">
<metadata xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:title>%s</dc:title>
<dc:creator>%s</dc:creator>%s</metadata></package>"""


def make(path: Path, title, author, subjects=(), words=800, sentence="The dog ran. It sat. "):
    subs = "".join("<dc:subject>%s</dc:subject>" % s for s in subjects)
    body = "<html><body><p>%s</p></body></html>" % (sentence * (words // 5))
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("content.opf", OPF % (title, author, subs))
        z.writestr("c1.xhtml", body)
    return path


class Reply:
    def __init__(self, t): self.text = t


class Model:
    def __init__(self, t): self.t = t; self.prompts = []
    def complete(self, prompt, system=None): self.prompts.append(prompt); return Reply(self.t)


NOWEB = lambda url: {}                                        # noqa: E731
HARD = "Notwithstanding considerable institutional reservations, the committee's deliberations proceeded. "
EMPTY = {"subjects": [], "maturity": None, "sources": []}


def stats(words, grade): return {"words": words, "grade": grade, "sample": "x"}


def book(**kw):
    b = dict(title="Into the Wild", authors=["Doreen Cronin"], series="Chicken Squad",
             subjects=[], description=None, isbn=None)
    b.update(kw)
    return b


def test_explicit_juvenile_subject_decides_without_a_model():
    s = decide(book(subjects=["Juvenile Fiction"]), EMPTY, stats(20000, 3.0), Model('{"library":"Adult"}'))
    assert s.library == KIDS and s.confidence == "high"


def test_a_catalogue_category_counts_like_the_books_own_subject():
    s = decide(book(), dict(EMPTY, subjects=["Juvenile Fiction"], sources=["Open Library"]),
               stats(20000, 3.0), None)
    assert s.library == KIDS and "a catalogue" in s.reasons[0]


def test_model_alone_is_medium_and_a_model_calling_a_short_easy_book_adult_is_overruled():
    m = Model('{"library":"Adult","folder":"Fiction","confidence":"high","reason":"x"}')
    s = decide(book(), EMPTY, stats(15000, 2.5), m)
    assert s.library == KIDS and s.confidence == "low" and "check" in s.reasons[0]


def test_model_calling_a_long_dense_book_kids_is_overruled_to_adult():
    m = Model('{"library":"Kids","confidence":"high","reason":"x"}')
    s = decide(book(title="Big"), EMPTY, stats(150000, 11.0), m)
    assert s.library == ADULT and s.confidence == "low"


def test_model_and_text_agreeing_reach_high():
    m = Model('{"library":"Kids","folder":"Fiction","confidence":"high","reason":"early reader"}')
    s = decide(book(), EMPTY, stats(9000, 2.0), m)
    assert s.library == KIDS and s.confidence == "high"


def test_the_model_is_shown_the_evidence():
    m = Model('{"library":"Kids","confidence":"medium","reason":"x"}')
    decide(book(), dict(EMPTY, subjects=["Chickens"], sources=["Google Books"]), stats(9000, 2.0), m)
    p = m.prompts[0]
    assert "Chicken Squad" in p and "Chickens" in p and "9000 words" in p and "reading level: 2.0" in p


def test_no_model_short_easy_text_is_a_low_kids_and_ordinary_text_is_adult():
    assert decide(book(), EMPTY, stats(9000, 2.0), None).library == KIDS
    s = decide(book(), EMPTY, stats(70000, 8.0), None)
    assert s.library == ADULT and s.confidence == "low"


def test_google_books_mature_rating_is_adult_high():
    s = decide(book(), dict(EMPTY, maturity="MATURE"), stats(9000, 2.0), None)
    assert s.library == ADULT and s.confidence == "high"


def test_lookup_only_trusts_a_result_with_the_same_title():
    def fetch(url):
        if "googleapis" in url:
            return {"items": [{"volumeInfo": {"title": "Something Else", "categories": ["Horror"]}},
                              {"volumeInfo": {"title": "Into the Wild", "categories": ["Juvenile Fiction"]}}]}
        return {"docs": [{"title": "Unrelated", "subject": ["War"]}]}
    ev = lookup(book(), fetch)
    assert ev["subjects"] == ["Juvenile Fiction"] and ev["sources"] == ["Google Books"]


def test_a_dead_network_is_just_no_evidence():
    def boom(url): raise OSError("offline")
    assert lookup(book(), boom)["subjects"] == []


def test_assess_reads_a_real_file_and_measures_it(tmp_path):
    p = make(tmp_path / "a.epub", "Cat Tales", "A. Writer", words=3000)
    b, s = assess(p, None, False)
    assert b["title"] == "Cat Tales" and s.library == KIDS


def test_classify_library_caches_and_refreshes(tmp_path):
    make(tmp_path / "a.epub", "Cat Tales", "A. Writer", ["Juvenile Fiction"])
    make(tmp_path / "b.epub", "Night Shift", "B. Author", ["Thrillers"], words=100000, sentence=HARD)
    cache = tmp_path / ".w" / "c.json"
    rows = classify_library(tmp_path, None, cache, False)
    assert {r["name"]: r["shelf"].split(" ›")[0] for r in rows} == {"a.epub": KIDS, "b.epub": ADULT}
    m = Model('{"library":"Teens"}')
    calls = []
    import epubkit.shelves as sh
    orig = sh.assess
    sh.assess = lambda *a, **k: calls.append(1) or orig(*a, **k)
    try:
        classify_library(tmp_path, None, cache, False)
        assert not calls                                   # cached
        classify_library(tmp_path, None, cache, False, refresh=True)
        assert len(calls) == 2
    finally:
        sh.assess = orig
    assert m


def test_section_groups_and_flags():
    rows = [dict(name="a", title="Cat Tales", authors="A", shelf="Kids", confidence="high", why="the book says: picture books"),
            dict(name="b", title="Maybe", authors="", shelf="Kids", confidence="medium", why="model: x"),
            dict(name="c", title="Night", authors="B", shelf="Adult › Fiction", confidence="medium", why="text")]
    t = "\n".join(section(rows))
    assert t.index("**Kids** (2)") < t.index("**Adult** (1)")
    assert "Cat Tales — A (the book says: picture books)\n" in t + "\n"
    assert "- Maybe (model: x) *check*" in t and "Fiction" in t
    assert section([]) == []


def test_json_is_found_inside_chatty_replies_and_junk_is_empty():
    assert json_from('here: {"a": 1} ok') == {"a": 1}
    assert json_from("nothing") == {} and json_from("{broken") == {}


def test_a_series_shares_one_shelf_and_the_odd_one_out_is_flagged():
    from epubkit.shelves import series_consensus
    mk = lambda n, lib, c: dict(name=n, title=n, authors="", series="ASOUE", shelf=lib,  # noqa: E731
                                confidence=c, why="w")
    rows = series_consensus([mk("1", "Kids", "medium"), mk("2", "Kids", "low"), mk("3", "Adult", "low"),
                             dict(mk("x", "Adult", "high"), series="")])
    assert [r["shelf"] for r in rows] == ["Kids", "Kids", "Kids", "Adult"]
    assert rows[2]["confidence"] == "low" and "rest of ASOUE" in rows[2]["why"]


def test_a_model_guessing_teens_from_reading_level_alone_stays_adult():
    m = Model('{"library":"Teens","folder":"Fiction","confidence":"medium",'
              '"reason":"The reading level and novella format suggest a young adult audience."}')
    s = decide(book(title="All Systems Red", series="The Murderbot Diaries"), EMPTY, stats(31000, 6.5), m)
    assert s.library == ADULT and s.confidence == "low" and "check" in s.reasons[0]


def test_a_ya_label_still_makes_teens_without_the_model():
    s = decide(book(subjects=["Young Adult Fiction"]), EMPTY, stats(90000, 7.0), None)
    assert s.library == TEENS


def test_a_sure_model_may_name_teens_but_only_at_medium():
    m = Model('{"library":"Teens","confidence":"high","reason":"known YA series"}')
    s = decide(book(), EMPTY, stats(100000, 7.0), m)
    assert s.library == TEENS and s.confidence == "medium"


def test_overrides_set_a_whole_series_by_hand(tmp_path):
    import json as _json
    from epubkit.shelves import apply_overrides, load_overrides
    (tmp_path / "epubkit-overrides.json").write_text(_json.dumps(
        {"The Murderbot Diaries": {"library": "Adult"}, "x.epub": {"title": "no library here"}}))
    rows = [dict(name="a.epub", title="Rogue Protocol", authors="Martha Wells",
                 series="The Murderbot Diaries", shelf="Teens › Fiction", confidence="medium", why="m")]
    out = apply_overrides(rows, load_overrides(tmp_path))
    assert out[0]["shelf"] == "Adult › Fiction" and out[0]["confidence"] == "high"
    assert "by hand" in out[0]["why"]


def test_a_series_of_three_is_one_line_in_the_report():
    rows = [dict(name=str(i), title="Book %d" % i, authors="Martha Wells", series="The Murderbot Diaries",
                 shelf="Adult › Fiction", confidence="medium", why="w") for i in range(3)]
    t = "\n".join(section(rows))
    assert "- The Murderbot Diaries, 3 books — Martha Wells (w)" in t and "Book 1" not in t
