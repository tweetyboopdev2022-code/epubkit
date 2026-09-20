"""The stages a book passes through, in the order they must run.

The order is not cosmetic and is the expensive part of this project's history:

  ingest    classify the file and copy the original somewhere safe
  convert   anything that is not an EPUB becomes one
  metadata  fill in title/author/series, then rename FROM the finished metadata
  repair    structural fixes and EPUB3 conformance
  validate  real EPUBCheck, which decides whether the book ships
  recaps    per-chapter summaries, which need the final spine
  report    say what was actually wrong, per book

Two of those orderings are load-bearing. Renaming happens after metadata and
before conversion-to-kepub, because the resume check looks for the finished
name. Recaps run last because they key on spine positions, and any stage that
rewrites the book can change them.
"""
from .base import Context, Stage, StageResult

__all__ = ["Stage", "StageResult", "Context"]
