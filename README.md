# Kjegla's Photo Organizer

> **⚠️ This branch is a work in progress - not a release.**
> The released version is [**v34**](../../releases). V35 adds duplicate
> handling, damage checking and extension fixing, but is still under review and
> has not been built into a download. See [STATUS.md](STATUS.md) for what is
> proven, what is not, and what is still undecided.

Sorts a messy folder of photos and videos into tidy folders by **camera model**
(e.g. `Sony A6000`, `iPhone 16 Pro`, `Samsung Galaxy S24 Ultra`), with optional
date subfolders inside each camera folder.

```
My Photos\
├── Sony A6000\2024\02-February\DSC01234.JPG
├── iPhone 16 Pro\2025\01-January\IMG_5555.HEIC
└── Samsung Galaxy S24 Ultra\2024\06-June\20240610_101512.jpg
```

## Features

- **Move or Copy** - copy mode never touches your originals
- **Preview (dry run)** - see exactly what would happen before doing it
- **Undo Last Run** - one click puts everything back
- Date subfolders: none, year, month, or year + month
- Reads real photo dates from EXIF, and real video dates from MP4/MOV metadata
- iPhone **HEIC** photos supported (sorted by exact model)
- RAW files (ARW, CR2, DNG, NEF, …) matched to their JPEG and sorted together
- Optional separation of RAW files and Android screenshots into subfolders
- **Finds duplicates by content, not by filename** - so `IMG_1234 (1).jpg` is
  recognized as the same photo as `IMG_1234.jpg` (see below)
- **Checks files for damage** - spots half-copied and truncated photos/videos
- **Clears out empty folders** left behind after a move
- Optional recursive scan of subfolders - safe to re-run any time
- Fast: multithreaded scanning, live progress with ETA

## Merging folders without losing anything

Three options make merging messy folders together a lot less nerve-racking.
**Nothing is ever deleted** - files are only ever moved somewhere you can find
them, and **↩️ Undo Last Run** puts everything back.

### ♻️ Find duplicates by content

Two files count as duplicates only when their contents match **exactly, byte
for byte** - never because their names look similar. That means `IMG_1234.jpg`,
`IMG_1234 (1).jpg` and `IMG_1234 - Copy.jpg` are finally recognized as one
photo, and two genuinely different photos are never mistaken for each other.

One copy is kept and organized as normal; the rest are moved to a
**`Duplicates`** folder that mirrors the folders they came from, so you can
look through them before deciding to delete anything. A
`kjegla_duplicates_*.txt` report lists every set and which copy was kept.

Which copy is kept: an intact file always beats a damaged one, then a file
with real camera info and a real date wins, then the tidier filename
(`IMG_1234.jpg` beats `IMG_1234 (1).jpg`).

It's quick even on big folders: two files can only be identical if they are the
same size, so anything with a unique size is ruled out without ever being read.

### 🩺 Check files for damage

**🩺 Check Files** scans your folder and reports anything broken without moving
a thing - useful before you commit to anything. Tick **Check files for damage
while organizing** and damaged files get moved to a **`Corrupt`** folder
instead of being mixed in with the good ones.

- **Quick** (default) checks each file's header and its end-of-file marker,
  which catches interrupted copies, half-downloaded files and 0-byte files.
- **Thorough** fully decodes every image to catch subtler damage. Much slower.

RAW files and a few video formats can't be verified without decoding them, so
they're honestly reported as *"couldn't be checked"* rather than guessed at -
that never means they're broken.

Plenty of normal photos have extra data sitting after the picture, and none of
them are treated as damaged: **motion photos** (Pixel `PXL_*.MP.jpg` and
Samsung, which append a short video clip), **iPhone Portrait/dual-camera
shots** (two images in one file, where the second one is often the part that
got cut off), and files that a recovery tool **padded with zero bytes**. What
counts as truncated is the photo's own data never being finished.

### 🏷️ Wrong file extension

Some files are perfectly fine but their name lies about the format - a JPEG
photo saved as `.MOV` (which then fails to open, because your computer hands it
to a video player), or a WEBP picture saved as `.png`. These are **not
damaged**, so they are never treated as such.

Each one is **renamed to the extension it should have had** and moved into a
**`Wrong Extension`** folder mirroring where it came from, so you can look
through them before putting them back. The renames get **their own undo
record**, stored inside that folder - the **↩️ Undo Renames** button reverses
just the renames and leaves the rest of the run alone.

This also finds files nothing else would. Google Takeout truncates long
filenames, chopping `.jpg` or `.mp4` clean off the end and leaving things like
`PXL_20250507_050944066.RAW-01.MP.COVER`. Because those no longer look like
photos, they would otherwise be skipped entirely - never sorted, never checked,
never deduplicated. Any file with an unfamiliar extension gets its first 16
bytes read to see what it actually is.

### 🧹 Delete empty folders

After a **Move**, any folder left genuinely empty is removed. Only empty
folders - no file is ever deleted, so a folder holding so much as a stray
`Thumbs.db` is left alone.

## Easiest way: download from Releases

Go to the [**Releases**](../../releases) page and grab the file for your computer:

| Your computer | Download | Then |
|---|---|---|
| **Windows** | `PhotoOrganizer-Windows.exe` | Double-click it |
| **Mac** | `PhotoOrganizer-macOS.zip` | Unzip, then **right-click → Open** (see below) |

> **Windows**: SmartScreen may warn about an unknown publisher the first time.
> Click **More info → Run anyway**.
>
> **Mac**: the app isn't signed with an Apple developer certificate, so the
> first launch must be **right-click (or Ctrl-click) the app → Open → Open**.
> Double-clicking will just show a warning. You only need to do this once.
> If macOS still refuses, open Terminal and run:
> `xattr -cr ~/Downloads/PhotoOrganizer.app`

## Running from source (Windows/Mac/Linux)

Requires [Python 3.10+](https://www.python.org/downloads/) - on Mac, install it
from that link (the python.org installer includes everything needed).

```bash
git clone https://github.com/Kjegla/Photo-Organizer.git
cd Photo-Organizer
pip3 install -r requirements.txt
python3 photo_organizer.py
```

## How to use it safely

1. Pick your photo folder with **Browse...**
2. Choose **Copy** mode the first time (originals stay untouched)
3. Click **🔍 Preview (Dry Run)** and read what it plans to do
4. Happy? Click **▶️ Execute Operation**
5. Changed your mind? **↩️ Undo Last Run**

A log file (`kjegla_media_log_*.txt`) is written into the source folder for
every run, so you can always see what happened. Duplicate and damage reports
(`kjegla_duplicates_*.txt`, `kjegla_health_*.txt`) land there too.

## Running the tests

```bash
pip3 install -r requirements-dev.txt
python -m pytest tests/ -q
```

77 tests, 262 checks. They create real photos and videos in a temporary folder
and drive the actual app with its window hidden, then assert where every file
ended up - so a passing run means the real thing works, not a simplified copy
of it.

Every test starts from a clean folder, so you can run just the one you care
about instead of the whole suite:

```bash
python -m pytest tests/ -q -k undo
```

`tests/test_golden_corpus.py` holds the awkward real files - motion photos,
iPhone Portrait shots, zero-padded recoveries, Takeout's truncated names -
each with the verdict it has to get and a note on why it is in the list.
Most of them are there because a real photo was once judged wrongly.

## Building the .exe yourself

```bash
pip install -r requirements.txt pyinstaller
python -m PyInstaller --onefile --windowed --noconfirm --collect-all sv_ttk --name PhotoOrganizer photo_organizer.py
```
