#!/usr/bin/env python3
"""Convert a folder of picture-book PDFs, a time slice at a time.

pdf2kepub.py already resumes its rendering; this drives it over a whole batch so
that one shell call does as much as it can and the next one carries on. Nine
graphic novels is roughly 1,900 pages and well over an hour of rendering, which
no single call is going to survive.

    python3 pdfbatch.py --plan jobs.json --out ./built --render-root ~/kb/r \\
        --seconds 150

The plan is {source filename: {title, series, series_index, author, ...}} - the
same shape the rest of the toolchain uses. Run the same command until it says
everything is done.
"""
import argparse, json, os, re, shutil, subprocess, sys, time

HERE = os.path.dirname(os.path.abspath(__file__))


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--plan", required=True)
    ap.add_argument("--src", required=True, help="folder holding the PDFs")
    ap.add_argument("--out", required=True)
    ap.add_argument("--render-root", required=True,
                    help="where rendered pages accumulate between runs")
    ap.add_argument("--seconds", type=float, default=150)
    ap.add_argument("--chunk", type=int, default=45)
    ap.add_argument("--jobs", type=int, default=4,
                    help="parallel pdftoppm workers per chunk (default 4)")
    ap.add_argument("--quality", type=int, default=78)
    ap.add_argument("--panel", default="1072x1448")
    a = ap.parse_args()

    plan = json.load(open(a.plan))
    os.makedirs(a.out, exist_ok=True)
    os.makedirs(a.render_root, exist_ok=True)
    t0 = time.time()
    done = pending = 0

    for fname, m in sorted(plan.items(), key=lambda kv: str(kv[1].get("series_index") or "z")):
        if not fname.lower().endswith(".pdf"):
            continue
        src = os.path.join(a.src, fname)
        if not os.path.exists(src):
            print("  MISSING %s" % fname)
            continue
        stem = "%s %02d - %s - %s" % (m["series"], int(m["series_index"]),
                                      m["title"].split(":")[-1].strip(), m["author"]) \
            if str(m.get("series_index")).strip() else "%s - %s" % (m["title"], m["author"])
        stem = re.sub(r'[\\/:*?"<>|]', "", stem)
        target = os.path.join(a.out, stem + ".kepub.epub")
        rdir = os.path.join(a.render_root, re.sub(r"\W+", "_", fname)[:50])
        if os.path.exists(target):
            done += 1
            # Rendered pages are kept so an interrupted book can resume, but once
            # its book exists they are dead weight - and heavy: nine graphic
            # novels leave 1.8 GB behind, which on a 10 GB volume stops the batch
            # halfway through with a disk-full error and no obvious cause.
            if os.path.isdir(rdir):
                shutil.rmtree(rdir, ignore_errors=True)
                print("  cleared %s pages for %s" % ("rendered", stem[:40]))
            continue
        if time.time() - t0 > a.seconds:
            pending += 1
            continue

        cmd = [sys.executable, "-u", os.path.join(HERE, "pdf2kepub.py"), src,
               "--out", a.out, "--title", m["title"], "--author", m["author"],
               "--filename", stem + ".kepub.epub",
               "--render-dir", rdir,
               "--render-chunk", str(a.chunk), "--render-jobs", str(a.jobs),
               "--quality", str(a.quality),
               "--panel", a.panel]
        for flag, key in (("--author-sort", "author_sort"), ("--series", "series"),
                          ("--series-index", "series_index"), ("--publisher", "publisher"),
                          ("--date", "date"), ("--subjects", "subjects"),
                          ("--description", "description")):
            if str(m.get(key) or "").strip():
                cmd += [flag, str(m[key])]
        # Stay on one book until it is finished or the budget runs out. Giving
        # each book a single slice in turn would leave every book half rendered
        # and nothing readable until the very last run.
        last = "?"
        while time.time() - t0 < a.seconds:
            p = subprocess.run(cmd, capture_output=True, text=True)
            tail = [l for l in (p.stdout + p.stderr).strip().splitlines()
                    if "rendered" in l or "wrote" in l or "pages," in l]
            if tail:
                last = tail[-1].strip()
            if p.returncode != 2:            # 2 means "more pages to render"
                break
        print("  %-46s %s" % (stem[:46], last))
        if os.path.exists(target):
            done += 1
        else:
            pending += 1

    total = len([f for f in plan if f.lower().endswith(".pdf")])
    print("\n%d of %d finished%s" % (done, total,
          "" if done == total else "  - run the same command again"))
    return 0 if done == total else 2


if __name__ == "__main__":
    sys.exit(main())
