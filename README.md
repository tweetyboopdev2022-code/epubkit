# kobokit

Point it at a folder of downloaded books. Get back a library your Kobo will
open without complaint: converted to EPUB, metadata filled in and filenames
normalised, structurally repaired, validated with EPUBCheck, and — if you want
them — spoiler-free recaps bound into the book itself.

It came out of repairing a real 373-book library. That library declared
`version="2.0"` in its packages while using EPUB 3 markup throughout, which is
how it accumulated 123,287 EPUBCheck errors. After the rewrite: 1,342 errors,
344 books completely clean, and not one lost page, image, or reading position.
The rules that got it there are in `docs/kobo-facts.md`, and most of them were
learned by breaking something first.

## Install

```sh
pip install -e ".[full]"      # lxml + epubcheck: the repair and validate path
pip install -e ".[full,pdf]"  # plus PDF conversion
```

The core package has no runtime dependencies beyond a `tomli` backport on
Python 3.10. The model clients are plain `urllib`. The extras are only pulled
in by the stages that need them, and a stage whose tools are missing says so
and steps aside instead of failing the run.

EPUBCheck needs a JVM. Without one, the validate stage is skipped and every
book is reported as unvalidated rather than as clean.

## Use

```sh
kobokit init                  # writes a starter kobokit.toml here
$EDITOR kobokit.toml
export ANTHROPIC_API_KEY=...  # only if you want recaps or AI metadata
kobokit check                 # config, missing tools, and a live endpoint ping
kobokit run
```

or without a config file at all:

```sh
kobokit run --in ~/Downloads/books --out ~/Books/kobo \
            --provider anthropic --model claude-sonnet-4-5 --workers 4
```

Other things you will want:

```sh
kobokit run --no-recaps               # structure and metadata only
kobokit run --only validate --in ~/Books/kobo   # re-check a built library
kobokit run --allow-imperfect         # ship books that improved but aren't clean
kobokit run -n                        # say what would happen, touch nothing
kobokit validate ~/Books/kobo         # EPUBCheck a folder, one line per book
kobokit recaps "Some Book.epub"       # recaps for one book
```

Runs are resumable. A book already recorded as done — same size, same content
fingerprint — is skipped on the next run. `--no-resume` forces it through.

## What it does to a book

**ingest** identifies the real format rather than trusting the extension,
checks the archive's CRCs, and copies the original into a backup folder before
anything is modified. The originals are never edited in place.

**convert** turns MOBI/AZW3, FB2, and PDF into EPUB. PDFs are routed first: a
text PDF is reflowed, a scanned or comic PDF becomes a fixed-layout EPUB with
the pages intact, because reflowing one of those produces garbage.

**metadata** reads what the book already declares, checks Open Library and
Google Books, and only then asks a model to fill what is still blank. It does
not overwrite what the book already states unless you ask it to. Files are
renamed from the finished metadata, with the full series name and the full
title spelled out — nothing is abbreviated to fit a filename, however long it
runs.

**repair** prunes dead references, rebuilds the table of contents, fixes
covers, and promotes the package to EPUB 3. Every rewrite passes a content gate
first: word count, document count, image count, and every `koboSpan` must
survive. A book that loses a page does not get written, no matter how clean it
validates. Kobo bookmarks point at span IDs, so a renamed span moves where the
reader resumes — those IDs are never touched.

**validate** runs EPUBCheck. By default a book with remaining errors is
reported and not shipped; `--allow-imperfect` ships it when it is measurably
better than what it replaced.

**recaps** summarises each chapter as of the *previous* one, so a recap can
never spoil the chapter you are about to read. Recaps are keyed to the spine,
not to chapter numbers, so they survive a restructured book. A cast list tracks
first appearances. The generated files go in the manifest, not the spine, so
they never interrupt reading.

**report** writes per-book prose that says what was actually wrong with the
book — not that a file was processed — and keeps damage the book arrived with
separate from anything the pipeline introduced.

## Providers

`ollama` (default, local, no key), `anthropic`, `openai`. Keys come from
`ANTHROPIC_API_KEY` / `OPENAI_API_KEY` in the environment. A key written into
a config file is rejected at load: config files get committed, environment
variables do not.

`kobokit check` pings the endpoint and tells you if the model is missing before
you start a four-hour run.

## Configuration

Defaults, then `kobokit.toml`, then `KOBOKIT_*` environment variables, then
command-line flags — each layer overriding the one before it. kobokit looks for
`kobokit.toml` beside the library, in the current directory, and in
`~/.config/kobokit/`. An unrecognised setting is an error that names the
setting rather than being ignored silently.

## Safety

Originals are copied to a backup folder before anything else happens. Books are
built in a work directory and copied into the library only once every stage has
passed. One bad book fails alone. Nothing is deleted.

`docs/kobo-facts.md` has the device-side details: how Kobo stores reading
positions, the two field groups that both have to be set for a book to look
read, and what `.kobo/google_drive/` means if your books sync from Drive.
Deploying to the device is deliberately not part of this pipeline.

## Licence

MIT.
# kobokit
