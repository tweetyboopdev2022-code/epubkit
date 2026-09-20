#!/usr/bin/env python3
"""Generate per-chapter recaps and a spoiler-safe cast list for a kepub.

Writes EPUB/recaps/recaps.tsv and EPUB/recaps/cast.tsv back into the book as
(spine index, spine path, label, text).

Chapters come from the OPF spine, not from a filename glob. The spine is what
Nickel itself reads, so the index and the path both match what the device
stores in ChapterIDBookmarked, and books that use .htm or a subdirectory work
the same as Calibre's index_split layout.
"""
import html, json, os, posixpath, re, shutil, subprocess, sys, tempfile, time
import urllib.parse, urllib.request
import xml.etree.ElementTree as ET

import dedupe_cast

ENDPOINT = os.environ.get("RECAP_ENDPOINT",
                          "http://localhost:11434/api/generate")
MODEL = os.environ.get("RECAP_MODEL", "llama3.1:latest")
MINCHARS = 2000
RAW = os.path.join(os.path.dirname(os.path.abspath(__file__)), "raw")

PROMPT = (
    "You are writing a spoiler-safe recap for a reader who has just finished "
    "this chapter and nothing beyond it.\n\n"
    "Rules:\n"
    "- Use ONLY information stated in the chapter below. If you happen to know "
    "this book, ignore that knowledge entirely.\n"
    "- Never reveal what a character turns out to be later; describe them only "
    "as this chapter presents them.\n"
    "- CHARACTERS lists only named individual people. Never list a species, a "
    "creature type, a group, a house, an organisation, an unnamed animal, an "
    "object, a place or a concept.\n"
    "- Each character line gives that person's role in the story: who they are "
    "to the others and what part they play. Never their clothes, their posture, "
    "or what they happened to be doing in one scene.\n"
    "- Never state what becomes of a person. Do not say anyone is killed, "
    "dies, betrays, is tortured, is imprisoned, is revealed as something, or "
    "turns out to be anything. Describe only who they are.\n"
    "- Write in the same language as the chapter. A French chapter gets a "
    "French summary and French character lines, throughout.\n"
    "- Plain text only. No markdown, no asterisks, no bold.\n\n"
    "Output exactly this shape and nothing else:\n"
    "SUMMARY: <2 to 3 sentences, past tense>\n"
    "CHARACTERS:\n"
    "<Name>: <role in the story, 4 to 10 words>\n\n"
    "The character lines should read like these invented examples:\n"
    "Marta Kellen: the innkeeper's daughter, hides the travellers\n"
    "A. Sorrel: the magistrate who signed the order\n\n"
    "CHAPTER:\n"
)


def localname(e):
    return e.tag.split("}")[-1] if isinstance(e.tag, str) else ""


def opf_path(work):
    tree = ET.parse(os.path.join(work, "META-INF", "container.xml"))
    for e in tree.iter():
        if localname(e) == "rootfile" and e.get("full-path"):
            return e.get("full-path")
    raise SystemExit("no rootfile in container.xml")


def spine_and_titles(work):
    rel_opf = opf_path(work)
    base = posixpath.dirname(rel_opf)
    tree = ET.parse(os.path.join(work, rel_opf))

    manifest, order, ncx_id = {}, [], None
    for e in tree.iter():
        n = localname(e)
        if n == "item":
            manifest[e.get("id")] = urllib.parse.unquote(e.get("href") or "")
        elif n == "itemref":
            order.append(e.get("idref"))
        elif n == "spine" and e.get("toc"):
            ncx_id = e.get("toc")

    def resolve(href):
        return posixpath.normpath(posixpath.join(base, href)) if base else href

    spine = []
    for idref in order:
        href = manifest.get(idref)
        if href:
            spine.append(resolve(href))

    titles = {}
    toc = manifest.get(ncx_id) if ncx_id else None
    for cand in ([toc] if toc else []) + [h for h in manifest.values()
                                          if h.endswith(".ncx") or "nav" in h.lower()]:
        p = os.path.join(work, resolve(cand))
        if not os.path.isfile(p):
            continue
        try:
            t = ET.parse(p)
        except ET.ParseError:
            continue
        for e in t.iter():
            if localname(e) in ("navPoint", "li", "a"):
                label = " ".join(x.strip() for x in e.itertext() if x.strip())
                src = e.get("href") or ""
                if not src:
                    for c in e.iter():
                        if localname(c) in ("content", "a"):
                            src = c.get("src") or c.get("href") or ""
                            if src:
                                break
                if src and label:
                    key = resolve(urllib.parse.unquote(src).split("#")[0])
                    titles.setdefault(key, label[:60])
        if titles:
            break
    return spine, titles


# Front matter passes the length test and gets summarised as chapter one,
# which also drags reviewers and blurb writers into the cast.
FRONT = ("praise for", "copyright", "all rights reserved", "table of contents",
         "also by", "about the author", "dedication", "acknowledgment",
         "acknowledgement", "a note on the type", "first published")


def is_front_matter(label, text):
    if not label.startswith("Section "):
        return False
    head = text[:1200].lower()
    return any(m in head for m in FRONT)


def chapter_text(path):
    s = open(path, encoding="utf-8", errors="ignore").read()
    s = re.sub(r"(?is)<(script|style).*?</\1>", " ", s)
    s = re.sub(r"(?s)<[^>]+>", " ", s)
    return re.sub(r"\s+", " ", html.unescape(s)).strip()


def ask(text):
    # A chapter that times out used to raise straight out of the book, losing
    # every chapter already summarised. Two jobs sharing one GPU makes that
    # routine, so a slow chapter is retried and then given up on alone.
    # num_predict defaults to unlimited, and the model does sometimes loop: one
    # chapter reached 44,000 tokens at 55 tok/s before this cap existed, which
    # is thirteen minutes for output that is never longer than a few hundred.
    body = json.dumps({"model": MODEL, "prompt": PROMPT + text, "stream": False,
                       "options": {"num_ctx": 16384, "temperature": 0.2,
                                   "num_predict": 700}}).encode()
    for attempt in (1, 2, 3):
        req = urllib.request.Request(ENDPOINT, data=body,
                                     headers={"Content-Type": "application/json"})
        try:
            return json.loads(urllib.request.urlopen(req, timeout=900).read()).get("response", "")
        except Exception as exc:
            print("    attempt %d failed: %s" % (attempt, exc), flush=True)
            if attempt == 3:
                return ""
            time.sleep(20)
    return ""


def strip_weak_alias(name, desc):
    # The model emits an alias that is just a token of the name, or the
    # words with nothing after them. Both are noise on the page.
    m = re.search(r",?\s*also\s+called\s*(.*)$", desc, re.I)
    if not m:
        return desc
    alias = m.group(1).strip(" .,'\"")
    head = desc[:m.start()].rstrip(" ,")
    if not alias:
        return head
    own = {t.lower().strip(".,") for t in name.split()}
    ali = {t.lower().strip(".,") for t in alias.split()}
    if ali & own or not ali:
        return head
    return head + ", also called " + alias


def parse(out):
    summary, chars = "", []
    out = re.sub(r"\*+", "", out)
    m = re.search(r"SUMMARY\s*:\s*(.+?)(?:\n?\s*CHARACTERS\s*:|\Z)", out, re.S | re.I)
    if m:
        summary = re.sub(r"\s+", " ", m.group(1)).strip()
    m = re.search(r"CHARACTERS\s*:\s*(.+)\Z", out, re.S | re.I)
    if m:
        for line in m.group(1).splitlines():
            line = line.strip().lstrip("-*0123456789. ").strip()
            if not line or ":" not in line:
                continue
            name, desc = line.split(":", 1)
            name, desc = name.strip(), re.sub(r"\s+", " ", desc).strip()
            bad = ("trunk", "letter", "room", "house", "book", "wand", "door",
                   "the ", "chapter", "place", "office", "school", "castle")
            desc = strip_weak_alias(name, desc)
            if (1 < len(name) < 40 and desc
                    and not any(b in name.lower() for b in bad)
                    and name[0].isupper()):
                chars.append((name, desc))
    return summary, chars


def write_rows(path, rows):
    with open(path, "w", encoding="utf-8") as fh:
        for order, fname, a, b in rows:
            fh.write("%d\t%s\t%s\t%s\n" % (order, fname, a, b))


def declare_recaps_in_manifest(work):
    """EPUB 2 lets a manifest omit files nobody's spine references, but EPUB 3
    requires every resource in the container to be declared - so recaps.tsv
    and cast.tsv must get <item> entries or the book fails EPUB 3 validation
    even though the file works fine on the device either way."""
    rel_opf = opf_path(work)
    full = os.path.join(work, rel_opf)
    xml = open(full, "r", encoding="utf-8").read()
    ids = set(re.findall(r'<item\b[^>]*\bid="([^"]+)"', xml))
    rid, cid = "recaps-tsv", "cast-tsv"
    while rid in ids:
        rid += "x"
    while cid in ids:
        cid += "x"
    items = ('    <item id="%s" href="recaps/recaps.tsv" media-type="text/tab-separated-values"/>\n'
             '    <item id="%s" href="recaps/cast.tsv" media-type="text/tab-separated-values"/>\n'
             '  </manifest>' % (rid, cid))
    # hrefs above are relative to the OPF's own directory (EPUB spec rule),
    # so the files on disk/in the zip must sit under that same directory -
    # not a hardcoded "EPUB", which is wrong for any book whose content
    # folder is actually named something else (e.g. "OEBPS").
    new_xml, n = re.subn(r'</manifest\s*>', items, xml, count=1)
    if n == 1:
        open(full, "w", encoding="utf-8").write(new_xml)
    return n == 1


def main():
    book, dest = sys.argv[1], os.path.abspath(sys.argv[2])
    work = tempfile.mkdtemp()
    subprocess.run(["unzip", "-o", "-q", book, "-d", work], check=True)
    # Some books store container.xml unreadable and unzip preserves that mode.
    subprocess.run(["chmod", "-R", "u+rwX", work], check=False)
    spine, titles = spine_and_titles(work)
    print("spine: %d documents, %d toc titles" % (len(spine), len(titles)), flush=True)

    recaps, raw_cast = [], []
    for order, rel in enumerate(spine):
        p = os.path.join(work, rel)
        if not os.path.isfile(p):
            continue
        t = chapter_text(p)
        if len(t) < MINCHARS:
            continue
        m = re.match(r"(?:unnamed\s+)?(Chapter\s+\d+)", t)
        label = m.group(1) if m else titles.get(rel, "Section %d" % order)
        if is_front_matter(label, t):
            print("%3d skip front matter %s" % (order, rel), flush=True)
            continue
        t0 = time.time()
        summary, chars = parse(ask(t))
        print("%3d %-40s %5.1fs  %d chars, %d people" %
              (order, label[:40], time.time() - t0, len(summary), len(chars)), flush=True)
        summary = re.split(r"CHARACTERS\s*:", summary, flags=re.I)[0].strip()
        if summary:
            recaps.append((order, rel, label, summary))
        for person, desc in chars:
            raw_cast.append((order, rel, person, desc))

    if not recaps:
        print("no chapters long enough to summarise; book left unchanged", flush=True)
        shutil.rmtree(work)
        return

    os.makedirs(RAW, exist_ok=True)
    stem = re.sub(r"[^A-Za-z0-9]+", "_", os.path.basename(book))[:60]
    write_rows(os.path.join(RAW, stem + ".recaps.tsv"), recaps)
    write_rows(os.path.join(RAW, stem + ".cast.tsv"), raw_cast)

    cast = dedupe_cast.collapse(raw_cast)
    content_base = posixpath.dirname(opf_path(work))
    recaps_rel = posixpath.join(content_base, "recaps") if content_base else "recaps"
    out = os.path.join(work, *recaps_rel.split("/"))
    os.makedirs(out, exist_ok=True)
    write_rows(os.path.join(out, "recaps.tsv"), recaps)
    write_rows(os.path.join(out, "cast.tsv"), cast)
    declared = declare_recaps_in_manifest(work)
    print("wrote %d recaps, %d cast rows, %d people (manifest declared: %s)"
          % (len(recaps), len(cast), len({r[2] for r in cast}), declared), flush=True)

    shutil.copyfile(book, dest)
    subprocess.run(["zip", "-q", dest, opf_path(work),
                    recaps_rel + "/recaps.tsv", recaps_rel + "/cast.tsv"],
                   cwd=work, check=True)
    print("updated book written to", dest, flush=True)
    shutil.rmtree(work)


if __name__ == "__main__":
    main()
