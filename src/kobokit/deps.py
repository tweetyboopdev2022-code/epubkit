"""Which optional pieces are installed, and what to do about the ones that are not.

A stage that needs a library it has not got should say so by name before a run
starts, not fail on the 87th book with a bare ModuleNotFoundError.
"""
from __future__ import annotations

import importlib.util
import shutil

#: module -> (what it is for, what to install)
OPTIONAL = {
    "lxml": ("rewriting books to EPUB 3", 'pip install "kobokit[full]"'),
    "PIL": ("covers and page images", 'pip install "kobokit[full]"'),
    "pymupdf": ("reading PDFs", 'pip install "kobokit[pdf]"'),
    "numpy": ("cropping pages of fixed-layout PDFs",
              'pip install "kobokit[pdf]"'),
    "mobi": ("converting MOBI and AZW3", 'pip install "kobokit[mobi]"'),
}

#: the modules each conversion route needs before it can run at all
ROUTES = {
    "pdf (reflowed)": ("pymupdf",),
    "pdf (fixed layout)": ("pymupdf", "numpy", "PIL"),
    "mobi / azw3": ("mobi",),
    "epub 3 repair": ("lxml",),
}


def have(module: str) -> bool:
    try:
        return importlib.util.find_spec(module) is not None
    except (ImportError, ValueError):
        return False


def missing_for(route: str) -> list[str]:
    return [m for m in ROUTES.get(route, ()) if not have(m)]


def advice(module: str) -> str:
    what, how = OPTIONAL.get(module, ("", "pip install " + module))
    return "%s (%s): %s" % (module, what, how) if what else how


def report() -> list[str]:
    """One line per route, saying whether it can run."""
    out = []
    for route in ROUTES:
        gone = missing_for(route)
        out.append("%-22s %s" % (
            route,
            "ready" if not gone else "needs " + ", ".join(gone)))
    java = "yes" if shutil.which("java") else "NO - EPUBCheck cannot run"
    out.append("%-22s %s" % ("java (epubcheck)", java))
    return out


#: module -> the extra that provides it, for an install that fixes it
EXTRA_FOR = {
    "lxml": "full",
    "PIL": "full",
    "pymupdf": "pdf",
    "numpy": "pdf",
    "mobi": "mobi",
}


def missing_modules() -> list[str]:
    """Every optional module that is not installed, in a stable order."""
    return [m for m in OPTIONAL if not have(m)]


def extras_for(modules) -> list[str]:
    seen = []
    for m in modules:
        extra = EXTRA_FOR.get(m)
        if extra and extra not in seen:
            seen.append(extra)
    return seen


def install_command(modules) -> list[str] | None:
    """The pip command that would install what is missing, or None.

    It installs into the interpreter running kobokit, whatever that is - a
    venv, a uv tool environment, or the system Python. Calling pip by module
    on sys.executable is what makes that true; a bare `pip` on the PATH may
    well belong to a different Python entirely, which is exactly how someone
    ends up installing numpy successfully and still being told it is missing.
    """
    extras = extras_for(modules)
    if not extras:
        return None
    import sys
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent.parent
    target = str(root) if (root / "pyproject.toml").is_file() else "kobokit"
    return [sys.executable, "-m", "pip", "install",
            "%s[%s]" % (target, ",".join(extras))]
