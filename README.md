# epubkit

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

epubkit needs **Python 3.10 or newer**, and it is worth checking which Python
you are about to use before anything else:

```sh
python3 -V
```

Install it into an environment of its own:

```sh
python3 -m venv ~/.venvs/epubkit
~/.venvs/epubkit/bin/pip install -e ".[full,pdf]"
```

`full` is lxml and EPUBCheck — the repair and validation path, and what makes
this worth running at all. `pdf` adds PyMuPDF for PDF conversion. Leave `pdf`
off if you have no PDFs; the stage that needs it will say so and step aside
rather than failing the run.

Then either put it on your PATH or alias it:

```sh
ln -s ~/.venvs/epubkit/bin/epubkit ~/bin/epubkit     # if ~/bin is on your PATH
# or, in ~/.zshrc or ~/.bashrc:
alias epubkit="$HOME/.venvs/epubkit/bin/epubkit"
```

If you have `uv`, all of that collapses to one line, and it fetches a suitable
Python itself:

```sh
uv tool install --editable ".[full,pdf]"
```

`epubkit doctor` tells you which of these you have, route by route, and
`epubkit doctor --fix` installs the missing ones into whichever environment
epubkit itself is running in — which matters, because a bare `pip` on your PATH
often belongs to a different Python, and that is how you install something
successfully and still get told it is missing. It always shows the command and
asks first.

### EPUBCheck needs Java

```sh
java -version                     # if this fails:
brew install --cask temurin       # macOS
sudo apt install default-jre      # Debian/Ubuntu
```

Without a JVM the validate stage is skipped and every book is reported as
**unvalidated** rather than as clean — which quietly removes the check that
this whole pipeline exists to pass. Worth confirming before a long run.

### Things that go wrong on macOS

**`ERROR: File "setup.py" or "setup.cfg" not found ... editable mode currently
requires a setuptools-based build`** — you are on Xcode's bundled Python. It
is 3.9 with a pip from 2021: too old to install a `pyproject.toml`-only
project, and below the version epubkit needs in any case. Install a real
Python (`brew install python@3.12`) and use that to make the venv.

**`zsh: command not found: epubkit` right after creating the symlink** — two
candidates, and this tells you which:

```sh
~/.venvs/epubkit/bin/epubkit --version
```

If that prints a version, the install is fine and it is a PATH problem: either
`~/bin` is not on your PATH, or it is and zsh has not noticed the new file yet,
because zsh caches where commands live. `rehash`, or a new terminal tab, fixes
the second. If instead it says *No such file*, the install never completed —
`ln -s` creates a link happily even when nothing is on the other end, so the
symlink existing proved nothing. Re-run the pip install and read the whole
output, not the last line.

**A long `pip install` that fails somewhere in lxml** usually wants Xcode's
command line tools: `xcode-select --install`.

## Use

```sh
epubkit init      # asks a few questions and writes the config for you
epubkit doctor    # checks the pieces are installed
epubkit check     # confirms the config works and pings the model
epubkit run
```

`epubkit init` is a guided setup rather than a file to edit. It counts the
books in the folder you give it, refuses a library folder that is the same as
the source, and — for a local model — asks the server which models it actually
has and lets you pick from that list, which is where a typed-from-memory model
name usually goes wrong. `epubkit init --defaults` writes the example config
without asking anything, for scripts.

To change one setting afterwards, no editor needed:

```sh
epubkit config list                        # every setting and its value
epubkit config get ai.model
epubkit config set ai.model qwen3:8b
epubkit config set convert.split_spreads always
```

Values are typed to match the setting, so `run.workers lots` is refused rather
than quietly stored, and the comments in your config file survive the edit. An
API key is refused outright — those belong in the environment.

Or skip the config file entirely:

```sh
epubkit run --in ~/Downloads/books --out ~/Books/kobo \
            --provider anthropic --model claude-sonnet-4-5 --workers 4
```

Other things you will want:

```sh
epubkit run --no-recaps               # structure and metadata only
epubkit run --only validate --in ~/Books/kobo   # re-check a built library
epubkit run --allow-imperfect         # ship books that improved but aren't clean
epubkit run -n                        # say what would happen, touch nothing
epubkit validate ~/Books/kobo         # EPUBCheck a folder, one line per book
epubkit recaps "Some Book.epub"       # recaps for one book
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

Wide pages are split. A landscape page in a picture book is nearly always a
two-page spread photographed whole, and left as one page it puts both halves
at half size on a six-inch screen. epubkit measures the page shape and splits
anything wide enough to be a spread.

By default a page is left whole where the artwork runs across the gutter — a
bus driving through the middle of the book is one picture, not two. In a book
where *every* illustration does that, though, the check protects every page
and nothing gets split at all, so the run always reports how it went
("spreads: 4 of 6 split, 2 left whole"). When the answer should have been all
of them, `convert.split_spreads = "force"` cuts every wide page down the
middle regardless.

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

**kepub** wraps the text in `koboSpan` elements, which is what gives a Kobo
page numbers, reading statistics and a position it actually remembers — the
device's `ChapterIDBookmarked` points straight at one of those spans. A book
that arrived as a kepub is repaired rather than re-wrapped, because
renumbering existing spans moves where the reader resumes. `convert.kepub =
false` keeps everything as plain EPUB.

**validate** runs EPUBCheck. By default a book with remaining errors is
reported and not shipped; `--allow-imperfect` ships it when it is measurably
better than what it replaced.

**recaps** summarises each chapter as of the *previous* one, so a recap can
never spoil the chapter you are about to read. Recaps are keyed to the spine,
not to chapter numbers, so they survive a restructured book. A cast list tracks
first appearances. The generated files go in the manifest, not the spine, so
they never interrupt reading.

Along the way the same pass does the smaller jobs: a plain typographic cover
for a book with no artwork at all (never replacing real art), recompression
for books bigger than `convert.slim_above_mb`, a structural check of the
archive that asks different questions from EPUBCheck, and a cast list
collapsed so one character is one person rather than three.

A file that holds several books is detected and named in the report, not
split — a wrong boundary produces a book missing its first chapter and another
with two, and neither announces itself. `epubkit split BOOK.epub` works out
the boundaries and shows you the plan; `--apply` writes the parts once you
have read it.

**report** writes per-book prose that says what was actually wrong with the
book — not that a file was processed — and keeps damage the book arrived with
separate from anything the pipeline introduced.

Every book goes through every step. A step that has nothing to do says why in
the report — "slim: under the threshold (12 books)", "plan_omnibus: too short
to be a bundle (9 books)" — so a step that quietly does nothing is visible
rather than invisible.

## Providers

`ollama` (default, local, no key), `anthropic`, `openai`. Keys come from
`ANTHROPIC_API_KEY` / `OPENAI_API_KEY` in the environment. A key written into
a config file is rejected at load: config files get committed, environment
variables do not.

`epubkit check` pings the endpoint and tells you if the model is missing before
you start a four-hour run. Do that first, every time — it is the difference
between finding out now and finding out in the third hour.

The Ollama endpoint does not have to be local. Pointing it at another machine
on your network, or across a VPN, is the same flag:

```sh
epubkit check --provider ollama --model qwen3:8b --endpoint http://10.0.0.5:11434
```

### Reasoning models

qwen3, deepseek-r1 and their relatives narrate their thinking before they
answer. epubkit asks for that to be turned off, strips any that arrives
anyway, and reads the model's *last* complete JSON object rather than
everything between the first brace and the last — a reasoning model quotes the
answer shape to itself on the way past, and the span between the two braces is
not JSON.

If you are running one of these on a single GPU, set `concurrency = 1` under
`[ai]`. Parallel requests queue behind each other on one card, so the default
of 2 buys nothing and can push a model into swapping.


## Coming from kobokit

epubkit was called kobokit until September 2026. The name `kobokit` now
belongs to a different tool (an installer that sets up a Kobo), so nothing here
answers to it any more: there is no `kobokit` command and no `kobokit` Python
package in epubkit. Everything you already have on disk keeps working, though.
Nothing is moved or rewritten, and each old name prints one notice saying what
to rename:

| Old | New | What happens if you leave it |
|---|---|---|
| `kobokit.toml` (here or in `~/.config/kobokit/`) | `epubkit.toml` (`~/.config/epubkit/`) | still read; the new name wins if both exist |
| `KOBOKIT_*` environment variables | `EPUBKIT_*` | still read; the new name wins if both are set |
| `<library>/.kobokit-work` | `.epubkit-work` | used in place, so an interrupted run resumes |
| `<library>/.kobokit-originals` | `.epubkit-originals` | used in place; your originals are never moved |
| `kobokit-state.json` | `epubkit-state.json` | copied to the new name on the next run |

To finish the move, rename those yourself when convenient, and remove the old
install (`pip uninstall kobokit`, or delete the virtualenv it lived in) before
installing the new kobokit, so the two commands do not collide.

## Configuration

Defaults, then `epubkit.toml`, then `EPUBKIT_*` environment variables, then
command-line flags — each layer overriding the one before it. epubkit looks for
`epubkit.toml` beside the library, in the current directory, and in
`~/.config/epubkit/`. An unrecognised setting is an error that names the
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
# epubkit
