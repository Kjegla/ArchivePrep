# V4 status

**This branch is unfinished.** The released version is still
[v34](../../releases). Nothing here has been built into a download and no tag
has been created, so the Releases page is untouched.

`v35-wip` remains as the historical snapshot of the last script-based
generation. V4 is a new architectural generation, not an extension of it.

## What V4 is for

**Prepare messy media collections for long-term archival.** It analyses,
transforms, and gets out of the way. It is not, and will not become, a digital
asset manager - no database, no catalog, no thumbnail browser, no editor, no
cloud. The absence of a library is the product.

It runs *before* Immich, digiKam or a NAS, not instead of them.

## Where it stands

Four tiers are done. Every one ended with the full suite green.

| Tier | What landed |
|---|---|
| **1 Safety** | Append-only undo journal; every transfer verified; copy-mode undo no longer deletes blindly |
| **2 Safety net** | pytest, independent tests, the golden corpus |
| **3 Structure** | Core split from the window; one record per file; deciding split from doing; per-run manifest; CI runs the tests |
| **4 Understanding** | One place that identifies a file; Takeout sidecar dates; captures; embedded motion clips |

**93 tests, 333 checks.** 89 of them need no screen at all.

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

**3. Harmless versus breaking extension mismatches.** The distinction now
exists and is reported: a WEBP called `.png` opens everywhere (`harmless`), a
photo called `.MOV` is handed to a video player and fails (`breaking`), and so
is a Takeout file whose extension was truncated away. **What is done about
them has not changed** - both still go to `Wrong Extension/`. The information
comes first; changing the behaviour is a separate decision.

**4. Unknown-extension sniffing was tied to extension fixing.** Resolved by
removing the coupling. Turning off "fix wrong extensions" used to *hide* those
files completely - never sorted, never checked, never deduplicated. Finding a
file and deciding what to do with it are different questions, and only the
second is the user's to answer.

## What changed that you would notice

- **A RAW and the JPEG it was taken with now land in the same date folder.**
  They used to be split across different years, because a RAW carries no date
  this application reads and fell back to the file's modified time. The old
  behaviour was asserted as correct by the test suite until V4.
- **Photos whose EXIF Google stripped are filed by when they were taken**,
  read from the Takeout sidecar, instead of by when you downloaded the export.
- **Every run writes `kjegla_manifest_*.csv`** - one row per file: what it was,
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
- Sidecar files are not carried along into the organised output.
- The output layout is still camera-model first, with no choice of template.
- Manifest hashing is opportunistic by design: a file with a unique size is
  never read, so it has no hash. dupeGuru still owns cross-archive comparison.
