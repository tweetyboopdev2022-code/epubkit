# What a Kobo actually does

Everything here was learned by getting it wrong first, on a real 373-book
library. Most of it is not written down anywhere official, and several of these
facts cost a book or a reading position to find.

## Metadata

A Kobo reads a book's metadata **once**, when it first sees the file. Editing
the file afterwards and copying it back over the old one changes nothing on
screen: the device already has what it thinks the book is. To make a metadata
change show up, the book has to be removed from the device, the device has to
notice it is gone, and the new file has to arrive as a book it has not seen.

A Kobo ignores series information inside a sideloaded file entirely, unless the
NickelSeries mod is installed. Series only shows up if it is written into the
device's own database. This is why kobokit puts the series in the **filename**
as well as in the OPF: the filename is what sorts in a folder listing, and it
is the only part the device cannot ignore.

## Reading positions

The database is `.kobo/KoboReader.sqlite` on the device.

A book's position lives in `content`, and **two separate groups of fields**
have to agree or the device contradicts itself:

| group | fields | what it drives |
|---|---|---|
| position | `ChapterIDBookmarked`, `___PercentRead`, `ReadStatus`, `DateLastRead` | the "Continue reading" tile on the home screen |
| reading history | `FirstTimeReading`, `TimesStartedReading`, `LastTimeStartedReading` | the read/unread badge in My Books |

Set only the first group and you get a book showing 51% on the home screen and
"Unread" in the library list, which is exactly as confusing as it sounds.

`ChapterIDBookmarked` points at a **koboSpan** — `<span class="koboSpan"
id="kobo.12.4">` — inside the book's own XHTML. It is a direct reference. Move
that span, renumber it, or rebuild the document it lives in, and the reader
resumes somewhere else. This is the single hardest constraint on rewriting a
book, and it is why kobokit's content gate counts every koboSpan before and
after and throws away any rewrite that lost one.

If you restore positions from a snapshot, re-read the live database first. A
snapshot taken weeks ago will happily roll a finished book back to where it was
that day.

## Why so many books fail EPUBCheck

The overwhelming cause, by a very long way: the package declares
`version="2.0"` while the content is EPUB 3 throughout — `epub:type`, `role`,
`aria-*`, `properties`, `refines`, an HTML5 doctype. EPUBCheck validates content
against the version the package *claims*, so every EPUB 3 feature in the book
is reported as an error. One library produced 123,287 errors this way.

Promoting the declaration to 3.0 and finishing the job — a nav document,
`dcterms:modified`, `opf:role` and `opf:file-as` moved into `<meta refines>`,
the cover-image property, the guide dropped — removes the whole class. It also
introduces two problems if done carelessly:

- Dropping the XHTML doctype breaks every named entity in the file. `&nbsp;`
  has no definition without a DTD. Numericise them (`&#160;`) *before* the
  doctype goes.
- `<meta http-equiv="Content-Type">` is not valid in EPUB 3. Replace it with
  `<meta charset="utf-8"/>`.

Other things worth knowing:

- NCX ids must be valid XML names. EPUBCheck says "without colons", but a
  leading digit fails just as hard and the message does not say so.
- Two `dtb:uid` metas with their attributes in opposite orders are the same
  element. Canonicalise before matching, never match an attribute order.
- `url(about:invalid)` in a stylesheet trades a missing-resource error for an
  invalid-URL error. Remove the whole declaration.
- A recovering XML parser will happily restructure a malformed NCX into
  something worse. Patch it textually, or drop it and rebuild from the spine.

## Regexes over XML

Attribute order carries no meaning, so any pattern of the form
`<item[^>]*id="..."[^>]*href="..."` silently misses every element written the
other way round — and silence is the problem: nothing errors, things just go
missing. And a naive in-tag pattern matches inside quoted values:
`content="width=1200, height=1600"` on a fixed-layout book looks exactly like
an obsolete presentational attribute. That one destroyed 210 pages before it
was caught. Tokenise attributes properly.

## Books that sync from Google Drive

A Kobo that syncs from Drive keeps them under `.kobo/google_drive/` on the
device. Rewriting the file in Drive therefore reaches the device without ever
plugging it in — which is convenient and also means a bad rewrite reaches it
just as fast.

## Mounted devices

An unmounted or stale mountpoint still lists as an empty directory. `ls`
succeeds, writes appear to work, and nothing lands on the device. Before
writing, check that the mount holds the number of books you expect; a count of
zero on a device you know has hundreds means the mount is stale, not that the
device is empty.

## macOS cloud files

Files in a cloud-synced folder that have not been downloaded cannot be read by
a normal file API — the read fails with `EDEADLK` ("Resource deadlock
avoided"), which is not what a missing file usually looks like. They have to be
materialised first.
