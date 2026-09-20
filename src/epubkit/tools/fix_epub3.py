#!/usr/bin/env python3
"""Bring an already-kepub'd book up to valid EPUB 3, in place or to an out dir.

Why: most of this library declares <package version="2.0"> while the content
inside is EPUB3/HTML5 - epub:type, role, aria-*, properties, refines, the HTML5
doctype. EPUBCheck validates content against whatever the package declares, so
every one of those becomes an error, and the count scales with how much EPUB3
the book actually uses. Promoting the declaration to 3.0 and finishing the job
is the fix that removes the whole class rather than papering over it.

What it changes, all of it mechanical:
  - <package> version -> 3.0, with the dc/opf namespaces it needs
  - a dcterms:modified meta (EPUB3 requires exactly one)
  - opf:role / opf:file-as / opf:scheme -> <meta refines=...> equivalents
  - a nav document built from the existing NCX, declared properties="nav"
  - <meta name="cover"> -> properties="cover-image" on the manifest item
  - doctype of every content document -> <!DOCTYPE html>
  - the epub: namespace declared on <html> wherever epub:type is used
  - properties="svg"/"scripted"/"mathml" declared where the content uses them
  - obsolete presentational attributes (align, valign, border...) dropped
  - toc.ncx dtb:uid synced to the OPF unique-identifier

It never touches the text, the spine order, the images, or the koboSpans.

    python3 fix_epub3.py <book.epub|folder> [--out DIR] [--inplace] [--json F]
"""
import argparse, copy, datetime, os, re, shutil, sys, zipfile
from lxml import etree

OPF_NS = "http://www.idpf.org/2007/opf"
DC_NS = "http://purl.org/dc/elements/1.1/"
NCX_NS = "http://www.daisy.org/z3986/2005/ncx/"
XHTML_NS = "http://www.w3.org/1999/xhtml"
EPUB_NS = "http://www.idpf.org/2007/ops"

OPF = "{%s}" % OPF_NS
DC = "{%s}" % DC_NS
NCX = "{%s}" % NCX_NS

# Attributes HTML5 dropped. They are presentational only - removing them cannot
# change what the text says, and Kobo applies its own typography anyway.
OBSOLETE_ATTRS = {
    "align", "valign", "border", "cellpadding", "cellspacing", "hspace",
    "vspace", "frame", "rules", "bgcolor", "background", "compact", "clear",
    "nowrap", "size", "noshade", "link", "vlink", "alink", "text", "char",
    "charoff", "axis", "abbr", "summary", "scrolling", "frameborder",
    "marginwidth", "marginheight", "longdesc", "language", "topmargin",
    "leftmargin", "marginbottom", "marginright", "width", "height",
    "value", "shape", "coords",
}
# ...except where the attribute is legitimate on that element in HTML5.
# width/height are real on replaced elements and obsolete on everything else,
# which is why they are listed above and rescued here rather than skipped.
OBSOLETE_KEEP = {
    ("input", "size"), ("select", "size"), ("hr", "size"),
    ("col", "span"), ("colgroup", "span"), ("th", "abbr"),
    ("img", "border"),
    ("img", "width"), ("img", "height"),
    ("canvas", "width"), ("canvas", "height"),
    ("embed", "width"), ("embed", "height"),
    ("iframe", "width"), ("iframe", "height"),
    ("object", "width"), ("object", "height"),
    ("video", "width"), ("video", "height"),
    ("input", "width"), ("input", "height"),
    ("svg", "width"), ("svg", "height"),
    ("image", "width"), ("image", "height"),
    ("rect", "width"), ("rect", "height"),
    ("use", "width"), ("use", "height"),
    ("pattern", "width"), ("pattern", "height"),
    ("mask", "width"), ("mask", "height"),
    ("filter", "width"), ("filter", "height"),
    ("foreignObject", "width"), ("foreignObject", "height"),
    ("source", "width"), ("source", "height"),
    # value is real on form controls, list items and params, obsolete elsewhere
    ("input", "value"), ("option", "value"), ("button", "value"),
    ("li", "value"), ("param", "value"), ("progress", "value"),
    ("meter", "value"), ("data", "value"), ("textarea", "value"),
    ("select", "value"), ("output", "value"),
    # shape/coords belong to <area>, not to <a>
    ("area", "shape"), ("area", "coords"),
    ("meta", "value"),   # rewritten to content= earlier, never dropped here
}

CONTENT_RE = re.compile(r"\.(x?html?|xhtm)$", re.I)

# A tag, with its attributes captured as one run, so they can be tokenised
# properly rather than pattern-matched inside the whole tag. Closing tags,
# comments, doctypes and CDATA all fail the first character class and are
# left untouched.
TAG_RE = re.compile(
    rb"<([a-zA-Z][-\w:.]*)"
    rb"((?:\s+[^\s=/>]+(?:\s*=\s*(?:\"[^\"]*\"|'[^']*'|[^\s>]+))?)*)"
    rb"\s*(/?)\s*>")
ATTR_RE = re.compile(
    rb"([^\s=/>]+)(?:\s*=\s*(\"[^\"]*\"|'[^']*'|[^\s>]+))?")


def strip_obsolete_attrs(mo):
    """Drop obsolete attributes from one tag, by attribute, never by text.

    The value side is never searched. That matters because
    <meta name="viewport" content="width=1200, height=1600"/> contains the
    literal text ' height=1600' inside a quoted value, and a regex looking
    for ' height=...' across the tag happily ate it - which turned every
    page of a fixed-layout comic into a fatal parse error.
    """
    tag, attrs_txt, slash = mo.group(1), mo.group(2) or b"", mo.group(3) or b""
    low = tag.lower().decode("ascii", "ignore")
    kept, dropped = [], False
    for am in ATTR_RE.finditer(attrs_txt):
        name = am.group(1).lower().decode("ascii", "ignore")
        if name in OBSOLETE_ATTRS and (low, name) not in OBSOLETE_KEEP:
            dropped = True
            continue
        # width/height survive on replaced elements only as plain integers;
        # height="100%" is a presentational leftover HTML5 rejects
        if name in ("width", "height"):
            v = (am.group(2) or b"").strip(b"\"'")
            if v and not v.isdigit():
                dropped = True
                continue
        kept.append(am.group(0).strip())
    if not dropped:
        return mo.group(0)
    out = b"<" + tag
    if kept:
        out += b" " + b" ".join(kept)
    return out + (b"/>" if slash else b">")


def is_content(name):
    return bool(CONTENT_RE.search(name))


# --------------------------------------------------------------------------
# OPF


def parse_xml(data):
    p = etree.XMLParser(recover=True, resolve_entities=False, huge_tree=True)
    return etree.fromstring(data, parser=p)


def opf_path_of(z):
    cont = z.read("META-INF/container.xml")
    root = parse_xml(cont)
    for rf in root.iter():
        if rf.tag.endswith("rootfile") and rf.get("full-path"):
            return rf.get("full-path")
    raise ValueError("no rootfile in container.xml")


def _meta(parent, **attrs):
    m = etree.SubElement(parent, OPF + "meta")
    for k, v in attrs.items():
        m.set(k.replace("_", "-"), v)
    return m


def fix_opf(root, uid_hint, content_props, cover_href, nav_href, has_ncx,
            drop_ncx_href=None, undeclared=None, linked=None):
    """Return (opf_root, unique_identifier_value). Mutates in place."""
    changed = []

    root.set("version", "3.0")

    # ids in the OPF must be XML NAMEs too, and some manifests here use bare
    # UUIDs that start with a digit. Renaming one means renaming everything
    # that points at it, so do both in one pass.
    remap = {}
    used = {el.get("id") for el in root.iter() if el.get("id")}
    for el in root.iter():
        v = el.get("id")
        if not v:
            continue
        # An XML NAME allows letters, digits, '.', '-' and '_', and must not
        # start with a digit. These manifests carry ids like
        # "Card,_Orson_Scott_-_Ender_8_-_Shadow_of_the_Giant1" - the comma is
        # what makes it invalid, not the length.
        new = re.sub(r"[^A-Za-z0-9._-]", "-", v)
        if new[:1] and not (new[:1].isalpha() or new[:1] == "_"):
            new = "id-" + new
        if new != v:
            base, k = new, 1
            while new in used:
                new = "%s-%d" % (base, k)
                k += 1
            used.add(new)
            remap[v] = new
            el.set("id", new)
    if remap:
        for el in root.iter():
            ref = el.get("idref")
            if ref in remap:
                el.set("idref", remap[ref])
            for attr in ("refines", "content"):
                val = el.get(attr)
                if val and val.startswith("#") and val[1:] in remap:
                    el.set(attr, "#" + remap[val[1:]])
            if el.get("toc") in remap:
                el.set("toc", remap[el.get("toc")])
        if root.get("unique-identifier") in remap:
            root.set("unique-identifier", remap[root.get("unique-identifier")])
        changed.append("opf-ids(%d)" % len(remap))

    md = root.find(OPF + "metadata")
    if md is None:
        md = root.find("metadata")
    manifest = root.find(OPF + "manifest")
    spine = root.find(OPF + "spine")

    # -- unique identifier -------------------------------------------------
    uid_attr = root.get("unique-identifier")
    ident_el = None
    for el in md.findall(DC + "identifier"):
        if uid_attr and el.get("id") == uid_attr:
            ident_el = el
            break
    if ident_el is None:
        ids = md.findall(DC + "identifier")
        ident_el = ids[0] if ids else None
    if ident_el is None:
        ident_el = etree.SubElement(md, DC + "identifier")
        ident_el.text = uid_hint
    if not ident_el.get("id"):
        ident_el.set("id", "pub-id")
    root.set("unique-identifier", ident_el.get("id"))
    uid_value = (ident_el.text or uid_hint or "").strip()

    # -- opf:scheme / opf:role / opf:file-as -> meta refines ----------------
    n_refines = 0
    seq = [0]
    # a refines property may only be declared once per expression, and some
    # books already carry the EPUB3 form alongside the EPUB2 attribute
    existing_refines = {(m.get("refines"), m.get("property"))
                        for m in md.findall(OPF + "meta")
                        if m.get("refines") and m.get("property")}

    def want_refines(eid, prop):
        key = ("#" + eid, prop)
        if key in existing_refines:
            return False
        existing_refines.add(key)
        return True

    def ensure_id(el, stem):
        if not el.get("id"):
            seq[0] += 1
            el.set("id", "%s-%d" % (stem, seq[0]))
        return el.get("id")

    for el in list(md):
        tag = etree.QName(el).localname if isinstance(el.tag, str) else ""
        scheme = el.get(OPF + "scheme")
        if scheme is not None:
            del el.attrib[OPF + "scheme"]
            if tag == "identifier" and scheme.lower() == "isbn":
                eid = ensure_id(el, "id")
                if want_refines(eid, "identifier-type"):
                    _meta(md, refines="#" + eid, property="identifier-type",
                          scheme="onix:codelist5").text = "15"
                    n_refines += 1
            changed.append("opf:scheme")
        role = el.get(OPF + "role")
        if role is not None:
            del el.attrib[OPF + "role"]
            eid = ensure_id(el, tag or "cr")
            if want_refines(eid, "role"):
                m = _meta(md, refines="#" + eid, property="role",
                          scheme="marc:relators")
                m.text = role
                n_refines += 1
            changed.append("opf:role")
        fa = el.get(OPF + "file-as")
        if fa is not None:
            del el.attrib[OPF + "file-as"]
            eid = ensure_id(el, tag or "cr")
            if want_refines(eid, "file-as"):
                _meta(md, refines="#" + eid, property="file-as").text = fa
                n_refines += 1
            changed.append("opf:file-as")
        # opf:event/opf:scheme on dc:date is EPUB2-only; drop the attribute
        for a in list(el.attrib):
            if a.startswith(OPF) and etree.QName(a).localname in (
                    "event", "authority", "term"):
                del el.attrib[a]
                changed.append("opf:" + etree.QName(a).localname)

    # -- dcterms:modified --------------------------------------------------
    have_mod = [m for m in md.findall(OPF + "meta")
                if m.get("property") == "dcterms:modified"]
    for extra in have_mod[1:]:
        md.remove(extra)
    if not have_mod:
        _meta(md, property="dcterms:modified").text = (
            datetime.datetime.now(datetime.timezone.utc)
            .strftime("%Y-%m-%dT%H:%M:%SZ"))
        changed.append("dcterms:modified")

    # -- dc:date must be a date, and only one is allowed in EPUB3 ----------
    dates = md.findall(DC + "date")
    for extra in dates[1:]:
        md.remove(extra)
        changed.append("extra dc:date")
    if dates:
        t = (dates[0].text or "").strip()
        if not re.match(r"^\d{4}(-\d{2}(-\d{2})?)?([T ].*)?$", t):
            md.remove(dates[0])
            changed.append("bad dc:date")

    # -- legacy <meta name="cover"> -> properties="cover-image" -------------
    legacy_cover = [m for m in md.findall(OPF + "meta")
                    if m.get("name") == "cover"]
    cover_id = legacy_cover[0].get("content") if legacy_cover else None
    for m in legacy_cover:
        md.remove(m)
        changed.append("meta name=cover")

    # -- manifest ----------------------------------------------------------
    if drop_ncx_href:
        for it in list(manifest.findall(OPF + "item")):
            if it.get("href") == drop_ncx_href or \
                    (it.get("media-type") or "") == "application/x-dtbncx+xml":
                manifest.remove(it)
        changed.append("drop ncx")

    items = manifest.findall(OPF + "item")
    by_id = {i.get("id"): i for i in items}
    by_href = {i.get("href"): i for i in items}

    # properties on content documents
    for it in items:
        href = it.get("href") or ""
        props = set((it.get("properties") or "").split())
        props.discard("")
        want = content_props.get(href, set())
        # nav is handled below; never invent one here
        want = {p for p in want if p in ("svg", "scripted", "mathml", "remote-resources")}
        if want - props:
            props |= want
            changed.append("properties")
        # a stale property claim is an error too
        for p in ("svg", "scripted", "mathml"):
            if p in props and p not in want:
                props.discard(p)
                changed.append("stale properties")
        if props:
            it.set("properties", " ".join(sorted(props)))
        elif it.get("properties") is not None:
            del it.attrib["properties"]

    # cover-image: exactly one, or none at all
    have_cover = [i for i in items
                  if "cover-image" in (i.get("properties") or "").split()]
    for extra in have_cover[1:]:
        props = set(extra.get("properties").split()) - {"cover-image"}
        if props:
            extra.set("properties", " ".join(sorted(props)))
        else:
            del extra.attrib["properties"]
        changed.append("duplicate cover-image")
    if not have_cover:
        target = by_id.get(cover_id) if cover_id else None
        if target is None and cover_href:
            target = by_href.get(cover_href)
        if target is not None and (target.get("media-type") or "").startswith("image/"):
            props = set((target.get("properties") or "").split())
            props.discard("")
            props.add("cover-image")
            target.set("properties", " ".join(sorted(props)))
            changed.append("cover-image")

    # resources the content links to that were never declared (RSC-008)
    for href, mtype in sorted((undeclared or {}).items()):
        if href in by_href:
            continue
        it = etree.SubElement(manifest, OPF + "item")
        it.set("id", "decl-" + re.sub(r"[^A-Za-z0-9]", "-", href).strip("-")[:48])
        it.set("href", href)
        it.set("media-type", mtype)
        by_href[href] = it
        changed.append("declared " + href)
    items = manifest.findall(OPF + "item")
    by_id = {i.get("id"): i for i in items}

    # exactly one nav
    navs = [i for i in items if "nav" in (i.get("properties") or "").split()]
    if len(navs) > 1:
        for extra in navs[1:]:
            props = set(extra.get("properties").split()) - {"nav"}
            if props:
                extra.set("properties", " ".join(sorted(props)))
            else:
                del extra.attrib["properties"]
        changed.append("duplicate nav")
    if not navs:
        it = by_href.get(nav_href)
        if it is None:
            it = etree.SubElement(manifest, OPF + "item")
            it.set("id", "nav-generated")
            it.set("href", nav_href)
            it.set("media-type", "application/xhtml+xml")
        props = set((it.get("properties") or "").split())
        props.discard("")
        props.add("nav")
        it.set("properties", " ".join(sorted(props)))
        changed.append("nav")

    # -- spine reachability -------------------------------------------------
    # EPUB3 ties these together: a content document linked from the text but
    # missing from the spine is RSC-011, and a linear="no" spine entry that
    # nothing links to is OPF-096. Both are decided by what the text actually
    # links to, so resolve them from the same set.
    if linked is not None and spine is not None:
        xhtml = {i.get("href"): i for i in manifest.findall(OPF + "item")
                 if (i.get("media-type") or "") == "application/xhtml+xml"}
        in_spine = {ir.get("idref") for ir in spine.findall(OPF + "itemref")}
        for href, it in sorted(xhtml.items()):
            if href == nav_href or it.get("id") in in_spine:
                continue
            if href in linked:
                ir = etree.SubElement(spine, OPF + "itemref")
                ir.set("idref", it.get("id"))
                ir.set("linear", "no")
                changed.append("spine+ " + href)
        by_id_all = {i.get("id"): i for i in manifest.findall(OPF + "item")}
        for ir in list(spine.findall(OPF + "itemref")):
            if (ir.get("linear") or "").lower() != "no":
                continue
            it = by_id_all.get(ir.get("idref"))
            href = it.get("href") if it is not None else None
            if href and href not in linked and href != nav_href:
                spine.remove(ir)
                changed.append("spine- " + href)

    # page-map is an Adobe EPUB2 extension the EPUB3 schema rejects
    if spine is not None and spine.get("page-map"):
        del spine.attrib["page-map"]
        changed.append("page-map")
    for it in list(manifest.findall(OPF + "item")):
        if (it.get("media-type") or "") == "application/oebps-page-map+xml":
            manifest.remove(it)
            changed.append("page-map item")

    # -- spine toc ----------------------------------------------------------
    if has_ncx:
        ncx_item = next((i for i in manifest.findall(OPF + "item")
                         if (i.get("media-type") or "") ==
                         "application/x-dtbncx+xml"), None)
        if ncx_item is not None and spine is not None:
            spine.set("toc", ncx_item.get("id"))
    elif spine is not None and spine.get("toc"):
        del spine.attrib["toc"]

    # <guide> and <tours> are EPUB2 constructs the nav document supersedes.
    for legacy in ("guide", "tours"):
        el = root.find(OPF + legacy)
        if el is not None:
            root.remove(el)
            changed.append(legacy)

    return uid_value, changed


# --------------------------------------------------------------------------
# nav document


def nav_from_ncx(ncx_bytes, nav_href, opf_dir, title="Contents",
                 spine_hrefs=None, spine_order=None, spine_titles=None,
                 valid_frag=None):
    """Build an EPUB3 nav document mirroring the NCX's navMap.

    Only targets that are actually spine documents are kept: a nav entry
    pointing at a non-spine resource is RSC-011, and EPUB2 NCXs in this
    library do point at cover pages that were never in the spine."""
    items = []
    spine_titles = spine_titles or {}
    if ncx_bytes:
        try:
            root = parse_xml(ncx_bytes)
            nm = root.find(NCX + "navMap")
            if nm is None:
                nm = root.find("navMap")

            def walk(parent):
                out = []
                for np in parent:
                    if not str(np.tag).endswith("navPoint"):
                        continue
                    lbl = ""
                    for t in np.iter():
                        if str(t.tag).endswith("text"):
                            lbl = (t.text or "").strip()
                            break
                    src = ""
                    for c in np.iter():
                        if str(c.tag).endswith("content") and c.get("src"):
                            src = c.get("src")
                            break
                    kids = walk(np)
                    base, _, frag = src.partition("#")
                    # a fragment that does not exist in the target is RSC-012,
                    # and dropping just the fragment still lands the reader on
                    # the right document
                    if frag and valid_frag is not None and \
                            frag not in valid_frag.get(base, set()):
                        src = base
                    if src and (spine_hrefs is None or base in spine_hrefs):
                        out.append((lbl or "Untitled", src, kids))
                    else:
                        out.extend(kids)
                return out

            if nm is not None:
                items = walk(nm)
        except Exception:
            items = []

    def render(nodes, depth=2):
        pad = "  " * depth
        s = pad + "<ol>\n"
        for lbl, src, kids in nodes:
            # nav hrefs are relative to the nav document, which sits beside
            # the OPF - the same base the NCX used, so src carries over.
            s += pad + '  <li><a href="%s">%s</a>' % (esc_attr(src), esc(lbl))
            if kids:
                s += "\n" + render(kids, depth + 2) + pad + "  "
            s += "</li>\n"
        s += pad + "</ol>\n"
        return s

    if not items and spine_hrefs:
        # No usable NCX (missing, or malformed past repair). The spine itself
        # is always a valid table of contents, and a nav pointing at real
        # spine documents beats a placeholder link that resolves to nothing.
        items = [(spine_titles.get(h, "Section %d" % (i + 1)), h, [])
                 for i, h in enumerate(spine_order or sorted(spine_hrefs))]
    if not items:
        items = [(title, "", [])]
    body = render(items)

    return ('<?xml version="1.0" encoding="utf-8"?>\n'
            '<!DOCTYPE html>\n'
            '<html xmlns="http://www.w3.org/1999/xhtml" '
            'xmlns:epub="http://www.idpf.org/2007/ops">\n'
            '<head>\n  <meta charset="utf-8"/>\n  <title>%s</title>\n</head>\n'
            '<body>\n<nav epub:type="toc" id="toc" role="doc-toc">\n'
            '  <h1>%s</h1>\n%s</nav>\n</body>\n</html>\n'
            % (esc(title), esc(title), body)).encode("utf-8")


def esc(s):
    return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def esc_attr(s):
    return esc(s).replace('"', "&quot;")


# --------------------------------------------------------------------------
# content documents


DOCTYPE_RE = re.compile(rb"<!DOCTYPE[^>\[]*(\[[^\]]*\])?[^>]*>", re.I)
HTML_OPEN_RE = re.compile(rb"<html\b[^>]*>", re.I)
# <meta http-equiv="Content-Type" content="text/html; charset=utf-8"/> is how
# XHTML 1.1 declared its encoding. HTML5 rejects it in favour of <meta charset>,
# so a book that was valid EPUB2 fails the moment the doctype changes - which is
# what turned an already-clean Harry Potter into 32 errors on the first run.
HTTP_EQUIV_CT_RE = re.compile(
    rb"<meta\b[^>]*http-equiv\s*=\s*[\"']?content-type[\"']?[^>]*>", re.I)
CHARSET_META_RE = re.compile(rb"<meta\b[^>]*\bcharset\s*=\s*[\"'][^\"']*[\"'][^>]*>", re.I)
ENTITY_RE = re.compile(rb"&([a-zA-Z][a-zA-Z0-9]{1,31});")
# The only named entities XML itself declares. Everything else came from the
# XHTML DTD, and <!DOCTYPE html> does not pull in a DTD - so &nbsp; and friends
# become undeclared-entity fatals unless they are made numeric first.
XML_SAFE = {b"amp", b"lt", b"gt", b"quot", b"apos"}
_ENT_MAP = None


def _entity_map():
    global _ENT_MAP
    if _ENT_MAP is None:
        from html.entities import html5 as _h5
        m = {}
        for name, char in _h5.items():
            if not name.endswith(";"):
                continue
            key = name[:-1].encode("ascii", "ignore")
            if key and key not in XML_SAFE:
                m[key] = ("&#%d;" % ord(char[0])).encode() if len(char) == 1 \
                    else "".join("&#%d;" % ord(c) for c in char).encode()
        _ENT_MAP = m
    return _ENT_MAP


def numericise_entities(data):
    """&nbsp; -> &#160;, so the content survives losing the XHTML DTD."""
    m = _entity_map()

    def sub(mo):
        name = mo.group(1)
        if name in XML_SAFE:
            return mo.group(0)
        rep = m.get(name)
        return rep if rep else mo.group(0)

    return ENTITY_RE.sub(sub, data)


HTTP_EQUIV_OK = {b"content-type", b"default-style", b"refresh",
                 b"x-ua-compatible", b"content-security-policy"}
HTTP_EQUIV_ANY_RE = re.compile(
    rb'<meta\b[^>]*?\shttp-equiv\s*=\s*(["\'])([^"\']*)\1[^>]*?/?>', re.I)
# both spellings occur: <title></title> and the self-closing <title/>
EMPTY_TITLE_RE = re.compile(
    rb"<title(?:\s[^>/]*)?/>|<title(?:\s[^>]*)?>\s*</title>", re.I)


#: Elements that may not appear inside a phrasing-only element. A converter
#: that wraps a whole page in <span class="underline"> and then drops the
#: page-break <div> inside it produces markup no EPUB version accepts, so
#: promoting the package cannot fix it.
BLOCK_IN_INLINE = (b"div", b"p", b"h1", b"h2", b"h3", b"h4", b"h5", b"h6",
                   b"table", b"ul", b"ol", b"dl", b"blockquote", b"hr",
                   b"pre", b"section", b"article", b"aside", b"figure")
INLINE_WRAPPERS = (b"span", b"a", b"b", b"i", b"em", b"strong", b"small",
                   b"sub", b"sup", b"u", b"cite", b"code", b"label")
#: Blocks that may only hold phrasing content. These are not unwrapped -
#: they carry the paragraph's styling and structure - they become <div>,
#: which holds the same content legally.
PHRASING_ONLY_BLOCKS = (b"p", b"h1", b"h2", b"h3", b"h4", b"h5", b"h6")
_TAG_SCAN = re.compile(rb"<(/?)([A-Za-z][-\w:.]*)([^>]*?)(/?)>")


def unwrap_inline_around_block(data):
    """Remove an inline wrapper that has a block element inside it.

    Only the wrapper's own tags go; every byte between them is kept, so the
    text, the images and any koboSpans inside are untouched. The wrapper was
    carrying styling at most, and styling that could never render as written.
    """
    stack = []          # [start, end, name, has_block, kind]
    edits = []          # (start, end, replacement)
    for mo in _TAG_SCAN.finditer(data):
        closing, name, attrs, selfclose = mo.groups()
        low = name.lower()
        if selfclose:
            continue
        if not closing:
            if low in INLINE_WRAPPERS:
                stack.append([mo.start(), mo.end(), low, False, "unwrap"])
                continue
            if low in PHRASING_ONLY_BLOCKS:
                stack.append([mo.start(), mo.end(), low, False, "retag"])
                continue
            if low in BLOCK_IN_INLINE:
                for frame in stack:
                    frame[3] = True          # every wrapper still open is bad
        else:
            for i in range(len(stack) - 1, -1, -1):
                if stack[i][2] == low:
                    frame = stack.pop(i)
                    del stack[i:]            # anything left unclosed inside
                    if frame[3]:
                        if frame[4] == "unwrap":
                            edits.append((frame[0], frame[1], b""))
                            edits.append((mo.start(), mo.end(), b""))
                        else:
                            # <p class="x"> -> <div class="x">, keeping the
                            # styling and the structure, losing the violation
                            edits.append((frame[0], frame[1],
                                          b"<div" + attrs_of(data, frame) +
                                          b">"))
                            edits.append((mo.start(), mo.end(), b"</div>"))
                    break

    if not edits:
        return data, 0
    out, last = [], 0
    for start, end, repl in sorted(edits):
        out.append(data[last:start])
        out.append(repl)
        last = end
    out.append(data[last:])
    return b"".join(out), len(edits) // 2


def attrs_of(data, frame):
    """The attribute text of an open tag already located in `data`."""
    mo = _TAG_SCAN.match(data, frame[0])
    return mo.group(3) if mo else b""


def fix_content(data, book_title=None):
    """Doctype, namespaces, obsolete attributes. Returns (data, notes)."""
    notes = []

    # <title></title> is invalid in XHTML5 and several books ship it empty
    if EMPTY_TITLE_RE.search(data):
        t = (book_title or "Untitled").strip() or "Untitled"
        t = esc(t)[:200].encode("utf-8")
        data = EMPTY_TITLE_RE.sub(b"<title>" + t + b"</title>", data)
        notes.append("empty-title")

    # http-equiv only takes a fixed set of values; the rest are EPUB2-era
    # no-ops like Content-Style-Type that HTML5 simply rejects
    def _he(mo):
        if mo.group(2).strip().lower() in HTTP_EQUIV_OK:
            return mo.group(0)
        notes.append("http-equiv")
        return b""

    data = HTTP_EQUIV_ANY_RE.sub(_he, data)

    # a block element inside an inline one is invalid in every EPUB version,
    # so this has to be repaired rather than declared away
    data, unwrapped = unwrap_inline_around_block(data)
    if unwrapped:
        notes.append("block-in-inline")

    # named entities must go numeric BEFORE the DTD-bearing doctype is dropped
    nd = numericise_entities(data)
    if nd != data:
        notes.append("entities")
        data = nd

    # HTML5 encoding declaration
    if HTTP_EQUIV_CT_RE.search(data):
        if CHARSET_META_RE.search(HTTP_EQUIV_CT_RE.sub(b"", data)):
            data = HTTP_EQUIV_CT_RE.sub(b"", data, count=0)
        else:
            data = HTTP_EQUIV_CT_RE.sub(b'<meta charset="utf-8"/>', data, count=1)
            data = HTTP_EQUIV_CT_RE.sub(b"", data)
        notes.append("charset")

    # doctype
    if DOCTYPE_RE.search(data):
        new = DOCTYPE_RE.sub(b"<!DOCTYPE html>", data, count=1)
        if new != data:
            notes.append("doctype")
            data = new
    else:
        m = re.search(rb"<\?xml[^>]*\?>", data)
        at = m.end() if m else 0
        data = data[:at] + b"\n<!DOCTYPE html>" + data[at:]
        notes.append("doctype+")

    # namespaces on <html>
    m = HTML_OPEN_RE.search(data)
    if m:
        tag = m.group(0)
        orig = tag
        if b"xmlns=" not in tag:
            tag = tag[:-1] + b' xmlns="http://www.w3.org/1999/xhtml"' + tag[-1:]
        if b"epub:" in data and b"xmlns:epub" not in tag:
            tag = tag[:-1] + b' xmlns:epub="http://www.idpf.org/2007/ops"' + tag[-1:]
        if tag != orig:
            data = data[:m.start()] + tag + data[m.end():]
            notes.append("xmlns")

    # <meta name="x" value="y"> is an EPUB2-era spelling; HTML wants content=
    def _meta_value(mo):
        tag = mo.group(0)
        if b"content=" in tag.lower():
            return tag
        return re.sub(rb"(\s)value(\s*=)", rb"\1content\2", tag, count=1,
                      flags=re.I)

    d2 = re.sub(rb"<meta\b[^>]*\svalue\s*=[^>]*>", _meta_value, data, flags=re.I)
    if d2 != data:
        data = d2
        notes.append("meta-value")

    # Elements HTML5 removed. Each is renamed to its nearest valid equivalent
    # rather than deleted, so every word between the tags stays on the page.
    for old, new in (("big", "span"), ("tt", "span"), ("font", "span"),
                     ("nobr", "span"), ("blink", "span"), ("marquee", "span"),
                     ("center", "div"), ("strike", "s"), ("acronym", "abbr"),
                     ("dir", "ul"), ("menu", "ul")):
        o, nw = old.encode(), new.encode()
        d2 = re.sub(rb"<%s(\s[^>]*)?>" % o, rb"<%s\1>" % nw, data, flags=re.I)
        d2 = re.sub(rb"<%s(\s[^>]*)?/>" % o, rb"<%s\1/>" % nw, d2, flags=re.I)
        d2 = re.sub(rb"</%s\s*>" % o, b"</%s>" % nw, d2, flags=re.I)
        if d2 != data:
            data = d2
            notes.append("<%s>" % old)

    # Obsolete presentational attributes. This tokenises the tag's attributes
    # properly instead of running a regex over the whole tag: a naive
    # "\s+width=..." pattern also matches INSIDE a quoted value, and
    # <meta name="viewport" content="width=1200, height=1600"/> is exactly
    # that shape - which silently destroyed every page of a fixed-layout
    # comic on the first attempt.
    before = data
    data = TAG_RE.sub(strip_obsolete_attrs, data)
    if data != before:
        notes.append("obsolete-attrs")

    # An id may appear only once per document. Calibre's page-break markers
    # repeat. koboSpan ids are NEVER renamed - a reading position points
    # straight at one, so moving it would move where the reader resumes.
    seen = set()
    n_dupes = [0]

    def dedupe(mo):
        val = mo.group(2)
        if val.startswith(b"kobo."):
            return mo.group(0)
        if val not in seen:
            seen.add(val)
            return mo.group(0)
        n_dupes[0] += 1
        new = val
        k = 1
        while new in seen:
            new = b"%s-dup%d" % (val, k)
            k += 1
        seen.add(new)
        return mo.group(1) + new + mo.group(3)

    data = re.sub(rb'(\sid\s*=\s*")([^"]*)(")', dedupe, data)
    if n_dupes[0]:
        notes.append("dup-ids(%d)" % n_dupes[0])

    return data, notes


DEAD_HREF_RE = re.compile(rb'(<a\b[^>]*?)\shref\s*=\s*(["\'])([^"\']*)\2', re.I)
# an <img>/<image> whose file is simply not in the book renders as nothing
# either way, and the reference itself is RSC-007
DEAD_IMG_RE = re.compile(
    rb'<(img|image)\b[^>]*?\s(?:src|(?:xlink:)?href)\s*=\s*(["\'])([^"\']*)\2'
    rb'[^>]*?/?>', re.I)


def drop_dead_images(data, exists):
    n = [0]

    def sub(mo):
        target = mo.group(3).decode("utf-8", "ignore").split("#")[0]
        if not target or "://" in target or target.startswith("data:"):
            return mo.group(0)
        if exists(target):
            return mo.group(0)
        n[0] += 1
        return b""

    return DEAD_IMG_RE.sub(sub, data), n[0]


DEAD_LINK_RE = re.compile(
    rb'<link\b[^>]*?\shref\s*=\s*(["\'])([^"\']*)\1[^>]*?/?>', re.I)
CSS_URL_RE = re.compile(rb'url\(\s*(["\']?)([^)"\']*)\1\s*\)', re.I)
CSS_IMPORT_RE = re.compile(
    rb'@import\s+(?:url\(\s*(["\']?)([^)"\']*)\1\s*\)|(["\'])([^"\']*)\3)[^;]*;',
    re.I)


def drop_dead_stylesheet_links(data, exists):
    """A <link> to a file that is not in the book - typically Adobe's
    page-template.xpgt, which several of these books reference and none of
    them ship - is RSC-007. Nothing renders from it either way."""
    n = [0]

    def sub(mo):
        target = mo.group(2).decode("utf-8", "ignore").split("#")[0]
        if not target or "://" in target or target.startswith("data:"):
            return mo.group(0)
        if exists(target):
            return mo.group(0)
        n[0] += 1
        return b""

    return DEAD_LINK_RE.sub(sub, data), n[0]


def clean_css(data, exists):
    """Remove url() and @import references to files absent from the book.

    The whole declaration goes, not just the url(): replacing the url with a
    placeholder like about:invalid reads as a remote resource, which trades
    RSC-007 for OPF-014 and fixes nothing."""
    n = [0]

    def imp(mo):
        target = (mo.group(2) or mo.group(4) or b"").decode("utf-8", "ignore")
        target = target.split("#")[0]
        if not target or "://" in target:
            return mo.group(0)
        if exists(target):
            return mo.group(0)
        n[0] += 1
        return b""

    # CSS is not HTML: a stylesheet carrying &quot; has an HTML escape where a
    # quote mark belongs, which makes font-family: &quot;Shift Light&quot;
    # unparseable. Nothing else in CSS uses ampersand escapes.
    for ent, ch in ((b"&quot;", b'"'), (b"&apos;", b"'"), (b"&#39;", b"'"),
                    (b"&#34;", b'"'), (b"&amp;", b"&"), (b"&nbsp;", b"\xc2\xa0")):
        if ent in data:
            data = data.replace(ent, ch)
            n[0] += 1

    data = CSS_IMPORT_RE.sub(imp, data)

    dead = []
    for mo in CSS_URL_RE.finditer(data):
        target = mo.group(2).decode("utf-8", "ignore").split("#")[0]
        if not target or "://" in target or target.startswith("data:"):
            continue
        if not exists(target):
            dead.append(mo.span())
    for s, e in reversed(dead):
        start = max(data.rfind(b";", 0, s), data.rfind(b"{", 0, s))
        start = start + 1 if start >= 0 else s
        semi = data.find(b";", e)
        brace = data.find(b"}", e)
        if semi != -1 and (brace == -1 or semi < brace):
            end = semi + 1
        elif brace != -1:
            end = brace
        else:
            end = e
        data = data[:start] + data[end:]
        n[0] += 1
    return data, n[0]


def drop_dead_links(data, exists):
    """Turn <a href="$00002"> into a plain <a>: the target does not exist in
    the book, so the link is RSC-007 and following it goes nowhere anyway.
    The anchor text is left exactly as it is."""
    n = [0]

    def sub(mo):
        target = mo.group(3).decode("utf-8", "ignore")
        base = target.split("#")[0]
        if not base or "://" in target or target.startswith("#"):
            return mo.group(0)
        if exists(base):
            return mo.group(0)
        n[0] += 1
        return mo.group(1)

    out = DEAD_HREF_RE.sub(sub, data)
    return out, n[0]


PROP_SCAN = (
    ("svg", re.compile(rb"<(\w+:)?svg\b", re.I)),
    ("scripted", re.compile(rb"<script\b|\son[a-z]+\s*=", re.I)),
    ("mathml", re.compile(rb"<(\w+:)?math\b", re.I)),
)


def scan_props(data):
    return {name for name, rx in PROP_SCAN if rx.search(data)}


# --------------------------------------------------------------------------


def fix_book(src, dst):
    """Rewrite src to dst. Returns a dict of what happened."""
    zin = zipfile.ZipFile(src)
    names = zin.namelist()
    opf_path = opf_path_of(zin)
    opf_dir = os.path.dirname(opf_path)

    def rel(n):
        """path of n relative to the OPF directory, as the manifest spells it"""
        if opf_dir and n.startswith(opf_dir + "/"):
            return n[len(opf_dir) + 1:]
        return n

    raw = {n: zin.read(n) for n in names}
    zin.close()

    # Which entries are content documents is decided by the manifest, not by
    # the filename: Calibre leaves spine documents with names like
    # "CR!VDNT5TE87N4Y187Z42MX0" and no extension at all, and EPUBCheck still
    # validates them as XHTML.
    _probe = parse_xml(raw[opf_path])
    book_title = None
    for t in _probe.iter(DC + "title"):
        if t.text and t.text.strip():
            book_title = t.text.strip()
            break
    if not book_title:
        book_title = os.path.splitext(os.path.basename(src))[0]
    xhtml_names = set()
    for it in _probe.iter(OPF + "item"):
        if (it.get("media-type") or "") == "application/xhtml+xml":
            h = re.sub(r"[?#].*$", "", it.get("href") or "")
            xhtml_names.add((opf_dir + "/" + h) if opf_dir else h)

    # content props + content fixes
    content_props = {}
    fixed_content = {}
    notes = set()
    for n in names:
        if not (is_content(n) or n in xhtml_names):
            continue
        d = raw[n]
        props = scan_props(d)
        d2, nts = fix_content(d, book_title)
        notes.update(nts)
        fixed_content[n] = d2
        if props:
            content_props[rel(n)] = props

    ncx_name = next((n for n in names if n.lower().endswith(".ncx")), None)
    # decided up front, because whether the NCX survives changes the OPF
    drop_ncx = False
    if ncx_name:
        try:
            etree.fromstring(raw[ncx_name], parser=etree.XMLParser(
                recover=False, resolve_entities=False, huge_tree=True))
        except Exception:
            drop_ncx = True

    nav_href = "nav.xhtml"
    i = 0
    while (opf_dir + "/" + nav_href if opf_dir else nav_href) in names:
        i += 1
        nav_href = "nav%d.xhtml" % i
    nav_name = (opf_dir + "/" + nav_href) if opf_dir else nav_href

    cover_href = None
    for n in names:
        b = os.path.basename(n).lower()
        if b.startswith("cover") and re.search(r"\.(jpe?g|png|gif)$", b):
            cover_href = rel(n)
            break

    opf_root = parse_xml(raw[opf_path])

    # resources the content actually links to, so undeclared ones can be added
    MT = {".css": "text/css", ".js": "application/javascript",
          ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
          ".gif": "image/gif", ".svg": "image/svg+xml",
          ".otf": "font/otf", ".ttf": "font/ttf", ".woff": "font/woff",
          ".woff2": "font/woff2", ".mp3": "audio/mpeg", ".mp4": "video/mp4"}
    declared = {i.get("href") for i in opf_root.iter(OPF + "item")}
    LINK_RE = re.compile(
        rb'(?:href|src)\s*=\s*["\']([^"\':#][^"\']*)["\']', re.I)
    undeclared = {}
    linked = set()
    for n, d in list(fixed_content.items()):
        cdir = os.path.dirname(n)
        for m in LINK_RE.finditer(d):
            target = m.group(1).decode("utf-8", "ignore").split("#")[0]
            if not target or "://" in target:
                continue
            full = os.path.normpath(os.path.join(cdir, target))
            if full not in raw:
                continue
            href = rel(full)
            linked.add(href)
            if href in declared or href in undeclared:
                continue
            ext = os.path.splitext(href)[1].lower()
            if ext in MT:
                undeclared[href] = MT[ext]

    # The NCX points at documents too, so a spine entry it references is
    # reachable even when no body text links to it. Leaving this out removed
    # a cover page from Harry Potter's spine that its own NCX still named.
    if ncx_name and not drop_ncx:
        for m in re.finditer(rb'<content\b[^>]*\bsrc\s*=\s*"([^"]*)"',
                             raw[ncx_name]):
            t = m.group(1).decode("utf-8", "ignore").split("#")[0]
            if t:
                linked.add(t)

    # links whose target simply is not in the book (RSC-007)
    ndead = 0
    for n, d in list(fixed_content.items()):
        cdir = os.path.dirname(n)

        def exists(base, _cdir=cdir):
            return os.path.normpath(os.path.join(_cdir, base)) in raw

        d2, k = drop_dead_links(d, exists)
        d2, k2 = drop_dead_images(d2, exists)
        d2, k3 = drop_dead_stylesheet_links(d2, exists)
        if k or k2 or k3:
            fixed_content[n] = d2
            ndead += k + k2 + k3

    # stylesheets carry references of their own
    fixed_css = {}
    for n in names:
        if not n.lower().endswith(".css"):
            continue
        # cleaning runs even when nothing is dead, to catch the entities
        cdir = os.path.dirname(n)

        def cexists(base, _cdir=cdir):
            return os.path.normpath(os.path.join(_cdir, base)) in raw

        d2, k = clean_css(raw[n], cexists)
        if d2 != raw[n]:
            fixed_css[n] = d2
            ndead += k
    if ndead:
        notes.add("dead-links(%d)" % ndead)

    # media-type must match what the bytes actually are: several covers here
    # are JPEGs named .png, which is OPF-029.
    def sniff(d):
        if d[:3] == b"\xff\xd8\xff":
            return "image/jpeg"
        if d[:8] == b"\x89PNG\r\n\x1a\n":
            return "image/png"
        if d[:6] in (b"GIF87a", b"GIF89a"):
            return "image/gif"
        if d[:4] == b"RIFF" and d[8:12] == b"WEBP":
            return "image/webp"
        return None

    for it in opf_root.iter(OPF + "item"):
        href = it.get("href") or ""
        full = (opf_dir + "/" + href) if opf_dir else href
        full = re.sub(r"[?#].*$", "", full)
        if full in raw and (it.get("media-type") or "").startswith("image/"):
            real = sniff(raw[full][:16])
            if real and real != it.get("media-type"):
                it.set("media-type", real)
                notes.add("media-type")
    # does a nav already exist?
    existing_nav = None
    for it in opf_root.iter(OPF + "item"):
        if "nav" in (it.get("properties") or "").split():
            existing_nav = it.get("href")
    if existing_nav:
        nav_href = existing_nav
        nav_name = None

    uid_value, changed = fix_opf(
        opf_root, uid_hint=os.path.basename(src),
        content_props=content_props, cover_href=cover_href,
        nav_href=nav_href, has_ncx=bool(ncx_name) and not drop_ncx,
        drop_ncx_href=rel(ncx_name) if (ncx_name and drop_ncx) else None,
        undeclared=undeclared, linked=linked)
    notes.update(changed)
    if drop_ncx:
        notes.add("ncx-malformed-dropped")

    # The opf: prefix is only equivalent to the default namespace if a reader
    # is namespace-aware, and not all of them are. Collapse to plain <meta>
    # under the default OPF namespace, which is what every other EPUB3 looks
    # like, so nothing downstream has to be clever.
    try:
        etree.cleanup_namespaces(opf_root,
                                 top_nsmap={None: OPF_NS, "dc": DC_NS})
    except Exception:
        pass
    opf_bytes = etree.tostring(opf_root, xml_declaration=True,
                               encoding="utf-8", pretty_print=False)

    # nav document
    nav_bytes = None
    manifest_el = opf_root.find(OPF + "manifest")
    spine_el = opf_root.find(OPF + "spine")
    by_id2 = {i.get("id"): i for i in manifest_el.findall(OPF + "item")}
    spine_hrefs, spine_order = set(), []
    if spine_el is not None:
        for ir in spine_el.findall(OPF + "itemref"):
            it = by_id2.get(ir.get("idref"))
            if it is not None and it.get("href"):
                spine_hrefs.add(it.get("href"))
                spine_order.append(it.get("href"))

    # ids actually present in each content document, so nav fragments can be
    # checked instead of guessed
    ID_RE = re.compile(rb'\bid\s*=\s*["\']([^"\']+)["\']')
    valid_frag, spine_titles = {}, {}
    for h in spine_hrefs:
        full = (opf_dir + "/" + h) if opf_dir else h
        d = fixed_content.get(full) or raw.get(full)
        if not d:
            continue
        valid_frag[h] = {m.group(1).decode("utf-8", "ignore")
                         for m in ID_RE.finditer(d)}
        t = re.search(rb"<title[^>]*>(.*?)</title>", d, re.S | re.I)
        if t:
            txt = re.sub(rb"\s+", b" ", t.group(1)).strip()
            if txt:
                spine_titles[h] = txt.decode("utf-8", "ignore")

    if nav_name:
        nav_bytes = nav_from_ncx(raw.get(ncx_name), nav_href, opf_dir,
                                 "Contents", spine_hrefs=spine_hrefs,
                                 spine_order=spine_order,
                                 spine_titles=spine_titles,
                                 valid_frag=valid_frag)
        notes.add("nav-built")

    # NCX uid. Rewriting the NCX through a recovering parser silently
    # restructures a malformed one - on Pride and Prejudice that turned a
    # single pre-existing error into five. So only ever patch the one
    # attribute, textually, and never reserialise.
    ncx_bytes = None
    if ncx_name and uid_value:
        src_ncx = raw[ncx_name]
        try:
            etree.fromstring(src_ncx, parser=etree.XMLParser(
                recover=False, resolve_entities=False, huge_tree=True))
            well_formed = True
        except Exception:
            well_formed = False
        if well_formed:
            # Attribute order carries no meaning in XML, and these files prove
            # it: Carrie's NCX has two dtb:uid metas with different values in
            # opposite attribute orders. So match the element, not an order,
            # and collapse them to a single canonical entry.
            uid_meta_re = re.compile(
                rb'<meta\b(?=[^>]*\bname\s*=\s*["\']dtb:uid["\'])[^>]*/?>', re.I)
            canonical = (b'<meta name="dtb:uid" content="'
                         + esc_attr(uid_value).encode("utf-8") + b'"/>')
            seen = [False]

            def _uid(mo):
                if seen[0]:
                    return b""          # drop the duplicates
                seen[0] = True
                return canonical

            patched, n = uid_meta_re.subn(_uid, src_ncx)
            if n and patched != src_ncx:
                ncx_bytes = patched
                notes.add("ncx-uid")
            base = ncx_bytes or src_ncx

            # NCX ids must be XML NAMEs. Two things break that here: a colon
            # (from id="urn:uuid:...") and, more often, a leading digit -
            # Watership Down's navPoints are bare UUIDs, a third of which
            # start with a number. EPUBCheck reports both as "must be an XML
            # name without colons", which points at only half the problem.
            def _id(mo):
                val = mo.group(2)
                new = val.replace(b":", b"-")
                if new[:1] and not (new[:1].isalpha() or new[:1] == b"_"):
                    new = b"id-" + new
                if new == val:
                    return mo.group(0)
                return mo.group(1) + new + mo.group(3)
            nb = re.sub(rb'(\sid\s*=\s*")([^"]*)(")', _id, base)
            if nb != base:
                base = nb
                ncx_bytes = nb
                notes.add("ncx-ids")

            # <pageList> is optional, and the ones here are missing the
            # required class attribute. Dropping it loses nothing a reader
            # uses; the navMap is the table of contents.
            nb = re.sub(rb"<pageList\b.*?</pageList>", b"", base,
                        flags=re.S | re.I)
            if nb != base:
                base = nb
                ncx_bytes = nb
                notes.add("ncx-pagelist")

            # the NCX points at fragments too, and the same dangling ones
            # that break the nav break it here (RSC-012)
            if valid_frag:
                def _src(mo):
                    raw_src = mo.group(2).decode("utf-8", "ignore")
                    b, _, frag = raw_src.partition("#")
                    if frag and frag not in valid_frag.get(b, set()):
                        return mo.group(1) + b.encode("utf-8") + mo.group(3)
                    return mo.group(0)
                nb = re.sub(rb'(<content\b[^>]*\bsrc\s*=\s*")([^"]*)(")',
                            _src, base)
                if nb != base:
                    base = nb
                    ncx_bytes = nb
                    notes.add("ncx-fragments")

            # playOrder is keyed to the TARGET, not to position: two navPoints
            # pointing at the same document must share a playOrder, and
            # distinct targets must form a gapless sequence. Stripping dead
            # fragments above can make two entries collide, so this runs after.
            tokens = list(re.finditer(
                rb'playOrder\s*=\s*"(\d+)"|<content\b[^>]*\bsrc\s*=\s*"([^"]*)"',
                base))
            pending, pairs = None, []
            for mo in tokens:
                if mo.group(1) is not None:
                    pending = mo
                elif pending is not None:
                    pairs.append((pending, mo.group(2).decode("utf-8", "ignore")))
                    pending = None
            if pairs:
                order_of, nxt = {}, 1
                for _, tgt in pairs:
                    if tgt not in order_of:
                        order_of[tgt] = nxt
                        nxt += 1
                if any(int(m.group(1)) != order_of[t] for m, t in pairs):
                    out, last = bytearray(), 0
                    for m, tgt in pairs:
                        out += base[last:m.start()]
                        out += b'playOrder="%d"' % order_of[tgt]
                        last = m.end()
                    out += base[last:]
                    ncx_bytes = bytes(out)
                    notes.add("ncx-playorder")
        else:
            # An NCX that will not parse cannot be repaired textually, and in
            # EPUB3 the nav document already carries the table of contents -
            # so drop it rather than ship a file that fails to parse.
            notes.add("ncx-malformed-dropped")
            drop_ncx = True

    # write
    tmp = dst + ".tmp"
    zout = zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED)
    # mimetype first, stored
    if "mimetype" in raw:
        zi = zipfile.ZipInfo("mimetype")
        zi.compress_type = zipfile.ZIP_STORED
        zout.writestr(zi, raw["mimetype"])
    for n in names:
        if n == "mimetype":
            continue
        if drop_ncx and n == ncx_name:
            continue
        if n == opf_path:
            d = opf_bytes
        elif n == ncx_name and ncx_bytes is not None:
            d = ncx_bytes
        elif n in fixed_content:
            d = fixed_content[n]
        elif n in fixed_css:
            d = fixed_css[n]
        else:
            d = raw[n]
        zout.writestr(n, d, zipfile.ZIP_DEFLATED)
    if nav_bytes is not None:
        zout.writestr(nav_name, nav_bytes, zipfile.ZIP_DEFLATED)
    zout.close()
    os.replace(tmp, dst)
    return {"notes": sorted(notes), "nav": bool(nav_bytes)}


def main(argv):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("target")
    ap.add_argument("--out", help="output directory (mirrors input names)")
    ap.add_argument("--inplace", action="store_true")
    a = ap.parse_args(argv)

    if os.path.isdir(a.target):
        books = [os.path.join(a.target, f) for f in sorted(os.listdir(a.target))
                 if f.lower().endswith(".epub") and not f.startswith("._")]
    else:
        books = [a.target]
    if a.out:
        os.makedirs(a.out, exist_ok=True)

    ok = fail = 0
    for p in books:
        dst = p if a.inplace else os.path.join(a.out or ".", os.path.basename(p))
        try:
            if dst != p and not a.inplace:
                pass
            r = fix_book(p, dst)
            ok += 1
            print("%-64s %s" % (os.path.basename(p)[:64], ",".join(r["notes"])[:60]))
        except Exception as e:
            fail += 1
            print("%-64s FAILED %r" % (os.path.basename(p)[:64], e))
    print("\n%d rewritten, %d failed" % (ok, fail))
    return 1 if fail else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
