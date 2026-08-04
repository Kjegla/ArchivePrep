# ArchivePrep

**Turns an unknown, messy media collection into a clean, understood archive you
can merge into long-term storage with confidence.**

> **v4.6 is the current release** and supersedes v34, which shipped under the
> project's former name, Kjegla's Photo Organizer. Despite the lower number,
> v4 is newer: it is a new architectural generation rather than the next
> increment. See [STATUS.md](STATUS.md) for what changed.

Media collections get messy on their own. People change phones. Cloud providers
change. Backups get duplicated. Exports arrive from different ecosystems. Old
drives get merged and folders run together. ArchivePrep analyses a collection,
does exactly what you asked, writes down what it did, and gets out of the way.

It runs **before** Immich, digiKam or a NAS - not instead of them.

```
Sony A6000\2024\02-February\DSC01234.JPG
iPhone 16 Pro\2025\01-January\IMG_5555.HEIC
Samsung Galaxy S24 Ultra\2024\06-June\20240610_101512.jpg
```

## What it is not

No library. No database. No catalog. No thumbnail browser. No editor. The
absence of a library is the point - your files stay files, and nothing takes
custody of them.

## Values

- **Fully offline.** No accounts, no cloud, no network.
- **No telemetry.** Nothing is measured, collected or sent.
- **No advertising, no subscriptions, no lock-in.**
- If it is ever supported, that would be voluntary donations only. Never a
  commercial platform.

## The problems it solves

Every capability exists because of a real archival problem, not because it
seemed clever:

**Organizing mixed media from many devices.** Sorts by camera model, then
optionally by date. Reads real dates from EXIF, from video container headers,
and - where Google stripped them - from Takeout's sidecar JSON. Files from one
shutter press stay together: a RAW is filed with the JPEG it was taken with,
rather than by whenever its file was last touched.

**Detecting true duplicates before merging.** Two files are duplicates only
when their contents match **exactly, byte for byte** - never because their
names look similar. `IMG_1234.jpg`, `IMG_1234 (1).jpg` and
`IMG_1234 - Copy.jpg` are finally recognized as one photo, and two genuinely
different photos are never mistaken for each other. One copy is kept; the rest
move to `Duplicates\`, mirroring where they came from. Nothing is deleted.

It is quick even on big folders: two files can only be identical if they are
the same size, so anything with a unique size is never read at all.

**Verifying integrity.** Built after finding that one of three iCloud exports
contained corrupted files. Spots half-copied and truncated photos and videos,
and is careful about what it does *not* call damage - motion photos, iPhone
Portrait shots and zero-padded recoveries all have data after the image and are
all perfectly fine. Three answers, and only three: **healthy**, **damaged**, or
**unknown** for formats it cannot verify — said honestly rather than guessed
at, because the question is only ever "would I trust this in my archive?"

**Handling real-world exports.** Google Takeout truncates long filenames,
sometimes chopping the extension clean off. Those files are found by reading
their contents, so they are never silently skipped. Takeout also writes a
motion photo's video out twice; the redundant copy is recognized by its bytes
sitting inside the photo, never by its name.

## Honest answers over confident guesses

If a file's camera cannot be established from its metadata or from the photos
it was captured with, it goes to `Unknown Camera` and stays there.

The same goes for dates, and they are asked for in this order:

1. the file's own metadata - EXIF, or a video container's header
2. Google Takeout's sidecar, where Google stripped the metadata
3. the photo it was captured with - a RAW takes its JPEG's date
4. **its filename**, but only where a camera stamped the capture time into it:
   `IMG_20200904_144311`, `PXL_20251103_092233580`, `20240610_101512`,
   `Screenshot_20240301-142205`
5. `Unknown Date`

**The file's modified time is not on that list, deliberately.** On anything
that arrived as a download it is the day you extracted the zip - one real
archive had 7,511 photos filed under five days in 2026 on exactly that basis,
and a thousand of them were carrying their true date in their own name the
whole time.

A date is only ever read from a filename shape a manufacturer publishes, and
only from the start of the name. A file called
`Facetune_03-03-2019-21-05-08.jpg` is `DD-MM-YYYY` and is left alone rather
than read as a different month and day. **A wrong year vanishes into an
archive and is never found again; `Unknown Date` is a pile you can come back
to.**

The modified time is still **recorded** in the manifest for every file, next
to the date that was actually used. It is not thrown away - it is just not
allowed to decide where a photo goes. If you ever want to overrule that, the
evidence is in the file.

That is not a failure - it is the honest answer, and an archive you can trust
is worth more than one where things were guessed into place.

## Safety

- **Copy mode never touches your originals.**
- **Preview** shows exactly what would happen, and an Execute straight
  afterwards does precisely that - the same decisions, replayed.
- **Undo** puts everything back. The record is written as each file moves, so
  an interrupted run is still fully reversible.
- Every transfer is verified: a move only removes the original once the copy is
  confirmed complete.
- Nothing is ever deleted, and nothing is ever renamed. Duplicates and damaged
  files are set aside in folders mirroring where they came from.

## What a run leaves behind

| File | What it holds |
|---|---|
| `archiveprep_manifest_*.csv` | One row per file: what it was, what was decided, where it went, its camera, its date **and where that date came from**, its modified time, its integrity verdict, its content hash where one was computed. This is what to read when merging into an existing archive. |
| `archiveprep_log_*.txt` | Everything the run did, in order |
| `archiveprep_duplicates_*.txt` | Every set of identical files and which copy was kept |
| `archiveprep_health_*.txt` | From **Check Files**, which moves nothing |

The log in the window has a **Find** box under it. Type any part of a filename
and every occurrence is highlighted where it sits, with a count and
**Previous** / **Next** to step between them - so "what happened to this one
file?" is answered without opening the `.txt`. It keeps counting while a run is
still printing, and it does not scroll the log out from under you while it
does. `Ctrl+F` jumps to it, `Enter` and `Shift+Enter` step, `Esc` clears it.

**The summary reports what the run decided, not what it noticed** — and only
what changed, so a run with no duplicates does not tell you so. A preview says
what it *would* do; a real run says what it did. The run header records every
setting that was in force, so the log can always be checked against what was
asked for. Preview then Execute leaves behind exactly what a fresh Execute
does, manifest included.

**The window and the run log say the same words.** A line read in the `.txt`
can be pasted into Find and located in the window, and the reverse. They are
written from one string rather than two, so they cannot drift apart again. The
only things the window says alone are remarks about the files a run produced -
it would be circular for a log to announce its own name inside itself.

## Your filenames are yours

**ArchivePrep never renames a file.** These are camera and cloud exports; the
name is the source's business, and a tool that quietly second-guesses it is a
tool you have to supervise.

It does read the first bytes of every file, for two reasons that earn it.
Google Takeout truncates long filenames and sometimes chops the extension
clean off — without reading the contents, those files would be invisible to
this application entirely. And a file is health-checked as what it **is**: a
photo that arrived named `.dng` is verified as a photo, rather than handed to
a video structure check that would report nonsense about it.

Neither of those renames anything, moves anything, or files anything
differently.

## Maintenance extras

- **Empty folder cleanup** after a move. Only genuinely empty folders — a
  folder holding so much as a stray `Thumbs.db` is left alone, because a folder
  with a file in it is not empty and this application does not decide which of
  your files do not count.

  Those folders look empty in Explorer, since `Thumbs.db` is hidden. So when a
  folder is left holding nothing your archive wants, you are told and asked
  whether to clear it. **That prompt is the only place this application ever
  deletes a file**, and it asks twice, because two different things get left
  behind:

  - **Caches the system regenerates** — `Thumbs.db`, `desktop.ini`,
    `.DS_Store`, macOS `._` stubs. Deleting one loses nothing.
  - **Sidecars only their own device can read** — `.aae`, `.thm`. An `.aae`
    holds the edits you made in Apple Photos; nothing outside Apple reads it,
    not Windows, not a NAS, not Immich. Nothing regenerates it either, so it
    is a **separate question** you can answer no to — and if you do, the file
    and its folder both stay.

  Never offered at all: `.xmp`, which Lightroom and darktable actively read,
  and Takeout's `.json`, which ArchivePrep itself reads for a capture date
  Google stripped from the photo.

## When something cannot be moved

Files copied from another computer often still carry that computer's
permissions. Your account can read them but not move them, and Windows refuses
with *access denied* — the same files delete fine in Explorer only because it
raises a UAC prompt and quietly uses your administrator rights.

ArchivePrep does not ask for administrator rights and will not change
permissions on your files by itself. It reports every file it could not move,
in one block at the end of the run, and gives you the two commands that fix it.
Those files stay exactly where they were, and the manifest records them as
`failed` rather than pretending they arrived.

## Install

Grab the file for your computer from [**Releases**](../../releases):

| Your computer | Download | Then |
|---|---|---|
| **Windows** | `ArchivePrep-Windows.exe` | Double-click it |
| **Mac** | `ArchivePrep-macOS.zip` | Unzip, then **right-click → Open** |

> **Windows**: SmartScreen may warn about an unknown publisher the first time.
> Click **More info → Run anyway**.
>
> **Mac**: the app isn't signed with an Apple developer certificate, so the
> first launch must be **right-click (or Ctrl-click) the app → Open → Open**.
> You only need to do this once. If macOS still refuses, open Terminal and run:
> `xattr -cr ~/Downloads/ArchivePrep.app`

## Running from source

Requires [Python 3.10+](https://www.python.org/downloads/).

```bash
git clone https://github.com/Kjegla/ArchivePrep.git
cd ArchivePrep
pip3 install -r requirements.txt
python3 archiveprep.py
```

## How to use it safely

1. Pick your folder with **Browse...**
2. Choose **Copy** the first time - originals stay untouched
3. Click **Preview (Dry Run)** and read what it plans to do
4. Happy? Click **Execute**
5. Changed your mind? **Undo Last Run**

## Running the tests

```bash
pip3 install -r requirements-dev.txt
python -m pytest tests/ -q
```

117 tests, 455 checks. They build real photos and videos in a temporary folder
and run the real code against them, so a passing run means the real thing
works - not a simplified copy of it. Every test starts from a clean folder, so
you can run just the one you care about:

```bash
python -m pytest tests/ -q -k undo
```

`tests/test_golden_corpus.py` holds the awkward real files - motion photos,
iPhone Portrait shots, zero-padded recoveries, Takeout's truncated names - each
with the verdict it has to get and a note on why it is in the list. Most are
there because a real photo was once judged wrongly.

## Building it yourself

```bash
pip install -r requirements.txt pyinstaller
python -m PyInstaller --onefile --windowed --noconfirm --name ArchivePrep archiveprep.py
```
