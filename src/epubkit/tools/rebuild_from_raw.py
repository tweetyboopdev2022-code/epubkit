#!/usr/bin/env python3
"""Repair the recap data in already-generated books, without the model.

Two defects need no regeneration because the raw model output is intact and
correct in both cases: front matter that was summarised as a chapter, and cast
entries where a titled name was merged into a full name. Everything here comes
out of raw/, so this costs seconds a book rather than minutes.

Usage: rebuild_from_raw.py [--apply]   (default is a dry run)
"""
import glob
import os
import re
import shutil
import subprocess
import sys
import tempfile

BASE = os.path.expanduser("~/sites/mods/recapmod_build")
sys.path.insert(0, BASE)
import dedupe_cast
import gen_recaps

OUT = os.path.join(BASE, "out")
RAW = os.path.join(BASE, "raw")


def stem_for(book):
    return re.sub(r"[^A-Za-z0-9]+", "_", os.path.basename(book))[:60]


def main():
    apply_it = "--apply" in sys.argv
    books = sorted(glob.glob(os.path.join(OUT, "**", "*.epub"), recursive=True))
    changed = dropped_total = merged_total = 0

    for book in books:
        stem = stem_for(book)
        r_raw = os.path.join(RAW, stem + ".recaps.tsv")
        c_raw = os.path.join(RAW, stem + ".cast.tsv")
        if not (os.path.isfile(r_raw) and os.path.isfile(c_raw)):
            print("no raw data, skipped: %s" % os.path.basename(book)[:60])
            continue

        recaps = dedupe_cast.read_rows(r_raw)
        keep = [r for r in recaps if not gen_recaps.is_front_matter(r[2], r[3])]
        dropped = len(recaps) - len(keep)
        bad_orders = {r[0] for r in recaps} - {r[0] for r in keep}

        raw_cast = [r for r in dedupe_cast.read_rows(c_raw) if r[0] not in bad_orders]
        cast = dedupe_cast.collapse(raw_cast)

        old_people = set()
        try:
            cur = subprocess.run(["unzip", "-p", book, "EPUB/recaps/cast.tsv"],
                                 capture_output=True, text=True, timeout=60).stdout
            old_people = {l.split("\t")[2] for l in cur.splitlines() if l.count("\t") >= 3}
        except Exception:
            pass
        new_people = {r[2] for r in cast}
        merged = len(new_people - old_people)

        if not dropped and not merged:
            continue

        changed += 1
        dropped_total += dropped
        merged_total += merged
        print("%-52s front-matter -%d  names +%d"
              % (os.path.basename(book)[:52], dropped, merged))

        if apply_it:
            w = tempfile.mkdtemp()
            d = os.path.join(w, "EPUB", "recaps")
            os.makedirs(d)
            dedupe_cast.write_rows(os.path.join(d, "recaps.tsv"), keep)
            dedupe_cast.write_rows(os.path.join(d, "cast.tsv"), cast)
            subprocess.run(["zip", "-q", os.path.abspath(book),
                            "EPUB/recaps/recaps.tsv", "EPUB/recaps/cast.tsv"],
                           cwd=w, check=True)
            shutil.rmtree(w)

    print()
    print("%d of %d books need repair: %d front-matter entries removed, "
          "%d people un-merged" % (changed, len(books), dropped_total, merged_total))
    if not apply_it:
        print("dry run. re-run with --apply to write.")


main()
