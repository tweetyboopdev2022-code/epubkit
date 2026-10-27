#!/usr/bin/env python3
"""Add epubkit recaps to the books Grimmory serves, where they sit.

Run this on the library server, the machine that holds both the library and Ollama:

    grimmory_recaps.py find                     where Grimmory keeps its books
    grimmory_recaps.py audit                    what recap data each book has
    grimmory_recaps.py run --only "Dragon Tattoo" --limit 1     one book, to test
    grimmory_recaps.py run                      the rest; safe to stop and rerun
    grimmory_recaps.py restore "Dragon Tattoo"  put a book back as it was
    grimmory_recaps.py check-device BOOK KOBO_COPY
                                                do the Kobo's paragraph numbers
                                                match the ones the recaps use?
    grimmory_recaps.py comics                   comics whose pages show blank
    grimmory_recaps.py comics --fix             rebuild those as picture books
    grimmory_recaps.py prune                    list backups older than 30 days
    grimmory_recaps.py prune --apply            delete them
    grimmory_recaps.py schedule-prune           do that every night, by itself

What `run` does for each book, one at a time:

1. skips it if its recaps are already current, or if it is fixed-layout (a
   comic or picture book has no prose to recap);
2. copies the book to ~/recap-backups/<date>/, keeping its folder structure;
3. runs `epubkit recaps` on it in place. epubkit checks, byte for byte, that
   nothing inside the book changes except the recap files - text, images,
   styles and metadata stay exactly as they were - and it overwrites the file
   in place, keeping its path, so Grimmory sees the same book modified rather
   than one book removed and another added;
4. checks the result, and puts the backup back if anything looks wrong;
5. logs the outcome to ~/recap-backups/log.tsv.

Stopping it (Ctrl-C, closing the terminal, a reboot) is safe: a book is only
written once all of its passages are done, and the next `run` skips every
book already finished. Only one run can go at a time.
"""
from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request
import zipfile
from pathlib import Path

HOME = Path.home()
BACKUPS = HOME / "recap-backups"
STATE = BACKUPS / "grimmory-recaps.json"
LOG = BACKUPS / "log.tsv"
CONFIG = BACKUPS / "epubkit-recaps.toml"

DEFAULT_ENDPOINT = "http://localhost:11434"
DEFAULT_MODEL = "gemma2:9b"


# ---------------------------------------------------------------- helpers


def say(msg: str = "") -> None:
    print(msg, flush=True)


def die(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)
    sys.exit(2)


def recaps_module():
    try:
        from epubkit.stages import recaps as R
        from epubkit.tools import kepubify
    except ImportError:
        die("epubkit is not installed for this python (%s).\n"
            "Install it from your checkout:  python3 -m pip install --user -e "
            "~/epubkit" % sys.executable)
    return R, kepubify


def load_state() -> dict:
    try:
        return json.loads(STATE.read_text())
    except (OSError, ValueError):
        return {}


def save_state(state: dict) -> None:
    BACKUPS.mkdir(parents=True, exist_ok=True)
    tmp = STATE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=1))
    tmp.replace(STATE)


def sha(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def log(book: Path, outcome: str, detail: str = "") -> None:
    BACKUPS.mkdir(parents=True, exist_ok=True)
    new = not LOG.exists()
    with open(LOG, "a", encoding="utf-8") as fh:
        if new:
            fh.write("when\toutcome\tbook\tdetail\n")
        fh.write("%s\t%s\t%s\t%s\n" % (dt.datetime.now().isoformat(timespec="seconds"),
                                       outcome, book, detail.replace("\t", " ")))


def overwrite_in_place(src: Path, dst: Path) -> None:
    """Same path, same file: never delete-and-recreate under Grimmory's nose."""
    with open(src, "rb") as a, open(dst, "r+b") as b:
        b.truncate(0)
        shutil.copyfileobj(a, b)


# ---------------------------------------------------------------- finding the library


def docker_mounts() -> list[tuple[str, str, bool]]:
    """(host folder, container folder, writable) for every Grimmory container."""
    try:
        names = subprocess.run(["docker", "ps", "--format", "{{.Names}}\t{{.Image}}"],
                               capture_output=True, text=True, timeout=20).stdout
    except (OSError, subprocess.TimeoutExpired):
        return []
    out = []
    for line in names.splitlines():
        name, _, image = line.partition("\t")
        if "grimmory" not in (name + image).lower() and "booklore" not in (name + image).lower():
            continue
        raw = subprocess.run(["docker", "inspect", name, "--format", "{{json .Mounts}}"],
                             capture_output=True, text=True).stdout
        try:
            for m in json.loads(raw or "[]"):
                out.append((m.get("Source", ""), m.get("Destination", ""), bool(m.get("RW"))))
        except ValueError:
            pass
    return out


def library_dirs(args) -> list[Path]:
    if args.library:
        dirs = [Path(p).expanduser().resolve() for p in args.library]
    else:
        dirs = []
        for src, _dest, _rw in docker_mounts():
            p = Path(src)
            if not p.is_dir():
                continue
            # the folders that actually hold books, not config or the database
            if any(p.rglob("*.epub")):
                dirs.append(p)
        if not dirs:
            die("Could not work out where Grimmory keeps its books. Run `find` to "
                "see its folders, then pass the right one with --library.")
    for d in dirs:
        if not d.is_dir():
            die("not a folder: %s" % d)
    return dirs


def books_in(dirs) -> list[tuple[Path, Path]]:
    """(library root, book) for every EPUB, skipping hidden folders."""
    out = []
    for root in dirs:
        for p in sorted(root.rglob("*.epub")):
            rel = p.relative_to(root)
            if any(part.startswith(".") for part in rel.parts):
                continue
            out.append((root, p))
    return out


# ---------------------------------------------------------------- commands


def cmd_find(args) -> int:
    mounts = docker_mounts()
    if not mounts:
        say("No running Grimmory container found (checked `docker ps` for "
            "grimmory/booklore). Is it running, and can this user use docker?")
        return 1
    say("Grimmory's folders on this machine:\n")
    for src, dest, rw in mounts:
        p = Path(src)
        n = sum(1 for _ in p.rglob("*.epub")) if p.is_dir() else 0
        say("  %-45s -> %-22s %s%s" % (src, dest, "read-write" if rw else "READ-ONLY",
                                       "   %d EPUBs" % n if n else ""))
    say("\nThe folders with EPUBs in them are the library. `audit` and `run` use "
        "them automatically; --library picks one yourself.")
    ro = [s for s, _d, rw in mounts if not rw and Path(s).is_dir() and any(Path(s).rglob("*.epub"))]
    if ro:
        say("\nNote: Grimmory has these mounted read-only, so it cannot write "
            "metadata into them itself:\n  " + "\n  ".join(ro))
    return 0


def cmd_audit(args) -> int:
    R, kepubify = recaps_module()
    books = books_in(library_dirs(args))
    counts: dict[str, int] = {}
    todo_words = 0
    for root, b in books:
        a = R.audit(b)
        kind = a["kind"]
        fixed = False
        try:
            with zipfile.ZipFile(b) as z:
                fixed = kepubify.is_fixed_layout(z)
        except zipfile.BadZipFile:
            kind = "unreadable"
        if fixed:
            kind = "fixed-layout (skipped)"
        elif kind == "passages" and a["format"] < R.FORMAT:
            kind = "passages (old layout)"
        elif kind == "passages" and a.get("lead_off"):
            kind = "passages (wrong lead)"
        elif kind == "passages" and a.get("lang_off"):
            kind = "passages (wrong language)"
        counts[kind] = counts.get(kind, 0) + 1
        if args.verbose:
            say("%-24s %8.0f KB  %s" % (kind, a["book_kb"], b.relative_to(root)))
        if kind not in ("passages", "fixed-layout (skipped)", "unreadable"):
            todo_words += _words(R, b)
    say("\n%d books\n" % len(books))
    for k, n in sorted(counts.items(), key=lambda kv: -kv[1]):
        say("  %5d  %s" % (n, k))
    passages = todo_words / 800 if todo_words else 0
    say("\nStill to do: about %s words, about %d model calls at 800 words a passage."
        % (format(todo_words, ","), passages))
    return 0


def _words(R, book: Path) -> int:
    try:
        docs = R._documents(book, 2000)
    except Exception:                                       # noqa: BLE001
        return 0
    return sum(len(t.split()) for _o, _p, _l, paras in docs for _n, t in paras)


def check_ollama(endpoint: str, model: str) -> None:
    try:
        with urllib.request.urlopen(endpoint.rstrip("/") + "/api/tags", timeout=10) as r:
            tags = json.loads(r.read().decode())
    except Exception as exc:                                # noqa: BLE001
        die("Ollama is not answering at %s (%s)." % (endpoint, exc))
    have = {m.get("name") for m in tags.get("models", [])}
    if model not in have:
        die("Ollama has no model %r. It has: %s" % (model, ", ".join(sorted(have))))


def write_config(args) -> Path:
    BACKUPS.mkdir(parents=True, exist_ok=True)
    CONFIG.write_text(
        "# written by grimmory_recaps.py; edit the command line, not this file\n"
        "[ai]\nprovider = \"ollama\"\nmodel = \"%s\"\nendpoint = \"%s\"\n"
        "timeout = 300\nmax_tokens = 1500\nconcurrency = %d\n\n"
        "[recaps]\nenabled = true\npassage_words = %d\ncast = true\n"
        "main_character = %s\n"
        "cast_only = %s\nkeep_recaps = %s\n"
        "cast_method = %s\ncast_report_dir = %s\nseries_dir = %s\n"
        % (args.model, args.endpoint, args.concurrency, args.passage_words,
           json.dumps(args.main_character or ""),
           "true" if getattr(args, "characters_only", False) else "false",
           "true" if getattr(args, "keep_recaps", False) else "false",
           json.dumps(getattr(args, "cast_method", "") or "passages"),
           json.dumps(getattr(args, "cast_report", "") or ""),
           json.dumps(getattr(args, "series_dir", "") or "")))
    return CONFIG


def cmd_run(args) -> int:
    R, kepubify = recaps_module()
    BACKUPS.mkdir(parents=True, exist_ok=True)
    lock = open(BACKUPS / ".lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        die("Another run is already going. Wait for it, or stop it first.")

    books = books_in(library_dirs(args))
    if args.only:
        needle = args.only.lower()
        books = [(r, b) for r, b in books if needle in b.name.lower()]
        if not books:
            die("No book's filename contains %r." % args.only)

    if args.characters_only or args.keep_recaps:
        args.force = True
    todo = []
    for root, b in books:
        a = R.audit(b)
        if args.characters_only and a["kind"] != "passages":
            continue                         # nothing to keep, so nothing to do
        if a["kind"] == "passages" and not args.force and not a.get("lead_off") \
                and not a.get("lang_off") and (
                a["format"] >= R.FORMAT or args.keep_older_passages):
            continue
        try:
            with zipfile.ZipFile(b) as z:
                if kepubify.is_fixed_layout(z):
                    continue
            from epubkit.tools import kindle_comic
            if kindle_comic.check(b)["kind"] in ("blank-pages", "picture-pages",
                                                  "lost-fixed-layout"):
                continue                     # a picture book; nothing to recap
        except zipfile.BadZipFile:
            log(b, "skipped", "not a readable zip")
            continue
        todo.append((root, b))
    # a series in order: each book is written with what the books before it
    # put in the series memory
    todo = R.reading_order(todo)
    if args.limit:
        todo = todo[:args.limit]
    say("%d books in the library, %d to do%s." % (
        len(books), len(todo), " this run" if args.limit else ""))
    if not todo or args.dry_run:
        for _r, b in todo:
            say("  would do: %s" % b.name)
        return 0

    for _root, b in todo:
        if not os.access(b, os.W_OK):
            die("Cannot write %s as %s. Grimmory's files may belong to another "
                "user; fix the permissions (or run this as that user) and rerun."
                % (b, os.environ.get("USER", "this user")))

    check_ollama(args.endpoint, args.model)
    cfg = write_config(args)
    state = load_state()
    stamp = dt.date.today().isoformat()
    failed = unchanged = 0
    started = time.monotonic()

    for i, (root, book) in enumerate(todo, 1):
        rel = book.relative_to(root)
        # Two copies. `original` is the book as it was before recaps ever
        # touched it, made once and never overwritten - a --force rerun must
        # not replace it with the previous run's output. `rollback` is the
        # book as it is right now, to put back if this run goes wrong.
        known = state.get(str(book), {})
        original = Path(known["backup"]) if known.get("backup") else None
        made_original = False
        if not original or not original.exists():
            original = BACKUPS / stamp / root.name / rel
            original.parent.mkdir(parents=True, exist_ok=True)
            n = 1
            while original.exists():
                n += 1
                original = original.with_name("%s.%d" % (rel.name, n))
            shutil.copy2(book, original)
            made_original = True
        rollback = BACKUPS / ".inflight" / book.name
        rollback.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(book, rollback)
        before = sha(book)
        say("\n[%d/%d] %s" % (i, len(todo), rel))
        cmd = [sys.executable, "-c",
               "import sys; from epubkit.cli import main; sys.exit(main(sys.argv[1:]))",
               "recaps", "-c", str(cfg), str(book)]
        if args.force:
            cmd.append("--force")
        rc = subprocess.call(cmd)
        after = sha(book)
        a = R.audit(book)

        if after == before:
            # epubkit left it alone (nothing to recap, or it refused)
            rollback.unlink()
            if made_original:
                original.unlink()
            log(book, "unchanged", "exit %d" % rc)
            say("  left unchanged" + (" - see the reason above" if rc else ""))
            if rc:
                failed += 1
            else:
                unchanged += 1
            continue
        if rc != 0 or a["kind"] != "passages" or a["error"]:
            overwrite_in_place(rollback, book)
            rollback.unlink()
            if made_original:
                original.unlink()
            log(book, "RESTORED", "exit %d, audit %s %s" % (rc, a["kind"], a["error"]))
            say("  something went wrong - the original is back in place")
            failed += 1
            continue
        rollback.unlink()
        state[str(book)] = {"backup": str(original),
                            "before": known.get("before", before), "after": after,
                            "when": dt.datetime.now().isoformat(timespec="seconds")}
        save_state(state)
        log(book, "done", "%d passages, %d people (%d heard of), +%.1f KB" % (
            a["passages"], a["people"], a["heard_of"], a["recap_kb"]))
        say("  done: %d passages, %d people (%d only heard of)"
            % (a["passages"], a["people"], a["heard_of"]))
        spent = time.monotonic() - started
        if i < len(todo):
            say("  %d of %d books, %.0f min so far, about %.0f min to go"
                % (i, len(todo), spent / 60, spent / i * (len(todo) - i) / 60))

    say("\nFinished: %d done, %d left unchanged, %d with problems. Log: %s"
        % (len(todo) - failed - unchanged, unchanged, failed, LOG))
    if len(todo) - failed:
        say("Grimmory may need a library rescan to notice the changed files.")
    return 1 if failed else 0


def cmd_restore(args) -> int:
    state = load_state()
    hits = [(k, v) for k, v in state.items() if args.book.lower() in Path(k).name.lower()]
    if not hits:
        die("No recapped book matches %r (see %s)." % (args.book, LOG))
    for path, info in hits:
        book, backup = Path(path), Path(info["backup"] or "/nonexistent")
        if info.get("backup_pruned"):
            say("the backup of %s was deleted as old on %s; it cannot be restored"
                % (book.name, info["backup_pruned"]))
            continue
        if not backup.exists():
            say("backup missing for %s: %s" % (book.name, backup))
            continue
        if book.exists() and sha(book) != info["after"]:
            say("%s has changed since recaps were added (Grimmory may have written "
                "metadata into it). Restoring would undo that too; --force to do it anyway."
                % book.name)
            if not args.force:
                continue
        overwrite_in_place(backup, book)
        log(book, "restored by hand", str(backup))
        del state[path]
        say("restored %s" % book.name)
    save_state(state)
    return 0


def cmd_prune(args) -> int:
    """Delete dated backup folders in ~/recap-backups older than --days.

    Only folders named YYYY-MM-DD are touched, and their age comes from the
    name, not from file times. The log, the run state, the lock and anything
    else in ~/recap-backups are left alone. Nothing happens while a run is
    going, and without --apply nothing is deleted at all - it only says what
    would go.
    """
    if not BACKUPS.is_dir():
        say("No %s yet; nothing to prune." % BACKUPS)
        return 0
    BACKUPS.mkdir(parents=True, exist_ok=True)
    lock = open(BACKUPS / ".lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        say("A run is going; not pruning now.")
        return 0
    cutoff = dt.date.today() - dt.timedelta(days=args.days)
    old = []
    for d in sorted(BACKUPS.iterdir()):
        if not d.is_dir():
            continue
        try:
            day = dt.date.fromisoformat(d.name)
        except ValueError:
            continue                          # not a dated backup folder
        if day < cutoff:
            size = sum(f.stat().st_size for f in d.rglob("*") if f.is_file())
            old.append((d, size))
    # leftovers of an interrupted run, a day old or more
    stale = [f for f in list((BACKUPS / ".inflight").glob("*")) + list(BACKUPS.glob(".rebuild-*"))
             if f.is_file() and time.time() - f.stat().st_mtime > 86400]
    total = sum(s for _d, s in old) + sum(f.stat().st_size for f in stale)
    if not old and not stale:
        say("Nothing older than %d days in %s." % (args.days, BACKUPS))
        return 0
    for d, size in old:
        say("%s %-12s %8.1f MB" % ("deleting" if args.apply else "would delete", d.name, size / 1e6))
    for f in stale:
        say("%s leftover %s" % ("deleting" if args.apply else "would delete", f.name))
    if not args.apply:
        say("\n%.1f MB would be freed. Add --apply to delete." % (total / 1e6))
        return 0
    state = load_state()
    gone = {str(d) for d, _s in old}
    for d, _s in old:
        shutil.rmtree(d)
    for f in stale:
        f.unlink()
    # books whose only backup just went: `restore` says so plainly rather
    # than looking for a file that is not there
    changed = False
    for info in state.values():
        if any(info.get("backup", "").startswith(g + "/") for g in gone):
            info["backup"] = ""
            info["backup_pruned"] = dt.date.today().isoformat()
            changed = True
    if changed:
        save_state(state)
    log(BACKUPS, "pruned", "%d folder(s), %.1f MB, older than %d days"
        % (len(old), total / 1e6, args.days))
    say("\nFreed %.1f MB." % (total / 1e6))
    return 0


def cmd_schedule_prune(args) -> int:
    """Install a systemd user timer that runs `prune --apply` every night."""
    unit_dir = HOME / ".config" / "systemd" / "user"
    unit_dir.mkdir(parents=True, exist_ok=True)
    script = Path(__file__).resolve()
    (unit_dir / "recap-backups-prune.service").write_text(
        "[Unit]\nDescription=Delete recap backups older than %d days\n\n"
        "[Service]\nType=oneshot\nExecStart=%s %s prune --apply --days %d\n"
        % (args.days, sys.executable, script, args.days))
    (unit_dir / "recap-backups-prune.timer").write_text(
        "[Unit]\nDescription=Nightly pruning of old recap backups\n\n"
        "[Timer]\nOnCalendar=*-*-* 03:30\nPersistent=true\n\n"
        "[Install]\nWantedBy=timers.target\n")
    for cmd in (["systemctl", "--user", "daemon-reload"],
                ["systemctl", "--user", "enable", "--now", "recap-backups-prune.timer"]):
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode:
            die("%s failed: %s" % (" ".join(cmd), r.stderr.strip()))
    linger = subprocess.run(["loginctl", "show-user", os.environ.get("USER", ""),
                             "--property=Linger"], capture_output=True, text=True).stdout
    say("Scheduled: every night at 03:30, backups older than %d days are deleted."
        % args.days)
    say("Check it with: systemctl --user list-timers recap-backups-prune.timer")
    if "Linger=yes" not in linger:
        say("Note: this user has no lingering session, so the timer only runs "
            "while you are logged in. `loginctl enable-linger` fixes that.")
    return 0


def cmd_comics(args) -> int:
    """Find picture books whose pages show blank, and optionally rebuild them.

    blank-pages: every page's picture is pinned with position:absolute in a
    book not marked fixed-layout (a Kindle panel-view conversion, usually).
    Blank in Grimmory's reader and on the Kobo, though no picture is missing.
    picture-pages: one picture per page in the flow; readable, just not fitted
    to the screen. --fix rebuilds blank-pages books; add --include-readable to
    rebuild picture-pages ones as well.
    """
    try:
        from epubkit.tools import kindle_comic
    except ImportError:
        die("epubkit is not installed for this python (%s)." % sys.executable)
    books = books_in(library_dirs(args))
    if args.only:
        books = [(r, b) for r, b in books if args.only.lower() in b.name.lower()]
    todo = []
    for root, b in books:
        r = kindle_comic.check(b)
        if r["kind"] == "lost-fixed-layout":
            say("%-13s %4d pages  the fixed-layout flag is gone from its OPF  %s"
                % ("lost-flag", r["pages"], b.relative_to(root)))
            if args.fix:
                original = BACKUPS / dt.date.today().isoformat() / "comics" / root.name / b.relative_to(root)
                original.parent.mkdir(parents=True, exist_ok=True)
                if not original.exists():
                    shutil.copy2(b, original)
                if kindle_comic.repair_flag(str(b)):
                    log(b, "fixed-layout flag restored", "original at %s" % original)
                    say("  flag restored")
            continue
        if r["kind"] in ("blank-pages", "picture-pages"):
            say("%-13s %4d pages  pictures %-9s %s  %s" % (
                r["kind"], r["pages"], r["picture_px"] or "?",
                "LOW-RES" if r["low_res"] else "       ", b.relative_to(root)))
            if r["kind"] == "blank-pages" or args.include_readable:
                todo.append((root, b))
    if not args.fix:
        say("\n%d to rebuild. Run again with --fix to rebuild them in place "
            "(the original is kept in %s)." % (len(todo), BACKUPS))
        return 0
    stamp = dt.date.today().isoformat()
    for root, b in todo:
        if not os.access(b, os.W_OK):
            die("Cannot write %s." % b)
        original = BACKUPS / stamp / "comics" / root.name / b.relative_to(root)
        original.parent.mkdir(parents=True, exist_ok=True)
        if not original.exists():
            shutil.copy2(b, original)
        tmp = BACKUPS / (".rebuild-" + b.name)
        try:
            r = kindle_comic.rebuild(str(b), str(tmp))
        except Exception as exc:                            # noqa: BLE001
            log(b, "comic NOT rebuilt", str(exc))
            say("  could not rebuild %s: %s" % (b.name, exc))
            continue
        overwrite_in_place(tmp, b)
        tmp.unlink()
        log(b, "comic rebuilt", "%d pages, pictures %s; original at %s"
            % (r["pages"], r["picture_px"], original))
        say("  rebuilt %s: %d pages" % (b.name, r["pages"]))
    say("\nRescan the library in Grimmory, then open one to check.")
    return 0


def cmd_check_device(args) -> int:
    """Compare paragraph numbering: the library book vs the copy on the Kobo.

    The recaps are keyed to koboSpan paragraph numbers. If Grimmory converts
    books to kepub itself on the way to the Kobo, its converter might number
    paragraphs differently from epubkit's, and every position would be off.
    """
    R, _k = recaps_module()
    a_book, b_book = Path(args.book), Path(args.device_copy)
    with zipfile.ZipFile(a_book) as a, zipfile.ZipFile(b_book) as b:
        docs_a = {p: R._paragraphs(a.read(p)) for _o, p in R._spine(a_book)}
        docs_b = {p: R._paragraphs(b.read(p)) for _o, p in R._spine(b_book)}
        has_recaps = any("recaps/parts.tsv" in n for n in b.namelist())
    same = differ = 0
    for path, paras in docs_a.items():
        other = docs_b.get(path)
        if other is None:
            say("  missing on the Kobo copy: %s" % path)
            differ += 1
            continue
        if [n for n, _t in paras] == [n for n, _t in other] and \
                [t[:40] for _n, t in paras] == [t[:40] for _n, t in other]:
            same += 1
        else:
            differ += 1
            if differ <= 5:
                say("  numbering differs in %s" % path)
    say("\n%d documents numbered the same, %d different." % (same, differ))
    say("The Kobo's copy %s the recap files." % ("carries" if has_recaps else "does NOT carry"))
    if differ or not has_recaps:
        say("Do not run the rest of the library until this is sorted out.")
        return 1
    say("Positions will line up.")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    def lib(p):
        p.add_argument("--library", action="append",
                       help="a folder of books (default: found from Grimmory's docker mounts)")

    sub.add_parser("find")
    a = sub.add_parser("audit")
    lib(a)
    a.add_argument("-v", "--verbose", action="store_true", help="one line per book")
    r = sub.add_parser("run")
    lib(r)
    r.add_argument("--only", help="only books whose filename contains this")
    r.add_argument("--limit", type=int, default=0, help="stop after this many books")
    r.add_argument("--force", action="store_true", help="redo books already current")
    r.add_argument("--characters-only", action="store_true",
                   help="keep each book's recap text and redo only the who's who; "
                        "books without recaps are skipped (implies --force)")
    r.add_argument("--keep-recaps", action="store_true",
                   help="like --characters-only for books that have recaps; books "
                        "without them get recaps in full (implies --force)")
    r.add_argument("--cast-method", choices=("passages", "book"), default="passages",
                   help="book: write the who's who from the whole book (names from "
                        "the text, one checked entry per person); with "
                        "--characters-only the passages are not read again")
    r.add_argument("--cast-report", default="",
                   help="with --cast-method book, write <book>.cast.txt into this "
                        "folder: what was found, set aside and written")
    r.add_argument("--series-dir", default="",
                   help="keep series memory files here instead of the usual place, "
                        "so a trial run on copies leaves the real ones alone")
    r.add_argument("--keep-older-passages", action="store_true",
                   help="leave books that already have passage recaps in an older "
                        "layout alone instead of redoing them")
    r.add_argument("--dry-run", action="store_true", help="list what would be done")
    r.add_argument("--model", default=DEFAULT_MODEL)
    r.add_argument("--endpoint", default=DEFAULT_ENDPOINT)
    r.add_argument("--concurrency", type=int, default=1)
    r.add_argument("--passage-words", type=int, default=800)
    r.add_argument("--main-character", default="",
                   help="whose relationships the who's who describes; only "
                        "sensible with --only for one book or series")
    s = sub.add_parser("restore")
    s.add_argument("book", help="part of the filename")
    s.add_argument("--force", action="store_true")
    pr = sub.add_parser("prune")
    pr.add_argument("--days", type=int, default=30, help="keep this many days (default 30)")
    pr.add_argument("--apply", action="store_true", help="actually delete")
    sp = sub.add_parser("schedule-prune")
    sp.add_argument("--days", type=int, default=30)
    k = sub.add_parser("comics")
    lib(k)
    k.add_argument("--fix", action="store_true", help="rebuild blank-page books in place")
    k.add_argument("--include-readable", action="store_true",
                   help="rebuild picture-pages books too, fitting pages to the screen")
    k.add_argument("--only", help="only books whose filename contains this")
    c = sub.add_parser("check-device")
    c.add_argument("book", help="the book in the library")
    c.add_argument("device_copy", help="the same book copied off the Kobo (.kobo/kepub/...)")
    args = ap.parse_args(argv)
    return {"find": cmd_find, "audit": cmd_audit, "run": cmd_run,
            "restore": cmd_restore, "check-device": cmd_check_device,
            "comics": cmd_comics, "prune": cmd_prune,
            "schedule-prune": cmd_schedule_prune}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
