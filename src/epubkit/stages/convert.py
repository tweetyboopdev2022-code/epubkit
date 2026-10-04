"""Anything that is not already an EPUB becomes one.

PDFs are the awkward case and get a decision rather than a default. A picture
book or comic is converted fixed-layout, because for those the image *is* the
page. A novel is reflowed, because 900 fixed page images on a six-inch screen
cannot change their font size and cannot be read comfortably.
"""
from __future__ import annotations

import re
from pathlib import Path

from .. import deps
from .base import Context, Stage, StageResult, run_tool


class ConvertStage(Stage):
    name = "convert"

    def applies(self, ctx: Context) -> bool:
        if ctx.meta.get("format_in") in ("pdf", "mobi", "fb2"):
            return True
        return self._kindle_comic(ctx) is not None

    @staticmethod
    def _kindle_comic(ctx):
        """A Kindle panel-view comic in EPUB clothing: every page blank on a Kobo."""
        if ctx.meta.get("format_in") not in ("epub", "kepub"):
            return None
        if "kindle_comic" not in ctx.meta:
            from ..tools import kindle_comic
            ctx.meta["kindle_comic"] = kindle_comic.check(ctx.path)
        found = ctx.meta["kindle_comic"]
        return found if found.get("kind") == "blank-pages" else None

    def run(self, ctx: Context) -> StageResult:
        kind = ctx.meta.get("format_in")
        src = ctx.path
        out_dir = ctx.work / "converted"
        out_dir.mkdir(parents=True, exist_ok=True)

        comic = self._kindle_comic(ctx)
        if comic:
            return self._rebuild_comic(ctx, src, out_dir, comic)

        if kind == "mobi":
            return self._mobi(ctx, src, out_dir)
        if kind == "fb2":
            return self._fb2(ctx, src, out_dir)
        return self._pdf(ctx, src, out_dir)

    # ------------------------------------------------------------------

    def _rebuild_comic(self, ctx, src: Path, out_dir: Path, found) -> StageResult:
        """Rebuild a Kindle panel-view comic as a real fixed-layout book.

        Its pages are pictures pinned with position:absolute in a zero-size
        box and it is not marked fixed-layout, so every page shows blank on a
        Kobo and in Grimmory's reader, though every picture is in the file.
        """
        from ..tools import kindle_comic
        name = src.name if src.name.endswith(".kepub.epub") else \
            src.name[:-len(".epub")] + ".kepub.epub"
        dst = out_dir / name
        try:
            r = kindle_comic.rebuild(str(src), str(dst), panel=ctx.config.convert.panel,
                                    quality=getattr(ctx.config.convert, "comic_quality", 0) or 88)
        except Exception as exc:                            # noqa: BLE001
            return StageResult(ok=False,
                               message="Kindle comic rebuild failed: %s" % exc)
        ctx.meta["fixed_layout"] = True
        ctx.note("a Kindle comic whose pages a Kobo cannot lay out; "
                 "rebuilt as a fixed-layout picture book (%d pages, %s)"
                 % (r["pages"], r.get("picture_px", "")))
        if found.get("low_res"):
            ctx.note("its pictures are only %s - they will look soft on the "
                     "device; a better copy would fix that" % found.get("picture_px"))
        return StageResult(path=dst)

    def _mobi(self, ctx, src: Path, out_dir: Path) -> StageResult:
        # mobi2epub takes an output *directory* and names the file itself
        dst = out_dir / (src.stem + ".epub")
        r = run_tool("mobi2epub", [str(src), "--out", str(out_dir)], timeout=600)
        if r.returncode != 0 or not dst.exists():
            return StageResult(ok=False,
                               message="mobi conversion failed: %s"
                                       % _tail(r.stderr or r.stdout))
        ctx.note("converted from mobi")
        # A Kindle comic comes out of its wrapper with Amazon's panel layout:
        # every page blank on a Kobo (Dog Man 1 did exactly this). Check the
        # unpacked book the same way an EPUB is checked, and rebuild it.
        from ..tools import kindle_comic
        found = kindle_comic.check(dst)
        ctx.meta["kindle_comic"] = found
        if found.get("kind") == "blank-pages":
            return self._rebuild_comic(ctx, dst, out_dir, found)
        if found.get("kind") == "fixed-layout":
            if _kindle_fixed_layout(dst):
                # Kindle's own fixed layout: sized by an Amazon-only OPF
                # setting, no viewport on any page, Amazon attributes
                # throughout. A Kobo cannot size those pages. The pictures
                # are full size, so rebuilding loses nothing.
                return self._rebuild_comic(ctx, dst, out_dir, found)
            ctx.meta["fixed_layout"] = True
        elif found.get("kind") == "lost-fixed-layout":
            if kindle_comic.repair_flag(str(dst)):
                ctx.meta["fixed_layout"] = True
                ctx.note("marked fixed-layout again (its pages are sized "
                         "pictures but the flag was missing)")
        return StageResult(path=dst)

    def _fb2(self, ctx, src: Path, out_dir: Path) -> StageResult:
        dst = out_dir / (src.stem.replace(".fb2", "") + ".epub")
        r = run_tool("fb2epub", [str(src), str(dst)], timeout=600)
        if r.returncode != 0 or not dst.exists():
            return StageResult(ok=False,
                               message="fb2 conversion failed: %s"
                                       % _tail(r.stderr or r.stdout))
        ctx.note("converted from fb2")
        return StageResult(path=dst)

    def _pdf(self, ctx, src: Path, out_dir: Path) -> StageResult:
        # pdf_route inspects the PDF and says reflow or fixed-layout; asking it
        # beats guessing from the filename, which gets comics wrong. It also
        # digs a title and author out of the PDF, which a PDF rarely carries
        # properly - the metadata stage corrects both afterwards.
        route, title, author = "reflow", src.stem, "Unknown"
        spread = False
        probe = run_tool("pdf_route", [str(src), "--meta", "--quiet"], timeout=300)
        if probe.returncode == 0 and probe.stdout.strip():
            parts = probe.stdout.strip().split("\t")
            route = parts[0].strip() or route
            if len(parts) > 1 and parts[1].strip():
                title = parts[1].strip()
            if len(parts) > 2 and parts[2].strip():
                author = parts[2].strip()
            spread = len(parts) > 3 and parts[3].strip() == "spread"
        else:
            ctx.note("could not inspect the PDF; reflowing it")
        ctx.note("pdf routed as %s" % route)

        fixed = route == "fixed"
        gone = deps.missing_for("pdf (fixed layout)" if fixed
                                else "pdf (reflowed)")
        if gone:
            return StageResult(
                ok=False,
                message="cannot convert this %s pdf: %s"
                        % (route, "; ".join(deps.advice(m) for m in gone)))
        module = "pdf2kepub" if fixed else "pdf2epub"
        name = src.stem + (".kepub.epub" if fixed else ".epub")
        args = [str(src), "--out", str(out_dir), "--title", title,
                "--author", author, "--filename", name]

        if fixed:
            setting = (ctx.config.convert.split_spreads or "auto").lower()
            if setting == "force":
                # every wide page cut down the middle, gutter or not
                args += ["--split-spreads", "--force-split"]
                ctx.note("splitting every page down the middle (force)")
            elif setting == "always" or (setting == "auto" and spread):
                # each page judged for itself; a spread whose artwork runs
                # across the gutter is left whole
                args.append("--split-spreads")
                ctx.note("pages are wide enough to be two-page spreads; "
                         "splitting them")
            elif spread:
                ctx.note("pages look like two-page spreads, but "
                         "convert.split_spreads is %r" % setting)
            args += ["--panel", ctx.config.convert.panel,
                     "--quality", str(ctx.config.convert.jpeg_quality)]

        # PDFs are the slowest thing here by a wide margin
        r = run_tool(module, args, timeout=3600)
        dst = out_dir / name
        if r.returncode != 0 or not dst.exists():
            return StageResult(ok=False,
                               message="pdf conversion failed (%s): %s"
                                       % (route, _tail(r.stderr or r.stdout)))
        ctx.meta["fixed_layout"] = fixed
        ctx.note("converted from pdf (%s)" % ("fixed layout" if fixed
                                              else "reflowed"))
        # Say how the split actually went. "Splitting them" followed by
        # silence is how half a book stays in spreads without anyone noticing.
        m = re.search(r"split (\d+) of (\d+) spreads; kept (\d+) whole",
                      r.stdout or "")
        if m:
            done, total, kept = (int(g) for g in m.groups())
            ctx.meta["spreads_kept"] = kept
            ctx.note("spreads: %d of %d split, %d left whole%s"
                     % (done, total, kept,
                        " (artwork crosses the gutter; "
                        "convert.split_spreads = \"force\" cuts them anyway)"
                        if kept else ""))
        return StageResult(path=dst)


def _kindle_fixed_layout(path) -> bool:
    """Fixed-layout pages made by Kindle Comic Creator, with no viewport."""
    import zipfile
    try:
        with zipfile.ZipFile(path) as z:
            docs = [n for n in z.namelist() if n.lower().endswith((".xhtml", ".html"))]
            sample = [z.read(n)[:4000] for n in docs[1:8]]
    except (zipfile.BadZipFile, OSError):
        return False
    if not sample:
        return False
    no_viewport = all(b'name="viewport"' not in d for d in sample)
    amazon = any(b"KC2" in d or b"data-AmznRemoved" in d or b"amzn" in d for d in sample)
    return no_viewport and amazon


def _tail(text: str | None, n: int = 240) -> str:
    """The last line of a tool's output, unless a missing library explains it.

    A bare "ModuleNotFoundError: No module named 'numpy'" repeated down a
    whole batch says nothing about what to do next.
    """
    if not text:
        return "no output"
    text = text.strip()
    if not text:
        return "no output"
    m = re.search(r"No module named ['\"]([A-Za-z0-9_.]+)['\"]", text)
    if m:
        return "missing library - " + deps.advice(m.group(1).split(".")[0])
    return text.splitlines()[-1][:n]
