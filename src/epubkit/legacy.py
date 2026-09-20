"""Compatibility with this tool's former name, kobokit.

epubkit was called kobokit until September 2026. Nothing a user already has on
disk is moved or rewritten here: the old names are simply still *read*, each
with a one-line notice saying what to rename. Old names are only ever
fallbacks; the new name always wins when both exist.

What is covered:

* ``kobokit.toml`` in the current directory or a parent, and
  ``~/.config/kobokit/kobokit.toml``
* ``KOBOKIT_*`` environment variables
* ``<output>/.kobokit-work`` and ``<output>/.kobokit-originals``, which hold a
  library's resume state and the untouched originals. These are used in place,
  never moved, so a half-finished run resumes and the originals stay findable
* ``kobokit-state.json``, copied (not moved) to the new name

What is deliberately NOT covered: the ``kobokit`` command and the ``kobokit``
Python package. That name now belongs to a separate tool, so a shim here would
run the wrong program.
"""
from __future__ import annotations

import logging
import os
import shutil
from pathlib import Path

OLD = "kobokit"
NEW = "epubkit"

OLD_CONFIG_NAME = "kobokit.toml"
OLD_ENV_PREFIX = "KOBOKIT_"
OLD_WORK_DIR = ".kobokit-work"
OLD_BACKUP_DIR = ".kobokit-originals"
OLD_STATE_NAME = "kobokit-state.json"

_told: set[str] = set()


def notice(message: str) -> None:
    """Say something once per run, on the log, never on stdout."""
    if message not in _told:
        _told.add(message)
        logging.getLogger("epubkit").warning("%s", message)


def old_config_path() -> Path | None:
    """The nearest kobokit.toml, or the one in the old XDG directory."""
    here = Path.cwd().resolve()
    for d in (here, *here.parents):
        p = d / OLD_CONFIG_NAME
        if p.is_file():
            return p
    xdg = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    p = xdg / OLD / OLD_CONFIG_NAME
    return p if p.is_file() else None



def old_env(suffix: str) -> str | None:
    """Value of KOBOKIT_<suffix>, with a notice, or None."""
    name = OLD_ENV_PREFIX + suffix
    if name in os.environ:
        notice("%s is the old name; set EPUBKIT_%s instead" % (name, suffix))
        return os.environ[name]
    return None


def dir_or_old(new: Path, old_name: str) -> Path:
    """`new`, unless only the old-named sibling exists - then that, in place."""
    old = new.with_name(old_name)
    if not new.exists() and old.is_dir():
        notice("using existing %s; rename it to %s when convenient"
               % (old, new.name))
        return old
    return new


def adopt_state(work_root: Path, new_name: str) -> None:
    """Copy kobokit-state.json to the new name so an interrupted run resumes."""
    new = work_root / new_name
    old = work_root / OLD_STATE_NAME
    if not new.exists() and old.is_file():
        shutil.copy2(old, new)
        notice("carried resume state over from %s" % old)
