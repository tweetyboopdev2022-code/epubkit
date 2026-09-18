"""Anything that is not already an EPUB becomes one.

PDFs are the awkward case and get a decision rather than a default. A picture
book or comic is converted fixed-layout, because for those the image *is* the
page. A novel is reflowed, because 900 fixed page images on a six-inch screen
cannot change their font size and cannot be read comfortably.
"""
from __future__ import annotations

from pathlib import Path

from .base import Context, Stage, StageResult, run_tool


class ConvertStage(Stage):
    name = "convert"

    def applies(self, ctx: Context) -> bool:
        return ctx.meta.get("format_in") in ("pdf", "mobi", "fb2")

    def run(self, ctx: Context) -> StageResult:
        kind = ctx.meta.get("format_in")
        src = ctx.path
        out_dir = ctx.work / "converted"
        out_dir.mkdir(parents=True, exist_ok=True)

        if kind == "mobi":
            return self._mobi(ctx, src, out_dir)
        if kind == "fb2":
            return self._fb2(ctx, src, out_dir)
        return self._pdf(ctx, src, out_dir)

    # ------------------------------------------------------------------

    def _mobi(self, ctx, src: Path, out_dir: Path) -> StageResult:
        # mobi2epub takes an output *directory* and names the file itself
        dst = out_dir / (src.stem + ".epub")
        r = run_tool("mobi2epub", [str(src), "--out", str(out_dir)], timeout=600)
        if r.returncode != 0 or not dst.exists():
            return StageResult(ok=False,
                               message="mobi conversion failed: %s"
                                       % _tail(r.stderr or r.stdout))
        ctx.note("converted from mobi")
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
        probe = run_tool("pdf_route", [str(src), "--meta", "--quiet"], timeout=300)
        if probe.returncode == 0 and probe.stdout.strip():
            parts = probe.stdout.strip().split("\t")
            route = parts[0].strip() or route
            if len(parts) > 1 and parts[1].strip():
                title = parts[1].strip()
            if len(parts) > 2 and parts[2].strip():
                author = parts[2].strip()
        else:
            ctx.note("could not inspect the PDF; reflowing it")
        ctx.note("pdf routed as %s" % route)

        fixed = route == "fixed"
        module = "pdf2kepub" if fixed else "pdf2epub"
        name = src.stem + (".kepub.epub" if fixed else ".epub")
        # PDFs are the slowest thing here by a wide margin
        r = run_tool(module, [str(src), "--out", str(out_dir),
                              "--title", title, "--author", author,
                              "--filename", name], timeout=3600)
        dst = out_dir / name
        if r.returncode != 0 or not dst.exists():
            return StageResult(ok=False,
                               message="pdf conversion failed (%s): %s"
                                       % (route, _tail(r.stderr or r.stdout)))
        ctx.meta["fixed_layout"] = fixed
        ctx.note("converted from pdf (%s)" % ("fixed layout" if fixed
                                              else "reflowed"))
        return StageResult(path=dst)


def _tail(text: str | None, n: int = 240) -> str:
    if not text:
        return "no output"
    return text.strip().splitlines()[-1][:n] if text.strip() else "no output"
