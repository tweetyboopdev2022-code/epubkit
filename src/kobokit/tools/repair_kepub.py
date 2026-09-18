#!/usr/bin/env python3
"""Repair already-kepub'd files in place: version-correct DOCTYPE, drop the
invalid id on <style> for EPUB2, dedupe stacked stylehack blocks, sync
toc.ncx's dtb:uid to the OPF identifier, and declare any recap TSVs that
are present but undeclared in the manifest. Byte-identical everywhere else.

Unlike kepubify.py --in-place (which only converts books that are NOT yet
kepub), this is for books that already carry koboSpans but were produced
before these fixes existed - it never re-runs the span/wrap transform.

    python3 repair_kepub.py <folder of .kepub.epub files>
"""
import re, os, sys, zipfile, glob

XHTML11 = ('<!DOCTYPE html PUBLIC "-//W3C//DTD XHTML 1.1//EN" '
           '"http://www.w3.org/TR/xhtml11/DTD/xhtml11.dtd">')
HTML5 = "<!DOCTYPE html>"
STYLE_RE = re.compile(
    rb'<style\b[^>]*\bid=["\']kobostylehack[s]?["\'][^>]*>.*?</style>',
    re.S | re.I)


def opf_info(z):
    cont = z.read("META-INF/container.xml").decode("utf-8", "ignore")
    m = re.search(r'full-path="([^"]+)"', cont)
    opf_path = m.group(1)
    raw = z.read(opf_path).decode("utf-8", "ignore")
    ver = (re.search(r'<[^>]*\bpackage\b[^>]*\bversion="([^"]+)"', raw) or [None, "2.0"])[1]
    ident = None
    for mm in re.finditer(r'<dc:identifier\b[^>]*>([^<]+)</dc:identifier>', raw):
        ident = mm.group(1).strip()
        if ident.startswith("urn:isbn:"):
            break
    return opf_path, raw, ver, ident


def fix_doc(data, is_epub3):
    doctype = HTML5 if is_epub3 else XHTML11
    if re.search(rb"<!DOCTYPE[^>]*>", data, re.I):
        data = re.sub(rb"<!DOCTYPE[^>]*>", doctype.encode(), data, count=1, flags=re.I)
    else:
        data = re.sub(rb"(<\?xml[^>]*\?>)", rb"\1\n" + doctype.encode(), data, count=1)
        if b"<?xml" not in data[:80]:
            data = doctype.encode() + b"\n" + data

    blocks = STYLE_RE.findall(data)
    if blocks:
        keep = blocks[0]
        keep_fixed = re.sub(rb'\s+id=["\'][^"\']*["\']', b"", keep, count=1)
        if is_epub3:
            keep_fixed = re.sub(rb"^<style\b", b'<style id="kobostylehacks"', keep_fixed, count=1)
        data = STYLE_RE.sub(b"", data)
        data = re.sub(rb"</head>", keep_fixed + b"</head>", data, count=1, flags=re.I)
    return data


def declare_manifest(namelist, opf_path, opf_raw):
    base = opf_path.rsplit("/", 1)[0] + "/" if "/" in opf_path else ""
    changed = False
    ids = set(re.findall(r'<item\b[^>]*\bid="([^"]+)"', opf_raw))
    additions = []
    for tsv, tag in (("recaps.tsv", "recaps-tsv"), ("cast.tsv", "cast-tsv")):
        full = base + "recaps/" + tsv
        if full not in namelist:
            continue
        href = "recaps/" + tsv
        if ('href="%s"' % href) in opf_raw:
            continue
        rid = tag
        while rid in ids:
            rid += "x"
        ids.add(rid)
        additions.append('    <item id="%s" href="%s" media-type="text/tab-separated-values"/>' % (rid, href))
    if additions:
        opf_raw = re.sub(r"</manifest\s*>", "\n".join(additions) + "\n  </manifest>", opf_raw, count=1)
        changed = True
    return opf_raw, changed


def repair(path):
    z = zipfile.ZipFile(path)
    names = z.namelist()
    opf_path, opf_raw, ver, ident = opf_info(z)
    is_epub3 = str(ver).startswith("3")
    ncx_path = next((n for n in names if n.lower().endswith(".ncx")), None)

    new_opf, manifest_changed = declare_manifest(names, opf_path, opf_raw)

    docs_touched = 0
    tmp = path + ".repairtmp"
    zo = zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED)
    for info in z.infolist():
        data = z.read(info.filename)
        if info.filename == "mimetype":
            zo.writestr(info, data, zipfile.ZIP_STORED); continue
        if info.filename == opf_path:
            data = new_opf.encode("utf-8")
        elif info.filename == ncx_path and ident:
            new_meta = '<meta name="dtb:uid" content="%s"/>' % ident.replace("&", "&amp;")
            txt = data.decode("utf-8", "ignore")
            if re.search(r'<meta\s+name="dtb:uid"[^>]*/?>', txt):
                txt = re.sub(r'<meta\s+name="dtb:uid"[^>]*/?>', new_meta, txt, count=1)
            else:
                txt = re.sub(r"(<head[^>]*>)", r"\1" + new_meta, txt, count=1)
            data = txt.encode("utf-8")
        elif info.filename.lower().endswith((".xhtml", ".html", ".htm")):
            fixed = fix_doc(data, is_epub3)
            if fixed != data:
                docs_touched += 1
            data = fixed
        zo.writestr(info, data)
    zo.close(); z.close()

    if docs_touched or manifest_changed or ident:
        os.replace(tmp, path)
    else:
        os.remove(tmp)
    with zipfile.ZipFile(path) as check:
        bad = check.testzip()
    return docs_touched, manifest_changed, bad


def main(folder):
    files = sorted(glob.glob(os.path.join(folder, "*.epub")))
    total_docs = fixed_manifest = errors = 0
    for f in files:
        try:
            docs, mani, bad = repair(f)
            total_docs += docs
            fixed_manifest += 1 if mani else 0
            if bad:
                errors += 1
                print("CRC FAIL: %s" % os.path.basename(f))
        except Exception as e:
            errors += 1
            print("ERROR %-70s %s: %s" % (os.path.basename(f)[:70], type(e).__name__, e))
    print("\n%d books processed, %d content docs patched, %d manifests declared, %d errors"
          % (len(files), total_docs, fixed_manifest, errors))


if __name__ == "__main__":
    main(sys.argv[1])
