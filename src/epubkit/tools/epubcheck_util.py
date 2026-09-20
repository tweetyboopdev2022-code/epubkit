#!/usr/bin/env python3
"""Shared wrapper around real EPUBCheck (the same validator Kobo's own
conversion tools and epubcheck.org run), so the pipeline's structural checks
and the standalone device audit use one implementation.

Why this exists: verify_epubs.py's own checks (zip integrity, manifest
completeness, a real cover, a usable TOC) catch a different class of problem
than EPUBCheck does. EPUBCheck catches spec violations that Kobo's Nickel
reader tolerates in some cases and mishandles in others - wrong DOCTYPE for
the package version, invalid attributes, a stale NCX identifier - the kind
of thing found in the Sep 2026 battery-drain investigation. Both checks
matter; neither replaces the other.

Requires a JVM and the `epubcheck` PyPI package (`pip install epubcheck
--break-system-packages`, which bundles the jar - no separate download).
On a Mac with no Java, `brew install openjdk` first. If neither is present,
find_jar() returns None and callers should skip EPUBCheck rather than fail -
a missing validator is a setup problem, not a reason to block every book.
"""
import os, re, shutil, subprocess, sys

_warned = False


def find_jar():
    """Locate epubcheck.jar: an explicit EPUBCHECK_JAR env var first, then
    the jar bundled by `pip install epubcheck`."""
    env = os.environ.get("EPUBCHECK_JAR")
    if env and os.path.exists(env):
        return env
    try:
        import epubcheck
        jar = os.path.join(os.path.dirname(epubcheck.__file__), "epubcheck.jar")
        if os.path.exists(jar):
            return jar
    except ImportError:
        pass
    return None


def available():
    return bool(shutil.which("java")) and bool(find_jar())


def _warn_once(msg):
    global _warned
    if not _warned:
        print("epubcheck_util: %s" % msg, file=sys.stderr)
        _warned = True


# EPUBCheck prints "SEVERITY(CODE): <path>(line,col): message". The path is
# the book's own path plus the entry inside it, so it contains spaces whenever
# the book does - which, with this library's full-title filenames, is always.
# Matching it with \S+ silently failed on every such book and swallowed the
# path into the message, losing the line number and the name of the file
# inside the book that was actually wrong. .*? is non-greedy and stops at the
# first "(digits,digits):", which a title like "(Part 1 of 3)" cannot imitate.
ERROR_RE = re.compile(
    r'^(FATAL|ERROR|WARNING|INFO)\(([A-Z]+-\d+)\): (?:(.*?)\((-?\d+),(-?\d+)\): )?(.*)$')


def run(path, timeout=120):
    """Run EPUBCheck against one file. Returns a list of dicts:
    {severity, code, line, col, message}. FATAL/ERROR only by default is the
    caller's choice, not this function's - it returns everything found so a
    caller can decide what blocks a batch versus what's just worth knowing.
    Returns None (not []) if EPUBCheck could not be run at all, so callers
    can tell "checked, zero problems" apart from "not checked"."""
    jar = find_jar()
    if not jar or not shutil.which("java"):
        _warn_once("java or epubcheck.jar not found - skipping EPUBCheck. "
                   "pip install epubcheck --break-system-packages (and "
                   "install a JVM if java is missing) to enable it.")
        return None
    try:
        r = subprocess.run(["java", "-jar", jar, str(path)],
                           capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return [{"severity": "ERROR", "code": "TIMEOUT", "line": None, "col": None,
                 "message": "epubcheck did not finish within %ds" % timeout}]
    except Exception as e:
        _warn_once("epubcheck failed to run: %r" % e)
        return None

    book = str(path)
    out = []
    for line in (r.stdout + r.stderr).splitlines():
        m = ERROR_RE.match(line.strip())
        if not m:
            continue
        sev, code, where, ln, col, msg = m.groups()
        # "where" is <book path>/<entry inside the book>; the useful half is
        # the entry, so report that rather than repeating the book's own path.
        entry = None
        if where:
            entry = where[len(book) + 1:] if where.startswith(book + "/") else where
        out.append({"severity": sev, "code": code, "file": entry,
                    "line": int(ln) if ln else None,
                    "col": int(col) if col else None, "message": msg})
    return out


def summarize(results):
    """A one-line count string, e.g. '3 errors (2x HTM-004, 1x NCX-001)'."""
    if results is None:
        return "not checked"
    errs = [r for r in results if r["severity"] in ("FATAL", "ERROR")]
    if not errs:
        return "clean"
    from collections import Counter
    c = Counter(r["code"] for r in errs)
    parts = ", ".join("%dx %s" % (n, code) for code, n in c.most_common())
    return "%d error%s (%s)" % (len(errs), "" if len(errs) == 1 else "s", parts)
