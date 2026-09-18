#!/usr/bin/env python3
"""Drop manifest/spine/TOC entries that point at files not in the archive.

OceanofPDF repackaging keeps the publisher's OPF but silently omits some
documents - a Never Land map, a back-matter ad page. What is left is a spine
that tells the reader to open a file that is not there. Kobo mostly skips it,
but it also corrupts the page count and can wedge the TOC.

Only ever removes a reference whose target is genuinely absent from the zip,
so it cannot lose content: there is no content at the other end. Two guards,
both learned the hard way from rebuild_toc.py writing an empty TOC over a good
one:

  * refuse when more than a quarter of the spine is dead - at that point the
    file is broken in some other way and quietly deleting half the book is not
    a repair
  * refuse to leave fewer than one spine item

Run with --apply; without it, reports and changes nothing.
"""
import sys, os, re, zipfile, argparse, posixpath

MAX_DEAD_FRACTION = 0.25


def dead_ids(z):
    names = set(z.namelist())
    opf = next((n for n in z.namelist() if n.endswith(".opf")), None)
    if not opf:
        return None, None, [], []
    raw = z.read(opf).decode("utf-8", "replace")
    base = posixpath.dirname(opf)
    ids = {}
    for tag in re.findall(r"<item\s[^>]*/?>", raw):
        i = re.search(r'\bid="([^"]+)"', tag)
        h = re.search(r'\bhref="([^"]+)"', tag)
        if i and h:
            ids[i.group(1)] = h.group(1)
    dead = []
    for cid, href in ids.items():
        if re.match(r"^(https?:|data:|mailto:)", href):
            continue
        target = posixpath.normpath(posixpath.join(base, href)) if base else posixpath.normpath(href)
        if target not in names:
            dead.append((cid, href))
    spine = re.findall(r'<itemref[^>]*idref="([^"]+)"', raw)
    return opf, raw, dead, spine


def prune(path, apply):
    z = zipfile.ZipFile(path)
    opf, raw, dead, spine = dead_ids(z)
    if opf is None:
        z.close(); return "no OPF"
    if not dead:
        z.close(); return None
    deadset = {c for c, _ in dead}
    dead_in_spine = [s for s in spine if s in deadset]
    live = [s for s in spine if s not in deadset]
    if spine and len(dead_in_spine) / float(len(spine)) > MAX_DEAD_FRACTION:
        z.close()
        return "REFUSED: %d of %d spine items dead - something else is wrong" % (
            len(dead_in_spine), len(spine))
    if spine and not live:
        z.close(); return "REFUSED: would empty the spine"
    if not apply:
        z.close()
        return "would drop %s" % ", ".join("%s (%s)" % (c, os.path.basename(h)) for c, h in dead)

    hrefs = [os.path.basename(h) for _, h in dead]
    tmp = path + ".tmp"
    zo = zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED)
    zo.writestr("mimetype", "application/epub+zip", zipfile.ZIP_STORED)
    for i in z.infolist():
        if i.filename == "mimetype":
            continue
        data = z.read(i.filename)
        if i.filename == opf:
            t = data.decode("utf-8", "replace")
            for cid in deadset:
                t = re.sub(r'\s*<item\s[^>]*id="%s"[^>]*/?>' % re.escape(cid), "", t)
                t = re.sub(r'\s*<itemref[^>]*idref="%s"[^>]*/?>' % re.escape(cid), "", t)
            for h in hrefs:
                t = re.sub(r'\s*<reference[^>]*href="[^"]*%s"[^>]*/?>' % re.escape(h), "", t)
            data = t.encode("utf-8")
        elif i.filename.endswith(".ncx"):
            t = data.decode("utf-8", "replace")
            for h in hrefs:
                t = re.sub(r'\s*<navPoint\b(?:(?!</navPoint>).)*?%s(?:(?!</navPoint>).)*?</navPoint>'
                           % re.escape(h), "", t, flags=re.S)
            data = t.encode("utf-8")
        zo.writestr(i, data)
    zo.close(); z.close()
    os.replace(tmp, path)
    return "dropped %s" % ", ".join(hrefs)


def main(argv):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("folder")
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args(argv)
    files = [f for f in sorted(os.listdir(a.folder)) if f.endswith(".epub")]
    hits = 0
    for f in files:
        r = prune(os.path.join(a.folder, f), a.apply)
        if r:
            hits += 1
            print("  %-50s %s" % (f[:50], r))
    print("\n%d of %d books had references to files that are not there" % (hits, len(files)))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
