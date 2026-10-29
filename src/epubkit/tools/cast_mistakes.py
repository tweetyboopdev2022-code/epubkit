"""Put known who's-who mistakes to the model again, quickly.

Each case is one question the whole-book who's who once got wrong (from a
pilot run, checked against an answer key): who it was about, the extracts
the model saw, the entry it had so far, and the words a right answer must
not and should say. The cases go through the same question and checks as a
real book (cast.build), without reading the book, so a change to the
prompts or checks can be tried in minutes instead of hours.

    python -m epubkit.tools.cast_mistakes dcc5-mistakes.json \\
        --series "Dungeon Crawler Carl" --book 5 [--runs 3] [--config FILE]

The cases file is a JSON list (or {"cases": [...]}) of objects with name,
extracts, and optionally current_entry, mentions, must_not_say, should_say
(a string or a list of strings each). The series' memory, names list and
facts list are read from the usual series folder, as for a real run.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from .. import series_memory
from ..ai import build as build_provider
from ..config import Config
from ..stages import cast

log = logging.getLogger("epubkit.cast_mistakes")


class _Ctx:
    log = log

    def report(self, _text):
        pass

    def note(self, _text):
        pass

    def tell(self, _text):
        pass


def _phrases(v) -> list[str]:
    if not v:
        return []
    return [v] if isinstance(v, str) else [str(x) for x in v]


def score(case: dict, entry) -> tuple[str, list[str], list[str]]:
    """(verdict, wrong phrases said, wanted phrases missing)."""
    if entry is None:
        return "lost", [], _phrases(case.get("should_say"))
    if entry == "":
        return "same", [], []
    low = entry.lower()
    wrong = [w for w in _phrases(case.get("must_not_say")) if w.lower() in low]
    missing = [w for w in _phrases(case.get("should_say")) if w.lower() not in low]
    return ("wrong" if wrong else "right"), wrong, missing


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("cases")
    ap.add_argument("--series", required=True)
    ap.add_argument("--book", type=float, required=True)
    ap.add_argument("--title", default="")
    ap.add_argument("--runs", type=int, default=1)
    ap.add_argument("--only", action="append", default=[], help="just these names")
    ap.add_argument("--config")
    ap.add_argument("--series-dir")
    ap.add_argument("--lang", default="en")
    ap.add_argument("--json", help="also write the results here")
    args = ap.parse_args(argv)

    raw = json.loads(Path(args.cases).read_text(encoding="utf-8"))
    cases = raw.get("cases", raw) if isinstance(raw, dict) else raw
    if args.only:
        cases = [c for c in cases if c["name"] in args.only]
    cfg = Config.load(Path(args.config) if args.config else None)
    provider = build_provider(cfg.ai)
    folder = args.series_dir or getattr(cfg.recaps, "series_dir", "") or None
    view = series_memory.Memory.open(folder, args.series).before(args.book)
    names_list = series_memory.Names.open(folder, args.series, args.book)
    facts = series_memory.Facts.open(folder, args.series, args.book)
    print("%d cases; memory %d people, names list %d, facts list %d people"
          % (len(cases), len(view.people), len(names_list.owner), len(facts.owner)))

    try:
        provider.complete("Say OK.", None, retries=1)
    except Exception as exc:                                # noqa: BLE001
        print("the model can't be reached: %s" % exc)
        if not cfg.ai.api_key():
            print("(no API key set: export OPENAI_API_KEY first)")
        return 2

    def ask(prompt, system, usable):
        text = ""
        for _ in range(2):
            text = provider.complete(prompt, system).text
            if usable(text):
                break
        return text

    results = []
    tally: dict[str, int] = {}
    for run in range(args.runs):
        entries, report, _ = cast.build(
            _Ctx(), [], ask, lang=args.lang, view=view, title=args.title or args.series,
            names_list=names_list, facts=facts, probe=cases)
        for case, entry in zip(cases, entries):
            verdict, wrong, missing = score(case, entry)
            tally[verdict] = tally.get(verdict, 0) + 1
            results.append(dict(run=run + 1, name=case["name"], kind=case.get("kind", ""),
                                verdict=verdict, entry=entry, wrong=wrong, missing=missing))
            print("%-6s %-14s %s" % (verdict.upper(), case["name"], entry))
            if wrong:
                print("       still says: %s" % "; ".join(wrong))
            if missing:
                print("       missing: %s" % "; ".join(missing))
        print("\nwhat the checks turned down, run %d:" % (run + 1))
        for line in report:
            print("  " + line)
        print()
    print("\n" + ", ".join("%d %s" % (n, v) for v, n in sorted(tally.items())))
    if args.json:
        Path(args.json).write_text(json.dumps(results, indent=1, ensure_ascii=False),
                                   encoding="utf-8")
    return 0 if not tally.get("wrong") else 1


if __name__ == "__main__":
    sys.exit(main())
