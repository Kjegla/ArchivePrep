# ArchivePrep - v4 status

**v4.6 is the current release** and supersedes [v34](../../releases), which
shipped under the project's former name, Kjegla's Photo Organizer.

The number goes down while the software goes forward: v22 through v35 were
increments of a single script, and v4 is a new architectural generation, not
the next step in that sequence. `v35-wip`
remains on the repository as the historical snapshot of the last script-based
version.

## Before running V4 on a folder organized by an older build

**The files the application writes are now named `archiveprep_*` instead of
`kjegla_*`, and undo records are found by that name.** An undo record left by
v35 or earlier will not be offered by the V4 window.

If any folder still has a run you might want to reverse:

- **Use "Undo Last Run" with the old build first**, or
- rename its `kjegla_undo_*` files to `archiveprep_undo_*` by hand.

Nothing is lost either way - the files and the record are both still there -
but V4 will not find the record for you. Folders with no pending undo need
nothing.

## What ArchivePrep is for

**Turn an unknown, messy media collection into a clean, understood archive you
can merge into long-term storage with confidence.** It analyses, transforms,
and gets out of the way. It is not, and will not become, a digital asset
manager - no database, no catalog, no thumbnail browser, no editor, no cloud.
The absence of a library is the product.

It runs *before* Immich, digiKam or a NAS, not instead of them.

## Why the features exist

Every capability traces to a real archival problem:

1. **Organizing mixed media** from many devices and sources
2. **Detecting true duplicates** before merging into the archive
3. **Verifying integrity** - after finding that one of three iCloud exports
   contained corrupted files
4. **Handling real-world export formats**, Google Takeout above all

Extension-mismatch *repair* was not one of them, and has been removed. Reading
file headers stays, because problem 4 depends on it - Takeout files with the
extension chopped off are invisible otherwise - but nothing is renamed, and
nothing is filed differently, on the strength of what it finds.

## Honest answers over confident guesses

`Unknown Camera` is not a failure. If a file's camera cannot be established
from its own metadata or from the photos it was captured with, Unknown is the
correct answer and it stays. The measure of the result is whether the archive
is understandable and trustworthy - not how few files are Unknown.

**The file's modified time is no longer a date source.** Measured on a real
86,791-file collection: 7,622 files were being dated by it, and 7,511 of those
landed on five days in 2026 - the days the Takeout zips were extracted. A
filename stamped by the camera (`IMG_20200904_144311`, `PXL_20251103_...`) now
sits in that slot for the 1,011 it can answer for; the remaining 6,611 go to
`Unknown Date`.

The cost is real and worth stating: a folder copied off an old drive **with
its timestamps intact** now goes to `Unknown Date` too. In that collection
that was 68 files against 7,511 filed under a download date. Restoring it is
one line if a collection ever justifies it.

## Where it stands

Four tiers are done. Every one ended with the full suite green.

| Tier | What landed |
|---|---|
| **1 Safety** | Append-only undo journal; every transfer verified; copy-mode undo no longer deletes blindly |
| **2 Safety net** | pytest, independent tests, the golden corpus |
| **3 Structure** | Core split from the window; one record per file; deciding split from doing; per-run manifest; CI runs the tests |
| **4 Understanding** | One place that identifies a file; Takeout sidecar dates; captures; embedded motion clips |

**117 tests, 455 checks.** 109 of them need no screen at all.

```bash
pip3 install -r requirements-dev.txt
python -m pytest tests/ -q
```

## The V35 open questions are all answered

These were left deliberately undecided for the V4 review.

**1. The redundant motion-photo clips.** Resolved. A video whose bytes sit,
byte for byte, at the end of a photo from the same capture is a duplicate of
it and goes to `Duplicates/` with that reason. Matched by content, never by
name - Takeout's naming has changed before and Samsung does the same thing
under different names.

**2. RAW files with no surviving JPEG.** Resolved by the capture rule rather
than by a decision. Files from one shutter press share a date; a RAW with no
capture siblings inherits nothing and stays honestly unidentified instead of
being assumed into the folder of the others.

**3 and 4, both about extension mismatches.** Answered by deleting the
question. Extension repair is gone entirely - `Wrong Extension/`, the
checkbox, the rename undo journal, the harmless/breaking split and the
`misnamed` verdict with it.

It solved none of the four problems above. It existed because the application
already reads file headers while solving the real ones, and it had grown a
second undo system for the least important thing here. Measured on a real
86,791-file collection: 804 files whose name disagreed with their contents,
**none of which failed to open and none of which were damaged**.

What stays is content sniffing, which is load-bearing for problem 4 - Takeout
files with no extension are invisible without it - and the health check now
verifies a file as what it *is* rather than as what it is called. Nothing is
renamed on the strength of either. The name a camera or an export gave a file
is the source's business.

**Removing it also fixed something.** `misnamed` returned early, so the files
whose names could not be trusted were the exact files that never got
health-checked at all - 804 of them in that collection. They are checked now.

## What changed that you would notice

- **A RAW and the JPEG it was taken with now land in the same date folder.**
  They used to be split across different years, because a RAW carries no date
  this application reads and fell back to the file's modified time. The old
  behaviour was asserted as correct by the test suite until V4.
- **Photos whose EXIF Google stripped are filed by when they were taken**,
  read from the Takeout sidecar, instead of by when you downloaded the export.
- **Every run writes `archiveprep_manifest_*.csv`** - one row per file: what it was,
  what was decided, where it went, its camera, its date and *where that date
  came from*, its integrity verdict, and its content hash where the duplicate
  hunt already computed one. This is what to read when merging a batch into an
  existing archive.
- **The run log lists decisions, then actions**, rather than interleaving them.
  That is the honest consequence of deciding everything before moving anything.

## Known, and deliberately left alone

`run_health_check` rebuilds a `{path: (verdict, reason)}` dictionary from the
records it already has, so that the twenty lines of report code below it -
written before records existed - keep working unchanged. It is one line and it
is inert. Rewriting report generation carries more regression risk than the
tidiness is worth, and it was postponed on those grounds rather than missed.

## Not yet done

- Sidecar dates cover Google Takeout JSON. `.xmp` and `.aae` are not read.
- Takeout JSON is read for a missing capture date and then left alone -
  it is not copied into the organised archive. The goal is the media's own
  metadata, not Google Photos concepts.
- Manifest hashing is opportunistic by design: a file with a unique size is
  never read, so it has no hash. dupeGuru still owns cross-archive comparison.
