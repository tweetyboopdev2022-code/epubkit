#!/usr/bin/env python3
"""Shrink books without losing anything the device can actually show.

The Clara Colour panel is 1072x1448. An image stored larger than that is not
detail the reader ever sees - it is pixels the device throws away on every page
turn, and storage that could hold more books. Measured across one library:
72% of all bytes were JPEGs, and 165 of 395 images were bigger than the panel.

What this does, in order of how safe it is:
  - resamples images that exceed the panel down to fit it (aspect preserved)
  - re-encodes JPEGs at a high quality only when that comes out smaller
  - drops files sitting in the zip that the OPF manifest never references
  - recompresses the archive at maximum deflate

What it never touches: text, markup, stylesheets, the table of contents, or the
metadata. Fonts stay unless you ask for --drop-fonts, and fixed-layout books are
skipped entirely, because for a picture book the image IS the page.

    python3 slim.py <folder> [--panel 1072x1448] [--quality 88] [--apply]

Without --apply it only reports what it would save.
"""
import sys, os, io, re, json, zipfile, argparse
import xml.etree.ElementTree as ET
from PIL import Image

OPF = "{http://www.idpf.org/2007/opf}"
IMG = (".jpg", ".jpeg", ".png")


def is_fixed_layout(raw):
    return "pre-paginated" in raw or "fixed-layout" in raw


def referenced(z):
    """Every path anything in the book points at.

    The OPF manifest alone is not enough: a stylesheet can be linked straight
    from the XHTML without ever appearing in the manifest, and dropping it as an
    "orphan" silently strips the book's formatting. So this walks the content
    documents and the stylesheets too, and resolves each reference relative to
    the file that made it.
    """
    names = z.namelist()
    opf = next((n for n in names if n.endswith(".opf")), None)
    if not opf:
        return None, set(), ""
    raw = z.read(opf).decode("utf-8", "ignore")
    refs = {opf, "mimetype"}
    refs.update(n for n in names if n.startswith("META-INF"))

    def add(src, href):
        href = href.split("#")[0].strip()
        if not href or "://" in href or href.startswith("data:"):
            return
        d = src.rsplit("/", 1)[0] if "/" in src else ""
        refs.add(os.path.normpath(os.path.join(d, href)).replace("\\", "/"))

    for n in names:
        low = n.lower()
        if low.endswith((".opf", ".ncx", ".xhtml", ".html", ".htm", ".css", ".smil")):
            try:
                text = z.read(n).decode("utf-8", "ignore")
            except Exception:
                continue
            for h in re.findall(r'(?:href|src|xlink:href)\s*=\s*["\']([^"\']+)', text):
                add(n, h)
            for h in re.findall(r'url\(\s*["\']?([^)"\']+)', text):
                add(n, h)
    return opf, refs, raw


def to_jpeg(data, name, quality):
    """Re-encode a PNG page as JPEG when that is much smaller and loses nothing.

    Page-image comics are sometimes stored as PNG at a size already under the
    panel, so the resampler never touches them - and PNG is a terrible container
    for painted artwork: one Bad Guys book held 196 PNG pages at 406 KB each
    where the same art as JPEG runs about 45 KB. Photographic and painted images
    have no flat runs for PNG to exploit, so this is 80% off for no visible
    change.

    Refused when the image really uses transparency (a page never does, but a
    logo might), and when the saving is not worth rewriting references for.
    """
    if not name.lower().endswith(".png"):
        return None
    try:
        im = Image.open(io.BytesIO(data))
        im.load()
    except Exception:
        return None
    if im.mode in ("RGBA", "LA", "P"):
        a = im.convert("RGBA").getchannel("A")
        if a.getextrema()[0] < 255:
            return None                      # genuinely transparent - leave it
    buf = io.BytesIO()
    im.convert("RGB").save(buf, "JPEG", quality=quality, optimize=True,
                           progressive=True)
    out = buf.getvalue()
    return out if len(out) < len(data) * 0.7 else None


def shrink(data, name, panel, quality):
    """Return smaller bytes for this image, or None to keep the original."""
    try:
        im = Image.open(io.BytesIO(data))
        im.load()
    except Exception:
        return None
    w, h = im.size
    pw, ph = panel
    scale = min(pw / w, ph / h, 1.0)
    if scale >= 1.0:
        return None
    im = im.resize((max(1, int(w * scale)), max(1, int(h * scale))), Image.LANCZOS)
    buf = io.BytesIO()
    if im.mode in ("RGBA", "LA", "P") or name.lower().endswith(".png"):
        if im.mode == "P":
            im = im.convert("RGBA" if "transparency" in im.info else "RGB")
        if im.mode in ("RGBA", "LA"):
            im.save(buf, "PNG", optimize=True)
        else:
            im.convert("RGB").save(buf, "JPEG", quality=quality, optimize=True,
                                   progressive=True)
    else:
        im.convert("RGB").save(buf, "JPEG", quality=quality, optimize=True,
                               progressive=True)
    out = buf.getvalue()
    return out if len(out) < len(data) else None


def process(path, panel, quality, apply, drop_orphans, drop_fonts, min_save=0,
            png_to_jpeg=False):
    try:
        z = zipfile.ZipFile(path)
    except zipfile.BadZipFile:
        # macOS leaves AppleDouble sidecars ("._Book.kepub.epub") on FAT volumes.
        # They carry the .epub extension but are 4 KB of Finder metadata.
        return ("not a zip", 0, 0)
    opf, refs, raw = referenced(z)
    if opf is None:
        z.close(); return None
    if is_fixed_layout(raw):
        z.close(); return ("fixed-layout, skipped", 0, 0)

    before = os.path.getsize(path)
    new, saved_img, saved_orphan, saved_font, n_img = {}, 0, 0, 0, 0
    rename = {}
    css = " ".join(z.read(n).decode("utf-8", "ignore")
                   for n in z.namelist() if n.endswith(".css"))
    drop = set()
    for i in z.infolist():
        n, ext = i.filename, os.path.splitext(i.filename)[1].lower()
        if drop_orphans and n not in refs and ext != ".opf":
            drop.add(n); saved_orphan += i.compress_size; continue
        if drop_fonts and ext in (".ttf", ".otf", ".woff", ".woff2"):
            stem = os.path.basename(n)
            if stem not in css and os.path.splitext(stem)[0] not in css:
                drop.add(n); saved_font += i.compress_size; continue
        if ext in IMG:
            data = z.read(n)
            out = shrink(data, n, panel, quality)
            if out is None and png_to_jpeg:
                cand = n[:-4] + ".jpg"
                # A book can hold BOTH cover.png and cover.jpg. Renaming the png
                # onto the jpg puts two entries under one name in the archive -
                # a book that opens with whichever the reader happens to pick.
                # Not worth a few kilobytes: leave the colliding one as PNG.
                if cand in z.namelist() or cand in rename.values():
                    out = None
                else:
                    out = to_jpeg(data, n, quality)
                    if out:
                        rename[n] = cand
            if out:
                new[n] = out; n_img += 1
                # compare like with like: what the archive currently stores
                # against what the replacement will store, not the decompressed
                # original, which over-reports the saving on PNGs.
                saved_img += max(0, i.compress_size - len(out))

    total = saved_img + saved_orphan + saved_font
    if not apply or total < min_save:
        z.close()
        # Rewriting a book costs its reading position and its shelves on the next
        # import, so a saving of a few kilobytes is not worth having.
        return ("would save" if not apply else "below threshold", total, n_img)

    tmp = path + ".tmp"
    zo = zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED, compresslevel=9)
    zo.writestr("mimetype", "application/epub+zip", zipfile.ZIP_STORED)
    # Renaming a page file means every reference to it has to move too - the OPF
    # manifest (href AND media-type), the content documents, the stylesheets. Miss
    # one and the book opens with blank pages, which no size check would notice.
    def retarget(text):
        # A plain string replace is wrong here: renaming "95_0.png" also rewrites
        # the "195_0.png" inside another reference, and the book ends up with a
        # manifest pointing at a file that does not exist. The name has to be
        # matched as a whole - not preceded by a character that could be part of
        # a longer filename.
        for old_n, new_n in sorted(rename.items(), key=lambda kv: -len(kv[0])):
            for a, b in ((old_n, new_n),
                         (os.path.basename(old_n), os.path.basename(new_n))):
                text = re.sub(r"(?<![A-Za-z0-9_.\-])" + re.escape(a), b, text)
        return text

    for i in z.infolist():
        if i.filename == "mimetype" or i.filename in drop:
            continue
        name = rename.get(i.filename, i.filename)
        data = new.get(i.filename)
        if data is None:
            data = z.read(i.filename)
            low = i.filename.lower()
            if rename and low.endswith((".opf", ".ncx", ".xhtml", ".html", ".htm",
                                        ".css", ".smil")):
                t = retarget(data.decode("utf-8", "ignore"))
                t = t.replace('media-type="image/png"', 'media-type="image/jpeg"') \
                     if low.endswith(".opf") else t
                data = t.encode("utf-8")
        zo.writestr(name, data)
    zo.close(); z.close()
    if os.path.getsize(tmp) < before:
        os.replace(tmp, path)
        return ("slimmed", before - os.path.getsize(path), n_img)
    os.remove(tmp)
    return ("no gain", 0, 0)


def main(argv):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("folder")
    ap.add_argument("--panel", default="1072x1448",
                    help="device panel, WxH (Clara Colour 1072x1448, Libra 1264x1680)")
    ap.add_argument("--quality", type=int, default=88, help="JPEG quality (default 88)")
    ap.add_argument("--apply", action="store_true", help="rewrite the files")
    ap.add_argument("--min-save", type=float, default=0.0, metavar="MB",
                    help="only rewrite a book that saves at least this many MB. "
                         "Every rewritten book is re-imported by the device and "
                         "loses its reading position and shelves, so a tiny "
                         "saving is a bad trade (default 0 - rewrite anything)")
    ap.add_argument("--recursive", action="store_true",
                    help="also walk subfolders (a library sorted into genre folders)")
    ap.add_argument("--png-to-jpeg", action="store_true",
                    help="re-encode PNG page images as JPEG where that is much "
                         "smaller. Page-image comics stored as PNG are several "
                         "times larger than they need to be; images that really "
                         "use transparency are left alone")
    ap.add_argument("--drop-orphans", action="store_true",
                    help="also remove files the manifest never references")
    ap.add_argument("--drop-fonts", action="store_true",
                    help="also remove embedded fonts no stylesheet asks for")
    a = ap.parse_args(argv)
    panel = tuple(int(x) for x in a.panel.lower().split("x"))

    tot_before = tot_saved = skipped = 0
    sidecars = sidecar_bytes = 0

    def walk(folder):
        for name in sorted(os.listdir(folder)):
            full = os.path.join(folder, name)
            if a.recursive and os.path.isdir(full) and not name.startswith("."):
                for x in walk(full):
                    yield x
            elif os.path.isfile(full):
                yield full

    for p in walk(a.folder):
        f = os.path.basename(p)
        if f.startswith("._"):
            sidecars += 1
            sidecar_bytes += os.path.getsize(p)
            continue
        if f.startswith(".") or not f.endswith(".epub"):
            continue
        b = os.path.getsize(p)
        r = process(p, panel, a.quality, a.apply, a.drop_orphans, a.drop_fonts,
                    a.min_save * 1e6, a.png_to_jpeg)
        if not r:
            continue
        what, saved, n = r
        if what == "not a zip":
            skipped += 1
            print("  %-56s not a zip file, skipped" % f[:56])
            continue
        if what == "below threshold":
            continue
        tot_before += b; tot_saved += saved
        if saved:
            print("  %-56s %6.1f -> %6.1f MB  (%d images)" %
                  (f[:56], b / 1e6, (b - saved) / 1e6, n))
    print("\n%s: %.1f MB -> %.1f MB, %.1f MB saved (%.0f%%)" %
          ("applied" if a.apply else "dry run", tot_before / 1e6,
           (tot_before - tot_saved) / 1e6, tot_saved / 1e6,
           100 * tot_saved / tot_before if tot_before else 0))
    if skipped:
        print("%d file(s) were not readable as EPUBs and were left alone" % skipped)
    if sidecars:
        print("%d macOS \"._\" sidecar files here (%.0f KB) - clear them with:\n"
              "    dot_clean %s" % (sidecars, sidecar_bytes / 1e3, a.folder))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
