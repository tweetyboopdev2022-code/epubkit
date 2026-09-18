"""The kobokit command."""
from __future__ import annotations

import argparse
import logging
import shutil
import sys
from pathlib import Path

from . import __version__
from .config import ALL_STAGES, CONFIG_NAME, Config, ConfigError

EXAMPLE_CONFIG = """\
# kobokit.toml - copy this next to your library, or to
# ~/.config/kobokit/kobokit.toml, and edit.
#
# API keys are NOT read from this file. Export them instead:
#   export ANTHROPIC_API_KEY=...
#   export OPENAI_API_KEY=...
# A key written here is rejected, because a config file gets committed and an
# environment variable does not.

[paths]
source = "~/Downloads/books"
output = "~/Books/kobo"
# work   = ""   # defaults to <output>/.kobokit-work
# backup = ""   # defaults to <output>/.kobokit-originals, holds the originals

[ai]
provider = "ollama"              # ollama | anthropic | openai
model    = "llama3.1:latest"
endpoint = "http://localhost:11434"
timeout  = 180
concurrency = 2                  # chapters summarised at once

[metadata]
sources = ["openlibrary", "googlebooks"]
use_ai_fallback = true           # model only fills what catalogues could not
overwrite_existing = false       # never overwrite what the book already states
# Full series name and full title, never abbreviated, however long it runs.
filename_template   = "{series} {index:02d} - {title} - {authors}"
standalone_template = "{title} - {authors}"

[recaps]
enabled = true
min_chars = 2000                 # shorter than this is front matter
lag_chapters = 1                 # never recap the chapter the reader is inside
cast = true

[validate]
require_clean = true             # a book with errors is reported, not shipped
accept_improvement = false       # ...unless it is better than what it replaced

[run]
stages = ["ingest", "convert", "metadata", "repair", "validate", "recaps", "report"]
workers = 3
continue_on_error = true
resume = true
"""


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="kobokit",
        description="Take a folder of downloaded books to a clean Kobo library.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""examples:
  kobokit init                        write a starter kobokit.toml
  kobokit check                       show the config and what is missing
  kobokit run --in ~/Downloads/books --out ~/Books/kobo
  kobokit run --no-recaps --workers 4
  kobokit run --only validate         re-validate an existing library
  kobokit recaps BOOK.epub            recaps for one book
""")
    p.add_argument("--version", action="version", version="kobokit " + __version__)
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("init", help="write a starter kobokit.toml here")

    for name, helptext in (("check", "validate the configuration and exit"),
                           ("run", "process a folder of books")):
        s = sub.add_parser(name, help=helptext)
        s.add_argument("-c", "--config", type=Path, help="path to kobokit.toml")
        s.add_argument("--in", dest="source", type=Path, help="source folder")
        s.add_argument("--out", dest="output", type=Path, help="library folder")
        s.add_argument("--provider", choices=("ollama", "anthropic", "openai"))
        s.add_argument("--model")
        s.add_argument("--endpoint")
        if name == "run":
            s.add_argument("--workers", type=int)
            s.add_argument("--only", nargs="+", metavar="STAGE",
                           choices=ALL_STAGES,
                           help="run only these stages (%s)" % ", ".join(ALL_STAGES))
            s.add_argument("--skip", nargs="+", metavar="STAGE", choices=ALL_STAGES)
            s.add_argument("--no-recaps", action="store_true")
            s.add_argument("--no-resume", action="store_true",
                           help="reprocess books already recorded as done")
            s.add_argument("--allow-imperfect", action="store_true",
                           help="write a book that improved but is not clean")
            s.add_argument("-n", "--dry-run", action="store_true")
            s.add_argument("-v", "--verbose", action="store_true")

    r = sub.add_parser("recaps", help="generate recaps for a single book")
    r.add_argument("book", type=Path)
    r.add_argument("-c", "--config", type=Path)
    r.add_argument("--provider", choices=("ollama", "anthropic", "openai"))
    r.add_argument("--model")
    r.add_argument("--endpoint")

    v = sub.add_parser("validate", help="EPUBCheck a folder or a single book")
    v.add_argument("target", type=Path)
    v.add_argument("--json", dest="json_out", type=Path)

    return p


def _overrides(args) -> dict:
    o: dict = {}
    for flag, key in (("source", "source"), ("output", "output")):
        if getattr(args, flag, None):
            o[key] = getattr(args, flag)
    for flag, key in (("provider", "ai.provider"), ("model", "ai.model"),
                      ("endpoint", "ai.endpoint")):
        if getattr(args, flag, None):
            o[key] = getattr(args, flag)
    if getattr(args, "workers", None):
        o["run.workers"] = args.workers
    if getattr(args, "dry_run", False):
        o["run.dry_run"] = True
    if getattr(args, "verbose", False):
        o["run.verbose"] = True
    if getattr(args, "no_resume", False):
        o["run.resume"] = False
    if getattr(args, "no_recaps", False):
        o["recaps.enabled"] = False
    if getattr(args, "allow_imperfect", False):
        o["validate.accept_improvement"] = True
    stages = list(ALL_STAGES)
    if getattr(args, "only", None):
        stages = [s for s in ALL_STAGES if s in args.only]
    if getattr(args, "skip", None):
        stages = [s for s in stages if s not in args.skip]
    if getattr(args, "only", None) or getattr(args, "skip", None):
        o["run.stages"] = tuple(stages)
    return o


def _logger(verbose: bool) -> logging.Logger:
    log = logging.getLogger("kobokit")
    log.setLevel(logging.DEBUG if verbose else logging.INFO)
    if not log.handlers:
        h = logging.StreamHandler(sys.stderr)
        h.setFormatter(logging.Formatter("%(message)s"))
        log.addHandler(h)
    return log


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.command == "init":
        target = Path.cwd() / CONFIG_NAME
        if target.exists():
            print("%s already exists; not overwriting it" % target)
            return 1
        target.write_text(EXAMPLE_CONFIG)
        print("wrote %s" % target)
        print("Edit paths.source and paths.output, then: kobokit check")
        return 0

    if args.command == "validate":
        return _cmd_validate(args)

    try:
        cfg = Config.load(getattr(args, "config", None), _overrides(args))
    except ConfigError as exc:
        print("configuration problem: %s" % exc, file=sys.stderr)
        return 2

    log = _logger(cfg.run.verbose)

    if args.command == "check":
        print(cfg.describe())
        problems = cfg.check(need_paths=False)
        if cfg.paths.source or cfg.paths.output:
            problems = cfg.check(need_paths=True)
        print()
        if problems:
            print("problems:")
            for p in problems:
                print("  - %s" % p)
            return 1
        print("configuration looks usable")
        if cfg._ai_needed():
            from .ai import build
            print("checking the model endpoint...")
            report = build(cfg.ai).health()
            print("  %s" % report)
            if not report.startswith("ok"):
                # Better to find this out now than four hours into a run.
                print("\nRecaps and AI metadata will fail until this is fixed. "
                      "Run with --no-recaps to proceed without a model.")
                return 1
        return 0

    if args.command == "recaps":
        return _cmd_recaps(args, cfg, log)

    return _cmd_run(cfg, log)


def _cmd_run(cfg: Config, log) -> int:
    problems = cfg.check()
    if problems:
        print("cannot start:", file=sys.stderr)
        for p in problems:
            print("  - %s" % p, file=sys.stderr)
        return 2

    provider = None
    if cfg._ai_needed():
        from .ai import build
        provider = build(cfg.ai)

    from .pipeline import Pipeline
    state = Pipeline(cfg, log, provider).run()

    counts = state.counts()
    log.info("")
    log.info("done: %s", ", ".join("%d %s" % (v, k)
                                   for k, v in sorted(counts.items())) or "nothing")
    failed = state.failed()
    if failed:
        log.info("")
        log.info("%d book(s) were not written:", len(failed))
        for rec in failed[:20]:
            log.info("  %-54s %s", Path(rec.source).name[:54], rec.message[:60])
    return 1 if failed else 0


def _cmd_recaps(args, cfg: Config, log) -> int:
    book = args.book.expanduser().resolve()
    if not book.is_file():
        print("no such book: %s" % book, file=sys.stderr)
        return 2
    from .ai import build
    from .stages.base import Context
    from .stages.recaps import RecapStage
    from .state import BookRecord

    provider = build(cfg.ai)
    work = book.parent / ".kobokit-work"
    work.mkdir(exist_ok=True)
    rec = BookRecord(source=str(book))
    ctx = Context(config=cfg, record=rec, path=book, work=work, log=log,
                  provider=provider)
    result = RecapStage().run(ctx)
    for n in rec.notes:
        log.info("%s", n)
    shutil.rmtree(work, ignore_errors=True)
    return 0 if result.ok else 1


def _cmd_validate(args) -> int:
    from .tools import epubcheck_util as ec
    target = args.target.expanduser().resolve()
    if not ec.available():
        print("EPUBCheck is unavailable. Needs a JVM and the epubcheck "
              "package: pip install epubcheck", file=sys.stderr)
        return 2
    books = ([target] if target.is_file()
             else sorted(p for p in target.rglob("*.epub")
                         if not p.name.startswith("._")))
    if not books:
        print("no books found at %s" % target, file=sys.stderr)
        return 1
    results, bad = {}, 0
    for p in books:
        res = ec.run(str(p))
        errs = [e for e in (res or []) if e["severity"] in ("FATAL", "ERROR")]
        results[str(p)] = res
        if errs:
            bad += 1
        print("%-66s %s" % (p.name[:66], ec.summarize(res)))
    print("\n%d checked, %d clean, %d with errors"
          % (len(books), len(books) - bad, bad))
    if args.json_out:
        import json
        args.json_out.write_text(json.dumps(results, indent=1))
        print("details written to %s" % args.json_out)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
