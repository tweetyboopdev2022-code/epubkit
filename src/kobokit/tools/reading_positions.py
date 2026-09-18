#!/usr/bin/env python3
"""Snapshot every sideloaded book's reading position before touching the
files, so kobo_db.py --restore-positions can put it back after a re-import.

    python3 reading_positions.py <KoboReader.sqlite> [--out FILE]

Editing a sideloaded file makes the Kobo re-import it, which wipes
ChapterIDBookmarked/___PercentRead/ReadStatus, and separately resets
FirstTimeReading/TimesStartedReading/LastTimeStartedReading - the fields
that drive the "My Books" list's own unread/read badge, independently of
ReadStatus/___PercentRead which drive the home-screen "Continue reading"
widget. This has no opinion on when to run it - just run it before any
batch that rewrites files on the device.
"""
import argparse, json, os, shutil, sqlite3, sys, tempfile, urllib.parse

PREFIX = "file:///mnt/onboard/"


def snapshot(db_path):
    tmp = tempfile.mktemp(suffix=".sqlite")
    shutil.copyfile(db_path, tmp)  # never touch the live db, and avoid locking it
    try:
        con = sqlite3.connect(tmp)
        rows = con.execute(
            """SELECT ContentID, Title, Attribution, ChapterIDBookmarked,
               ___PercentRead, ReadStatus, DateLastRead, FirstTimeReading,
               TimesStartedReading, LastTimeStartedReading FROM content
               WHERE ContentType=6 AND ContentID LIKE ?""", (PREFIX + "%",)).fetchall()
    finally:
        os.remove(tmp)

    out = {}
    for cid, title, author, mark, pct, status, when, first_time, times_started, last_started in rows:
        fname = urllib.parse.unquote(cid[len(PREFIX):])
        if "/" in fname:
            continue  # library root only, matching how books are sideloaded
        out[fname] = {"content_id": cid, "title": title, "author": author,
                      "chapter_bookmarked": mark, "percent_read": pct,
                      "read_status": status, "date_last_read": when,
                      "first_time_reading": first_time,
                      "times_started_reading": times_started,
                      "last_time_started_reading": last_started}
    return out


def main(argv):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("db")
    ap.add_argument("--out", default="reading-positions.json")
    a = ap.parse_args(argv)

    positions = snapshot(a.db)
    json.dump(positions, open(a.out, "w"), indent=1)
    marked = sum(1 for v in positions.values() if v["chapter_bookmarked"])
    inprog = sum(1 for v in positions.values() if v["percent_read"] and 0 < v["percent_read"] < 100)
    print("%d books, %d with a bookmark, %d in progress -> %s" %
          (len(positions), marked, inprog, a.out))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
