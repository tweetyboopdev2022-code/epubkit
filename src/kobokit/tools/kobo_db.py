#!/usr/bin/env python3
"""Read and write a Kobo's own library database (.kobo/KoboReader.sqlite).

    python3 kobo_db.py <db> --report
    python3 kobo_db.py <db> --plan meta.json [--apply]
    python3 kobo_db.py <db> --prune --root <mounted drive> [--apply]

Why this exists: a Kobo reads a sideloaded book's metadata only the first time it
sees the file, and it ignores series metadata inside sideloaded files entirely.
Correcting the EPUB is therefore not enough on its own. See
references/kobo-facts.md before using this.

Everything is a dry run unless --apply is given, and --apply takes a timestamped
backup of the database first and prints where it went.

The device must be connected and idle. Eject properly afterwards.

meta.json is keyed by filename as it appears on the drive:

{
  "The Maze Runner 01 - The Maze Runner - James Dashner.epub": {
    "series": "The Maze Runner", "series_index": "1",
    "shelf": "Maze Runner"
  },
  "Pigeon - The Pigeon Needs a Bath! - Mo Willems.kepub.epub": {
    "title": "The Pigeon Needs a Bath!", "author": "Mo Willems",
    "series": "Pigeon", "shelf": "Nora Collection"
  }
}

All keys are optional. title/author are worth setting for PDFs, whose embedded
metadata the device reads badly. Shelves are created if they do not exist.
"""
import argparse, os, shutil, sqlite3, sys, time, urllib.parse

PREFIX = 'file:///mnt/onboard/'


def name_of(content_id):
    return urllib.parse.unquote(content_id[len(PREFIX):]) if content_id.startswith(PREFIX) else content_id


def report(c):
    rows = list(c.execute("""select Title, Attribution, Series, SeriesNumber, ContentID
                             from content where ContentType='6' and ContentID like ?
                             order by coalesce(Series,''), SeriesNumberFloat, Title""",
                          (PREFIX + '%',)))
    print('%d sideloaded books, %d with series\n' % (len(rows), sum(1 for r in rows if r[2])))
    print('%-28s %-5s %-42s %s' % ('SERIES', '#', 'TITLE', 'AUTHOR'))
    print('-' * 110)
    for t, a, s, n, cid in rows:
        print('%-28s %-5s %-42s %s' % ((s or '-')[:28], n or '-', str(t)[:42], str(a)[:24]))
    print('\nshelves:')
    for nm, k in c.execute("""select s.Name, (select count(*) from ShelfContent sc
                              where sc.ShelfName=s.Name and sc._IsDeleted='false')
                              from Shelf s where s._IsDeleted='false' order by 2 desc"""):
        print('   %-28s %d' % (nm, k))
    stale = c.execute("""select count(*) from ShelfContent sc where sc._IsDeleted='false'
                         and not exists (select 1 from content ct where ct.ContentID=sc.ContentId)
                      """).fetchone()[0]
    print('\nshelf entries pointing at books that no longer exist: %d' % stale)
    unshelved = [r[0] for r in c.execute("""select Title from content ct where ct.ContentType='6'
        and ct.ContentID like ? and not exists (select 1 from ShelfContent sc
        where sc.ContentId=ct.ContentID and sc._IsDeleted='false')""", (PREFIX + '%',))]
    print('books on no shelf: %d' % len(unshelved))
    for u in unshelved[:12]:
        print('   ' + str(u))


def apply_plan(c, plan, now, dry):
    ids = {name_of(r[0]): r[0] for r in
           c.execute("select ContentID from content where ContentType='6' and ContentID like ?",
                     (PREFIX + '%',))}
    missing = [k for k in plan if k not in ids]
    for m in missing:
        print('not on the device (has it imported yet?): %s' % m)
    touched = shelves = 0
    for fn, md in plan.items():
        cid = ids.get(fn)
        if not cid:
            continue
        sets, args = [], []
        if md.get('series') is not None:
            sets += ['Series=?', 'SeriesNumber=?', 'SeriesNumberFloat=?']
            idx = md.get('series_index') or None
            args += [md['series'] or None, idx, float(idx) if idx else None]
        if md.get('title'):
            sets.append('Title=?'); args.append(md['title'])
        if md.get('author'):
            sets.append('Attribution=?'); args.append(md['author'])
        if sets:
            print('%-64s %s' % (fn[:64], ', '.join(s.split('=')[0] for s in sets)))
            if not dry:
                c.execute('update content set %s where ContentID=? or BookID=?' % ','.join(sets),
                          args + [cid, cid])
            touched += 1
        sh = md.get('shelf')
        if sh:
            if not dry:
                exists = c.execute("select count(*) from Shelf where Name=? and _IsDeleted='false'",
                                   (sh,)).fetchone()[0]
                if not exists:
                    c.execute("""insert into Shelf (CreationDate,Id,InternalName,LastModified,Name,
                                 Type,_IsDeleted,_IsVisible,_IsSynced)
                                 values (?,?,?,?,?, 'UserTag','false','true','false')""",
                              (now, sh, sh, now, sh))
                c.execute("""insert or replace into ShelfContent
                             (ShelfName,ContentId,DateModified,_IsDeleted,_IsSynced)
                             values (?,?,?,'false','false')""", (sh, cid, now))
                c.execute("update Shelf set LastModified=?, _IsDeleted='false' where Name=?",
                          (now, sh))
            shelves += 1
    print('\n%d books updated, %d shelf placements' % (touched, shelves))


def prune(c, root, dry):
    """Remove entries for files that are no longer on the drive, including the
    per-chapter rows and cover index rows that would otherwise be orphaned."""
    dead = []
    for (cid,) in c.execute("select ContentID from content where ContentType='6' and ContentID like ?",
                            (PREFIX + '%',)):
        if not os.path.exists(os.path.join(root, name_of(cid))):
            dead.append(cid)
    print('%d books in the database whose file is gone' % len(dead))
    for d in dead[:20]:
        print('   ' + name_of(d)[:96])
    if dry or not dead:
        return
    for cid in dead:
        c.execute("delete from volume_shortcovers where volumeId=?", (cid,))
        c.execute("delete from content where BookID=?", (cid,))
        c.execute("delete from content where ContentID=?", (cid,))
        c.execute("delete from ShelfContent where ContentId=?", (cid,))
    c.execute("""delete from ShelfContent where _IsDeleted='false' and not exists
                 (select 1 from content ct where ct.ContentID=ShelfContent.ContentId)""")
    print('removed')


def restore_positions(c, snapshot, dry):
    """Write ChapterIDBookmarked/___PercentRead/ReadStatus/DateLastRead, and
    FirstTimeReading/TimesStartedReading/LastTimeStartedReading, back onto
    sideloaded books, keyed by filename, from a snapshot json (as written by
    pipeline/reading_positions.py before the files were touched).

    Editing a sideloaded book makes the Kobo re-import it and wipe its
    reading position - this is how that position comes back afterwards.

    The second group of three fields is what drives the "My Books" list's
    own unread/read badge, separately from ReadStatus/___PercentRead which
    drive the home-screen "Continue reading" widget - a re-imported book can
    otherwise show "Reading 51%" on the home screen while still showing as
    Unread in My Books, because FirstTimeReading is still 'true'. Snapshots
    taken before this field was added to reading_positions.py won't have
    these keys; such a book is left alone for this group rather than forced
    back to unread.
    """
    ids = {name_of(r[0]): r[0] for r in
           c.execute("select ContentID from content where ContentType='6' and ContentID like ?",
                     (PREFIX + '%',))}
    restored = missing = unchanged = 0
    for fn, pos in snapshot.items():
        cid = ids.get(fn)
        if not cid:
            missing += 1
            continue
        mark = pos.get('chapter_bookmarked')
        pct = pos.get('percent_read')
        status = pos.get('read_status')
        when = pos.get('date_last_read')
        first_time = pos.get('first_time_reading')
        times_started = pos.get('times_started_reading')
        last_started = pos.get('last_time_started_reading')
        if not (mark or pct or status):
            unchanged += 1
            continue
        print('%-64s %s%% status=%s' % (fn[:64], pct, status))
        if not dry:
            c.execute("""update content set ChapterIDBookmarked=?, ___PercentRead=?,
                         ReadStatus=?, DateLastRead=? where ContentID=? or BookID=?""",
                      (mark, pct, status, when, cid, cid))
            if first_time is not None or times_started is not None:
                c.execute("""update content set FirstTimeReading=coalesce(?, FirstTimeReading),
                             TimesStartedReading=coalesce(?, TimesStartedReading),
                             LastTimeStartedReading=coalesce(?, LastTimeStartedReading, DateLastRead)
                             where ContentID=? or BookID=?""",
                          (first_time, times_started, last_started, cid, cid))
        restored += 1
    print('\n%d positions restored, %d books not found on device yet, %d had nothing to restore'
          % (restored, missing, unchanged))


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('db')
    ap.add_argument('--report', action='store_true')
    ap.add_argument('--plan')
    ap.add_argument('--restore-positions', help='reading-positions snapshot json to restore '
                    'from, after a re-import wiped bookmarks (see reading_positions.py)')
    ap.add_argument('--prune', action='store_true')
    ap.add_argument('--root', help='the mounted Kobo drive, for --prune')
    ap.add_argument('--apply', action='store_true')
    a = ap.parse_args()
    dry = not a.apply

    if a.apply:
        bak = '%s.backup-%s' % (a.db, time.strftime('%Y%m%d-%H%M%S'))
        shutil.copy(a.db, bak)
        print('database backed up to %s\n' % bak)

    c = sqlite3.connect(a.db)
    c.isolation_level = None
    if a.apply:
        c.execute('BEGIN')
    now = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())

    if a.report or (not a.plan and not a.prune):
        report(c)
    if a.plan:
        import json
        apply_plan(c, json.load(open(a.plan)), now, dry)
    if a.restore_positions:
        import json
        restore_positions(c, json.load(open(a.restore_positions)), dry)
    if a.prune:
        if not a.root:
            print('--prune needs --root pointing at the mounted drive'); return 1
        prune(c, a.root, dry)

    if a.apply:
        c.execute('COMMIT')
        assert c.execute('pragma integrity_check').fetchone()[0] == 'ok'
        print('\ndatabase integrity: ok')
    elif a.plan or a.restore_positions or a.prune:
        print('\ndry run -- nothing written. Re-run with --apply.')
    c.close()
    return 0


if __name__ == '__main__':
    sys.exit(main())
