#!/usr/bin/env python3
"""Flag books whose cover is missing, too small, or a placeholder.

Catches without rendering anything to look at:
  - no cover declared, or declared but pointing at a file that isn't there
  - cover smaller than a Kobo panel wants, or the wrong shape for a book jacket
  - Calibre's generated "stack of books" cover, and other known filler art
  - one cover image shared by several books in the same batch
  - a typographic cover this toolchain generated earlier, so it stays on the list
    until the real jacket turns up

Placeholder detection is a 64-bit average hash, so a re-encoded or resized copy
of the same filler still matches. Add more with --learn.
"""
import sys, os, io, re, json, zipfile, argparse
from PIL import Image

OPF = "{http://www.idpf.org/2007/opf}"
HASHES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "placeholder_hashes.json")
MIN_W, MIN_H = 400, 600
# height / width for a normal jacket. The floor used to be 1.3, which flagged
# every one of the 20 Bad Guys books: that series is printed at 5.5x7 inches,
# a ratio of 1.26, and so are plenty of other children's graphic novels. Twenty
# false alarms is worse than none, because the three books that really had a
# problem were buried among them.
RATIO_LO, RATIO_HI = 1.18, 1.9


def ahash(im):
    g = im.convert("L").resize((8, 8), Image.LANCZOS)
    px = list(g.tobytes())
    avg = sum(px) / 64.0
    bits = 0
    for i, p in enumerate(px):
        if p >= avg:
            bits |= 1 << i
    return "%016x" % bits


def dist(a, b):
    return bin(int(a, 16) ^ int(b, 16)).count("1")


def cover_of(path):
    """Return (name, PIL image) for the book's cover, or (None, None)."""
    z = zipfile.ZipFile(path)
    names = z.namelist()
    opf = next((n for n in names if n.endswith(".opf")), None)
    if not opf:
        return None, None
    base = opf.rsplit("/", 1)[0] + "/" if "/" in opf else ""
    raw = z.read(opf).decode("utf-8", "ignore")
    href = None
    # Attribute order in an OPF is arbitrary. These patterns used to require
    # name= before content= and properties= before href=; Random House ships
    # <meta content="cover-image" name="cover"/> and <item href=... properties=
    # "cover-image"/>, so every one of eleven Never Girls and Disney Fairies
    # books came back "no usable cover" while the jacket sat right there in the
    # archive. Match either order.
    m = (re.search(r'<meta[^>]+name="cover"[^>]+content="([^"]+)"', raw)
         or re.search(r'<meta[^>]+content="([^"]+)"[^>]+name="cover"', raw))
    if m:
        cid = re.escape(m.group(1))
        h = (re.search(r'id="%s"[^>]*href="([^"]+)"' % cid, raw)
             or re.search(r'href="([^"]+)"[^>]*id="%s"' % cid, raw))
        if h:
            href = h.group(1)
    if not href:
        h = (re.search(r'<item[^>]+properties="[^"]*cover-image[^"]*"[^>]*href="([^"]+)"', raw)
             or re.search(r'<item[^>]+href="([^"]+)"[^>]*properties="[^"]*cover-image[^"]*"', raw))
        href = h.group(1) if h else None
    if not href:
        c = [n for n in names if re.search(r'cover.*\.(jpe?g|png)$', n, re.I)]
        href = c[0] if c else None
    if not href:
        return None, None
    for cand in (base + href, href):
        if cand in names:
            try:
                return cand, Image.open(io.BytesIO(z.read(cand)))
            except Exception:
                return cand, None
    return href, None


def colour_variety(im):
    """Roughly how many distinct colours an image uses, 0..1.

    A publisher's colophon is a flat shape on a flat ground - a handful of colours (measured: a Puffin colophon scores 0.08, a real jacket 1.65).
    colours. A jacket is illustrated or photographic and uses thousands. Picking
    "the largest jacket-shaped image" without this chose the Puffin logo over the
    actual cover of a Percy Jackson book, because the logo happened to be a few
    pixels wider.
    """
    try:
        sm = im.convert("RGB").resize((64, 64))
    except Exception:
        return 1.0
    q = {(r >> 4, g >> 4, b >> 4) for r, g, b in sm.getdata()}
    return len(q) / 512.0


def best_jacket(z, exclude, known, threshold):
    """The largest jacket-shaped image in the archive that isn't known filler."""
    best, best_px = None, 0
    for n in z.namelist():
        if not re.search(r"\.(jpe?g|png)$", n, re.I) or n == exclude:
            continue
        try:
            im = Image.open(io.BytesIO(z.read(n)))
        except Exception:
            continue
        w, h = im.size
        if not w or not (RATIO_LO <= h / float(w) <= RATIO_HI):
            continue
        if w < MIN_W or h < MIN_H:
            continue
        if any(dist(ahash(im), kh) <= threshold for kh in known.values()):
            continue
        if colour_variety(im) < 0.5:
            continue                     # a logo or a flat plate, not a jacket
        if w * h > best_px:
            best, best_px = n, w * h
    return best


def declare(path, known, threshold):
    """No usable cover declared: find a real jacket inside and point the OPF at it."""
    z = zipfile.ZipFile(path)
    opf = next((n for n in z.namelist() if n.endswith(".opf")), None)
    if not opf:
        z.close(); return None
    pick = best_jacket(z, None, known, threshold)
    if not pick:
        z.close(); return None
    base = opf.rsplit("/", 1)[0] + "/" if "/" in opf else ""
    href = pick[len(base):] if pick.startswith(base) else pick
    raw = z.read(opf).decode("utf-8", "ignore")
    m = re.search(r'<item[^>]+href="%s"[^>]*id="([^"]+)"' % re.escape(href), raw) or \
        re.search(r'<item[^>]+id="([^"]+)"[^>]*href="%s"' % re.escape(href), raw)
    if m:
        cid = m.group(1)
    else:
        cid = "cover-image-generated"
        raw = raw.replace("</manifest>",
                          '  <item id="%s" href="%s" media-type="image/%s"/>\n</manifest>'
                          % (cid, href, "png" if href.lower().endswith("png") else "jpeg"))
    raw = re.sub(r'\s*<meta[^>]+name="cover"[^>]*/>', "", raw)
    raw = raw.replace("</metadata>", '  <meta name="cover" content="%s"/>\n</metadata>' % cid)
    tmp = path + ".tmp"
    zo = zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED)
    zo.writestr("mimetype", "application/epub+zip", zipfile.ZIP_STORED)
    for i in z.infolist():
        if i.filename == "mimetype":
            continue
        zo.writestr(i, raw.encode("utf-8") if i.filename == opf else z.read(i.filename))
    zo.close(); z.close()
    os.replace(tmp, path)
    return pick


def promote(path, declared, known, threshold):
    """If the declared cover is filler, swap in the best real jacket already
    inside the file. Replaces the bytes at the declared path, so every existing
    reference to it stays valid."""
    z = zipfile.ZipFile(path)
    best, best_px = None, 0
    for n in z.namelist():
        if not re.search(r"\.(jpe?g|png)$", n, re.I) or n == declared:
            continue
        try:
            im = Image.open(io.BytesIO(z.read(n)))
        except Exception:
            continue
        w, h = im.size
        if not w or not (RATIO_LO <= h / float(w) <= RATIO_HI):
            continue
        if any(dist(ahash(im), kh) <= threshold for kh in known.values()):
            continue
        if w * h > best_px:
            best, best_px = n, w * h
    if not best:
        z.close()
        return None
    data = z.read(best)
    tmp = path + ".tmp"
    zo = zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED)
    zo.writestr("mimetype", "application/epub+zip", zipfile.ZIP_STORED)
    for i in z.infolist():
        if i.filename == "mimetype":
            continue
        zo.writestr(i, data if i.filename == declared else z.read(i.filename))
    zo.close(); z.close()
    os.replace(tmp, path)
    return best


def load_known():
    try:
        return json.load(open(HASHES))
    except Exception:
        return {}


def white_fraction(im):
    """How much of the image is bare paper. Cheap, and robust to a black jacket."""
    try:
        g = im.convert("L").resize((120, 150))
    except Exception:
        return 0.0
    px = list(g.getdata())
    return sum(1 for v in px if v > 235) / float(len(px))


def main(argv):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("folder")
    ap.add_argument("--learn", metavar="LABEL",
                    help="record the covers in this folder as known placeholders "
                         "under LABEL instead of checking")
    ap.add_argument("--fix", action="store_true",
                    help="when the declared cover is known filler, promote the best "
                         "real jacket already inside the file")
    ap.add_argument("--list-missing", action="store_true",
                    help="print only the filenames with no usable cover, one a "
                         "line, for --cover-for to consume")
    ap.add_argument("--threshold", type=int, default=6,
                    help="max hash distance to count as the same placeholder (default 6)")
    a = ap.parse_args(argv)

    files = [f for f in sorted(os.listdir(a.folder)) if f.endswith(".epub")]
    known = load_known()

    if a.learn:
        for f in files:
            n, im = cover_of(os.path.join(a.folder, f))
            if im:
                known["%s: %s" % (a.learn, f[:40])] = ahash(im)
        json.dump(known, open(HASHES, "w"), indent=1, sort_keys=True)
        print("recorded %d hashes into %s" % (len(files), HASHES))
        return 0

    seen, issues = {}, []
    for f in files:
        n, im = cover_of(os.path.join(a.folder, f))
        if im is None:
            fixed = declare(os.path.join(a.folder, f), known, a.threshold) if a.fix else None
            if fixed:
                issues.append((f, "no cover was declared - found and declared %s" % fixed))
            else:
                issues.append((f, "no usable cover" if n is None
                               else "cover %s will not decode" % n))
            continue
        w, h = im.size
        hh = ahash(im)
        if "generated" in os.path.basename(n).lower():
            issues.append((f, "generated typographic cover - real jacket still wanted"))
        if w < MIN_W or h < MIN_H:
            issues.append((f, "cover only %dx%d" % (w, h)))
        ratio = h / float(w) if w else 0
        if not (RATIO_LO <= ratio <= RATIO_HI):
            issues.append((f, "cover shape %dx%d is not a jacket" % (w, h)))
        elif white_fraction(im) > 0.55:
            # A jacket is printed edge to edge; an interior page is mostly paper.
            # This is what catches a book whose "cover" is really page one of the
            # comic - which the shape test cannot see, because an interior page
            # has exactly the same trim as the jacket.
            issues.append((f, "cover looks like an interior page, not a jacket "
                              "(%.0f%% blank paper)" % (100 * white_fraction(im))))
        for label, kh in known.items():
            if dist(hh, kh) <= a.threshold:
                fixed = promote(os.path.join(a.folder, f), n, known, a.threshold) \
                    if a.fix else None
                if fixed:
                    issues.append((f, "placeholder art (%s) - promoted %s from inside "
                                      "the file" % (label.split(":")[0], fixed)))
                else:
                    issues.append((f, "placeholder art (%s)" % label.split(":")[0]))
                break
        if hh in seen:
            issues.append((f, "same cover image as %s" % seen[hh][:40]))
        else:
            seen[hh] = f

    if a.list_missing:
        # Only the books with nothing to promote from inside. A poor cover is
        # still a real one, and generating over it would trade a jacket for
        # typography.
        for f, why in issues:
            if why == "no usable cover":
                print(f)
        return 0

    for f, why in issues:
        print("  %-58s %s" % (f[:58], why))
    print("\n%d of %d books have a cover worth looking at" %
          (len({f for f, _ in issues}), len(files)))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
