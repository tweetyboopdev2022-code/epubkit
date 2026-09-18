#!/usr/bin/env python3
"""Rename books to "Series NN - Title - Author", from their own metadata.

    python3 rename_files.py <folder>            # dry run, prints the plan
    python3 rename_files.py <folder> --apply

Series positions are zero-padded to two digits with fractions appended, so they
sort correctly as text: 00, 00.5, 00.6, 01, 01.5, 02, 02.5, 03. The obvious
0.5 / 1 scheme sorts wrongly in every file browser.

On a Kobo, renaming is not cosmetic: the device identifies a book by its full
path, so a rename makes it re-import the file and finally pick up corrected
metadata. It also discards that book's reading position, so check for part-read
books first -- see references/kobo-facts.md.

Options:
  --series-label OLD=NEW   rewrite a series name for filenames only
                           (repeatable, e.g. "Dragonlance: Chaos War=Dragonlance Chaos War")
  --title OLD=NEW          shorter title for the filename (repeatable)
  --plan out.json          also write the mapping to a file
"""
import argparse, json, os, re, sys, zipfile
import xml.etree.ElementTree as ET

OPF = '{http://www.idpf.org/2007/opf}'
DC = '{http://purl.org/dc/elements/1.1/}'
ILLEGAL = r'[\\/:*?"<>|]'


def safe(s):
    s = s.replace('/', ' and ').replace(':', '')
    return re.sub(r'\s+', ' ', re.sub(ILLEGAL, '', s)).strip(' .')


def trim_series_from_title(title, series):
    """Drop a series name the title repeats.

    Published titles usually carry the series ("InvestiGators: Weather or Not"),
    and the filename already opens with it, so using the title whole gives
    "InvestiGators 09 - InvestiGators Weather or Not - ...". The book's own
    metadata keeps the full published title; only the filename is trimmed.
    """
    if not series:
        return title
    t, s = title.strip(), series.strip()
    for sep in (": ", " - ", ", ", " "):
        pre = s + sep
        if t.lower().startswith(pre.lower()) and len(t) > len(pre) + 2:
            rest = t[len(pre):].strip()
            # "The Bad Guys in Mission Unpluckable" minus the series leaves a
            # dangling connective - "The Bad Guys 02 - in Mission Unpluckable".
            # The word belonged to the series name, not to the title.
            rest = re.sub(r"^(in|and|meets|vs\.?|versus)\s+", "", rest,
                          flags=re.I).strip()
            # With a bare space the series name is often part of the title's own
            # noun phrase, not a prefix: "Mr. Lemoncello's Library Olympics"
            # trimmed down to "Olympics", which names nothing. A colon or dash
            # is an explicit separator and can be trusted; a space can only be
            # trusted when what survives still reads as a title.
            if sep == " " and len(rest.split()) < 2:
                return t
            return rest or t
    return t


def numlabel(idx):
    if idx in (None, ''):
        return None
    f = float(idx)
    w = int(f)
    return '%02d' % w if f == w else ('%02d' % w) + ('%s' % round(f - w, 2))[1:]


def meta(path):
    z = zipfile.ZipFile(path)
    op = re.search(r'full-path="([^"]+)"',
                   z.read('META-INF/container.xml').decode('utf-8', 'replace')).group(1)
    md = ET.fromstring(z.read(op)).find(OPF + 'metadata')
    t = getattr(md.find(DC + 'title'), 'text', None) or 'Untitled'
    au = [e.text for e in md.findall(DC + 'creator') if e.text] or ['Unknown']
    m = {x.get('name'): x.get('content') for x in md.findall(OPF + 'meta') if x.get('name')}
    z.close()
    return t, au, m.get('calibre:series'), m.get('calibre:series_index')


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('folder')
    ap.add_argument('--apply', action='store_true')
    ap.add_argument('--series-label', action='append', default=[])
    ap.add_argument('--title', action='append', default=[])
    ap.add_argument('--plan')
    a = ap.parse_args()

    slab = dict(s.split('=', 1) for s in a.series_label)
    tover = dict(s.split('=', 1) for s in a.title)

    files = sorted(f for f in os.listdir(a.folder)
                   if f.lower().endswith(('.epub', '.kepub.epub')) and not f.startswith('._'))
    plan, seen = [], {}
    for f in files:
        try:
            title, authors, ser, idx = meta(os.path.join(a.folder, f))
        except Exception as e:
            print('skip  %s  (%r)' % (f, e)); continue
        ext = '.kepub.epub' if f.endswith('.kepub.epub') else '.epub'
        t = tover.get(title, title)
        au = ' & '.join(authors)
        if ser:
            lab = slab.get(ser, ser)
            n = numlabel(idx)
            # only when the user has not given an explicit override for this title
            if title not in tover:
                t = trim_series_from_title(t, ser) or t
                t = trim_series_from_title(t, lab) or t
            new = ('%s %s - %s - %s%s' % (safe(lab), n, safe(t), safe(au), ext)) if n \
                else ('%s - %s - %s%s' % (safe(lab), safe(t), safe(au), ext))
        else:
            new = '%s - %s%s' % (safe(t), safe(au), ext)
        seen[new] = seen.get(new, 0) + 1
        plan.append((f, new))

    dupes = {k for k, v in seen.items() if v > 1}
    for old, new in plan:
        if old == new:
            print('=     %s' % new)
        else:
            print('%s  %s\n      was: %s' % ('->' if new not in dupes else '!!', new, old))
    if dupes:
        print('\nrefusing to run: %d target names collide' % len(dupes))
        for d in dupes:
            print('   ' + d)
        return 1
    bad = [n for _, n in plan if re.search(ILLEGAL, n)]
    if bad:
        print('\nrefusing to run: illegal characters in %r' % bad[:3])
        return 1

    if a.plan:
        json.dump(plan, open(a.plan, 'w'), indent=1, ensure_ascii=False)

    if not a.apply:
        print('\ndry run -- %d of %d would be renamed. Re-run with --apply.'
              % (sum(1 for o, n in plan if o != n), len(plan)))
        return 0

    n = 0
    for old, new in plan:
        if old == new:
            continue
        o, p = os.path.join(a.folder, old), os.path.join(a.folder, new)
        if os.path.exists(p):
            print('exists, skipping: %s' % new); continue
        os.rename(o, p); n += 1
    print('\nrenamed %d files' % n)
    return 0


if __name__ == '__main__':
    sys.exit(main())
