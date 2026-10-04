"""The epubkit command."""
from __future__ import annotations

import argparse
import logging
import shutil
import sys
from pathlib import Path

from . import __version__
from .config import ALL_STAGES, CONFIG_NAME, Config, ConfigError

EXAMPLE_CONFIG = """\
# epubkit.toml - copy this next to your library, or to
# ~/.config/epubkit/epubkit.toml, and edit.
#
# API keys are NOT read from this file. Export them instead:
#   export ANTHROPIC_API_KEY=...
#   export OPENAI_API_KEY=...
# A key written here is rejected, because a config file gets committed and an
# environment variable does not.

[paths]
source = "~/Downloads/books"
output = "~/Books/kobo"
# work   = ""   # defaults to <output>/.epubkit-work
# backup = ""   # defaults to <output>/.epubkit-originals, holds the originals

[ai]
provider = "ollama"              # ollama | anthropic | openai
model    = "llama3.1:latest"
endpoint = "http://localhost:11434"
timeout  = 180
concurrency = 2                  # chapters summarised at once

[metadata]
sources = ["openlibrary", "googlebooks"]
use_ai_fallback = true           # model only fills what catalogues could not
suggest_shelves = true          # last step: Kids / Teens / Adult for every book, in REPORT.md
shelf_lookup = true             # ...checked against Open Library / Google Books
overwrite_existing = false       # never overwrite what the book already states
# Full series name and full title, never abbreviated, however long it runs.
filename_template   = "{series} {index:02d} - {title} - {authors}"
standalone_template = "{title} - {authors}"

[convert]
# A landscape PDF page is nearly always a two-page spread photographed whole.
# "auto" splits it into two pages when the pages are wide enough to be one;
# "always" and "never" override that. Either way a page whose artwork runs
# across the gutter is left whole.
split_spreads = "auto"           # auto | always | never | force
                                 # "force" cuts every wide page down the
                                 # middle. Use it for a picture book whose
                                 # illustrations all run across the spread,
                                 # where the gutter check keeps every page
                                 # whole and nothing gets split.
kepub   = true                   # wrap text in koboSpans: page numbers,
                                 # statistics and a position the Kobo keeps
slim_above_mb = 25.0             # recompress books bigger than this; 0 = off
detect_omnibus = true            # say when a file holds several books
split_omnibus  = "auto"          # auto | report | off: split box sets first, then run every book
make_missing_covers = true       # plain cover when there is no art at all
panel   = "1072x1448"            # device screen, for fixed-layout output
jpeg_quality = 88

[recaps]
enabled = true
min_chars = 2000                 # shorter than this is front matter
passage_words = 1500             # recap in passages this long, so it reaches your page
cast = true

[validate]
require_clean = true             # a book with errors is reported, not shipped
accept_improvement = false       # ...unless it is better than what it replaced

[run]
stages = ["ingest", "convert", "split", "metadata", "repair", "validate", "recaps", "report", "shelves"]
workers = 3
continue_on_error = true
resume = true
"""


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="epubkit",
        description="Take a folder of downloaded books to a clean Kobo library.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""examples:
  epubkit init                        write a starter epubkit.toml
  epubkit check                       show the config and what is missing
  epubkit run --in ~/Downloads/books --out ~/Books/kobo
  epubkit run --no-recaps --workers 4
  epubkit run --only validate         re-validate an existing library
  epubkit recaps BOOK.epub            recaps for one book
""")
    p.add_argument("--version", action="version", version="epubkit " + __version__)
    sub = p.add_subparsers(dest="command", required=True)

    i = sub.add_parser("init", help="set up epubkit, answering a few questions")
    i.add_argument("--defaults", action="store_true",
                   help="write the example config without asking anything")
    i.add_argument("-c", "--config", type=Path, dest="config",
                   help="write somewhere other than ./epubkit.toml")

    for name, helptext in (("check", "validate the configuration and exit"),
                           ("run", "process a folder of books")):
        s = sub.add_parser(name, help=helptext)
        s.add_argument("-c", "--config", type=Path, help="path to epubkit.toml")
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
            s.add_argument("--plain", action="store_true",
                           help="no live progress display, just one line per book")
            s.add_argument("--no-resume", action="store_true",
                           help="reprocess books already recorded as done")
            s.add_argument("--allow-imperfect", action="store_true",
                           help="write a book that improved but is not clean")
            s.add_argument("-n", "--dry-run", action="store_true")
            s.add_argument("-v", "--verbose", action="store_true")

    r = sub.add_parser("recaps", help="add recaps to books or folders of books, in place")
    r.add_argument("books", type=Path, nargs="+", metavar="BOOK_OR_FOLDER")
    r.add_argument("--audit", action="store_true",
                   help="only report what recap data each book carries; needs no model")
    r.add_argument("--force", action="store_true",
                   help="redo books whose recaps are already current")
    r.add_argument("--limit", type=int, default=0,
                   help="stop after this many books (0: no limit)")
    r.add_argument("-c", "--config", type=Path)
    r.add_argument("--provider", choices=("ollama", "anthropic", "openai"))
    r.add_argument("--model")
    r.add_argument("--endpoint")

    sp = sub.add_parser("split", help="split an omnibus into one file per book")
    sp.add_argument("book", type=Path)
    sp.add_argument("--out", type=Path, help="where the parts go "
                                             "(default: beside the omnibus)")
    sp.add_argument("--plan", type=Path,
                    help="a plan to use instead of working the boundaries out")
    sp.add_argument("--apply", action="store_true",
                    help="actually write the parts; without it, only the plan")

    cf = sub.add_parser("config", help="show or change one setting")
    cf.add_argument("action", choices=("get", "set", "list"))
    cf.add_argument("key", nargs="?", help="e.g. ai.model")
    cf.add_argument("value", nargs="?")
    cf.add_argument("-c", "--config", type=Path)

    d = sub.add_parser("doctor", help="check the optional pieces are installed")
    d.add_argument("--fix", action="store_true",
                   help="install what is missing into epubkit's own environment")
    d.add_argument("-y", "--yes", action="store_true",
                   help="do not ask before installing")

    sh = sub.add_parser("shelves", help="re-suggest Kids / Teens / Adult for finished books")
    sh.add_argument("library", type=Path, nargs="?",
                    help="the folder of finished books (default: the output folder)")
    sh.add_argument("-c", "--config", type=Path)
    sh.add_argument("--offline", action="store_true",
                    help="do not look the books up online")
    sh.add_argument("--provider", choices=("ollama", "anthropic", "openai"))
    sh.add_argument("--model")
    sh.add_argument("--endpoint")

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
    if "split" in (getattr(args, "skip", None) or ()):
        o["convert.split_omnibus"] = "off"
    if getattr(args, "only", None) or getattr(args, "skip", None):
        o["run.stages"] = tuple(stages)
    return o


def _logger(verbose: bool) -> logging.Logger:
    log = logging.getLogger("epubkit")
    log.setLevel(logging.DEBUG if verbose else logging.INFO)
    if not log.handlers:
        h = logging.StreamHandler(sys.stderr)
        h.setFormatter(logging.Formatter("%(message)s"))
        log.addHandler(h)
    return log


def main(argv: list[str] | None = None) -> int:
    # `epubkit config list | head` should not end in a traceback
    try:
        import signal
        signal.signal(signal.SIGPIPE, signal.SIG_DFL)
    except (ImportError, AttributeError, ValueError):
        pass                                    # not POSIX, or not main thread

    args = build_parser().parse_args(argv)

    if args.command == "init":
        target = args.config or (Path.cwd() / CONFIG_NAME)
        if args.defaults:
            if target.exists():
                print("%s already exists; not overwriting it" % target)
                return 1
            target.write_text(EXAMPLE_CONFIG)
            print("wrote %s" % target)
            print("Edit paths.source and paths.output, then: epubkit check")
            return 0
        from .wizard import Cancelled
        from .wizard import run as run_wizard
        try:
            return run_wizard(target)
        except Cancelled:
            print("\nStopped. Nothing was written.")
            return 1

    if args.command == "validate":
        return _cmd_validate(args)

    if args.command == "doctor":
        return _cmd_doctor(args)

    if args.command == "split":
        return _cmd_split(args)

    try:
        cfg = Config.load(getattr(args, "config", None), _overrides(args))
    except ConfigError as exc:
        print("configuration problem: %s" % exc, file=sys.stderr)
        return 2

    log = _logger(cfg.run.verbose)

    if args.command == "shelves":
        from .ai import build
        from .shelves import run as run_shelves
        lib = (args.library or cfg.paths.output)
        try:
            provider = build(cfg.ai)
        except Exception as exc:                            # noqa: BLE001
            print("no model available (%s); using subjects only" % str(exc)[:80])
            provider = None
        return run_shelves(Path(lib).expanduser(), provider,
                           lookup_online=cfg.metadata.shelf_lookup and not args.offline)

    if args.command == "config":
        return _cmd_config(args, cfg)

    if args.command == "check":
        from . import deps
        print(cfg.describe())
        print()
        for line in deps.report():
            print("  " + line)
        problems = cfg.check(need_paths=False)
        if cfg.paths.source or cfg.paths.output:
            problems = cfg.check(need_paths=True)
        print()
        if problems:
            print("problems:")
            for p in problems:
                print("  - %s" % p)
            return 1
        for notice in cfg.notices():
            print("note: %s" % notice)
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

    return _cmd_run(cfg, log, plain=getattr(args, "plain", False))


def _cmd_run(cfg: Config, log, plain: bool = False) -> int:
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
    elif cfg.metadata.suggest_shelves:
        try:                                # the shelf step can use a model, needn't
            from .ai import build
            provider = build(cfg.ai)
        except Exception:                                   # noqa: BLE001
            provider = None

    from .pipeline import Pipeline
    from .progress import Progress
    progress = Progress.for_stream(sys.stderr, plain=plain or cfg.run.dry_run)
    undo = progress.attach(log)
    try:
        state = Pipeline(cfg, log, provider, progress).run()
    finally:
        undo()

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


def _recap_progress(name: str):
    """Say where a single-book recap has got to, with an estimate.

    A novel is dozens of model calls, each up to a minute on a local model,
    and a command that prints nothing for half an hour looks hung.
    """
    import re
    import time

    start = time.monotonic()
    tty = sys.stderr.isatty()
    last = [""]

    def step(text: str) -> None:
        spent = time.monotonic() - start
        line = "%s: %s, %d:%02d so far" % (name[:50], text, spent // 60, spent % 60)
        m = re.match(r"passage (\d+)/(\d+)", text)
        if m and int(m.group(1)) > 0:
            done, total = int(m.group(1)), int(m.group(2))
            left = spent / done * (total - done)
            line += ", about %d min left" % max(1, round(left / 60))
        if line == last[0]:
            return
        last[0] = line
        if tty:
            sys.stderr.write("\r\033[K" + line)
        else:
            sys.stderr.write(line + "\n")
        sys.stderr.flush()

    return step


def _recap_books(paths) -> list[Path]:
    out = []
    for p in paths:
        p = p.expanduser().resolve()
        if p.is_dir():
            out += sorted(q for q in p.rglob("*.epub")
                          if not any(part.startswith(".") for part in q.relative_to(p).parts))
        elif p.is_file():
            out.append(p)
        else:
            print("no such book or folder: %s" % p, file=sys.stderr)
    return out


def _cmd_recaps(args, cfg: Config, log) -> int:
    """Add recaps to books where they already live, one at a time.

    Only the recap files inside each book change - everything else is checked
    byte-for-byte before a book is written, and the file is overwritten in
    place, keeping its path. Books already carrying current recaps are
    skipped, so an interrupted run over a whole library just picks up again.
    """
    import tempfile

    from .stages import recaps as R

    books = _recap_books(args.books)
    if not books:
        print("no books found", file=sys.stderr)
        return 2

    if args.audit:
        return _recap_audit(books, R)

    from .ai import build
    from .stages.base import Context
    from .state import BookRecord

    provider = build(cfg.ai)
    todo = []
    for b in books:
        a = R.audit(b)
        if a["kind"] == "passages" and a["format"] >= R.FORMAT and not args.force \
                and not a.get("lead_off"):
            continue
        todo.append(b)
    skipped = len(books) - len(todo)
    if skipped:
        print("%d of %d books already have current recaps; skipping them "
              "(--force redoes them)" % (skipped, len(books)))
    if args.limit:
        todo = todo[:args.limit]

    failed = 0
    for i, book in enumerate(todo, 1):
        work = Path(tempfile.mkdtemp(prefix="epubkit-recaps-"))
        rec = BookRecord(source=str(book))
        label = "[%d/%d] %s" % (i, len(todo), book.name) if len(todo) > 1 else book.name
        ctx = Context(config=cfg, record=rec, path=book, work=work, log=log,
                      provider=provider, step=_recap_progress(label))
        try:
            result = R.RecapStage().run(ctx)
            ok = result.ok
        except Exception as exc:                            # noqa: BLE001
            ok = False
            rec.notes.append("not written: %s" % exc)
        if sys.stderr.isatty():
            sys.stderr.write("\n")
        for n in rec.notes:
            log.info("  %s", n)
        if not ok:
            failed += 1
        shutil.rmtree(work, ignore_errors=True)
    return 1 if failed else 0


def _recap_audit(books, R) -> int:
    kinds = {}
    rows = []
    for b in books:
        a = R.audit(b)
        kind = a["kind"]
        if kind == "passages" and a["format"] < R.FORMAT:
            kind = "passages (old)"
        elif kind == "passages" and a.get("lead_off"):
            kind = "wrong lead"
        kinds[kind] = kinds.get(kind, 0) + 1
        rows.append((kind, a))
    print("%-16s %6s %6s %7s %8s %9s  %s" % ("recaps", "parts", "people", "heard", "recap KB",
                                            "book KB", "book"))
    for kind, a in sorted(rows, key=lambda r: (r[0], r[1]["book"])):
        print("%-16s %6s %6s %7s %8.1f %9.1f  %s%s" % (
            kind, a["passages"] or "", a["people"] or "", a["heard_of"] or "",
            a["recap_kb"], a["book_kb"], a["book"],
            "  (not a kepub)" if not a["kepub"] else "") +
            ("  ERROR %s" % a["error"] if a["error"] else ""))
    print()
    explain = {
        "none": "no recaps",
        "flat": "recaps from an older epubkit that the Kobo never read",
        "chapters": "whole-chapter recaps, from gen_recaps.py",
        "passages (old)": "passage recaps in an earlier layout",
        "wrong lead": "written around the wrong main character; redone on the next run",
        "passages": "current: up to the page, with who you have met and heard of",
    }
    for k, n in sorted(kinds.items(), key=lambda kv: -kv[1]):
        print("%5d  %-16s %s" % (n, k, explain.get(k, "")))
    total = sum(a["recap_kb"] for _k, a in rows)
    print("\nrecap data in total: %.0f KB across %d books" % (total, len(rows)))
    return 0


def _cmd_config(args, cfg: Config) -> int:
    from . import configedit as ce

    if args.action == "list":
        width = max(len(k) for k in ce.known_keys(cfg))
        for key in ce.known_keys(cfg):
            try:
                value = ce.resolve(cfg, key)
            except ConfigError:
                continue
            print("%-*s  %s" % (width, key,
                                "-" if value is None else ce.render_value(value)))
        print("\nconfig file: %s" % (cfg.source_file or "none yet"))
        return 0

    if not args.key:
        print("which setting? `epubkit config list` shows them all",
              file=sys.stderr)
        return 2

    try:
        if args.action == "get":
            value = ce.resolve(cfg, args.key)
            print("-" if value is None else ce.render_value(value))
            return 0

        if args.value is None:
            print("no value given. Try: epubkit config set %s VALUE" % args.key,
                  file=sys.stderr)
            return 2
        path = ce.target_file(cfg, args.config)
        written = ce.set_value(cfg, args.key, args.value, path)
        print("%s  (in %s)" % (written, path))
        return 0
    except ConfigError as exc:
        print("%s" % exc, file=sys.stderr)
        return 2


def _cmd_split(args) -> int:
    """Work out where the books inside an omnibus start, then split on request.

    Two steps on purpose. A wrong boundary produces a book missing its first
    chapter and another with two, and neither announces itself - so the plan
    is shown and only written out when asked for.
    """
    book = args.book.expanduser().resolve()
    if not book.is_file():
        print("no such book: %s" % book, file=sys.stderr)
        return 2

    out_dir = (args.out or book.parent).expanduser()
    plan = args.plan or (out_dir / (book.stem + ".split.json"))

    if not args.plan:
        from .stages.base import run_tool
        # the plan is written into --out, which may not exist yet
        plan.parent.mkdir(parents=True, exist_ok=True)
        r = run_tool("plan_omnibus", [str(book), "--out", str(plan)],
                     timeout=900)
        print(r.stdout or r.stderr or "")
        if r.returncode != 0:
            if r.stdout and r.stderr:
                print(r.stderr, file=sys.stderr)
            return 1
        if "no omnibus structure found" in (r.stdout or ""):
            return 1
        print("Plan written to %s. Read it, fix any boundary or title that is "
              "wrong, then re-run with --plan %s --apply" % (plan, plan))
        if not args.apply:
            return 0

    if not args.apply:
        print("Nothing written. Add --apply to split it.")
        return 0

    out_dir.mkdir(parents=True, exist_ok=True)
    from .stages.base import run_tool
    r = run_tool("split_omnibus",
                 [str(book), "--plan", str(plan), "--out", str(out_dir)],
                 timeout=1800)
    print(r.stdout or r.stderr or "")
    if r.returncode != 0:
        return 1
    print("\nThe omnibus itself was not touched. Run epubkit on the parts to "
          "give them metadata and covers of their own.")
    return 0


def _cmd_doctor(args) -> int:
    from . import deps

    print("epubkit can run:")
    for line in deps.report():
        print("  " + line)

    missing = deps.missing_modules()
    java = shutil.which("java")
    if not missing and java:
        print("\nEverything is in place.")
        return 0

    print()
    for m in missing:
        print("missing: %s" % deps.advice(m))
    if not java:
        print("missing: java, which EPUBCheck runs on. Without it books are "
              "reported unvalidated rather than clean.")
        print("         macOS: brew install --cask temurin")
        print("         Debian/Ubuntu: sudo apt install default-jre")

    if not missing:
        return 1
    cmd = deps.install_command(missing)
    if not args.fix:
        print("\nTo install the Python pieces: epubkit doctor --fix")
        return 1

    print("\nThis will run:\n  %s" % " ".join(cmd))
    print("It installs into %s" % sys.executable)
    if not args.yes:
        try:
            answer = input("Go ahead? [y/N] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            return 1
        if answer not in ("y", "yes"):
            print("Left alone.")
            return 1

    import subprocess
    result = subprocess.run(cmd)
    if result.returncode != 0:
        print("\nThe install failed. The output above says why; a build error "
              "in lxml on macOS usually wants: xcode-select --install",
              file=sys.stderr)
        return result.returncode

    # re-check in a fresh process: a module installed into a running
    # interpreter is not reliably importable in that same process
    still = subprocess.run(
        [sys.executable, "-c",
         "from epubkit import deps; print(','.join(deps.missing_modules()))"],
        capture_output=True, text=True).stdout.strip()
    if still:
        print("\nStill missing after the install: %s" % still)
        return 1
    print("\nDone. Everything epubkit needs is installed.")
    return 0 if java else 1


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
