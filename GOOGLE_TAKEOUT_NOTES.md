# Google Takeout / Pixel filename notes

Research notes from picking apart a real 3,322-file Google Takeout export from
a Pixel 9 Pro. Written down so none of it has to be worked out twice.

The short version: **`.COVER` is never junk, and the only genuinely redundant
files are the ones with no extension at all.**

## How Takeout names things

Google splits one photo into several files and tags each one. The tag says
which part of the group it is:

```
PXL_20250418_155419102.RAW-01.COVER.jpg      <- the JPEG you actually see
PXL_20250418_155419102.RAW-02.ORIGINAL.dng   <- the RAW

PXL_20250417_131910377.BURST-01.COVER.jpg    <- the frame Google picked
PXL_20250417_131910377.BURST-02.jpg
PXL_20250417_131910377.BURST-03.jpg

PXL_20250413_174728943.LONG_EXPOSURE-01.COVER.jpg
PXL_20250413_174728943.LONG_EXPOSURE-02.ORIGINA.jpg    <- note the truncation

PXL_20250406_174655788.VB-01.COVER.mp4       <- original video
PXL_20250406_174655788.VB-02.MAIN.mp4        <- the Video Boost version
```

`-01.COVER` means **"this is the one to show"**, not "this is a thumbnail".
Counts in the sample export:

| Tag | Count | What it is |
|---|---|---|
| `RAW-01.COVER` | 886 | the JPEG of a RAW shot |
| `VB-01.COVER` | 3 | the original video, before Video Boost |
| `BURST-01.COVER` | 2 | the chosen frame of a burst |
| `LONG_EXPOSURE-01.COVER` | 1 | the long-exposure result |

**Deleting `.COVER` files would destroy 889 real photos.** Do not treat the tag
as a sign of a throwaway file.

Video Boost pairs are *both* worth keeping. The COVER is not a subset of the
MAIN - checked directly, and 40 MB versus 180 MB are two genuinely different
encodings of the same footage.

## Filename truncation

Takeout truncates long filenames, sometimes chopping the extension clean off:

```
PXL_20250507_050944066.RAW-01.MP.COVER          <- ".jpg" was cut off... almost
PXL_20250413_174728943.LONG_EXPOSURE-02.ORIGINA.jpg   <- lost the final "L"
```

A file with no recognizable extension is invisible to anything that scans by
extension - never sorted, never checked for damage, never deduplicated. That is
why the organizer reads the first 16 bytes of files with unfamiliar extensions.

## The redundant motion-photo clips

14 files in the sample export are pure waste. They come in pairs:

```
PXL_20251109_190231652.MP.jpg    5,848,574 B   the photo
PXL_20251109_190231652.MP        3,486,831 B   redundant
```

A **motion photo** is one file: a complete JPEG with a short MP4 welded onto the
back. Takeout also writes that MP4 out separately, and truncation removes its
extension. So the same bytes are stored twice.

Worked example, checkable in Explorer with just the Size column:

```
PXL_20250615_013103222.RAW-01.MP.COVER.jpg    7,573,412 B
  |- still image                              3,482,775 B  (ends properly at FFD9)
  |- appended video                           4,090,637 B
PXL_20250615_013103222.RAW-01.MP.COVER        4,090,637 B  <- exactly the same
```

Verified by direct byte comparison on **14 of 14**: each extension-less file is
byte-for-byte identical to the tail of its `.jpg`. Total waste **41.3 MB**.

Deleting them loses nothing. The motion still plays, because the phone and
Google Photos read the clip from inside the `.jpg` - the separate file is not
what they use.

A scan of all 3,322 files for *any* video hiding inside a sibling image found
exactly these 14 and nothing else.

**Rule of thumb: the file with no extension at all is the throwaway. Anything
ending `.jpg` or `.dng` is the real photo.**

## Proposed detection rule (not implemented)

Where these clips should go is **still undecided** - see
[STATUS.md](STATUS.md). How to *find* them is settled:

> A file qualifies only when its contents are video **and** those contents sit,
> byte for byte, at the very end of a sibling image sharing the same photo
> name.

Deliberately not by filename. Takeout's naming has changed before, Samsung
motion photos work the same way under different names, and the bytes are the
only thing that cannot be wrong. A real video cannot qualify by accident - it
would have to already be inside one of your photos, which only happens when it
genuinely is a spare copy.

## What the extension fixer touches today

Of 3,322 files, 30 are moved to `Wrong Extension/` - the 14 clips above, plus
16 WEBP images saved as `.png` (hash-named, no camera EXIF, saved from the web
or an app rather than taken with the camera).

The remaining 3,292 are left completely alone. Specifically **untouched**:

| | On disk | Touched |
|---|---|---|
| `.RAW-01.COVER.jpg` | 891 | 0 |
| `.ORIGINAL.dng` | 897 | 0 |
| `.MP.jpg` | 5 | 0 |
| `.VB-*.mp4` | 6 | 0 |
| BURST / LONG_EXPOSURE | 8 | 0 |

The check only asks whether the extension matches the contents. A JPEG named
`.jpg` is correct no matter what else appears in the name, so `.COVER`, `.MP`
and `.RAW-01` are all irrelevant to it.

## Sidecar JSON

Takeout writes a `.supplemental-metadata.json` next to most files. That is the
actual sidecar data - upload time, geolocation, `googlePhotosOrigin`. The
organizer ignores `.json` outright and never reads or moves it.
