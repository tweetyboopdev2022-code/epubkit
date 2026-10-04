from epubkit.stages.split import derive_titles, fill_plan


def test_labels_that_spell_out_series_number_and_title_are_taken_apart():
    series, parts = derive_titles(["A Series of Unfortunate Events #1: The Bad Beginning",
                                   "A Series of Unfortunate Events #2: The Reptile Room"])
    assert series == "A Series of Unfortunate Events"
    assert parts == [("1", "The Bad Beginning"), ("2", "The Reptile Room")]
    assert derive_titles(["Book 1: Dawn", "Book 2: Dusk"])[0] == "Book"


def test_plain_titles_get_no_invented_series():
    assert derive_titles(["The Fellowship of the Ring", "The Two Towers"]) == (
        "", [("", "The Fellowship of the Ring"), ("", "The Two Towers")])


def test_labels_that_disagree_or_do_not_rise_are_not_trusted():
    assert derive_titles(["Saga #2: Two", "Saga #1: One"])[0] == ""
    assert derive_titles(["Saga #1: One", "Other #2: Two"])[0] == ""


def test_the_plan_gets_titles_author_series_and_full_file_names():
    data = {"books": [{"range": [1, 2], "out": "x.epub", "metadata": {"title": t}}
                      for t in ("Chicken Squad #1: The First Misadventure",
                                "Chicken Squad #2: The Case of the Weird Blue Chicken")]}
    fill_plan(data, "Doreen Cronin")
    b = data["books"][1]
    assert b["out"] == "Chicken Squad 02 - The Case of the Weird Blue Chicken - Doreen Cronin.epub"
    md = b["metadata"]
    assert (md["series"], md["series_index"], md["authors"]) == (
        "Chicken Squad", "2", [["Doreen Cronin", ""]])
