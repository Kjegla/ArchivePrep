#!/usr/bin/env python3
"""
Kjegla's Photo Organizer by Camera Model - Customized Edition (v35)
Safely organizes photos into folders based on camera model metadata.
Can either MOVE or COPY files with date-based subfolders.

v35 changes:
- Real duplicate detection by CONTENT, not by filename. Two files are
  duplicates only when their bytes match exactly (dupeGuru's "Contents"
  approach), so "IMG_1234 (1).jpg" is finally recognized as the same photo
  as "IMG_1234.jpg". Duplicates are never deleted - they are set aside in a
  "Duplicates" folder mirroring the path they came from.
- Smart keeper picking: within a group of identical files a damaged copy can
  never win over a healthy one, and a real filename beats a "(1)"/"- Copy" one.
- Damaged-file detection: a standalone "Check Files" button, plus an option to
  route damaged files to a "Corrupt" folder while organizing. Quick mode
  checks headers and end-of-file markers; Thorough mode fully decodes images.
- Empty folders left behind by a Move are swept away properly (whole source
  tree, bottom-up). Only genuinely empty folders - no file is ever deleted.
- Wrong file extensions: a file whose contents disagree with its name (a JPEG
  saved as ".MOV", a WEBP saved as ".png") is renamed to the extension it
  should have had and set aside in a "Wrong Extension" folder. These are not
  damaged, and are never treated as such. The renames get their own undo
  record inside that folder, driven by a separate "Undo Renames" button.
- Files with an unrecognized extension are identified by reading their first
  16 bytes, so Google Takeout's truncated names (".RAW-01.MP.COVER" with the
  ".jpg" chopped off) are no longer skipped entirely.
- RAW and video files match their JPEG on the shared photo name rather than
  the whole filename, so Pixel's "X.RAW-02.ORIGINAL.dng" now finds
  "X.RAW-01.COVER.jpg" instead of landing in Unknown Camera.
- A "PXL_" file whose metadata cannot be read is identified as a Pixel from
  its filename rather than being filed as an unknown camera.

Unfinished - see STATUS.md and GOOGLE_TAKEOUT_NOTES.md in the repo for what is
still undecided, notably how to handle Google Takeout's redundant motion-photo
clips and whether harmless extension mismatches deserve flagging at all.

v34 changes:
- Preview results are cached: Execute right after a Preview replays the
  previewed plan directly, skipping the whole re-analysis (metadata reads,
  duplicate hashing). The cache is validated against a folder fingerprint
  (every file's path/size/mtime) and the current settings - if anything
  changed since the preview, Execute automatically falls back to a fresh
  full analysis, so a stale plan can never run.

v33 changes:
- Real HEIC support via pillow-heif (exact iPhone model + EXIF dates)
- Optional recursive scan of subfolders (safe to re-run: already-organized
  files are recognized and skipped)
- True duplicate detection: name collisions with identical content are
  skipped instead of piling up as _1/_2 copies
- Undo Last Run button (moves files back, or deletes copies)
- Video dates read from MP4/MOV metadata instead of file-modified time
- Live ETA and counters in the status bar
- Modern Sun Valley (sv-ttk) theme with light/dark toggle
"""

import os
import re
import copy
import json
import time
import shutil
import hashlib
from pathlib import Path
from datetime import datetime
from types import SimpleNamespace
import threading
import tkinter as tk
from tkinter import ttk, filedialog, messagebox, scrolledtext
import queue
from concurrent.futures import ThreadPoolExecutor, as_completed
import multiprocessing

# Pillow is used for metadata extraction (reads only file headers)
try:
    from PIL import Image as PILImage, ExifTags
    PIL_AVAILABLE = True
except ImportError:
    PIL_AVAILABLE = False

# pillow-heif enables reading iPhone HEIC/HEIF metadata; the app still works
# without it (HEIC then falls back to the generic "iPhone" folder)
try:
    from pillow_heif import register_heif_opener
    register_heif_opener()
    HEIF_AVAILABLE = True
except Exception:
    HEIF_AVAILABLE = False

# Sun Valley theme (modern Windows 11 look); optional
try:
    import sv_ttk
    SV_TTK_AVAILABLE = True
except ImportError:
    SV_TTK_AVAILABLE = False

# EXIF tag ids (base IFD)
TAG_MAKE = 271
TAG_MODEL = 272
TAG_SOFTWARE = 305
TAG_DATETIME = 306
# EXIF tag ids (Exif sub-IFD)
TAG_DATETIME_ORIGINAL = 36867
TAG_DATETIME_DIGITIZED = 36868

# Supported extensions (always compared against Path.suffix.lower())
IMAGE_EXTS = {'.jpg', '.jpeg', '.png', '.bmp', '.gif', '.tiff', '.tif',
              '.heic', '.heif', '.avif', '.webp'}

RAW_EXTS = {'.raw', '.cr2', '.cr3', '.nef', '.arw', '.dng', '.orf', '.rw2',
            '.raf', '.srw', '.x3f', '.pef', '.sr2'}

VIDEO_EXTS = {'.mp4', '.avi', '.mov', '.mkv', '.wmv', '.flv', '.webm',
              '.m4v', '.mpg', '.mpeg', '.3gp', '.3g2', '.mts', '.m2ts',
              '.vob', '.ogv', '.rm', '.rmvb', '.asf', '.amv', '.divx',
              '.m2v', '.svi', '.mxf', '.roq', '.nsv', '.f4v', '.f4p'}

ALL_MEDIA_EXTS = IMAGE_EXTS | RAW_EXTS | VIDEO_EXTS

# HEIC/HEIF photos are overwhelmingly from iPhones; used as a fallback
# when the file's metadata cannot be read
HEIC_EXTS = {'.heic', '.heif'}

# Pixel and Google Photos tack variant tags onto one photo's name:
#   PXL_20250515_182147320.RAW-01.MP.jpg        <- the JPEG
#   PXL_20250515_182147320.RAW-02.ORIGINAL.dng  <- its RAW
# Both belong to the photo PXL_20250515_182147320, so the tags are stripped
# before matching a RAW or video to the image it was taken with.
VARIANT_TAG_RE = re.compile(
    r'\.(RAW-\d+|ORIGINAL|COVER|MP|PORTRAIT|NIGHT|MOTION|LONG_EXPOSURE'
    r'|ACTION_PAN|PANO|TRIM|EXPORT|EDITED|EDIT)$', re.IGNORECASE)

# Some camera apps stamp their name onto every file they write. Only used
# when nothing else identifies the camera - better than "Unknown Camera".
# Deliberately short: prefixes like IMG_ and DSC are used by everyone.
FILENAME_CAMERA_PREFIXES = (
    ('pxl_', 'Google Pixel'),
)

# Only these can be screenshots
SCREENSHOT_CAPABLE_EXTS = {'.png', '.jpg', '.jpeg'}

# Anchored at the start of the filename to avoid false positives such as
# "class_photo.jpg" (which merely *contains* "ss_")
SCREENSHOT_NAME_PREFIXES = ('screenshot', 'screen_', 'screen-',
                            'capture_', 'capture-', 'snap_', 'snap-',
                            'scr_', 'ss_', 'ss-')

# Common Android screen resolutions (portrait); both orientations are checked
ANDROID_RESOLUTIONS = [
    (1080, 1920), (1080, 2160), (1080, 2280), (1080, 2340), (1080, 2400),
    (1440, 2560), (1440, 2880), (1440, 2960), (1440, 3040), (1440, 3200),
    (720, 1280), (720, 1440), (720, 1520), (720, 1560),
]

# Phone model mappings (Samsung evolution + iPhone models)
PHONE_MAPPINGS = {
    # Early Galaxy series
    "GT-I5700": "Samsung Galaxy Spica",
    "GT-I9000": "Samsung Galaxy S",
    "GT-I9100": "Samsung Galaxy S2",
    "GT-I9300": "Samsung Galaxy S3",
    "GT-I9500": "Samsung Galaxy S4",
    "GT-I9505": "Samsung Galaxy S4",
    "SM-G900": "Samsung Galaxy S5",
    "SM-G900F": "Samsung Galaxy S5",
    "SM-G920": "Samsung Galaxy S6",
    "SM-G920F": "Samsung Galaxy S6",
    "SM-G925": "Samsung Galaxy S6 Edge",
    "SM-G930": "Samsung Galaxy S7",
    "SM-G935": "Samsung Galaxy S7 Edge",
    "SM-G950": "Samsung Galaxy S8",
    "SM-G955": "Samsung Galaxy S8+",
    "SM-G960": "Samsung Galaxy S9",
    "SM-G965": "Samsung Galaxy S9+",
    "SM-G970": "Samsung Galaxy S10e",
    "SM-G973": "Samsung Galaxy S10",
    "SM-G975": "Samsung Galaxy S10+",
    "SM-G980": "Samsung Galaxy S20",
    "SM-G985": "Samsung Galaxy S20+",
    "SM-G988": "Samsung Galaxy S20 Ultra",
    "SM-G990": "Samsung Galaxy S21",
    "SM-G991": "Samsung Galaxy S21",
    "SM-G996": "Samsung Galaxy S21+",
    "SM-G998": "Samsung Galaxy S21 Ultra",
    "SM-S901": "Samsung Galaxy S22",
    "SM-S906": "Samsung Galaxy S22+",
    "SM-S908": "Samsung Galaxy S22 Ultra",
    "SM-S911": "Samsung Galaxy S23",
    "SM-S916": "Samsung Galaxy S23+",
    "SM-S918": "Samsung Galaxy S23 Ultra",
    "SM-S921": "Samsung Galaxy S24",
    "SM-S926": "Samsung Galaxy S24+",
    "SM-S928": "Samsung Galaxy S24 Ultra",
    "SM-S931": "Samsung Galaxy S25",
    "SM-S936": "Samsung Galaxy S25+",
    "SM-S938": "Samsung Galaxy S25 Ultra",
    # OnePlus
    "IN2023": "OnePlus 8 Pro",
    "IN2025": "OnePlus 8 Pro",
    # iPhone models - Original iPhone through iPhone 4S
    "iPhone1,1": "iPhone (Original)",
    "iPhone1,2": "iPhone 3G",
    "iPhone2,1": "iPhone 3GS",
    "iPhone3,1": "iPhone 4",
    "iPhone3,2": "iPhone 4",
    "iPhone3,3": "iPhone 4",
    "iPhone4,1": "iPhone 4S",
    # iPhone 5 series
    "iPhone5,1": "iPhone 5",
    "iPhone5,2": "iPhone 5",
    "iPhone5,3": "iPhone 5C",
    "iPhone5,4": "iPhone 5C",
    "iPhone6,1": "iPhone 5S",
    "iPhone6,2": "iPhone 5S",
    # iPhone 6 series
    "iPhone7,2": "iPhone 6",
    "iPhone7,1": "iPhone 6 Plus",
    "iPhone8,1": "iPhone 6S",
    "iPhone8,2": "iPhone 6S Plus",
    "iPhone8,4": "iPhone SE (1st Gen)",
    # iPhone 7 series
    "iPhone9,1": "iPhone 7",
    "iPhone9,3": "iPhone 7",
    "iPhone9,2": "iPhone 7 Plus",
    "iPhone9,4": "iPhone 7 Plus",
    # iPhone 8 and X series
    "iPhone10,1": "iPhone 8",
    "iPhone10,4": "iPhone 8",
    "iPhone10,2": "iPhone 8 Plus",
    "iPhone10,5": "iPhone 8 Plus",
    "iPhone10,3": "iPhone X",
    "iPhone10,6": "iPhone X",
    # iPhone XS, XR series
    "iPhone11,2": "iPhone XS",
    "iPhone11,4": "iPhone XS Max",
    "iPhone11,6": "iPhone XS Max",
    "iPhone11,8": "iPhone XR",
    # iPhone 11 series
    "iPhone12,1": "iPhone 11",
    "iPhone12,3": "iPhone 11 Pro",
    "iPhone12,5": "iPhone 11 Pro Max",
    "iPhone12,8": "iPhone SE (2nd Gen)",
    # iPhone 12 series
    "iPhone13,1": "iPhone 12 Mini",
    "iPhone13,2": "iPhone 12",
    "iPhone13,3": "iPhone 12 Pro",
    "iPhone13,4": "iPhone 12 Pro Max",
    # iPhone 13 series
    "iPhone14,4": "iPhone 13 Mini",
    "iPhone14,5": "iPhone 13",
    "iPhone14,2": "iPhone 13 Pro",
    "iPhone14,3": "iPhone 13 Pro Max",
    "iPhone14,6": "iPhone SE (3rd Gen)",
    # iPhone 14 series
    "iPhone14,7": "iPhone 14",
    "iPhone14,8": "iPhone 14 Plus",
    "iPhone15,2": "iPhone 14 Pro",
    "iPhone15,3": "iPhone 14 Pro Max",
    # iPhone 15 series
    "iPhone15,4": "iPhone 15",
    "iPhone15,5": "iPhone 15 Plus",
    "iPhone16,1": "iPhone 15 Pro",
    "iPhone16,2": "iPhone 15 Pro Max",
    # iPhone 16 series (model numbers are estimated based on pattern)
    "iPhone17,1": "iPhone 16",
    "iPhone17,2": "iPhone 16 Plus",
    "iPhone17,3": "iPhone 16 Pro",
    "iPhone17,4": "iPhone 16 Pro Max",
}


def is_raw_file(file_path):
    return file_path.suffix.lower() in RAW_EXTS


def is_video_file(file_path):
    return file_path.suffix.lower() in VIDEO_EXTS


def _exif_text(value):
    """Normalize an EXIF value to a clean string (or None)."""
    if value is None:
        return None
    if isinstance(value, bytes):
        value = value.decode('utf-8', 'ignore')
    text = str(value).replace('\x00', '').strip()
    return text or None


def read_metadata(file_path):
    """Read image metadata (one header-only read via Pillow).

    Returns a dict with 'model', 'date', 'width', 'height', 'software'.
    Values are None when unavailable. Never raises.
    """
    meta = {'model': None, 'date': None, 'width': None, 'height': None,
            'software': None}
    try:
        with PILImage.open(file_path) as img:
            meta['width'], meta['height'] = img.size
            exif = img.getexif()
            if not exif:
                return meta

            model = _exif_text(exif.get(TAG_MODEL))
            make = _exif_text(exif.get(TAG_MAKE))
            meta['model'] = model or make
            meta['software'] = _exif_text(exif.get(TAG_SOFTWARE))

            try:
                exif_ifd = exif.get_ifd(ExifTags.IFD.Exif)
            except Exception:
                exif_ifd = {}
            date_str = _exif_text(exif_ifd.get(TAG_DATETIME_ORIGINAL)
                                  or exif_ifd.get(TAG_DATETIME_DIGITIZED)
                                  or exif.get(TAG_DATETIME))
            if date_str:
                for fmt in ("%Y:%m:%d %H:%M:%S", "%Y-%m-%d %H:%M:%S"):
                    try:
                        meta['date'] = datetime.strptime(date_str, fmt)
                        break
                    except ValueError:
                        pass
    except Exception:
        pass
    return meta


def friendly_camera_name(raw_model):
    """Convert a raw EXIF model string to a friendly folder-safe name."""
    if not raw_model:
        return None
    model = str(raw_model).replace('\x00', '').strip()
    if not model:
        return None
    for char in '<>:"/\\|?*':
        model = model.replace(char, '_')
    model = model.rstrip('. ')

    if 'ILCE-6000' in model or 'A6000' in model:
        return 'Sony A6000'
    if model in PHONE_MAPPINGS:
        return PHONE_MAPPINGS[model]
    for key, name in PHONE_MAPPINGS.items():
        if key in model:
            return name
    return model or None


def media_base_name(file_path):
    """The photo name that a file and its variants share.

    "PXL_20250515_182147320.RAW-02.ORIGINAL.dng" and
    "PXL_20250515_182147320.RAW-01.MP.jpg" are the RAW and the JPEG of one
    photo, so both come back as "PXL_20250515_182147320".
    """
    stem = file_path.stem
    while True:
        stripped = VARIANT_TAG_RE.sub('', stem)
        if stripped == stem or not stripped:
            return stem
        stem = stripped


def camera_from_filename(file_path):
    """Camera family taken from a maker's filename prefix, or None."""
    name = file_path.name.lower()
    for prefix, camera in FILENAME_CAMERA_PREFIXES:
        if name.startswith(prefix):
            return camera
    return None


def lookup_model(base_name_to_model, file_path):
    """Model of the image a RAW or video was taken with, if one is known.

    Tries the exact filename first, then the shared photo name, so Pixel's
    ".RAW-02.ORIGINAL" files still find their ".RAW-01.MP" JPEG.
    """
    return (base_name_to_model.get(file_path.stem)
            or base_name_to_model.get(media_base_name(file_path)))


def model_for_image(file_path, meta):
    """Camera model for an image, assuming iPhone for unreadable HEIC files.

    Falls back to the filename when the metadata is gone - a "PXL_" file is
    a Pixel photo whether or not its EXIF survived.
    """
    model = friendly_camera_name(meta.get('model') if meta else None)
    if not model and file_path.suffix.lower() in HEIC_EXTS:
        model = "iPhone"
    if not model:
        model = camera_from_filename(file_path)
    return model


def looks_like_screenshot(file_path, meta):
    """Heuristically decide whether an image is an Android screenshot."""
    if file_path.suffix.lower() not in SCREENSHOT_CAPABLE_EXTS:
        return False

    name = file_path.name.lower()
    if name.startswith(SCREENSHOT_NAME_PREFIXES) or 'screenshot' in name:
        return True

    if not meta:
        return False

    software = (meta.get('software') or '').lower()
    if any(term in software for term in ('screenshot', 'android', 'system ui')):
        return True

    # Screenshots have no camera model and match the screen resolution exactly
    if not meta.get('model'):
        width, height = meta.get('width'), meta.get('height')
        if width and height:
            for res_w, res_h in ANDROID_RESOLUTIONS:
                if (abs(width - res_w) <= 10 and abs(height - res_h) <= 10) or \
                   (abs(width - res_h) <= 10 and abs(height - res_w) <= 10):
                    return True
    return False


def _hash_file(path):
    """Content hash of a file, read in 1 MB chunks."""
    h = hashlib.blake2b()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(1 << 20), b''):
            h.update(chunk)
    return h.digest()


def files_identical(a, b):
    """True if two files have identical content (size check first, then hash)."""
    try:
        if a.stat().st_size != b.stat().st_size:
            return False
        return _hash_file(a) == _hash_file(b)
    except OSError:
        return False


# Head-hash size for the cheap middle stage of duplicate detection
HEAD_HASH_BYTES = 64 * 1024


def _hash_head(path):
    """Hash of a file's first 64 KB - a cheap way to rule out same-size files
    that are obviously different before paying for a full read."""
    h = hashlib.blake2b()
    with open(path, 'rb') as f:
        h.update(f.read(HEAD_HASH_BYTES))
    return h.digest()


# Where duplicates and damaged files are set aside. These are skipped when
# scanning, so re-running the organizer never drags them back out.
DUPLICATES_FOLDER = "Duplicates"
CORRUPT_FOLDER = "Corrupt"
WRONG_EXT_FOLDER = "Wrong Extension"
SET_ASIDE_FOLDERS = {DUPLICATES_FOLDER.lower(), CORRUPT_FOLDER.lower(),
                     WRONG_EXT_FOLDER.lower()}

# Filenames Windows/macOS produce when something gets copied twice:
# "photo (1).jpg", "photo - Copy.jpg", "photo - Copy (2).jpg", "photo copy 2.jpg",
# and the Norwegian/German/French equivalents Explorer uses.
#
# Deliberately NOT matching a bare trailing "_2"/"-3": that is how most cameras
# and phones name every single photo they take (IMG_1234, DSC-0001,
# IMG_20230510_143000), so treating it as a copy marker would pick the wrong
# keeper almost every time. Where two names really do differ only by a trailing
# number, the shorter-name tie-breaker settles it instead.
COPY_NAME_RE = re.compile(
    r'(\s*\(\d+\)'                              # "photo (1)", "photo(2)"
    r'|[ _-]+(copy|kopi|kopie|copie)( \d+)?'    # "photo - Copy", "photo copy 2"
    r')$', re.IGNORECASE)


def looks_like_copy_name(stem):
    """True if a filename stem looks like an auto-generated copy of another."""
    return bool(COPY_NAME_RE.search(stem.strip()))


# ISO-BMFF (MP4/MOV) timestamps count seconds from 1904-01-01 UTC
MP4_EPOCH_OFFSET = 2082844800
MVHD_CAPABLE_EXTS = {'.mp4', '.mov', '.m4v', '.3gp', '.3g2'}


def read_video_date(file_path):
    """Read the recording date from an MP4/MOV file's moov/mvhd box.

    Walks box headers with seeks - never reads the media data itself.
    Returns a datetime or None. Never raises.
    """
    if file_path.suffix.lower() not in MVHD_CAPABLE_EXTS:
        return None
    try:
        file_size = file_path.stat().st_size
        with open(file_path, 'rb') as f:

            def walk(offset, end, in_moov):
                while offset + 8 <= end:
                    f.seek(offset)
                    header = f.read(8)
                    if len(header) < 8:
                        return None
                    box_size = int.from_bytes(header[:4], 'big')
                    box_type = header[4:8]
                    header_len = 8
                    if box_size == 1:  # 64-bit largesize follows
                        box_size = int.from_bytes(f.read(8), 'big')
                        header_len = 16
                    elif box_size == 0:  # box extends to end of file
                        box_size = end - offset
                    if box_size < header_len:
                        return None
                    if not in_moov and box_type == b'moov':
                        found = walk(offset + header_len, offset + box_size, True)
                        if found:
                            return found
                    elif in_moov and box_type == b'mvhd':
                        f.seek(offset + header_len)
                        payload = f.read(12)
                        if len(payload) < 12:
                            return None
                        if payload[0] == 1:  # version 1: 64-bit timestamps
                            ctime = int.from_bytes(payload[4:12], 'big')
                        else:
                            ctime = int.from_bytes(payload[4:8], 'big')
                        if ctime <= MP4_EPOCH_OFFSET:
                            return None
                        dt = datetime.fromtimestamp(ctime - MP4_EPOCH_OFFSET)
                        # Reject obviously bogus timestamps
                        if 1990 <= dt.year <= datetime.now().year + 1:
                            return dt
                        return None
                    offset += box_size
                return None

            return walk(0, file_size, False)
    except Exception:
        return None


def _mp4_structure_ok(file_path):
    """Structural sanity check for an MP4/MOV container.

    Walks the top-level box headers (never the media data) and requires:
      - every box header to be well-formed and to fit inside the file
      - the boxes to tile the file without running past its end
      - both a 'moov' (index) and an 'mdat' (media) box to be present
    An interrupted copy or a half-finished download fails on the first two.

    Returns (ok, reason). Never raises.
    """
    try:
        file_size = file_path.stat().st_size
        with open(file_path, 'rb') as f:
            offset = 0
            seen = set()
            while offset + 8 <= file_size:
                f.seek(offset)
                header = f.read(8)
                if len(header) < 8:
                    return False, "file ends inside a box header"
                box_size = int.from_bytes(header[:4], 'big')
                box_type = header[4:8]
                header_len = 8
                if box_size == 1:  # 64-bit largesize follows the header
                    extra = f.read(8)
                    if len(extra) < 8:
                        return False, "file ends inside a box header"
                    box_size = int.from_bytes(extra, 'big')
                    header_len = 16
                elif box_size == 0:  # this box runs to end of file
                    box_size = file_size - offset
                if box_size < header_len:
                    return False, "malformed box size"
                if offset + box_size > file_size:
                    return False, ("truncated - the file claims more data than "
                                   "it actually contains")
                seen.add(box_type)
                offset += box_size
            if offset != file_size:
                return False, "trailing bytes after the last box"
            if b'moov' not in seen:
                return False, "missing the 'moov' index box"
            if b'mdat' not in seen:
                return False, "missing the 'mdat' media box"
            return True, ""
    except OSError as e:
        return False, f"unreadable ({e})"
    except Exception as e:
        return False, f"could not be parsed ({e})"


# End-of-stream markers, keyed by the format the file *actually is* rather
# than by its extension. A half-copied file has a perfectly valid header and
# no closing marker, which is exactly what these catch.
#
# GIF is deliberately absent: its trailer is the single byte 0x3B, which turns
# up constantly inside compressed data, so it can't be searched for reliably.
FORMAT_END_MARKERS = {
    'JPEG image': b'\xff\xd9',
    'PNG image': b'IEND\xaeB`\x82',
}

def _find_marker_from_end(file_path, size, marker, stop_before=0):
    """Search backwards for the last occurrence of `marker`, giving up once
    `stop_before` is reached. Returns its offset, or -1.

    An intact file hits the marker in the very first chunk read, so this
    normally touches 1 MB regardless of how big the file is.
    """
    chunk = 1 << 20
    overlap = len(marker) - 1
    pos = size
    with open(file_path, 'rb') as f:
        while pos > stop_before:
            start = max(stop_before, pos - chunk)
            f.seek(start)
            data = f.read(min(pos + overlap, size) - start)
            found = data.rfind(marker)
            if found != -1:
                return start + found
            pos = start
    return -1


# JPEG markers that stand alone, with no length field after them
JPEG_STANDALONE_MARKERS = {0x01, 0xD0, 0xD1, 0xD2, 0xD3, 0xD4, 0xD5, 0xD6,
                           0xD7, 0xD8, 0xD9}


def _jpeg_scan_start(file_path, size):
    """Offset where the main photo's compressed data begins - just past the
    Start-of-Scan header. Returns -1 if the file never gets that far.

    Walks only the segment headers, skipping each segment's payload using its
    declared length, so this reads a few KB even from a huge photo.

    This is what makes truncation detection trustworthy: a camera embeds a
    small preview thumbnail near the start of a JPEG, and that preview has an
    end marker of its own. Knowing where the real photo's data starts means a
    thumbnail's marker can never be mistaken for proof that the photo
    finished being written.
    """
    try:
        with open(file_path, 'rb') as f:
            if f.read(2) != b'\xff\xd8':
                return -1
            offset = 2
            while offset + 4 <= size:
                f.seek(offset)
                pair = f.read(2)
                if len(pair) < 2 or pair[0] != 0xFF:
                    return -1
                marker = pair[1]
                if marker == 0xFF:  # padding between segments
                    offset += 1
                    continue
                if marker in JPEG_STANDALONE_MARKERS:
                    offset += 2
                    continue
                raw_len = f.read(2)
                if len(raw_len) < 2:
                    return -1
                seg_len = int.from_bytes(raw_len, 'big')
                if seg_len < 2:
                    return -1
                if marker == 0xDA:  # Start of Scan: the photo itself follows
                    return offset + 2 + seg_len
                offset += 2 + seg_len
    except OSError:
        return -1
    return -1


def _image_stream_complete(file_path, size, real_format):
    """True if an image's data was fully written out.

    Deliberately does NOT demand that the end marker be the last thing in the
    file. Several completely normal kinds of photo put data after it, and they
    all open and display perfectly:

      - **Motion photos.** Google Pixel (`PXL_*.MP.jpg`) and Samsung phones
        append a short video clip to the end of an ordinary JPEG.
      - **iPhone Portrait/dual-camera shots.** These are MPO files: one
        complete photo followed by a second embedded image, and it is usually
        that second image that got cut off, not the photo.
      - **Zero padding.** Recovery tools, card readers and backup exports pad
        a complete file out to a block boundary, which can leave the marker
        megabytes from the end.

    Calling any of those damaged would quarantine perfectly good photos. What
    genuinely indicates truncation is the photo's own data never being closed
    off - so look for the end marker anywhere at or after the point where the
    photo's data begins, and ignore whatever comes afterwards.
    """
    marker = FORMAT_END_MARKERS.get(real_format)
    if not marker:
        return True  # nothing reliable to check for this format

    if real_format == 'JPEG image':
        scan_start = _jpeg_scan_start(file_path, size)
        if scan_start < 0:
            # Couldn't map the structure; fall back to "is there a marker at
            # all", which is lenient rather than accusing a photo unfairly.
            return _find_marker_from_end(file_path, size, marker) != -1
        return _find_marker_from_end(file_path, size, marker,
                                     stop_before=scan_start) != -1

    return _find_marker_from_end(file_path, size, marker) != -1


# Magic bytes, used to tell what a file actually is when its extension lies.
# A misnamed file is not damaged - its contents are usually perfectly fine,
# and renaming it is all that's needed.
#
# Each entry is (signature, description, extensions it is valid for, the
# extension we would rename it to).
FILE_SIGNATURES = (
    (b'\xff\xd8\xff', 'JPEG image', {'.jpg', '.jpeg'}, '.jpg'),
    (b'\x89PNG\r\n\x1a\n', 'PNG image', {'.png'}, '.png'),
    (b'GIF87a', 'GIF image', {'.gif'}, '.gif'),
    (b'GIF89a', 'GIF image', {'.gif'}, '.gif'),
    (b'BM', 'BMP image', {'.bmp'}, '.bmp'),
    (b'II*\x00', 'TIFF or RAW image', {'.tif', '.tiff'} | RAW_EXTS, '.tif'),
    (b'MM\x00*', 'TIFF or RAW image', {'.tif', '.tiff'} | RAW_EXTS, '.tif'),
)

ISOBMFF_BOXES = (b'ftyp', b'moov', b'mdat', b'wide', b'free', b'skip')

# ISO-BMFF "brands" that mean the container holds a still image rather than
# video. Everything else in that family is treated as video.
HEIC_BRANDS = (b'heic', b'heix', b'hevc', b'hevx', b'mif1', b'msf1', b'heis')
AVIF_BRANDS = (b'avif', b'avis')


def sniff_real_format(head):
    """Identify a file's real type from its first bytes.

    Returns (description, valid_extensions, canonical_extension), or
    (None, None, None) when the format isn't one we can name confidently - in
    which case we say nothing rather than guess.
    """
    for signature, description, valid_exts, canonical in FILE_SIGNATURES:
        if head.startswith(signature):
            return description, valid_exts, canonical
    if head.startswith(b'RIFF'):
        if head[8:12] == b'WEBP':
            return 'WEBP image', {'.webp'}, '.webp'
        if head[8:12] == b'AVI ':
            return 'AVI video', {'.avi'}, '.avi'
        return None, None, None
    if head[4:8] in ISOBMFF_BOXES:
        brand = head[8:12]
        if brand in HEIC_BRANDS:
            return 'HEIC image', HEIC_EXTS, '.heic'
        if brand in AVIF_BRANDS:
            return 'AVIF image', {'.avif'}, '.avif'
        # MP4 and MOV are interchangeable in practice and players handle
        # either, so treat the whole video family as valid for each other
        # rather than nagging about a .mov that is technically an .mp4.
        return 'MP4/QuickTime video', MVHD_CAPABLE_EXTS, '.mp4'
    return None, None, None


def canonical_extension_for(file_path):
    """The extension a file *should* have, based on its contents. None if we
    can't say confidently."""
    try:
        with open(file_path, 'rb') as f:
            return sniff_real_format(f.read(16))[2]
    except OSError:
        return None


def file_health(file_path, thorough=False, format_only=False):
    """Check whether a media file is intact.

    Returns (status, reason) where status is:
      'ok'        - opened and passed every check we can apply
      'damaged'   - definitely broken (empty, unreadable, or truncated)
      'misnamed'  - the contents are fine, but the file extension is wrong
                    (e.g. a JPEG photo saved as .MOV). Renaming fixes it -
                    these must never be treated as damaged.
      'unchecked' - a format we cannot verify (most RAW, AVI/MKV/WMV...).
                    Reported honestly rather than guessed at, so a good file
                    is never wrongly called broken.
    Never raises.
    """
    suffix = file_path.suffix.lower()
    try:
        size = file_path.stat().st_size
    except OSError as e:
        return 'damaged', f"unreadable ({e})"
    if size == 0:
        return 'damaged', "empty file (0 bytes)"

    # Before anything else: does the file's own header agree with its
    # extension? Checking a JPEG as if it were a video reports nonsense.
    try:
        with open(file_path, 'rb') as f:
            head = f.read(16)
    except OSError as e:
        return 'damaged', f"unreadable ({e})"

    real_format, valid_exts, canonical = sniff_real_format(head)
    if real_format and suffix not in valid_exts:
        named = suffix.lstrip('.').upper() or "(no extension)"
        return 'misnamed', (f"contents are a {real_format}, not a {named} "
                            f"file - should be named {canonical}")

    if format_only:
        # Caller only wanted to know whether the extension is a lie
        return 'ok', ""

    if suffix in MVHD_CAPABLE_EXTS:
        ok, reason = _mp4_structure_ok(file_path)
        return ('ok', "") if ok else ('damaged', reason)

    if suffix in VIDEO_EXTS:
        return 'unchecked', "video format we cannot verify without decoding"

    if suffix in RAW_EXTS:
        return 'unchecked', "RAW format we cannot verify"

    if suffix not in IMAGE_EXTS:
        return 'unchecked', "unrecognized format"

    if not PIL_AVAILABLE:
        return 'unchecked', "Pillow is not installed"

    # Header / structure check. verify() consumes the image object, so a
    # thorough decode has to reopen the file afterwards.
    try:
        with PILImage.open(file_path) as img:
            img.verify()
    except Exception as e:
        return 'damaged', f"image data is not readable ({e})"

    try:
        if not _image_stream_complete(file_path, size, real_format):
            return 'damaged', "truncated - the image data was never finished"
    except OSError as e:
        return 'damaged', f"unreadable ({e})"

    if thorough:
        try:
            with PILImage.open(file_path) as img:
                img.load()
        except Exception as e:
            return 'damaged', f"image will not fully decode ({e})"

    return 'ok', ""


def read_media_metadata(file_path):
    """Metadata for any media file: EXIF for images, mvhd date for videos."""
    if is_video_file(file_path):
        return {'model': None, 'date': read_video_date(file_path),
                'width': None, 'height': None, 'software': None}
    return read_metadata(file_path)


def is_set_aside(file_path, source_path):
    """True if a file lives under the Duplicates/ or Corrupt/ folder.

    Those hold files a previous run deliberately put away, so scanning them
    again would just drag them straight back into the organized folders.
    """
    try:
        first = file_path.relative_to(source_path).parts[0]
    except (ValueError, IndexError):
        return False
    return first.lower() in SET_ASIDE_FOLDERS


# Extensions that are definitely not photos, so there is no point opening them
# to look. Anything else with an unfamiliar extension gets sniffed.
NON_MEDIA_EXTS = {
    '.json', '.txt', '.xml', '.html', '.htm', '.csv', '.log', '.ini', '.db',
    '.md', '.pdf', '.doc', '.docx', '.xls', '.xlsx', '.ppt', '.pptx', '.zip',
    '.rar', '.7z', '.gz', '.tar', '.exe', '.dll', '.msi', '.bat', '.cmd',
    '.ps1', '.py', '.js', '.css', '.lnk', '.url', '.ico', '.thm', '.xmp',
    '.aae', '.plist', '.sqlite', '.dat', '.bin', '.iso', '.mp3', '.wav',
    '.flac', '.m4a', '.aac', '.ogg', '.wma',
}


def looks_like_media_by_content(file_path):
    """True if a file's first bytes say it is a photo or video, whatever its
    name claims.

    Exports mangle extensions surprisingly often - Google Takeout truncates
    long filenames, chopping ".jpg" off the end and leaving things like
    "PXL_20250507_050944066.RAW-01.MP.COVER". Without this those files are
    invisible to the organizer: never sorted, never checked, never deduped.
    Reads 16 bytes, so it costs essentially nothing.
    """
    try:
        with open(file_path, 'rb') as f:
            return sniff_real_format(f.read(16))[0] is not None
    except OSError:
        return False


def collect_media_files(source_path, include_subfolders, sniff_unknown=True):
    """List media files in the source. Shared by the organizer and the
    preview-cache validation so both always see the same file set.

    Returns (regular_media_files, raw_files).
    """
    if include_subfolders:
        all_files = []
        for dirpath, dirs, names in os.walk(source_path):
            # Don't descend into files a previous run set aside
            if Path(dirpath) == Path(source_path):
                dirs[:] = [d for d in dirs if d.lower() not in SET_ASIDE_FOLDERS]
            all_files.extend(Path(dirpath) / name for name in names)
    else:
        all_files = [f for f in source_path.iterdir() if f.is_file()]
    # Never touch our own log/undo files
    all_files = [f for f in all_files if not f.name.startswith('kjegla_')]

    raw_files = [f for f in all_files if f.suffix.lower() in RAW_EXTS]
    regular = [f for f in all_files
               if f.suffix.lower() in (IMAGE_EXTS | VIDEO_EXTS)]

    if sniff_unknown:
        known = ALL_MEDIA_EXTS | NON_MEDIA_EXTS
        regular.extend(f for f in all_files
                       if f.suffix.lower() not in known
                       and looks_like_media_by_content(f))
    return regular, raw_files


def plan_key(settings):
    """Settings that affect where files go. If any of these differ between
    preview and execute, the cached plan is invalid."""
    return (settings.source, settings.operation, settings.subfolder_mode,
            settings.separate_raw, settings.separate_screenshots,
            settings.include_subfolders, settings.dedupe_content,
            settings.check_corrupt, settings.corrupt_thorough,
            settings.cleanup_empty, settings.fix_extensions)


def folder_fingerprint(media_files):
    """Cheap folder-state snapshot: path -> (size, mtime). Detects any
    added/removed/modified file without reading file contents."""
    fp = {}
    for f in media_files:
        try:
            st = f.stat()
            fp[str(f)] = (st.st_size, st.st_mtime_ns)
        except OSError:
            fp[str(f)] = None
    return fp


class PhotoOrganizerGUI:
    def __init__(self, root):
        self.root = root
        self.root.title("Kjegla's Photo Organizer")
        self.root.geometry("960x780")

        # Set icon (VLC cone if available)
        try:
            self.root.iconbitmap(default='vlc_cone.ico')
        except Exception:
            pass

        # Variables
        self.source_folder = tk.StringVar()
        self.operation_mode = tk.StringVar(value="move")
        self.subfolder_mode = tk.StringVar(value="none")  # none, year, month, year-month
        self.separate_raw = tk.BooleanVar(value=False)
        self.separate_screenshots = tk.BooleanVar(value=False)
        self.use_multithreading = tk.BooleanVar(value=True)
        self.include_subfolders = tk.BooleanVar(value=False)
        self.dedupe_content = tk.BooleanVar(value=True)
        self.check_corrupt = tk.BooleanVar(value=False)
        self.corrupt_thorough = tk.BooleanVar(value=False)
        self.cleanup_empty = tk.BooleanVar(value=True)
        self.fix_extensions = tk.BooleanVar(value=True)
        self.processing = False
        self.cancel_requested = False
        self.last_undo_file = None
        self.last_rename_undo_file = None  # undo for the extension fixes alone
        self.cached_plan = None  # preview results reusable by Execute
        self.queue = queue.Queue()

        self.max_threads = min(multiprocessing.cpu_count(), 12)

        # Statistics tracking
        self.stats = self._empty_stats()

        if not PIL_AVAILABLE:
            self.show_dependency_error()
            return

        self.setup_ui()

        # Start queue checker
        self.root.after(100, self.check_queue)

    @staticmethod
    def _empty_stats():
        return {
            'total_files': 0,
            'processed': 0,
            'no_metadata': 0,
            'errors': 0,
            'screenshots': 0,
            'duplicates': 0,
            'content_duplicates': 0,
            'damaged': 0,
            'misnamed': 0,
            'unchecked': 0,
            'empty_folders_removed': 0,
            'duplicate_bytes': 0,
            'already_organized': 0,
            'by_model': {},
            'by_year': {},
            'total_size_mb': 0,
        }

    def show_dependency_error(self):
        """Show error message if the Pillow library is not installed."""
        error_frame = ttk.Frame(self.root, padding="20")
        error_frame.pack(fill=tk.BOTH, expand=True)

        ttk.Label(error_frame, text="⚠️ Required Library Missing",
                  font=('Arial', 14, 'bold')).pack(pady=10)

        msg = ("The 'Pillow' library is not installed.\n\n"
               "To install it, open a command prompt/terminal and run:\n"
               "pip install Pillow\n\n"
               "After installation, restart this program.")

        ttk.Label(error_frame, text=msg, font=('Arial', 10)).pack(pady=10)

        ttk.Button(error_frame, text="Exit",
                   command=self.root.quit).pack(pady=10)

    def setup_ui(self):
        """Setup the GUI interface."""
        main_frame = ttk.Frame(self.root, padding="12")
        main_frame.grid(row=0, column=0, sticky=(tk.W, tk.E, tk.N, tk.S))

        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(0, weight=1)
        self.root.minsize(920, 720)
        main_frame.columnconfigure(1, weight=1)

        # Title row with theme toggle
        title_frame = ttk.Frame(main_frame)
        title_frame.grid(row=0, column=0, columnspan=3, pady=(0, 10), sticky=(tk.W, tk.E))

        ttk.Label(title_frame, text="📷 Kjegla's Photo Organizer",
                  font=('Segoe UI', 16, 'bold')).pack(side=tk.LEFT, expand=True)

        if SV_TTK_AVAILABLE:
            self.theme_btn = ttk.Button(title_frame, text="☀️", width=3,
                                        command=self.toggle_theme)
            self.theme_btn.pack(side=tk.RIGHT)

        # Source folder selection
        ttk.Label(main_frame, text="Source Folder:").grid(
            row=1, column=0, sticky=tk.W, padx=5, pady=5)

        folder_entry = ttk.Entry(main_frame, textvariable=self.source_folder)
        folder_entry.grid(row=1, column=1, sticky=(tk.W, tk.E), padx=5, pady=5)

        browse_btn = ttk.Button(main_frame, text="Browse...",
                                command=self.browse_folder)
        browse_btn.grid(row=1, column=2, padx=5, pady=5)

        # Options area: two side-by-side columns
        options_frame = ttk.Frame(main_frame)
        options_frame.grid(row=2, column=0, columnspan=3, sticky=(tk.W, tk.E), pady=5)
        options_frame.columnconfigure(0, weight=1, uniform="opts")
        options_frame.columnconfigure(1, weight=1, uniform="opts")

        left_col = ttk.Frame(options_frame)
        left_col.grid(row=0, column=0, sticky=(tk.W, tk.E, tk.N), padx=(0, 6))
        right_col = ttk.Frame(options_frame)
        right_col.grid(row=0, column=1, sticky=(tk.W, tk.E, tk.N), padx=(6, 0))

        # Operation mode (left column)
        mode_frame = ttk.LabelFrame(left_col, text="Operation Mode", padding="10")
        mode_frame.pack(fill=tk.X, pady=(0, 8))

        ttk.Radiobutton(mode_frame, text="Move files (files leave the source folder)",
                        variable=self.operation_mode, value="move").pack(anchor=tk.W, pady=2)
        ttk.Radiobutton(mode_frame, text="Copy files (originals stay untouched)",
                        variable=self.operation_mode, value="copy").pack(anchor=tk.W, pady=2)

        # Date subfolders (left column). Files always go into a camera-model
        # folder first; these choose the date structure inside it.
        subfolder_frame = ttk.LabelFrame(
            left_col, text="Date Subfolders (inside each camera folder)", padding="10")
        subfolder_frame.pack(fill=tk.X)

        ttk.Radiobutton(subfolder_frame, text="None (e.g. Sony A6000/photo.jpg)",
                        variable=self.subfolder_mode, value="none").pack(anchor=tk.W, pady=2)
        ttk.Radiobutton(subfolder_frame, text="By Year (e.g. Sony A6000/2024/)",
                        variable=self.subfolder_mode, value="year").pack(anchor=tk.W, pady=2)
        ttk.Radiobutton(subfolder_frame, text="By Month (e.g. iPhone 16 Pro/2024-03/)",
                        variable=self.subfolder_mode, value="month").pack(anchor=tk.W, pady=2)
        ttk.Radiobutton(subfolder_frame, text="By Year and Month (e.g. Sony A6000/2024/03-March/)",
                        variable=self.subfolder_mode, value="year-month").pack(anchor=tk.W, pady=2)

        # Extra options (right column)
        extras_frame = ttk.LabelFrame(right_col, text="Options", padding="10")
        extras_frame.pack(fill=tk.X, pady=(0, 8))

        self.raw_checkbox = ttk.Checkbutton(
            extras_frame,
            text="📸 Separate RAW files into 'RAW' subfolder",
            variable=self.separate_raw)
        self.raw_checkbox.pack(anchor=tk.W, pady=3)

        self.screenshot_checkbox = ttk.Checkbutton(
            extras_frame,
            text="📱 Separate Android screenshots",
            variable=self.separate_screenshots)
        self.screenshot_checkbox.pack(anchor=tk.W, pady=3)

        self.subfolders_checkbox = ttk.Checkbutton(
            extras_frame,
            text="🗂️ Include subfolders (scan recursively)",
            variable=self.include_subfolders)
        self.subfolders_checkbox.pack(anchor=tk.W, pady=3)

        self.multithread_checkbox = ttk.Checkbutton(
            extras_frame,
            text=f"⚡ Multithreaded scanning ({self.max_threads} threads)",
            variable=self.use_multithreading)
        self.multithread_checkbox.pack(anchor=tk.W, pady=3)

        video_note = ttk.Label(
            extras_frame,
            text="🎬 Videos go to a 'Videos' subfolder automatically.",
            font=('Segoe UI', 9), foreground='#888888', justify=tk.LEFT)
        video_note.pack(anchor=tk.W, pady=(6, 2))

        # Cleanup & safety (right column, below Options)
        safety_frame = ttk.LabelFrame(right_col, text="Cleanup & Safety",
                                      padding="10")
        safety_frame.pack(fill=tk.BOTH, expand=True)

        self.dedupe_checkbox = ttk.Checkbutton(
            safety_frame,
            text="♻️ Find duplicates by content (any filename)",
            variable=self.dedupe_content)
        self.dedupe_checkbox.pack(anchor=tk.W, pady=3)

        self.corrupt_checkbox = ttk.Checkbutton(
            safety_frame,
            text="🩺 Check files for damage while organizing",
            variable=self.check_corrupt, command=self._sync_thorough_state)
        self.corrupt_checkbox.pack(anchor=tk.W, pady=3)

        self.thorough_checkbox = ttk.Checkbutton(
            safety_frame,
            text="       └ Thorough check (much slower)",
            variable=self.corrupt_thorough)
        self.thorough_checkbox.pack(anchor=tk.W, pady=(0, 3))

        self.fixext_checkbox = ttk.Checkbutton(
            safety_frame,
            text="🏷️ Fix files whose extension doesn't match their contents",
            variable=self.fix_extensions)
        self.fixext_checkbox.pack(anchor=tk.W, pady=3)

        self.cleanup_checkbox = ttk.Checkbutton(
            safety_frame,
            text="🧹 Delete empty folders left behind (Move only)",
            variable=self.cleanup_empty)
        self.cleanup_checkbox.pack(anchor=tk.W, pady=3)

        safety_note = ttk.Label(
            safety_frame,
            text="Nothing is ever deleted. Duplicates go to 'Duplicates',\n"
                 "damaged files to 'Corrupt', and wrongly-named ones are\n"
                 "renamed into 'Wrong Extension'. Undo puts it all back.",
            font=('Segoe UI', 9), foreground='#888888', justify=tk.LEFT)
        safety_note.pack(anchor=tk.W, pady=(6, 2))

        self._sync_thorough_state()

        # Action buttons
        button_frame = ttk.Frame(main_frame)
        button_frame.grid(row=3, column=0, columnspan=3, pady=10)

        self.preview_btn = ttk.Button(button_frame, text="🔍 Preview (Dry Run)",
                                      command=self.preview_operation)
        self.preview_btn.pack(side=tk.LEFT, padx=4)

        self.check_btn = ttk.Button(button_frame, text="🩺 Check Files",
                                    command=self.check_files_operation)
        self.check_btn.pack(side=tk.LEFT, padx=4)

        self.execute_btn = ttk.Button(button_frame, text="▶️ Execute Operation",
                                      command=self.execute_operation)
        self.execute_btn.pack(side=tk.LEFT, padx=4)

        self.undo_btn = ttk.Button(button_frame, text="↩️ Undo Last Run",
                                   command=self.undo_last_operation, state=tk.DISABLED)
        self.undo_btn.pack(side=tk.LEFT, padx=4)

        self.undo_renames_btn = ttk.Button(button_frame, text="↩️ Undo Renames",
                                           command=self.undo_renames,
                                           state=tk.DISABLED)
        self.undo_renames_btn.pack(side=tk.LEFT, padx=4)

        self.stats_btn = ttk.Button(button_frame, text="📊 Statistics",
                                    command=self.show_statistics, state=tk.DISABLED)
        self.stats_btn.pack(side=tk.LEFT, padx=4)

        self.cancel_btn = ttk.Button(button_frame, text="⏹️ Cancel",
                                     command=self.cancel_operation, state=tk.DISABLED)
        self.cancel_btn.pack(side=tk.LEFT, padx=4)

        # Progress bar
        self.progress_var = tk.IntVar()
        self.progress_bar = ttk.Progressbar(main_frame, variable=self.progress_var,
                                            maximum=100)
        self.progress_bar.grid(row=4, column=0, columnspan=3, sticky=(tk.W, tk.E),
                               pady=(0, 5))

        # Status label
        self.status_label = ttk.Label(main_frame, text="Ready", font=('Segoe UI', 9))
        self.status_label.grid(row=5, column=0, columnspan=3, sticky=tk.W)

        # Output text area
        output_frame = ttk.LabelFrame(main_frame, text="Output Log", padding="5")
        output_frame.grid(row=6, column=0, columnspan=3, sticky=(tk.W, tk.E, tk.N, tk.S),
                          pady=10)
        output_frame.columnconfigure(0, weight=1)
        output_frame.rowconfigure(0, weight=1)

        self.output_text = scrolledtext.ScrolledText(output_frame, wrap=tk.WORD,
                                                     width=80, height=14,
                                                     font=('Consolas', 9),
                                                     borderwidth=0)
        self.output_text.grid(row=0, column=0, sticky=(tk.W, tk.E, tk.N, tk.S))

        ttk.Button(output_frame, text="Clear Log",
                   command=self.clear_log).grid(row=1, column=0, pady=5)

        main_frame.rowconfigure(6, weight=1)
        self._style_text_widget(self.output_text)

    def _sync_thorough_state(self):
        """The thorough toggle only means anything when checking is switched on."""
        self.thorough_checkbox.config(
            state=tk.NORMAL if self.check_corrupt.get() else tk.DISABLED)

    def _style_text_widget(self, widget):
        """Match a plain tk Text widget to the current sv-ttk theme."""
        if not SV_TTK_AVAILABLE:
            return
        if sv_ttk.get_theme() == "dark":
            widget.config(bg="#1c1c1c", fg="#e8e8e8", insertbackground="#e8e8e8")
        else:
            widget.config(bg="#fdfdfd", fg="#1a1a1a", insertbackground="#1a1a1a")

    def toggle_theme(self):
        """Switch between the dark and light Sun Valley themes."""
        if not SV_TTK_AVAILABLE:
            return
        new_theme = "light" if sv_ttk.get_theme() == "dark" else "dark"
        sv_ttk.set_theme(new_theme)
        self.theme_btn.config(text="☀️" if new_theme == "dark" else "🌙")
        self._style_text_widget(self.output_text)

    def show_statistics(self):
        """Show statistics in a popup window."""
        if not self.stats['total_files']:
            messagebox.showinfo("Statistics", "No statistics available yet. Run an operation first!")
            return

        stats_window = tk.Toplevel(self.root)
        stats_window.title("Organization Statistics")
        stats_window.geometry("500x600")

        stats_text = scrolledtext.ScrolledText(stats_window, wrap=tk.WORD,
                                               width=60, height=30,
                                               font=('Consolas', 10),
                                               borderwidth=0)
        stats_text.pack(padx=10, pady=10, fill=tk.BOTH, expand=True)
        self._style_text_widget(stats_text)

        report = "=" * 50 + "\n"
        report += "📊 PHOTO ORGANIZATION STATISTICS\n"
        report += "=" * 50 + "\n\n"

        report += f"📁 Total files scanned: {self.stats['total_files']}\n"
        report += f"✅ Successfully processed: {self.stats['processed']}\n"
        report += f"⚠️  No metadata found: {self.stats['no_metadata']}\n"
        report += f"❌ Errors encountered: {self.stats['errors']}\n"
        report += f"📱 Screenshots detected: {self.stats['screenshots']}\n"
        report += f"♻️ Identical duplicates skipped: {self.stats['duplicates']}\n"
        report += (f"♻️ Duplicate copies set aside: "
                   f"{self.stats['content_duplicates']} "
                   f"({self.stats['duplicate_bytes'] / (1024 * 1024):.2f} MB)\n")
        report += f"🩹 Damaged files found: {self.stats['damaged']}\n"
        report += f"🏷️ Wrong file extension: {self.stats['misnamed']}\n"
        report += f"❓ Could not be checked: {self.stats['unchecked']}\n"
        report += f"🧹 Empty folders removed: {self.stats['empty_folders_removed']}\n"
        report += f"✔️ Already organized (untouched): {self.stats['already_organized']}\n"
        report += f"💾 Total size processed: {self.stats['total_size_mb']:.2f} MB\n"

        if self.stats['by_model']:
            report += "\n" + "=" * 50 + "\n"
            report += "📱 FILES BY CAMERA MODEL:\n"
            report += "=" * 50 + "\n"
            for model, count in sorted(self.stats['by_model'].items(),
                                       key=lambda x: x[1], reverse=True):
                report += f"  {model}: {count} files\n"

        if self.stats['by_year']:
            report += "\n" + "=" * 50 + "\n"
            report += "📅 FILES BY YEAR:\n"
            report += "=" * 50 + "\n"
            for year, count in sorted(self.stats['by_year'].items()):
                report += f"  {year}: {count} files\n"

        if self.stats.get('duration_seconds'):
            rate = self.stats['processed'] / self.stats['duration_seconds']
            report += f"\n⚡ Processing speed: {rate:.1f} files/second\n"

        stats_text.insert(1.0, report)
        stats_text.config(state=tk.DISABLED)

        ttk.Button(stats_window, text="Close",
                   command=stats_window.destroy).pack(pady=10)

    def browse_folder(self):
        """Open folder browser dialog."""
        folder = filedialog.askdirectory(title="Select Source Folder")
        if folder:
            self.source_folder.set(folder)
            # Offer undo if the folder holds a not-yet-undone record
            undo_files = sorted(Path(folder).glob("kjegla_undo_*.json"))
            self.last_undo_file = str(undo_files[-1]) if undo_files else None
            rename_undos = sorted((Path(folder) / WRONG_EXT_FOLDER)
                                  .glob("kjegla_undo_renames_*.json"))
            self.last_rename_undo_file = (str(rename_undos[-1])
                                          if rename_undos else None)
            if not self.processing:
                self.undo_btn.config(state=tk.NORMAL if self.last_undo_file else tk.DISABLED)
                self.undo_renames_btn.config(
                    state=tk.NORMAL if self.last_rename_undo_file else tk.DISABLED)

    def clear_log(self):
        """Clear the output log."""
        self.output_text.delete(1.0, tk.END)

    def log(self, message):
        """Add message to output log."""
        self.queue.put(("log", message, None))

    def update_status(self, status):
        """Update status label."""
        self.queue.put(("status", status, None))

    def update_progress(self, value):
        """Update progress bar."""
        self.queue.put(("progress", value, None))

    def check_queue(self):
        """Check queue for updates from processing thread (batched)."""
        log_lines = []
        last_status = None
        last_progress = None
        try:
            while True:
                action, value, extra = self.queue.get_nowait()

                if action == "log":
                    log_lines.append(value)
                elif action == "status":
                    last_status = value
                elif action == "progress":
                    last_progress = value
                elif action == "enable_buttons":
                    self.preview_btn.config(state=tk.NORMAL)
                    self.execute_btn.config(state=tk.NORMAL)
                    self.check_btn.config(state=tk.NORMAL)
                    self.cancel_btn.config(state=tk.DISABLED)
                    self.stats_btn.config(state=tk.NORMAL if self.stats['total_files'] > 0 else tk.DISABLED)
                    self.undo_btn.config(state=tk.NORMAL if self.last_undo_file else tk.DISABLED)
                    self.undo_renames_btn.config(
                        state=tk.NORMAL if self.last_rename_undo_file else tk.DISABLED)
                elif action == "disable_buttons":
                    self.preview_btn.config(state=tk.DISABLED)
                    self.execute_btn.config(state=tk.DISABLED)
                    self.check_btn.config(state=tk.DISABLED)
                    self.undo_btn.config(state=tk.DISABLED)
                    self.undo_renames_btn.config(state=tk.DISABLED)
                    self.cancel_btn.config(state=tk.NORMAL)
                elif action == "undo_available":
                    self.last_undo_file = value
                elif action == "rename_undo_available":
                    self.last_rename_undo_file = value

        except queue.Empty:
            pass

        if log_lines:
            self.output_text.insert(tk.END, "\n".join(log_lines) + "\n")
            # Keep the widget bounded so very large runs stay responsive
            line_count = int(self.output_text.index('end-1c').split('.')[0])
            if line_count > 6000:
                self.output_text.delete('1.0', f'{line_count - 5000}.0')
            self.output_text.see(tk.END)
        if last_status is not None:
            self.status_label.config(text=last_status)
        if last_progress is not None:
            self.progress_var.set(last_progress)

        self.root.after(100, self.check_queue)

    def _snapshot_settings(self):
        """Read all tkinter variables on the main thread into a plain object."""
        return SimpleNamespace(
            source=self.source_folder.get(),
            operation=self.operation_mode.get(),
            subfolder_mode=self.subfolder_mode.get(),
            separate_raw=self.separate_raw.get(),
            separate_screenshots=self.separate_screenshots.get(),
            use_multithreading=self.use_multithreading.get(),
            include_subfolders=self.include_subfolders.get(),
            dedupe_content=self.dedupe_content.get(),
            check_corrupt=self.check_corrupt.get(),
            corrupt_thorough=self.corrupt_thorough.get(),
            cleanup_empty=self.cleanup_empty.get(),
            fix_extensions=self.fix_extensions.get(),
        )

    def get_target_folder(self, source_path, file_path, camera_model, photo_date,
                          settings, is_screenshot):
        """Compute the target folder for a file (pure - no side effects)."""
        base_folder = source_path / (camera_model or "Unknown Camera")

        mode = settings.subfolder_mode
        if mode == "year":
            target_folder = base_folder / str(photo_date.year)
        elif mode == "month":
            target_folder = base_folder / photo_date.strftime("%Y-%m")
        elif mode == "year-month":
            target_folder = base_folder / str(photo_date.year) / photo_date.strftime("%m-%B")
        else:
            target_folder = base_folder

        if is_video_file(file_path):
            target_folder = target_folder / "Videos"
        elif settings.separate_raw and is_raw_file(file_path):
            target_folder = target_folder / "RAW"
        elif is_screenshot:
            target_folder = target_folder / "Screenshots"

        return target_folder

    def _extract_metadata(self, files, settings, p0=0, p1=25):
        """Phase A: read metadata for images and videos (p0-p1%% progress).

        Returns {Path: meta dict}. Parallelized when multithreading is on.
        """
        metadata = {}
        total = len(files)
        if total == 0:
            return metadata

        def note_progress(done):
            self.update_progress(p0 + int(done / total * (p1 - p0)))
            self.update_status(f"Reading metadata {done}/{total}")

        if settings.use_multithreading and total > 1:
            with ThreadPoolExecutor(max_workers=self.max_threads) as executor:
                futures = {executor.submit(read_media_metadata, f): f for f in files}
                done = 0
                for future in as_completed(futures):
                    if self.cancel_requested:
                        executor.shutdown(wait=False, cancel_futures=True)
                        break
                    file_path = futures[future]
                    try:
                        metadata[file_path] = future.result()
                    except Exception:
                        metadata[file_path] = {}
                    done += 1
                    if done % 25 == 0 or done == total:
                        note_progress(done)
        else:
            for done, file_path in enumerate(files, start=1):
                if self.cancel_requested:
                    break
                metadata[file_path] = read_media_metadata(file_path)
                if done % 25 == 0 or done == total:
                    note_progress(done)

        return metadata

    def _check_health(self, files, settings, p0=25, p1=40):
        """Phase A2: check every file for damage. Returns {Path: (status, reason)}.

        Parallelized when multithreading is on - this is I/O bound, so extra
        threads help even in quick mode.
        """
        health = {}
        total = len(files)
        if total == 0:
            return health
        thorough = settings.corrupt_thorough
        # When only the extension option is on there's no need to pay for the
        # full damage check - stop after identifying what each file really is.
        format_only = not settings.check_corrupt
        label = ("Checking file names" if format_only else
                 "Thorough check" if thorough else "Checking files")
        last_note = [0.0]

        def note(done):
            now = time.monotonic()
            if now - last_note[0] >= 0.2 or done == total:
                self.update_progress(p0 + int(done / total * (p1 - p0)))
                self.update_status(f"{label} {done}/{total}")
                last_note[0] = now

        if settings.use_multithreading and total > 1:
            with ThreadPoolExecutor(max_workers=self.max_threads) as executor:
                futures = {executor.submit(file_health, f, thorough,
                                           format_only): f for f in files}
                for future in as_completed(futures):
                    if self.cancel_requested:
                        executor.shutdown(wait=False, cancel_futures=True)
                        break
                    file_path = futures[future]
                    try:
                        health[file_path] = future.result()
                    except Exception as e:
                        health[file_path] = ('damaged', f"check failed ({e})")
                    note(len(health))
        else:
            for file_path in files:
                if self.cancel_requested:
                    break
                health[file_path] = file_health(file_path, thorough,
                                                format_only)
                note(len(health))

        return health

    def _hash_many(self, paths, hasher, settings, note):
        """Hash a batch of files (parallel when enabled). Returns {Path: digest}.

        Files that cannot be read are simply left out - they are handled by
        the damage check, not here.
        """
        results = {}
        if not paths:
            return results
        if settings.use_multithreading and len(paths) > 1:
            with ThreadPoolExecutor(max_workers=self.max_threads) as executor:
                futures = {executor.submit(hasher, p): p for p in paths}
                done = 0
                for future in as_completed(futures):
                    if self.cancel_requested:
                        executor.shutdown(wait=False, cancel_futures=True)
                        break
                    path = futures[future]
                    try:
                        results[path] = future.result()
                    except OSError:
                        pass
                    done += 1
                    note(done)
        else:
            for done, path in enumerate(paths, start=1):
                if self.cancel_requested:
                    break
                try:
                    results[path] = hasher(path)
                except OSError:
                    pass
                note(done)
        return results

    @staticmethod
    def _keeper_rank(path, metadata, health):
        """Sort key for choosing which copy of an identical set to keep.

        Lowest wins. Health comes first on purpose: a damaged copy can never
        be kept over a healthy one.
        """
        status = health.get(path, ('unchecked', ''))[0]
        health_rank = {'ok': 0, 'misnamed': 1, 'unchecked': 1,
                       'damaged': 2}.get(status, 1)

        meta = metadata.get(path) or {}
        richness = (0 if meta.get('model') else 1) + (0 if meta.get('date') else 1)

        looks_copied = 1 if looks_like_copy_name(path.stem) else 0
        try:
            mtime = path.stat().st_mtime
        except OSError:
            mtime = float('inf')
        # Final entries are pure tie-breakers so repeated runs agree
        return (health_rank, richness, looks_copied, len(path.stem), mtime,
                str(path).lower())

    def _find_duplicates(self, files, metadata, health, settings, p0=40, p1=60):
        """Phase A3: find files with byte-for-byte identical content.

        Three stages, each only touching what the previous one could not rule
        out:
          1. group by size - a file with a unique size cannot have a twin and
             is never read at all
          2. hash the first 64 KB of same-size files
          3. full content hash, only where the head hashes also matched
        (Files at or under 64 KB are settled by stage 2 - the head hash
        already covered the whole file.)

        Returns (duplicate_of, groups): duplicate_of maps each set-aside file
        to its keeper; groups is [(keeper, [set-aside...])] for the report.
        """
        # Stage 1
        sizes = {}
        by_size = {}
        for f in files:
            try:
                size = f.stat().st_size
            except OSError:
                continue
            if size == 0:
                continue  # empty files are a damage problem, not a duplicate one
            sizes[f] = size
            by_size.setdefault(size, []).append(f)

        candidates = [f for group in by_size.values() if len(group) > 1
                      for f in group]
        if not candidates:
            self.update_progress(p1)
            return {}, []

        self.log(f"\n♻️ Comparing {len(candidates)} files that share a size "
                 f"with at least one other ({len(files) - len(candidates)} "
                 f"ruled out without reading them)")

        # Stage 2: head hashes, using the first 60% of this phase's progress
        mid = p0 + int((p1 - p0) * 0.6)
        head_total = len(candidates)
        last_note = [0.0]

        def note_head(done):
            now = time.monotonic()
            if now - last_note[0] >= 0.2 or done == head_total:
                self.update_progress(p0 + int(done / head_total * (mid - p0)))
                self.update_status(f"Comparing files {done}/{head_total}")
                last_note[0] = now

        head_hashes = self._hash_many(candidates, _hash_head, settings, note_head)

        by_head = {}
        for f, digest in head_hashes.items():
            by_head.setdefault((sizes[f], digest), []).append(f)

        final_groups = []
        need_full = []
        for (size, _digest), group in by_head.items():
            if len(group) < 2:
                continue
            if size <= HEAD_HASH_BYTES:
                final_groups.append(group)  # head hash was the whole file
            else:
                need_full.append(group)

        # Stage 3: full content hashes
        full_candidates = [f for group in need_full for f in group]
        full_total = len(full_candidates)
        last_note[0] = 0.0

        def note_full(done):
            now = time.monotonic()
            if now - last_note[0] >= 0.2 or done == full_total:
                self.update_progress(mid + int(done / full_total * (p1 - mid)))
                self.update_status(f"Verifying possible duplicates "
                                   f"{done}/{full_total}")
                last_note[0] = now

        full_hashes = self._hash_many(full_candidates, _hash_file, settings,
                                      note_full)
        by_full = {}
        for f, digest in full_hashes.items():
            by_full.setdefault((sizes[f], digest), []).append(f)
        final_groups.extend(g for g in by_full.values() if len(g) > 1)

        # Pick a keeper per group; everything else gets set aside
        duplicate_of = {}
        groups = []
        for group in final_groups:
            ordered = sorted(group,
                             key=lambda p: self._keeper_rank(p, metadata, health))
            keeper, rest = ordered[0], ordered[1:]
            for dup in rest:
                duplicate_of[dup] = keeper
            groups.append((keeper, rest))

        self.update_progress(p1)
        return duplicate_of, groups

    def _write_duplicate_report(self, report_path, groups, source_path):
        """Write a plain listing of every duplicate group and its keeper."""
        try:
            with open(report_path, 'w', encoding='utf-8') as f:
                f.write(f"Kjegla's Photo Organizer - duplicate report "
                        f"{datetime.now()}\n")
                f.write(f"Source: {source_path}\n")
                f.write("Files are duplicates only when their contents match "
                        "exactly, byte for byte.\n")
                f.write("=" * 60 + "\n\n")
                for keeper, rest in groups:
                    try:
                        size_mb = keeper.stat().st_size / (1024 * 1024)
                    except OSError:
                        size_mb = 0.0
                    f.write(f"KEEP     {keeper.relative_to(source_path)} "
                            f"({size_mb:.2f} MB)\n")
                    for dup in rest:
                        f.write(f"  DUP    {dup.relative_to(source_path)}\n")
                    f.write("\n")
        except OSError:
            pass

    def organize_photos(self, settings, dry_run=True):
        """Organize photos and videos from source folder by camera model.

        Runs on a worker thread; must not touch tkinter widgets/variables.
        """
        start_time = datetime.now()
        source_path = Path(settings.source)

        # Reset statistics
        self.stats = self._empty_stats()

        operation = settings.operation
        operation_text = "Moving" if operation == "move" else "Copying"

        self.log("=" * 60)
        self.log(f"Kjegla's Photo Organizer - {'DRY RUN' if dry_run else operation_text.upper()}")
        self.log(f"Source: {source_path}")
        self.log(f"Operation: {operation}")
        self.log(f"Subfolder mode: {settings.subfolder_mode}")
        self.log(f"Separate RAW files: {'Yes' if settings.separate_raw else 'No'}")
        self.log(f"Separate Screenshots: {'Yes' if settings.separate_screenshots else 'No'}")
        self.log(f"Include subfolders: {'Yes' if settings.include_subfolders else 'No'}")
        self.log(f"Multithreading: {'Yes' if settings.use_multithreading else 'No'}")
        self.log(f"Find duplicates by content: "
                 f"{'Yes' if settings.dedupe_content else 'No'}")
        self.log(f"Check files for damage: "
                 f"{('Yes (thorough)' if settings.corrupt_thorough else 'Yes') if settings.check_corrupt else 'No'}")
        self.log(f"Delete empty folders: "
                 f"{'Yes' if settings.cleanup_empty else 'No'}")
        self.log("=" * 60)

        try:
            regular_media_files, raw_files = collect_media_files(
                source_path, settings.include_subfolders,
                sniff_unknown=settings.fix_extensions)
        except OSError as e:
            self.log(f"❌ Could not read source folder: {e}")
            self.update_status("Error reading source folder")
            return

        # Process regular files first so RAW/video matching can use their models
        media_files = regular_media_files + raw_files

        # Snapshot the folder state now; a dry run stores this so Execute can
        # later prove nothing changed since the preview
        fingerprint = folder_fingerprint(media_files) if dry_run else None

        self.stats['total_files'] = len(media_files)

        if not media_files:
            self.log("No media files found in the source folder!")
            self.update_status("No media files found")
            return

        # Phase A: one metadata read per image/video (parallel if enabled)
        self.log("\n📋 Reading media metadata...")
        metadata = self._extract_metadata(regular_media_files, settings)

        if self.cancel_requested:
            self.log("\n⏹️ Operation cancelled by user")
            self.update_status("Cancelled")
            return

        # Phase A2: damage check and/or extension check (either can be off)
        health = {}
        if settings.check_corrupt or settings.fix_extensions:
            if settings.check_corrupt:
                self.log("\n🩺 Checking files for damage"
                         f"{' (thorough)' if settings.corrupt_thorough else ''}...")
            else:
                self.log("\n🏷️ Checking whether file extensions match their "
                         "contents...")
            health = self._check_health(media_files, settings)
            if self.cancel_requested:
                self.log("\n⏹️ Operation cancelled by user")
                self.update_status("Cancelled")
                return
            self.stats['damaged'] = sum(1 for s, _ in health.values()
                                        if s == 'damaged')
            self.stats['misnamed'] = sum(1 for s, _ in health.values()
                                         if s == 'misnamed')
            self.stats['unchecked'] = sum(1 for s, _ in health.values()
                                          if s == 'unchecked')
            fine = (len(health) - self.stats['damaged']
                    - self.stats['misnamed'] - self.stats['unchecked'])
            if settings.check_corrupt:
                self.log(f"  {self.stats['damaged']} damaged, "
                         f"{self.stats['misnamed']} with the wrong file extension, "
                         f"{self.stats['unchecked']} could not be checked, "
                         f"{fine} fine")
            else:
                self.log(f"  {self.stats['misnamed']} file(s) have an extension "
                         f"that doesn't match their contents")

        # Phase A3: content-based duplicate detection (optional)
        duplicate_of, duplicate_groups = {}, []
        if settings.dedupe_content:
            self.log("\n♻️ Looking for duplicate files by content...")
            duplicate_of, duplicate_groups = self._find_duplicates(
                media_files, metadata, health, settings)
            if self.cancel_requested:
                self.log("\n⏹️ Operation cancelled by user")
                self.update_status("Cancelled")
                return
            self.stats['content_duplicates'] = len(duplicate_of)
            for dup in duplicate_of:
                try:
                    self.stats['duplicate_bytes'] += dup.stat().st_size
                except OSError:
                    pass
            if duplicate_groups:
                wasted = self.stats['duplicate_bytes'] / (1024 * 1024)
                self.log(f"  Found {len(duplicate_groups)} set(s) of identical "
                         f"files - {len(duplicate_of)} extra copies "
                         f"({wasted:.2f} MB)")
            else:
                self.log("  No duplicates found")
        else:
            self.update_progress(60)

        # Map base filenames to camera models for RAW/video matching
        base_name_to_model = {}
        for file_path, meta in metadata.items():
            model = model_for_image(file_path, meta)
            if model:
                base_name_to_model[file_path.stem] = model
                # Also filed under the shared photo name, so a RAW called
                # "....RAW-02.ORIGINAL.dng" finds "....RAW-01.MP.jpg".
                # setdefault, so an exact filename match always wins.
                base_name_to_model.setdefault(media_base_name(file_path), model)

        self.log(f"Found {len(base_name_to_model)} images with camera metadata")
        self.log(f"Found {len(raw_files)} RAW files and "
                 f"{sum(1 for f in media_files if is_video_file(f))} video files")

        # Create log file (and matching undo record for real runs)
        run_stamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        log_filename = f"kjegla_media_log_{run_stamp}.txt"
        log_path = source_path / log_filename
        undo_path = source_path / f"kjegla_undo_{run_stamp}.json"

        # Per-run context: planned targets make dry-run duplicate renames
        # accurate; undo_entries feeds undo; plan_ops collects [source, target]
        # pairs for the preview cache; duplicate_of/health carry the results of
        # the two analysis phases into the per-file loop
        ctx = SimpleNamespace(planned_targets=set(), undo_entries=[],
                              plan_ops=[], duplicate_of=duplicate_of,
                              health=health, rename_entries=[])

        if duplicate_groups:
            report_name = f"kjegla_duplicates_{run_stamp}.txt"
            self._write_duplicate_report(source_path / report_name,
                                         duplicate_groups, source_path)
            self.log(f"📄 Duplicate report saved: {report_name}")

        with open(log_path, 'w', encoding='utf-8') as log_file:
            log_file.write(f"Kjegla's Media Organization Log - {datetime.now()}\n")
            log_file.write(f"Source Folder: {source_path}\n")
            log_file.write(f"Mode: {'DRY RUN' if dry_run else operation.upper()}\n")
            log_file.write(f"Subfolder organization: {settings.subfolder_mode}\n")
            log_file.write(f"Separate RAW files: {'Yes' if settings.separate_raw else 'No'}\n")
            log_file.write(f"Separate Screenshots: {'Yes' if settings.separate_screenshots else 'No'}\n")
            log_file.write("=" * 60 + "\n\n")

            total = self.stats['total_files']
            last_progress = -1
            phase_start = time.monotonic()
            last_status_time = 0.0

            for idx, file_path in enumerate(media_files):
                if self.cancel_requested:
                    self.log("\n⏹️ Operation cancelled by user")
                    break

                # Phase B occupies 60-100% of the progress bar
                progress = 60 + int((idx + 1) / total * 40)
                if progress != last_progress:
                    self.update_progress(progress)
                    last_progress = progress

                # Status line with live rate / ETA / error count (throttled)
                now = time.monotonic()
                if now - last_status_time >= 0.2 or idx + 1 == total:
                    elapsed = now - phase_start
                    rate = (idx + 1) / elapsed if elapsed > 0 else 0
                    if rate > 0.01 and idx + 1 < total:
                        remaining = (total - idx - 1) / rate
                        eta = f"{int(remaining // 60)}:{int(remaining % 60):02d}"
                    else:
                        eta = "--:--"
                    self.update_status(
                        f"Processing {idx + 1}/{total} • {rate:.0f} files/s "
                        f"• ETA {eta} • errors {self.stats['errors']}")
                    last_status_time = now

                try:
                    self._process_one_file(file_path, source_path, settings,
                                           base_name_to_model, metadata,
                                           ctx, operation,
                                           operation_text, dry_run, log_file)
                except Exception as e:
                    self.log(f"\n❌ Error processing {file_path.name}: {e}")
                    self.log("   Skipping this file and continuing...")
                    log_file.write(f"ERROR processing {file_path.name}: {e}\n")
                    self.stats['errors'] += 1

                # Flush the undo record periodically so a crash can't lose it
                if not dry_run and ctx.undo_entries and (idx + 1) % 50 == 0:
                    self._write_undo(undo_path, operation, source_path,
                                     ctx.undo_entries)

            # Summary
            duration = (datetime.now() - start_time).total_seconds()
            self.stats['duration_seconds'] = duration

            # Finalize the undo record
            if not dry_run and ctx.undo_entries:
                self._write_undo(undo_path, operation, source_path,
                                 ctx.undo_entries)
                self.queue.put(("undo_available", str(undo_path), None))

            # Renames get their own undo record, kept inside the folder they
            # affect, so they can be reversed without unpicking the whole run
            if not dry_run and ctx.rename_entries:
                rename_undo = (source_path / WRONG_EXT_FOLDER /
                               f"kjegla_undo_renames_{run_stamp}.json")
                rename_undo.parent.mkdir(parents=True, exist_ok=True)
                self._write_undo(rename_undo, "move", source_path,
                                 ctx.rename_entries)
                self.queue.put(("rename_undo_available", str(rename_undo), None))
                self.log(f"\n↩️ {len(ctx.rename_entries)} rename(s) can be "
                         f"undone on their own with 'Undo Renames'.")

            # After a move, tidy up every folder left empty behind us
            if not dry_run and operation == "move" and settings.cleanup_empty:
                removed = self._sweep_empty_dirs(source_path, log_file)
                self.stats['empty_folders_removed'] = removed
                if removed:
                    self.log(f"\n🧹 Removed {removed} empty folder(s)")

            self.log("\n" + "=" * 60)
            self.log("SUMMARY:")
            self.log(f"Total media files: {self.stats['total_files']}")
            self.log(f"Files processed: {self.stats['processed']}")
            self.log(f"Files without metadata: {self.stats['no_metadata']}")
            self.log(f"Screenshots detected: {self.stats['screenshots']}")
            if self.stats['content_duplicates']:
                wasted = self.stats['duplicate_bytes'] / (1024 * 1024)
                self.log(f"Duplicate copies set aside: "
                         f"{self.stats['content_duplicates']} ({wasted:.2f} MB) "
                         f"→ '{DUPLICATES_FOLDER}' folder")
            if self.stats['damaged']:
                self.log(f"Damaged files found: {self.stats['damaged']} "
                         f"→ '{CORRUPT_FOLDER}' folder")
            if self.stats['misnamed']:
                self.log(f"Wrongly-named files fixed: {self.stats['misnamed']} "
                         f"→ renamed to the right extension and moved to "
                         f"'{WRONG_EXT_FOLDER}' (their contents were fine)")
            if self.stats['unchecked']:
                self.log(f"Files that could not be checked: "
                         f"{self.stats['unchecked']} (RAW / some video formats)")
            if self.stats['duplicates']:
                self.log(f"Identical duplicates skipped: {self.stats['duplicates']}")
            if self.stats['already_organized']:
                self.log(f"Already organized (untouched): {self.stats['already_organized']}")
            self.log(f"Errors: {self.stats['errors']}")
            self.log(f"Total size: {self.stats['total_size_mb']:.2f} MB")
            self.log(f"Duration: {duration:.1f} seconds")

            log_file.write("\n" + "=" * 60 + "\n")
            log_file.write("SUMMARY:\n")
            log_file.write(f"Total files: {self.stats['total_files']}\n")
            log_file.write(f"Processed: {self.stats['processed']}\n")
            log_file.write(f"No metadata/unknown: {self.stats['no_metadata']}\n")
            log_file.write(f"Screenshots detected: {self.stats['screenshots']}\n")
            log_file.write(f"Identical duplicates skipped: {self.stats['duplicates']}\n")
            log_file.write(f"Duplicate copies set aside: {self.stats['content_duplicates']}\n")
            log_file.write(f"Damaged files: {self.stats['damaged']}\n")
            log_file.write(f"Wrong file extension: {self.stats['misnamed']}\n")
            log_file.write(f"Could not be checked: {self.stats['unchecked']}\n")
            log_file.write(f"Empty folders removed: {self.stats['empty_folders_removed']}\n")
            log_file.write(f"Already organized: {self.stats['already_organized']}\n")
            log_file.write(f"Errors: {self.stats['errors']}\n")
            log_file.write(f"Total size: {self.stats['total_size_mb']:.2f} MB\n")
            log_file.write(f"Duration: {duration:.1f} seconds\n")

            if self.stats['by_model']:
                self.log("\nFiles per camera model:")
                log_file.write("\nFiles per camera model:\n")
                for model, count in sorted(self.stats['by_model'].items()):
                    self.log(f"  {model}: {count} files")
                    log_file.write(f"  {model}: {count} files\n")

            if self.stats['no_metadata'] > 0:
                self.log(f"\n📂 Unknown/unmatched files: {self.stats['no_metadata']} "
                         f"(moved to 'Unknown Camera' folder)")

            if self.stats['screenshots'] > 0:
                self.log(f"\n📱 Screenshots separated: {self.stats['screenshots']} files")

            self.log(f"\n📄 Log file saved: {log_filename}")

            if dry_run:
                self.log("\n⚠️  This was a DRY RUN - no files were actually moved/copied!")
                if not self.cancel_requested:
                    self.cached_plan = {
                        'key': plan_key(settings),
                        'fingerprint': fingerprint,
                        'ops': ctx.plan_ops,
                        'stats': copy.deepcopy(self.stats),
                    }
                    self.log("⚡ Preview cached - Execute will reuse it "
                             "without re-scanning (as long as nothing changes).")
                self.update_status("Dry run complete")
            else:
                self.cached_plan = None  # the folder just changed
                self.log(f"\n✅ Operation complete! Files were {operation}d successfully.")
                if ctx.undo_entries:
                    self.log("↩️ This run can be undone with the 'Undo Last Run' button.")
                self.update_status(f"Operation complete - {self.stats['processed']} files {operation}d")

    @staticmethod
    def _write_undo(undo_path, operation, source_path, entries):
        """Persist the undo record (list of [target, original] pairs)."""
        try:
            with open(undo_path, 'w', encoding='utf-8') as f:
                json.dump({'operation': operation,
                           'created': datetime.now().isoformat(),
                           'source': str(source_path),
                           'entries': entries}, f)
        except OSError:
            pass

    @staticmethod
    def _sweep_empty_dirs(source_root, log_file=None):
        """Delete every genuinely empty folder under the source, bottom-up.

        Walking bottom-up means a whole nest of empty folders collapses in one
        pass. Only empty directories are removed - no file is ever deleted, so
        a folder holding so much as a Thumbs.db is left alone. The source root
        itself is never removed.
        """
        removed = 0
        source_root = Path(source_root)
        for dirpath, _dirs, _names in os.walk(source_root, topdown=False):
            folder = Path(dirpath)
            if folder == source_root:
                continue
            try:
                if not any(folder.iterdir()):
                    folder.rmdir()
                    removed += 1
                    if log_file:
                        log_file.write(f"Removed empty folder: "
                                       f"{folder.relative_to(source_root)}\n")
            except OSError:
                pass  # in use, permission denied, or raced - just leave it
        return removed

    def _set_aside_file(self, file_path, source_path, folder_name, reason,
                        ctx, settings, dry_run, log_file, new_name=None):
        """Move a duplicate, damaged or misnamed file into its set-aside folder.

        The original folder structure is mirrored inside Duplicates/, Corrupt/
        or Wrong Extension/, so anything can be put back by hand. Nothing is
        ever deleted, and because this is an ordinary move it lands in the undo
        record too. `new_name` renames the file on the way (used to give a
        misnamed file the extension it should have had).
        """
        rel = file_path.relative_to(source_path)
        icon = {DUPLICATES_FOLDER: "♻️", CORRUPT_FOLDER: "🩹",
                WRONG_EXT_FOLDER: "🏷️"}.get(folder_name, "📦")

        if settings.operation == "copy":
            # Copy mode leaves the source untouched and the good original is
            # already there, so making a third copy of a redundant or broken
            # file would only add clutter.
            self.log(f"\n{icon} {rel}: {reason}")
            self.log("  ⏭️ Left where it is (copy mode never touches the source)")
            log_file.write(f"{folder_name}: {rel} - {reason} "
                           f"(copy mode, not copied)\n")
            return

        target = source_path / folder_name / rel
        if new_name and new_name != file_path.name:
            target = target.with_name(new_name)

        def claimed(path):
            return path.exists() or str(path).lower() in ctx.planned_targets

        if claimed(target):
            if target.exists() and files_identical(file_path, target):
                self.log(f"\n{icon} {rel}: already set aside in "
                         f"{folder_name}/, leaving it alone")
                log_file.write(f"{folder_name}: {rel} already present - skipped\n")
                return
            base = target
            counter = 1
            while claimed(target):
                target = base.parent / f"{base.stem}_{counter}{base.suffix}"
                counter += 1
        ctx.planned_targets.add(str(target).lower())

        self.log(f"\n{icon} {rel}: {reason}")
        relative_target = target.relative_to(source_path)

        if dry_run:
            ctx.plan_ops.append([str(file_path), str(target)])
            self.log(f"  🔍 Would move to: {relative_target}")
            log_file.write(f"{folder_name}: would move {rel} -> "
                           f"{relative_target}\n")
            return

        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            shutil.move(str(file_path), str(target))
            ctx.undo_entries.append([str(target), str(file_path)])
            if folder_name == WRONG_EXT_FOLDER:
                # These also get their own undo record, so the renames can be
                # reversed on their own without touching the rest of the run
                ctx.rename_entries.append([str(target), str(file_path)])
            self.log(f"  ➡️ Moved to: {relative_target}")
            log_file.write(f"{folder_name}: {rel} -> {relative_target}\n")
        except Exception as e:
            self.log(f"  ❌ Could not set aside: {e}")
            log_file.write(f"ERROR setting aside {rel}: {e}\n")
            self.stats['errors'] += 1

    def _process_one_file(self, file_path, source_path, settings, base_name_to_model,
                          metadata, ctx, operation, operation_text,
                          dry_run, log_file):
        """Classify one file, compute its target, and move/copy (or preview) it."""
        meta = metadata.get(file_path)

        # Files the analysis phases flagged go to a set-aside folder instead of
        # into the organized folders. Duplicates are checked first: a duplicate
        # that also happens to be damaged is still just a duplicate, and its
        # keeper is guaranteed to be the healthier copy.
        keeper = ctx.duplicate_of.get(file_path)
        if keeper is not None:
            self._set_aside_file(
                file_path, source_path, DUPLICATES_FOLDER,
                f"identical to {keeper.relative_to(source_path)}",
                ctx, settings, dry_run, log_file)
            return

        if settings.check_corrupt:
            status, reason = ctx.health.get(file_path, ('unchecked', ''))
            if status == 'damaged':
                self._set_aside_file(file_path, source_path, CORRUPT_FOLDER,
                                     f"damaged - {reason}",
                                     ctx, settings, dry_run, log_file)
                return
        if settings.fix_extensions:
            status, reason = ctx.health.get(file_path, ('ok', ''))
            if status == 'misnamed':
                new_ext = canonical_extension_for(file_path)
                new_name = (file_path.stem + new_ext) if new_ext else None
                self._set_aside_file(file_path, source_path, WRONG_EXT_FOLDER,
                                     f"wrong file extension - {reason}",
                                     ctx, settings, dry_run, log_file,
                                     new_name=new_name)
                return

        # Classify the file and determine its camera model
        camera_model = None
        is_screenshot = False
        match_note = None

        if is_raw_file(file_path):
            file_type = "📸 RAW"
            # RAW metadata is never read directly - match to a sibling image
            camera_model = lookup_model(base_name_to_model, file_path)
            if camera_model:
                match_note = f"  🔗 Matched RAW to JPEG: {camera_model}"
            else:
                camera_model = camera_from_filename(file_path)
                match_note = (f"  🏷️  No matching JPEG; filename says {camera_model}"
                              if camera_model else
                              "  ⚠️  No matching JPEG for RAW, will move to Unknown")
        elif is_video_file(file_path):
            file_type = "🎬 Video"
            camera_model = lookup_model(base_name_to_model, file_path)
            if camera_model:
                match_note = f"  🔗 Matched video to image: {camera_model}"
            else:
                camera_model = camera_from_filename(file_path)
                match_note = (f"  🏷️  No matching image; filename says {camera_model}"
                              if camera_model else
                              "  ⚠️  No matching image for video, will move to Unknown")
        else:
            file_type = "📷 Image"
            camera_model = model_for_image(file_path, meta)
            if (camera_model == "iPhone" and file_path.suffix.lower() in HEIC_EXTS
                    and not (meta and meta.get('model'))):
                match_note = "  📱 HEIC format, assuming iPhone"
            if settings.separate_screenshots and looks_like_screenshot(file_path, meta):
                is_screenshot = True
                file_type += " (Screenshot)"

        # Date: metadata (EXIF / video header) if available, else modified time
        photo_date = meta.get('date') if meta else None
        if photo_date is None:
            photo_date = datetime.fromtimestamp(file_path.stat().st_mtime)

        target_folder = self.get_target_folder(source_path, file_path, camera_model,
                                               photo_date, settings, is_screenshot)
        target_path = target_folder / file_path.name

        # Recursive re-runs: files already in their correct spot are untouched
        if target_path == file_path:
            self.log(f"\n✔️ Already organized: "
                     f"{file_path.relative_to(source_path)}")
            log_file.write(f"Already organized - skipped: {file_path.name}\n")
            self.stats['already_organized'] += 1
            return

        file_size_mb = file_path.stat().st_size / (1024 * 1024)
        self.stats['total_size_mb'] += file_size_mb

        self.log(f"\n{file_type} Processing: {file_path.name} ({file_size_mb:.2f} MB)")
        log_file.write(f"Processing: {file_path.name}\n")
        if match_note:
            self.log(match_note)
        if is_screenshot:
            self.stats['screenshots'] += 1

        # Update statistics
        self.stats['by_year'][str(photo_date.year)] = \
            self.stats['by_year'].get(str(photo_date.year), 0) + 1
        if camera_model:
            self.stats['by_model'][camera_model] = \
                self.stats['by_model'].get(camera_model, 0) + 1
        else:
            self.stats['no_metadata'] += 1

        # Handle duplicates. Name collisions with identical content are
        # skipped entirely; different content gets a _1/_2 rename.
        # (ctx.planned_targets makes dry-run renames accurate.)
        def claimed(path):
            return path.exists() or str(path).lower() in ctx.planned_targets

        if claimed(target_path):
            if target_path.exists() and files_identical(file_path, target_path):
                self.log(f"  ♻️ Identical file already at destination, skipping"
                         f"{' (left in source)' if operation == 'move' and not dry_run else ''}")
                log_file.write("  Identical duplicate - skipped\n")
                self.stats['duplicates'] += 1
                return
            counter = 1
            while claimed(target_path):
                target_path = target_folder / f"{file_path.stem}_{counter}{file_path.suffix}"
                counter += 1
                if target_path.exists() and files_identical(file_path, target_path):
                    self.log("  ♻️ Identical file already at destination, skipping")
                    log_file.write("  Identical duplicate - skipped\n")
                    self.stats['duplicates'] += 1
                    return
            self.log(f"  🔄 Duplicate found, will rename to: {target_path.name}")
        ctx.planned_targets.add(str(target_path).lower())

        if not dry_run:
            target_folder.mkdir(parents=True, exist_ok=True)
            try:
                if operation == "move":
                    shutil.move(str(file_path), str(target_path))
                else:  # copy
                    shutil.copy2(str(file_path), str(target_path))

                ctx.undo_entries.append([str(target_path), str(file_path)])
                relative_path = target_path.relative_to(source_path)
                self.log(f"  ✅ {operation_text} to: {relative_path}")
                log_file.write(f"  {operation_text} to: {relative_path}\n")
                self.stats['processed'] += 1
            except Exception as e:
                self.log(f"  ❌ File operation error: {e}")
                log_file.write(f"  FILE OPERATION ERROR: {e}\n")
                self.stats['errors'] += 1
        else:
            ctx.plan_ops.append([str(file_path), str(target_path)])
            relative_path = target_path.relative_to(source_path)
            self.log(f"  🔍 Would {operation} to: {relative_path}")
            log_file.write(f"  Would {operation} to: {relative_path}\n")
            self.stats['processed'] += 1

    def _execute_cached_plan(self, plan, settings):
        """Execute a cached preview plan without re-analyzing anything.

        Returns True if the plan ran; False if the folder no longer matches
        the preview fingerprint (caller then runs a fresh full analysis).
        """
        source_path = Path(settings.source)
        start_time = datetime.now()

        self.update_status("Verifying folder is unchanged since preview...")
        try:
            regular, raw = collect_media_files(
                source_path, settings.include_subfolders,
                sniff_unknown=settings.fix_extensions)
        except OSError as e:
            self.log(f"❌ Could not read source folder: {e}")
            return False
        if folder_fingerprint(regular + raw) != plan['fingerprint']:
            self.log("⚠️ Folder changed since the preview - "
                     "running a fresh analysis instead.")
            return False

        ops = plan['ops']
        operation = settings.operation
        operation_text = "Moving" if operation == "move" else "Copying"

        # Start from the preview's statistics; redo the live counters
        self.stats = copy.deepcopy(plan['stats'])
        self.stats['processed'] = 0
        self.stats['errors'] = 0

        run_stamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        log_filename = f"kjegla_media_log_{run_stamp}.txt"
        undo_path = source_path / f"kjegla_undo_{run_stamp}.json"

        self.log("=" * 60)
        self.log(f"Kjegla's Photo Organizer - {operation_text.upper()} (cached preview)")
        self.log(f"Source: {source_path}")
        self.log(f"⚡ Folder unchanged since preview - executing "
                 f"{len(ops)} planned operation(s) directly")
        self.log("=" * 60)

        undo_entries = []
        total = len(ops)
        phase_start = time.monotonic()
        last_status_time = 0.0
        last_progress = -1

        with open(source_path / log_filename, 'w', encoding='utf-8') as log_file:
            log_file.write(f"Kjegla's Media Organization Log - {datetime.now()}\n")
            log_file.write(f"Source Folder: {source_path}\n")
            log_file.write(f"Mode: {operation.upper()} (cached preview replay)\n")
            log_file.write("=" * 60 + "\n\n")

            for idx, (src, dst) in enumerate(ops):
                if self.cancel_requested:
                    self.log("\n⏹️ Operation cancelled by user")
                    break

                progress = int((idx + 1) / total * 100) if total else 100
                if progress != last_progress:
                    self.update_progress(progress)
                    last_progress = progress
                now = time.monotonic()
                if now - last_status_time >= 0.2 or idx + 1 == total:
                    elapsed = now - phase_start
                    rate = (idx + 1) / elapsed if elapsed > 0 else 0
                    if rate > 0.01 and idx + 1 < total:
                        remaining = (total - idx - 1) / rate
                        eta = f"{int(remaining // 60)}:{int(remaining % 60):02d}"
                    else:
                        eta = "--:--"
                    self.update_status(
                        f"Processing {idx + 1}/{total} • {rate:.0f} files/s "
                        f"• ETA {eta} • errors {self.stats['errors']}")
                    last_status_time = now

                src_p, dst_p = Path(src), Path(dst)
                try:
                    if not src_p.exists():
                        raise FileNotFoundError("source file vanished")
                    # The fingerprint doesn't cover target folders on flat
                    # scans, so re-check the landing spot and dodge if taken
                    if dst_p.exists():
                        base = dst_p
                        counter = 1
                        while dst_p.exists():
                            dst_p = base.parent / f"{base.stem}_{counter}{base.suffix}"
                            counter += 1
                        self.log(f"  🔄 {base.name}: destination taken, "
                                 f"renaming to {dst_p.name}")
                    dst_p.parent.mkdir(parents=True, exist_ok=True)
                    if operation == "move":
                        shutil.move(str(src_p), str(dst_p))
                    else:
                        shutil.copy2(str(src_p), str(dst_p))
                    undo_entries.append([str(dst_p), str(src_p)])
                    relative = dst_p.relative_to(source_path)
                    self.log(f"  ✅ {operation_text}: {src_p.name} → {relative}")
                    log_file.write(f"{operation_text}: {src} -> {dst_p}\n")
                    self.stats['processed'] += 1
                except Exception as e:
                    self.log(f"  ❌ {src_p.name}: {e}")
                    log_file.write(f"ERROR {src}: {e}\n")
                    self.stats['errors'] += 1

                # Flush the undo record periodically so a crash can't lose it
                if undo_entries and (idx + 1) % 50 == 0:
                    self._write_undo(undo_path, operation, source_path,
                                     undo_entries)

            if undo_entries:
                self._write_undo(undo_path, operation, source_path, undo_entries)
                self.queue.put(("undo_available", str(undo_path), None))

            if operation == "move" and settings.cleanup_empty:
                removed = self._sweep_empty_dirs(source_path, log_file)
                self.stats['empty_folders_removed'] = removed
                if removed:
                    self.log(f"\n🧹 Removed {removed} empty folder(s)")

            duration = (datetime.now() - start_time).total_seconds()
            self.stats['duration_seconds'] = duration

            self.log("\n" + "=" * 60)
            self.log("SUMMARY:")
            self.log(f"Files processed: {self.stats['processed']}")
            self.log(f"Errors: {self.stats['errors']}")
            self.log(f"Duration: {duration:.1f} seconds "
                     f"(analysis skipped - cached preview)")
            log_file.write(f"\nSUMMARY: processed {self.stats['processed']}, "
                           f"errors {self.stats['errors']}, {duration:.1f}s\n")
            self.log(f"\n📄 Log file saved: {log_filename}")
            self.log(f"\n✅ Operation complete! Files were {operation}d successfully.")
            if undo_entries:
                self.log("↩️ This run can be undone with the 'Undo Last Run' button.")
            self.update_status(
                f"Operation complete - {self.stats['processed']} files {operation}d")

        self.cached_plan = None
        return True

    def _validate_source(self):
        """Validate the source folder on the main thread. Returns Path or None."""
        if not self.source_folder.get():
            messagebox.showwarning("No Folder", "Please select a source folder first!")
            return None
        source_path = Path(self.source_folder.get())
        if not source_path.exists() or not source_path.is_dir():
            messagebox.showerror("Error", "Please select a valid source folder!")
            return None
        return source_path

    def _start_worker(self, dry_run, use_cache=False):
        """Snapshot settings and launch the processing thread."""
        settings = self._snapshot_settings()
        self.processing = True
        self.cancel_requested = False
        self.queue.put(("disable_buttons", None, None))

        def run():
            try:
                ran = False
                if use_cache and not dry_run:
                    plan = self.cached_plan
                    if plan and plan['key'] == plan_key(settings):
                        ran = self._execute_cached_plan(plan, settings)
                if not ran:
                    self.organize_photos(settings, dry_run=dry_run)
            except Exception as e:
                self.log(f"\n❌ Unexpected error: {e}")
            finally:
                self.processing = False
                self.queue.put(("enable_buttons", None, None))
                self.update_progress(0)

        thread = threading.Thread(target=run, daemon=True)
        thread.start()

    def preview_operation(self):
        """Run a dry run preview."""
        if self.processing:
            return
        if self._validate_source() is None:
            return
        self._start_worker(dry_run=True)

    def execute_operation(self):
        """Execute the actual operation."""
        if self.processing:
            return
        if self._validate_source() is None:
            return

        operation = self.operation_mode.get()
        cache_ready = (self.cached_plan is not None and
                       self.cached_plan['key'] == plan_key(self._snapshot_settings()))
        cache_note = ("\n\n⚡ Your preview will be reused - no re-scan needed "
                      "(unless the folder changed since then)." if cache_ready else "")
        result = messagebox.askyesno(
            "Confirm Operation",
            f"Are you sure you want to {operation} files?\n\n"
            f"Source: {self.source_folder.get()}\n"
            f"Operation: {operation.upper()}\n"
            f"Subfolder organization: {self.subfolder_mode.get()}\n"
            f"Separate RAW files: {'Yes' if self.separate_raw.get() else 'No'}\n"
            f"Separate Screenshots: {'Yes' if self.separate_screenshots.get() else 'No'}\n"
            f"Include subfolders: {'Yes' if self.include_subfolders.get() else 'No'}\n"
            f"Find duplicates by content: {'Yes' if self.dedupe_content.get() else 'No'}\n"
            f"Check files for damage: {'Yes' if self.check_corrupt.get() else 'No'}\n"
            f"Delete empty folders: {'Yes' if self.cleanup_empty.get() else 'No'}\n\n"
            f"{'Files will be MOVED from source!' if operation == 'move' else 'Original files will remain untouched.'}"
            f"{cache_note}")

        if not result:
            return
        self._start_worker(dry_run=False, use_cache=True)

    def check_files_operation(self):
        """Run the damage check on its own - reports only, moves nothing."""
        if self.processing:
            return
        if self._validate_source() is None:
            return

        settings = self._snapshot_settings()
        self.processing = True
        self.cancel_requested = False
        self.queue.put(("disable_buttons", None, None))

        def run():
            try:
                self._run_health_check(settings)
            except Exception as e:
                self.log(f"\n❌ Unexpected error: {e}")
            finally:
                self.processing = False
                self.queue.put(("enable_buttons", None, None))
                self.update_progress(0)

        threading.Thread(target=run, daemon=True).start()

    def _run_health_check(self, settings):
        """Worker: check every media file in the source and report the results.

        Deliberately read-only - nothing is moved, renamed or deleted, so this
        is safe to run on a folder before deciding what to do with it.
        """
        source_path = Path(settings.source)
        start_time = datetime.now()
        self.stats = self._empty_stats()

        # This button always does the full check, whatever the checkboxes say
        settings = copy.copy(settings)
        settings.check_corrupt = True

        self.log("=" * 60)
        self.log("Kjegla's Photo Organizer - FILE HEALTH CHECK"
                 f"{' (thorough)' if settings.corrupt_thorough else ''}")
        self.log(f"Source: {source_path}")
        self.log("Nothing will be moved, renamed or deleted.")
        self.log("=" * 60)

        try:
            regular, raw = collect_media_files(source_path,
                                               settings.include_subfolders,
                                               sniff_unknown=True)
        except OSError as e:
            self.log(f"❌ Could not read source folder: {e}")
            self.update_status("Error reading source folder")
            return

        media_files = regular + raw
        self.stats['total_files'] = len(media_files)
        if not media_files:
            self.log("No media files found in the source folder!")
            self.update_status("No media files found")
            return

        self.log(f"\n🩺 Checking {len(media_files)} file(s)...")
        health = self._check_health(media_files, settings, p0=0, p1=100)

        if self.cancel_requested:
            self.log("\n⏹️ Check cancelled by user")
            self.update_status("Cancelled")
            return

        by_name = lambda p: str(p).lower()
        damaged = sorted((f for f, (s, _) in health.items() if s == 'damaged'),
                         key=by_name)
        misnamed = sorted((f for f, (s, _) in health.items() if s == 'misnamed'),
                          key=by_name)
        unchecked = [f for f, (s, _) in health.items() if s == 'unchecked']
        healthy = len(health) - len(damaged) - len(misnamed) - len(unchecked)
        self.stats['damaged'] = len(damaged)
        self.stats['misnamed'] = len(misnamed)
        self.stats['unchecked'] = len(unchecked)
        self.stats['processed'] = len(health)

        run_stamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        report_name = f"kjegla_health_{run_stamp}.txt"
        try:
            with open(source_path / report_name, 'w', encoding='utf-8') as f:
                f.write(f"Kjegla's Photo Organizer - file health check "
                        f"{datetime.now()}\n")
                f.write(f"Source: {source_path}\n")
                f.write(f"Mode: {'thorough' if settings.corrupt_thorough else 'quick'}\n")
                f.write("=" * 60 + "\n\n")
                f.write(f"Fine: {healthy}\n")
                f.write(f"Damaged: {len(damaged)}\n")
                f.write(f"Wrong file extension: {len(misnamed)}\n")
                f.write(f"Could not be checked: {len(unchecked)}\n\n")
                if damaged:
                    f.write("DAMAGED FILES\n")
                    for path in damaged:
                        f.write(f"  {path.relative_to(source_path)} - "
                                f"{health[path][1]}\n")
                    f.write("\n")
                if misnamed:
                    f.write("WRONG FILE EXTENSION (the contents are fine - "
                            "these are NOT damaged, renaming fixes them)\n")
                    for path in misnamed:
                        f.write(f"  {path.relative_to(source_path)} - "
                                f"{health[path][1]}\n")
                    f.write("\n")
                if unchecked:
                    f.write("COULD NOT BE CHECKED (format we cannot verify - "
                            "this does NOT mean they are broken)\n")
                    for path in sorted(unchecked, key=lambda p: str(p).lower()):
                        f.write(f"  {path.relative_to(source_path)}\n")
        except OSError as e:
            self.log(f"⚠️ Could not write the report file: {e}")

        self.log("\n" + "=" * 60)
        self.log("RESULTS:")
        self.log(f"✅ Fine: {healthy}")
        self.log(f"🩹 Damaged: {len(damaged)}")
        self.log(f"🏷️ Wrong file extension: {len(misnamed)} "
                 f"(contents are fine - renaming fixes these)")
        self.log(f"❓ Could not be checked: {len(unchecked)} "
                 f"(RAW / some video formats - not a sign they are broken)")

        if damaged:
            self.log("\nDamaged files:")
            for path in damaged[:200]:
                self.log(f"  🩹 {path.relative_to(source_path)} - "
                         f"{health[path][1]}")
            if len(damaged) > 200:
                self.log(f"  ... and {len(damaged) - 200} more "
                         f"(the full list is in the report file)")
        else:
            self.log("\n🎉 No damaged files found.")

        if misnamed:
            self.log("\nWrong file extension (these are NOT damaged - the "
                     "contents are fine, the name just lies about the format):")
            for path in misnamed[:100]:
                self.log(f"  🏷️ {path.relative_to(source_path)} - "
                         f"{health[path][1]}")
            if len(misnamed) > 100:
                self.log(f"  ... and {len(misnamed) - 100} more "
                         f"(the full list is in the report file)")

        if damaged or misnamed:
            self.log("\n💡 Tick 'Check files for damage while organizing' to have "
                     f"damaged files moved into a '{CORRUPT_FOLDER}' folder and "
                     f"misnamed ones into '{WRONG_EXT_FOLDER}' on the next run.")

        duration = (datetime.now() - start_time).total_seconds()
        self.stats['duration_seconds'] = duration
        self.log(f"\nDuration: {duration:.1f} seconds")
        self.log(f"📄 Report saved: {report_name}")
        self.update_status(f"Check complete - {len(damaged)} damaged, "
                           f"{healthy} fine")

    def undo_renames(self):
        """Undo only the extension fixes, leaving the rest of the run alone."""
        if self.processing:
            return
        undo_file = self.last_rename_undo_file
        if not undo_file or not Path(undo_file).exists():
            messagebox.showinfo("Undo Renames", "No renames to undo.")
            self.undo_renames_btn.config(state=tk.DISABLED)
            return

        try:
            with open(undo_file, 'r', encoding='utf-8') as f:
                record = json.load(f)
        except (OSError, json.JSONDecodeError) as e:
            messagebox.showerror("Undo Renames",
                                 f"Could not read the rename record:\n{e}")
            return

        entries = record.get('entries', [])
        if not entries:
            messagebox.showinfo("Undo Renames", "The rename record is empty.")
            return

        if not messagebox.askyesno(
                "Undo Renames",
                f"Put {len(entries)} renamed file(s) back where they were, "
                f"under their original names?\n\n"
                f"Everything else this run did stays as it is."):
            return

        self.processing = True
        self.cancel_requested = False
        self.queue.put(("disable_buttons", None, None))

        def run():
            try:
                self._run_undo(Path(undo_file), record, label="renames")
                self.last_rename_undo_file = None
            except Exception as e:
                self.log(f"\n❌ Undo error: {e}")
            finally:
                self.processing = False
                self.queue.put(("enable_buttons", None, None))
                self.update_progress(0)

        threading.Thread(target=run, daemon=True).start()

    def undo_last_operation(self):
        """Undo the most recent execute run (main thread entry point)."""
        if self.processing:
            return
        undo_file = self.last_undo_file
        if not undo_file or not Path(undo_file).exists():
            messagebox.showinfo("Undo", "No undoable operation found.")
            self.undo_btn.config(state=tk.DISABLED)
            return

        try:
            with open(undo_file, 'r', encoding='utf-8') as f:
                record = json.load(f)
        except (OSError, json.JSONDecodeError) as e:
            messagebox.showerror("Undo", f"Could not read undo record:\n{e}")
            return

        entries = record.get('entries', [])
        op = record.get('operation', 'move')
        if not entries:
            messagebox.showinfo("Undo", "The undo record is empty.")
            return

        if op == "move":
            msg = (f"Undo will move {len(entries)} files back to their "
                   f"original locations.\n\nContinue?")
        else:
            msg = (f"Undo will DELETE the {len(entries)} copies created by the "
                   f"last copy run.\nYour original files are untouched.\n\nContinue?")
        if not messagebox.askyesno("Undo Last Run", msg):
            return

        self.processing = True
        self.cancel_requested = False
        self.queue.put(("disable_buttons", None, None))

        def run():
            try:
                self._run_undo(Path(undo_file), record)
            except Exception as e:
                self.log(f"\n❌ Undo error: {e}")
            finally:
                self.processing = False
                self.queue.put(("enable_buttons", None, None))
                self.update_progress(0)

        threading.Thread(target=run, daemon=True).start()

    def _run_undo(self, undo_file, record, label=None):
        """Worker: revert every operation in the undo record."""
        entries = record.get('entries', [])
        op = record.get('operation', 'move')
        source_root = Path(record.get('source', str(undo_file.parent)))
        total = len(entries)
        restored = 0
        problems = 0

        self.log("\n" + "=" * 60)
        self.log(f"↩️ UNDOING {label or ('last ' + op + ' run')} ({total} files)")
        self.log("=" * 60)

        for idx, (target, original) in enumerate(entries):
            if self.cancel_requested:
                self.log("\n⏹️ Undo cancelled by user")
                break
            target_p = Path(target)
            try:
                if op == "move":
                    if not target_p.exists():
                        self.log(f"  ⚠️ Missing, cannot restore: {target}")
                        problems += 1
                    elif Path(original).exists():
                        self.log(f"  ⚠️ Original location occupied, skipping: {original}")
                        problems += 1
                    else:
                        Path(original).parent.mkdir(parents=True, exist_ok=True)
                        shutil.move(str(target_p), original)
                        restored += 1
                else:  # copy run: delete the created copies
                    if target_p.exists():
                        os.remove(str(target_p))
                        restored += 1
            except Exception as e:
                self.log(f"  ❌ {target}: {e}")
                problems += 1

            if (idx + 1) % 20 == 0 or idx + 1 == total:
                self.update_progress(int((idx + 1) / total * 100))
                self.update_status(f"Undoing {idx + 1}/{total}")

        removed = self._sweep_empty_dirs(source_root)

        # Mark the record consumed so it can't be replayed
        try:
            undo_file.rename(undo_file.with_name(undo_file.name + ".undone"))
        except OSError:
            pass
        if label != "renames":
            self.last_undo_file = None
        self.cached_plan = None  # the folder just changed

        verb = "restored" if op == "move" else "removed"
        self.log(f"\n✅ Undo complete: {restored} files {verb}, "
                 f"{problems} issue(s), {removed} empty folder(s) cleaned up")
        self.update_status(f"Undo complete - {restored} files {verb}")

    def cancel_operation(self):
        """Cancel the current operation."""
        self.cancel_requested = True
        self.update_status("Cancelling...")


def main():
    root = tk.Tk()
    if SV_TTK_AVAILABLE:
        sv_ttk.set_theme("dark")
    app = PhotoOrganizerGUI(root)
    root.mainloop()


if __name__ == "__main__":
    main()

"""
To create an executable:

1. Install required packages:
   pip install Pillow pillow-heif sv-ttk pyinstaller

2. Create the executable:
   python -m PyInstaller --onefile --windowed --noconfirm --collect-all sv_ttk --name "PhotoOrganizerV35" photo_organizer_v35.py

3. Optional: Add VLC cone icon (download vlc_cone.ico):
   add --icon=vlc_cone.ico to the command above

The executable will be in the 'dist' folder.
"""
