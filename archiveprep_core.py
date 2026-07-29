#!/usr/bin/env python3
"""Everything the organizer does, with no user interface attached.

This is the whole application except the window: reading metadata, judging
whether a file is intact, finding duplicates by content, deciding where each
file belongs, and carrying out (or reversing) the moves.

It knows about the outside world through exactly one object - Progress, below
- which it uses to report what it is doing and to notice that it has been
asked to stop. Nothing here imports tkinter, so all of it can be tested, and
run, without a screen.
"""
import os
import re
import copy
import csv
import json
import time
import shutil
import hashlib
import threading
from pathlib import Path
from datetime import datetime
from types import SimpleNamespace
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass

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


class Progress:
    """Where the core sends its running commentary, and how it is told to stop.

    The window hands one of these in carrying its own queue; the tests use the
    default, which discards everything and is never cancelled. Keeping it to
    one object is the point - it is the only thing the core knows about the
    outside world, so there is exactly one seam to understand.
    """

    def __init__(self, queue=None):
        self.queue = queue
        self.cancel = threading.Event()

    def log(self, message):
        if self.queue is not None:
            self.queue.put(("log", message, None))

    def status(self, text):
        if self.queue is not None:
            self.queue.put(("status", text, None))

    def percent(self, value):
        if self.queue is not None:
            self.queue.put(("progress", value, None))

    def notify(self, kind, value):
        """Tell the window something happened that changes what it can offer -
        an undo record appearing, for instance."""
        if self.queue is not None:
            self.queue.put((kind, value, None))

    @property
    def cancelled(self):
        return self.cancel.is_set()


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


def _same_volume(a, b):
    """True if two paths sit on the same filesystem, so a move is a rename."""
    try:
        return a.stat().st_dev == b.stat().st_dev
    except OSError:
        return False  # unknown - take the careful path


def transfer_file(src, dst, operation):
    """Move or copy a file, proving the bytes actually arrived.

    A move within one volume is a rename: instant, atomic, and there is
    nothing to check. Anything else physically copies the data - and a move
    would then delete the original - so the copy is checked *before* that
    happens. A short write that never raised an error (a full disk, a
    network drive that dropped out) would otherwise quietly destroy the only
    copy of a photo. One extra stat is a cheap price for closing that.

    Raises OSError if the destination did not receive every byte.
    """
    size = src.stat().st_size
    if operation == "move" and _same_volume(src, dst.parent):
        shutil.move(str(src), str(dst))
        return

    shutil.copy2(str(src), str(dst))
    arrived = dst.stat().st_size
    if arrived != size:
        raise OSError(f"only {arrived} of {size} bytes arrived - the "
                      f"destination may be full or disconnected")
    if operation == "move":
        os.remove(str(src))


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


def probe_file(file_path):
    """What a file actually is, read from its first bytes.

    Returns (real_format, valid_extensions, canonical_extension), or three
    Nones when the format is not one we can name confidently - in which case
    we say nothing rather than guess.

    This is the one place that asks the question. Everything that needs to
    know what a file really is goes through here.
    """
    try:
        with open(file_path, 'rb') as f:
            return sniff_real_format(f.read(16))
    except OSError:
        return None, None, None


# Which kind of program opens a file. A name that lies within a kind is a
# nuisance; a name that lies across kinds stops the file opening at all.
def _media_class(ext):
    ext = (ext or '').lower()
    if ext in IMAGE_EXTS or ext in RAW_EXTS:
        return 'image'
    if ext in VIDEO_EXTS:
        return 'video'
    return 'unknown'


def extension_verdict(file_path, real_format, valid_exts, canonical):
    """How badly, if at all, a file's name disagrees with its contents.

    'ok'       - the name is right, or we cannot say what the file is
    'harmless' - the name is wrong but points at the same kind of media, so
                 viewers open it anyway. A WEBP saved as .png displays fine
                 everywhere; nothing is broken and nothing needs doing.
    'breaking' - the name sends the file to entirely the wrong program. A
                 photo called .MOV is handed to a video player and simply
                 fails to open, and a Google Takeout file whose extension was
                 truncated away is invisible to everything, including this
                 application until it sniffs the contents.

    STATUS.md #3 asked whether these two deserve the same treatment. They
    plainly do not, and now the difference is recorded and reported. What is
    *done* about it has deliberately not changed yet: both still go to
    Wrong Extension/, so nothing silently starts behaving differently on an
    archive that has already been organized once.
    """
    suffix = file_path.suffix.lower()
    if not real_format or suffix in valid_exts:
        return 'ok'
    if _media_class(suffix) == _media_class(canonical):
        return 'harmless'
    return 'breaking'


def file_health(file_path, thorough=False, format_only=False, probe=None):
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
    # A caller that has already read the header can pass it in rather than
    # make us read it again.
    real_format, valid_exts, canonical = probe or probe_file(file_path)
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
    return probe_file(file_path)[0] is not None


def collect_media_files(source_path, include_subfolders):
    """List media files in the source. Shared by the organizer and the
    preview-cache validation so both always see the same file set.

    Files with an unfamiliar extension are always identified by reading their
    first bytes. That used to be tied to the "fix wrong extensions" option,
    which meant turning that option off did not merely leave such files alone
    - it hid them completely, so they were never sorted, never checked for
    damage and never deduplicated (STATUS.md #4). Finding a file and deciding
    what to do about it are different questions, and only the second one is
    the user's to answer. Sniffing costs 16 bytes.

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
    # Never treat our own output as media. Both prefixes: a folder
    # organized before the rename still holds archiveprep's older
    # 'kjegla_' logs and reports.
    all_files = [f for f in all_files
                 if not f.name.startswith(('kjegla_', 'archiveprep_'))]

    raw_files = [f for f in all_files if f.suffix.lower() in RAW_EXTS]
    regular = [f for f in all_files
               if f.suffix.lower() in (IMAGE_EXTS | VIDEO_EXTS)]

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


@dataclass(slots=True)
class MediaFile:
    """One file, and everything a run has learned about it.

    This replaces the parallel dictionaries the phases used to hand each other
    - metadata, health, duplicate_of, sizes - all keyed by Path, each with its
    own idea of what a missing key meant. Two lookups of the same dictionary
    in the same function once used different fallbacks; with one record per
    file that class of mistake cannot be made.

    Deliberately a record and not an object: no methods, no behaviour. The
    phases are functions that read it and fill it in.
    """
    path: Path
    size: int = 0

    # what it is
    kind: str = 'image'                    # image | video | raw
    camera_model: str = None
    captured_at: datetime = None
    date_source: str = 'none'              # exif | video | mtime | none
    is_screenshot: bool = False

    # can it be trusted - exactly what file_health() said
    verdict: str = 'unchecked'             # ok | damaged | misnamed | unchecked
    verdict_reason: str = ''
    # Invariant: verdict == 'misnamed' exactly when extension != 'ok'. They
    # are the same fact at two levels of detail - verdict is what routes the
    # file, extension is how much the wrong name actually costs the user.
    # Both come off one probe in _inspect(); if you change the condition in
    # one, change it in the other.
    extension: str = 'ok'                  # ok | harmless | breaking
    canonical_ext: str = None              # what the name should have been

    # what the run decided
    duplicate_of: Path = None
    content_hash: bytes = None
    capture_id: str = ''                   # files from one shutter press
    action: str = ''                       # organize | duplicate | corrupt |
                                           # wrong_extension | skipped
    target: Path = None
    reason: str = ''


@dataclass(slots=True)
class Operation:
    """One file movement the plan intends - the unit APPLY works in.

    Operations stay flat and independent on purpose: a failure moves one file
    and poisons nothing else, and undo stays a plain list of reversals.
    """
    source: Path
    target: Path
    kind: str                              # organize | duplicate | corrupt |
                                           # wrong_extension
    # Carried per operation rather than read from settings, so APPLY needs no
    # settings at all - it does exactly what the plan says and nothing else.
    operation: str = 'move'                # move | copy


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


def get_target_folder(source_path, file_path, camera_model, photo_date,
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


def _kind_of(path):
    """Whether a file is a photo or a video.

    The extension answers this for almost everything. When it does not -
    Google Takeout truncates long filenames and chops the extension clean
    off - the bytes are asked instead, which is how the file came to be
    noticed at all. Deciding this from the name alone would leave a stripped
    motion-photo clip classified as a photo.
    """
    suffix = path.suffix.lower()
    if suffix in VIDEO_EXTS:
        return 'video'
    if suffix in IMAGE_EXTS:
        return 'image'
    canonical = probe_file(path)[2]
    return 'video' if canonical in VIDEO_EXTS else 'image'


def index_sidecars(paths):
    """Map each directory to the JSON sidecars in it, listing each one once.

    Takeout truncates long filenames - sometimes part-way through
    ".supplemental-metadata" - so a sidecar cannot be found by guessing an
    exact name. One listing per directory is both reliable and far cheaper
    than probing candidate names per file.
    """
    index = {}
    for directory in {p.parent for p in paths}:
        try:
            index[directory] = [n for n in os.listdir(directory)
                                if n.lower().endswith('.json')]
        except OSError:
            index[directory] = []
    return index


def find_sidecar(media_path, sidecar_index):
    """The JSON sidecar belonging to a media file, or None."""
    names = sidecar_index.get(media_path.parent, ())
    prefix = media_path.name.lower() + '.'
    for name in names:
        if name.lower().startswith(prefix):
            return media_path.parent / name       # IMG_1.jpg.<anything>.json
    stem = media_path.stem.lower() + '.json'
    for name in names:
        if name.lower() == stem:
            return media_path.parent / name       # IMG_1.json beside IMG_1.jpg
    return None


def read_sidecar_date(sidecar_path):
    """When Google Photos says the photo was taken.

    Takeout strips or omits EXIF inconsistently, and for the files it strips,
    this JSON is the only surviving record of when the photo was taken. With
    no EXIF and no sidecar the organizer falls back to the file's modified
    time - which on a Takeout export is when it was *extracted*, so those
    photos get filed under the year of the download rather than the year they
    happened. On a large export that is thousands of files in the wrong place.

    photoTakenTime is preferred over creationTime, which is the upload time
    and can be years later. Returns a datetime, or None. Never raises.
    """
    try:
        with open(sidecar_path, 'r', encoding='utf-8') as f:
            data = json.load(f)
    except (OSError, ValueError, UnicodeDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    for key in ('photoTakenTime', 'creationTime'):
        entry = data.get(key)
        stamp = entry.get('timestamp') if isinstance(entry, dict) else None
        if not stamp:
            continue
        try:
            dt = datetime.fromtimestamp(int(stamp))
        except (ValueError, OSError, OverflowError):
            continue
        # Same sanity window the video-header reader uses
        if 1990 <= dt.year <= datetime.now().year + 1:
            return dt
    return None


def _scan_files(paths, settings, progress, p0=0, p1=25):
    """Read every file once and return {path: MediaFile}.

    Metadata, the camera model and the screenshot verdict all come from the
    same read, so they are all decided here. RAW files get no model of their
    own - they borrow one from the image they were taken with, which cannot
    be worked out until every image has been seen.
    """
    records = {}
    total = len(paths)
    if total == 0:
        return records

    # Listed once up front so the scan threads only ever read it
    sidecars = index_sidecars(paths)

    def note_progress(done):
        progress.percent(p0 + int(done / total * (p1 - p0)))
        progress.status(f"Reading metadata {done}/{total}")

    def scan(path):
        mf = MediaFile(path=path)
        try:
            mf.size = path.stat().st_size
        except OSError:
            pass
        if is_raw_file(path):
            mf.kind = 'raw'        # RAW metadata is never read directly
        else:
            mf.kind = _kind_of(path)
            meta = read_media_metadata(path)
            if mf.kind == 'image':
                mf.camera_model = model_for_image(path, meta)
                mf.is_screenshot = looks_like_screenshot(path, meta)
            if meta.get('date'):
                mf.captured_at = meta['date']
                mf.date_source = 'video' if mf.kind == 'video' else 'exif'

        # The file's own metadata is always believed first; a sidecar only
        # answers when the file itself cannot.
        if mf.captured_at is None:
            sidecar = find_sidecar(path, sidecars)
            if sidecar is not None:
                taken = read_sidecar_date(sidecar)
                if taken is not None:
                    mf.captured_at = taken
                    mf.date_source = 'sidecar'
        return mf

    if settings.use_multithreading and total > 1:
        with ThreadPoolExecutor(max_workers=settings.max_threads) as executor:
            futures = {executor.submit(scan, p): p for p in paths}
            done = 0
            for future in as_completed(futures):
                if progress.cancelled:
                    executor.shutdown(wait=False, cancel_futures=True)
                    break
                path = futures[future]
                try:
                    records[path] = future.result()
                except Exception:
                    records[path] = MediaFile(path=path)
                done += 1
                if done % 25 == 0 or done == total:
                    note_progress(done)
    else:
        for done, path in enumerate(paths, start=1):
            if progress.cancelled:
                break
            records[path] = scan(path)
            if done % 25 == 0 or done == total:
                note_progress(done)

    return records


def _inspect(path, thorough, format_only):
    """Everything one read of a file's header can tell us, in one go.

    Is it intact, what is it really, and how far is its name from the truth -
    three questions off the same 16 bytes. They used to be asked separately,
    which meant reading the header twice per file, and the second read
    happened back on the main thread inside a phase whose whole purpose is
    to do this work in parallel.

    Returns (verdict, reason, canonical_extension, extension_verdict).
    """
    probe = probe_file(path)
    verdict, reason = file_health(path, thorough, format_only, probe=probe)
    return verdict, reason, probe[2], extension_verdict(path, *probe)


def _check_health(records, settings, progress, p0=25, p1=40):
    """Fill in each record's verdict. Parallel when enabled - it is I/O bound,
    so extra threads help even in quick mode."""
    total = len(records)
    if total == 0:
        return
    thorough = settings.corrupt_thorough
    # When only the extension option is on there's no need to pay for the
    # full damage check - stop after identifying what each file really is.
    format_only = not settings.check_corrupt
    label = ("Checking file names" if format_only else
             "Thorough check" if thorough else "Checking files")
    last_note = [0.0]
    done = [0]

    def note():
        done[0] += 1
        now = time.monotonic()
        if now - last_note[0] >= 0.2 or done[0] == total:
            progress.percent(p0 + int(done[0] / total * (p1 - p0)))
            progress.status(f"{label} {done[0]}/{total}")
            last_note[0] = now

    def store(mf, result):
        (mf.verdict, mf.verdict_reason, mf.canonical_ext,
         mf.extension) = result

    values = list(records.values())
    if settings.use_multithreading and total > 1:
        with ThreadPoolExecutor(max_workers=settings.max_threads) as executor:
            futures = {executor.submit(_inspect, mf.path, thorough,
                                       format_only): mf for mf in values}
            for future in as_completed(futures):
                if progress.cancelled:
                    executor.shutdown(wait=False, cancel_futures=True)
                    break
                mf = futures[future]
                try:
                    store(mf, future.result())
                except Exception as e:
                    mf.verdict, mf.verdict_reason = 'damaged', f"check failed ({e})"
                note()
    else:
        for mf in values:
            if progress.cancelled:
                break
            store(mf, _inspect(mf.path, thorough, format_only))
            note()


def _hash_many(paths, hasher, settings, progress, note):
    """Hash a batch of files (parallel when enabled). Returns {Path: digest}.

    Files that cannot be read are simply left out - they are handled by
    the damage check, not here.
    """
    results = {}
    if not paths:
        return results
    if settings.use_multithreading and len(paths) > 1:
        with ThreadPoolExecutor(max_workers=settings.max_threads) as executor:
            futures = {executor.submit(hasher, p): p for p in paths}
            done = 0
            for future in as_completed(futures):
                if progress.cancelled:
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
            if progress.cancelled:
                break
            try:
                results[path] = hasher(path)
            except OSError:
                pass
            note(done)
    return results


def _keeper_rank(mf):
    """Sort key for choosing which copy of an identical set to keep.

    Lowest wins. Health comes first on purpose: a damaged copy can never be
    kept over a healthy one.
    """
    health_rank = {'ok': 0, 'misnamed': 1, 'unchecked': 1,
                   'damaged': 2}.get(mf.verdict, 1)
    richness = (0 if mf.camera_model else 1) + (0 if mf.captured_at else 1)
    looks_copied = 1 if looks_like_copy_name(mf.path.stem) else 0
    try:
        mtime = mf.path.stat().st_mtime
    except OSError:
        mtime = float('inf')
    # Final entries are pure tie-breakers so repeated runs agree
    return (health_rank, richness, looks_copied, len(mf.path.stem), mtime,
            str(mf.path).lower())


def capture_key(path):
    """The capture a file belongs to: its folder, plus the photo name it
    shares with its variants.

    Deliberately scoped to one folder. Two folders can easily both hold an
    IMG_1234.jpg from different years, and treating those as one capture
    would hand a photo the wrong date - a false positive of exactly the kind
    this application exists to avoid. A capture split across folders is
    simply not found, which is the safe way to be wrong.
    """
    return f"{path.parent}|{media_base_name(path)}"


def _group_captures(records):
    """Group the files that came from one press of the shutter.

    A RAW and the JPEG it was taken with, a photo and its motion clip, the
    frames of a burst - Google and the camera makers all express this by
    giving the files a shared photo name and tacking a tag on the end, which
    media_base_name() already knows how to strip.
    """
    captures = {}
    for mf in records.values():
        mf.capture_id = capture_key(mf.path)
        captures.setdefault(mf.capture_id, []).append(mf)
    return captures


# How much a date is worth trusting, best first. A file's own EXIF beats what
# Google recorded about it, which beats a video container's header.
_DATE_TRUST = {'exif': 0, 'sidecar': 1, 'video': 2}


def _share_capture_dates(captures, progress):
    """Give every member of a capture the date of its best-evidenced member.

    A RAW carries no date this application will read, so it used to fall back
    to the file's modified time and land in a different year folder from the
    JPEG it was taken with - the test suite asserted that split as correct
    behaviour. One shutter press belongs in one place.

    Nothing is invented. A file that has its own date keeps it, and a capture
    where nothing has a date stays without one and falls back to the file time
    exactly as before. This also settles STATUS.md #2: a RAW with no surviving
    JPEG has no capture siblings, so it inherits nothing and stays honestly
    unidentified rather than being assumed into the folder of the others.
    """
    shared = 0
    for members in captures.values():
        if len(members) < 2:
            continue
        dated = [m for m in members
                 if m.captured_at is not None and m.date_source in _DATE_TRUST]
        if not dated:
            continue
        primary = min(dated, key=lambda m: (_DATE_TRUST[m.date_source],
                                            str(m.path).lower()))
        for mf in members:
            if mf.captured_at is None:
                mf.captured_at = primary.captured_at
                mf.date_source = 'capture'
                shared += 1
    if shared:
        progress.log(f"  {shared} file(s) took their date from the photo "
                     f"they were captured with")
    return shared


def _hash_tail(path, length):
    """Hash the last `length` bytes of a file."""
    h = hashlib.blake2b()
    with open(path, 'rb') as f:
        f.seek(-length, os.SEEK_END)
        remaining = length
        while remaining > 0:
            chunk = f.read(min(1 << 20, remaining))
            if not chunk:
                break
            h.update(chunk)
            remaining -= len(chunk)
    return h.digest()


def _find_embedded_clips(captures, progress):
    """Find video files that are already sitting inside a photo.

    A motion photo is one file: a complete JPEG with a short MP4 welded onto
    the back. Google Takeout also writes that MP4 out a second time as its own
    file, and truncation usually strips its extension - so the same bytes are
    stored twice. In a real 3,322-file export, 14 files were exactly this and
    nothing else was (GOOGLE_TAKEOUT_NOTES.md).

    Matched by content, never by name: the clip qualifies only when its bytes
    sit, byte for byte, at the very end of a photo it was captured with.
    Takeout's naming has changed before, Samsung does the same thing under
    different names, and the bytes are the only thing that cannot be wrong. A
    real video cannot qualify by accident - it would have to already be inside
    one of your photos, which only happens when it genuinely is a spare copy.

    Costs nothing on an archive that has none: a clip is only compared against
    photos from its own capture that are larger than it is.
    """
    groups = []
    for members in captures.values():
        if len(members) < 2:
            continue
        clips = [m for m in members
                 if m.kind == 'video' and m.duplicate_of is None and m.size > 0]
        if not clips:
            continue
        hosts = [m for m in members if m.kind == 'image']
        for clip in clips:
            for host in hosts:
                if host.size <= clip.size or host.duplicate_of is not None:
                    continue
                try:
                    if _hash_tail(host.path, clip.size) != _hash_file(clip.path):
                        continue
                except OSError:
                    continue
                clip.duplicate_of = host.path
                clip.reason = (f"already stored inside "
                               f"{host.path.name} - the motion plays from "
                               f"inside the photo, not from this copy")
                groups.append((host, [clip]))
                break
    if groups:
        progress.log(f"  {len(groups)} clip(s) are already stored inside the "
                     f"photo they belong to - the separate copy is redundant")
    return groups


def _find_duplicates(records, settings, progress, p0=40, p1=60):
    """Mark every file that is byte-for-byte identical to another.

    Three stages, each only touching what the previous one could not rule out:
      1. group by size - a file with a unique size cannot have a twin and is
         never read at all
      2. hash the first 64 KB of same-size files
      3. full content hash, only where the head hashes also matched
    (Files at or under 64 KB are settled by stage 2 - the head hash already
    covered the whole file.)

    Sets mf.duplicate_of on every copy that will be set aside, and returns
    [(keeper, [set-aside...])] for the report.
    """
    by_size = {}
    for mf in records.values():
        if mf.size == 0:
            continue  # empty files are a damage problem, not a duplicate one
        by_size.setdefault(mf.size, []).append(mf)

    candidates = [mf for group in by_size.values() if len(group) > 1
                  for mf in group]
    if not candidates:
        progress.percent(p1)
        return []

    progress.log(f"\nComparing {len(candidates)} files that share a size "
                 f"with at least one other ({len(records) - len(candidates)} "
                 f"ruled out without reading them)")

    # Stage 2: head hashes, using the first 60% of this phase's progress
    mid = p0 + int((p1 - p0) * 0.6)
    head_total = len(candidates)
    last_note = [0.0]

    def note_head(done):
        now = time.monotonic()
        if now - last_note[0] >= 0.2 or done == head_total:
            progress.percent(p0 + int(done / head_total * (mid - p0)))
            progress.status(f"Comparing files {done}/{head_total}")
            last_note[0] = now

    by_path = {mf.path: mf for mf in candidates}
    head_hashes = _hash_many([mf.path for mf in candidates], _hash_head,
                             settings, progress, note_head)

    by_head = {}
    for path, digest in head_hashes.items():
        mf = by_path[path]
        by_head.setdefault((mf.size, digest), []).append(mf)

    final_groups = []
    need_full = []
    for (size, digest), group in by_head.items():
        if len(group) < 2:
            continue
        if size <= HEAD_HASH_BYTES:
            for mf in group:
                mf.content_hash = digest   # the head hash was the whole file
            final_groups.append(group)
        else:
            need_full.append(group)

    # Stage 3: full content hashes
    full_candidates = [mf for group in need_full for mf in group]
    full_total = len(full_candidates)
    last_note[0] = 0.0

    def note_full(done):
        now = time.monotonic()
        if now - last_note[0] >= 0.2 or done == full_total:
            progress.percent(mid + int(done / full_total * (p1 - mid)))
            progress.status(f"Verifying possible duplicates {done}/{full_total}")
            last_note[0] = now

    full_hashes = _hash_many([mf.path for mf in full_candidates], _hash_file,
                             settings, progress, note_full)
    by_full = {}
    for path, digest in full_hashes.items():
        mf = by_path[path]
        mf.content_hash = digest
        by_full.setdefault((mf.size, digest), []).append(mf)
    final_groups.extend(g for g in by_full.values() if len(g) > 1)

    # Pick a keeper per group; everything else gets set aside
    groups = []
    for group in final_groups:
        ordered = sorted(group, key=_keeper_rank)
        keeper, rest = ordered[0], ordered[1:]
        for dup in rest:
            dup.duplicate_of = keeper.path
        groups.append((keeper, rest))

    progress.percent(p1)
    return groups


def _write_duplicate_report(report_path, groups, source_path):
    """Write a plain listing of every duplicate group and its keeper."""
    try:
        with open(report_path, 'w', encoding='utf-8') as f:
            f.write(f"ArchivePrep - duplicate report "
                    f"{datetime.now()}\n")
            f.write(f"Source: {source_path}\n")
            f.write("Files are duplicates only when their contents match "
                    "exactly, byte for byte.\n")
            f.write("=" * 60 + "\n\n")
            for keeper, rest in groups:
                size_mb = keeper.size / (1024 * 1024)
                f.write(f"KEEP     {keeper.path.relative_to(source_path)} "
                        f"({size_mb:.2f} MB)\n")
                for dup in rest:
                    f.write(f"  DUP    {dup.path.relative_to(source_path)}\n")
                f.write("\n")
    except OSError:
        pass


def write_manifest(manifest_path, records, source_path):
    """One row per file: what it was, what was decided, and where it went.

    This is the run's real deliverable alongside the moved files - the thing
    to consult when merging the organised batch into an existing archive. The
    content hash is whatever the duplicate hunt already paid to compute, so
    it is filled in for files that shared a size with another and blank for
    the rest; nothing is read a second time just to populate a column.
    """
    columns = ['original_path', 'action', 'target_path', 'reason', 'size_bytes',
               'content_hash', 'kind', 'camera_model', 'captured_at',
               'date_source', 'is_screenshot', 'verdict', 'verdict_reason',
               'extension', 'duplicate_of', 'capture_id']
    try:
        with open(manifest_path, 'w', encoding='utf-8', newline='') as f:
            writer = csv.writer(f)
            writer.writerow(columns)
            for mf in sorted(records.values(), key=lambda m: str(m.path).lower()):
                def rel(p):
                    if p is None:
                        return ''
                    try:
                        return str(Path(p).relative_to(source_path))
                    except ValueError:
                        return str(p)
                writer.writerow([
                    rel(mf.path), mf.action, rel(mf.target), mf.reason, mf.size,
                    mf.content_hash.hex() if mf.content_hash else '',
                    mf.kind, mf.camera_model or '',
                    mf.captured_at.isoformat(' ') if mf.captured_at else '',
                    mf.date_source, 'yes' if mf.is_screenshot else 'no',
                    mf.verdict, mf.verdict_reason, mf.extension,
                    rel(mf.duplicate_of), mf.capture_id,
                ])
    except OSError:
        pass


def _build_model_index(records):
    """Map the photo names images were filed under to their camera model, so a
    RAW or a video can borrow the model of the image it was taken with.

    Built from the records in a fixed order rather than in whatever order the
    metadata threads happened to finish, so two runs over the same folder
    always agree - which matters, because the output of a run is compared
    against an existing archive.
    """
    index = {}
    for mf in sorted(records.values(), key=lambda m: str(m.path).lower()):
        if mf.kind == 'image' and mf.camera_model:
            index[mf.path.stem] = mf.camera_model
            # Also filed under the shared photo name, so a RAW called
            # "....RAW-02.ORIGINAL.dng" finds "....RAW-01.MP.jpg".
            # setdefault, so an exact filename match always wins.
            index.setdefault(media_base_name(mf.path), mf.camera_model)
    return index


def _write_run_summary(progress, log_file, stats, duration):
    """The end-of-run tally, to the window and to the log file alike."""
    progress.log("\n" + "=" * 60)
    progress.log("SUMMARY:")
    progress.log(f"Total media files: {stats['total_files']}")
    progress.log(f"Files processed: {stats['processed']}")
    progress.log(f"Files without metadata: {stats['no_metadata']}")
    progress.log(f"Screenshots detected: {stats['screenshots']}")
    if stats['content_duplicates']:
        wasted = stats['duplicate_bytes'] / (1024 * 1024)
        progress.log(f"Duplicate copies set aside: {stats['content_duplicates']} "
                     f"({wasted:.2f} MB) -> '{DUPLICATES_FOLDER}' folder")
    if stats['damaged']:
        progress.log(f"Damaged files found: {stats['damaged']} "
                     f"-> '{CORRUPT_FOLDER}' folder")
    if stats['misnamed']:
        progress.log(f"Wrongly-named files fixed: {stats['misnamed']} "
                     f"-> renamed to the right extension and moved to "
                     f"'{WRONG_EXT_FOLDER}' (their contents were fine)")
    if stats['unchecked']:
        progress.log(f"Files that could not be checked: {stats['unchecked']} "
                     f"(RAW / some video formats)")
    if stats['duplicates']:
        progress.log(f"Identical duplicates skipped: {stats['duplicates']}")
    if stats['already_organized']:
        progress.log(f"Already organized (untouched): {stats['already_organized']}")
    progress.log(f"Errors: {stats['errors']}")
    progress.log(f"Total size: {stats['total_size_mb']:.2f} MB")
    progress.log(f"Duration: {duration:.1f} seconds")

    log_file.write("\n" + "=" * 60 + "\n")
    log_file.write("SUMMARY:\n")
    log_file.write(f"Total files: {stats['total_files']}\n")
    log_file.write(f"Processed: {stats['processed']}\n")
    log_file.write(f"No metadata/unknown: {stats['no_metadata']}\n")
    log_file.write(f"Screenshots detected: {stats['screenshots']}\n")
    log_file.write(f"Identical duplicates skipped: {stats['duplicates']}\n")
    log_file.write(f"Duplicate copies set aside: {stats['content_duplicates']}\n")
    log_file.write(f"Damaged files: {stats['damaged']}\n")
    log_file.write(f"Wrong file extension: {stats['misnamed']}\n")
    log_file.write(f"Could not be checked: {stats['unchecked']}\n")
    log_file.write(f"Empty folders removed: {stats['empty_folders_removed']}\n")
    log_file.write(f"Already organized: {stats['already_organized']}\n")
    log_file.write(f"Errors: {stats['errors']}\n")
    log_file.write(f"Total size: {stats['total_size_mb']:.2f} MB\n")
    log_file.write(f"Duration: {duration:.1f} seconds\n")

    if stats['by_model']:
        progress.log("\nFiles per camera model:")
        log_file.write("\nFiles per camera model:\n")
        for model, count in sorted(stats['by_model'].items()):
            progress.log(f"  {model}: {count} files")
            log_file.write(f"  {model}: {count} files\n")

    if stats['no_metadata'] > 0:
        progress.log(f"\nUnknown/unmatched files: {stats['no_metadata']} "
                     f"(moved to 'Unknown Camera' folder)")
    if stats['screenshots'] > 0:
        progress.log(f"\nScreenshots separated: {stats['screenshots']} files")


def organize_photos(settings, progress, dry_run=True):
    """SCAN what is there, DECIDE what should happen, then APPLY it.

    A preview stops after DECIDE; a real run carries on into APPLY. Both go
    down the same path to get there, so what a preview shows is what an
    execute does - the two used to be separate implementations and had
    already drifted apart.

    Returns (stats, cached_plan). The plan is only kept for a preview, so
    Execute can replay it without scanning the folder again.
    """
    start_time = datetime.now()
    source_path = Path(settings.source)
    stats = _empty_stats()
    cached_plan = None

    operation = settings.operation
    operation_text = "Moving" if operation == "move" else "Copying"

    progress.log("=" * 60)
    progress.log(f"ArchivePrep - "
                 f"{'DRY RUN' if dry_run else operation_text.upper()}")
    progress.log(f"Source: {source_path}")
    progress.log(f"Operation: {operation}")
    progress.log(f"Subfolder mode: {settings.subfolder_mode}")
    progress.log(f"Separate RAW files: {'Yes' if settings.separate_raw else 'No'}")
    progress.log(f"Separate Screenshots: "
                 f"{'Yes' if settings.separate_screenshots else 'No'}")
    progress.log(f"Include subfolders: "
                 f"{'Yes' if settings.include_subfolders else 'No'}")
    progress.log(f"Multithreading: {'Yes' if settings.use_multithreading else 'No'}")
    progress.log(f"Find duplicates by content: "
                 f"{'Yes' if settings.dedupe_content else 'No'}")
    progress.log(f"Check files for damage: "
                 f"{('Yes (thorough)' if settings.corrupt_thorough else 'Yes') if settings.check_corrupt else 'No'}")
    progress.log(f"Delete empty folders: {'Yes' if settings.cleanup_empty else 'No'}")
    progress.log("=" * 60)

    # ---- SCAN ------------------------------------------------------------
    try:
        regular_media_files, raw_files = collect_media_files(
            source_path, settings.include_subfolders)
    except OSError as e:
        progress.log(f"[error] could not read source folder: {e}")
        progress.status("Error reading source folder")
        return stats, cached_plan

    media_files = regular_media_files + raw_files

    # Snapshot the folder state now; a dry run stores this so Execute can
    # later prove nothing changed since the preview
    fingerprint = folder_fingerprint(media_files) if dry_run else None
    stats['total_files'] = len(media_files)

    if not media_files:
        progress.log("No media files found in the source folder!")
        progress.status("No media files found")
        return stats, cached_plan

    progress.log("\nReading media metadata...")
    records = _scan_files(media_files, settings, progress)
    if progress.cancelled:
        progress.log("\nOperation cancelled by user")
        progress.status("Cancelled")
        return stats, cached_plan

    if settings.check_corrupt or settings.fix_extensions:
        if settings.check_corrupt:
            progress.log("\nChecking files for damage"
                         f"{' (thorough)' if settings.corrupt_thorough else ''}...")
        else:
            progress.log("\nChecking whether file extensions match their "
                         "contents...")
        _check_health(records, settings, progress)
        if progress.cancelled:
            progress.log("\nOperation cancelled by user")
            progress.status("Cancelled")
            return stats, cached_plan
        verdicts = [mf.verdict for mf in records.values()]
        stats['damaged'] = verdicts.count('damaged')
        stats['misnamed'] = verdicts.count('misnamed')
        stats['unchecked'] = verdicts.count('unchecked')
        fine = len(verdicts) - stats['damaged'] - stats['misnamed'] - stats['unchecked']
        if settings.check_corrupt:
            progress.log(f"  {stats['damaged']} damaged, "
                         f"{stats['misnamed']} with the wrong file extension, "
                         f"{stats['unchecked']} could not be checked, {fine} fine")
        else:
            progress.log(f"  {stats['misnamed']} file(s) have an extension "
                         f"that doesn't match their contents")

    # Files from one shutter press belong together, and share a date
    captures = _group_captures(records)
    _share_capture_dates(captures, progress)

    duplicate_groups = []
    if settings.dedupe_content:
        progress.log("\nLooking for duplicate files by content...")
        duplicate_groups = _find_duplicates(records, settings, progress)
        duplicate_groups += _find_embedded_clips(captures, progress)
        if progress.cancelled:
            progress.log("\nOperation cancelled by user")
            progress.status("Cancelled")
            return stats, cached_plan
        dups = [mf for mf in records.values() if mf.duplicate_of is not None]
        stats['content_duplicates'] = len(dups)
        stats['duplicate_bytes'] = sum(mf.size for mf in dups)
        if duplicate_groups:
            wasted = stats['duplicate_bytes'] / (1024 * 1024)
            progress.log(f"  Found {len(duplicate_groups)} set(s) of identical "
                         f"files - {len(dups)} extra copies ({wasted:.2f} MB)")
        else:
            progress.log("  No duplicates found")
    else:
        progress.percent(60)

    base_name_to_model = _build_model_index(records)
    progress.log(f"Found {len(base_name_to_model)} images with camera metadata")
    progress.log(f"Found {len(raw_files)} RAW files and "
                 f"{sum(1 for mf in records.values() if mf.kind == 'video')} "
                 f"video files")

    run_stamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    log_filename = f"archiveprep_log_{run_stamp}.txt"
    log_path = source_path / log_filename
    undo_path = source_path / f"archiveprep_undo_{run_stamp}.jsonl"

    ctx = SimpleNamespace(planned={}, ops=[], stats=stats, progress=progress)

    if duplicate_groups:
        report_name = f"archiveprep_duplicates_{run_stamp}.txt"
        _write_duplicate_report(source_path / report_name, duplicate_groups,
                                source_path)
        progress.log(f"Duplicate report saved: {report_name}")

    with open(log_path, 'w', encoding='utf-8') as log_file:
        log_file.write(f"ArchivePrep run log - {datetime.now()}\n")
        log_file.write(f"Source Folder: {source_path}\n")
        log_file.write(f"Mode: {'DRY RUN' if dry_run else operation.upper()}\n")
        log_file.write(f"Subfolder organization: {settings.subfolder_mode}\n")
        log_file.write(f"Separate RAW files: "
                       f"{'Yes' if settings.separate_raw else 'No'}\n")
        log_file.write(f"Separate Screenshots: "
                       f"{'Yes' if settings.separate_screenshots else 'No'}\n")
        log_file.write("=" * 60 + "\n\n")

        # ---- DECIDE ------------------------------------------------------
        # Files are planned in a fixed order so the numbering of any _1/_2
        # renames is the same every time the same folder is organized.
        ordered = [records[p] for p in media_files if p in records]
        total = len(ordered)
        for idx, mf in enumerate(ordered):
            if progress.cancelled:
                progress.log("\nOperation cancelled by user")
                break
            if total:
                progress.percent(60 + int((idx + 1) / total * (0 if dry_run else 20)))
            try:
                _plan_one_file(mf, source_path, settings, base_name_to_model,
                               ctx, dry_run, log_file)
            except Exception as e:
                progress.log(f"\n[error] processing {mf.path.name}: {e}")
                progress.log("   Skipping this file and continuing...")
                log_file.write(f"ERROR processing {mf.path.name}: {e}\n")
                stats['errors'] += 1

        # ---- APPLY -------------------------------------------------------
        undo_entries, rename_entries = [], []
        if not dry_run:
            _journal_start(undo_path, operation, source_path)
            undo_entries, rename_entries = _apply_plan(
                ctx.ops, source_path, progress, stats, log_file, undo_path,
                p0=80, p1=100)

            if undo_entries:
                progress.notify("undo_available", str(undo_path))
            else:
                undo_path.unlink(missing_ok=True)

            if rename_entries:
                rename_undo = (source_path / WRONG_EXT_FOLDER /
                               f"archiveprep_undo_renames_{run_stamp}.jsonl")
                _journal_start(rename_undo, "move", source_path)
                for entry in rename_entries:
                    _journal_append(rename_undo, entry)
                progress.notify("rename_undo_available", str(rename_undo))
                progress.log(f"\n{len(rename_entries)} rename(s) can be "
                             f"undone on their own with 'Undo Renames'.")

            if operation == "move" and settings.cleanup_empty:
                removed = _sweep_empty_dirs(source_path, log_file)
                stats['empty_folders_removed'] = removed
                if removed:
                    progress.log(f"\nRemoved {removed} empty folder(s)")

        duration = (datetime.now() - start_time).total_seconds()
        stats['duration_seconds'] = duration
        _write_run_summary(progress, log_file, stats, duration)
        progress.log(f"\nLog file saved: {log_filename}")

    # ---- REPORT ----------------------------------------------------------
    manifest_name = f"archiveprep_manifest_{run_stamp}.csv"
    write_manifest(source_path / manifest_name, records, source_path)
    progress.log(f"Manifest saved: {manifest_name}")

    if dry_run:
        progress.log("\nThis was a DRY RUN - no files were actually "
                     "moved/copied!")
        if not progress.cancelled:
            cached_plan = {
                'key': plan_key(settings),
                'fingerprint': fingerprint,
                'ops': ctx.ops,
                'stats': copy.deepcopy(stats),
            }
            progress.log("Preview cached - Execute will reuse it "
                         "without re-scanning (as long as nothing changes).")
        progress.status("Dry run complete")
    else:
        progress.log(f"\nOperation complete! Files were {operation}d "
                     f"successfully.")
        if undo_entries:
            progress.log("This run can be undone with the 'Undo Last Run' "
                         "button.")
        progress.status(f"Operation complete - {stats['processed']} files "
                        f"{operation}d")

    return stats, cached_plan


def _journal_start(undo_path, operation, source_path):
    """Open an undo journal and write its header line.

    The journal is one line of JSON per record: a header, then one
    [target, original] pair for every file touched. Nothing already
    written is ever rewritten.

    That is the whole point. The previous version rebuilt the entire
    record from scratch every 50 files, so a crash or a power cut
    during one of those rewrites left truncated JSON behind - and a
    truncated undo record cannot be read at all, which made the whole
    run impossible to reverse. An undo record failing is the one
    failure this application cannot afford.
    """
    try:
        undo_path.parent.mkdir(parents=True, exist_ok=True)
        with open(undo_path, 'w', encoding='utf-8') as f:
            json.dump({'operation': operation,
                       'created': datetime.now().isoformat(),
                       'source': str(source_path)}, f)
            f.write("\n")
    except OSError:
        pass

def _journal_append(undo_path, entry):
    """Record one [target, original] pair, before moving on to the next.

    Opened and closed per entry so a crashed process cannot take any
    entry down with it - by the time this returns the bytes are with the
    operating system. That costs one file open per file organized, which
    is nothing next to the copy or move it is recording.
    """
    try:
        with open(undo_path, 'a', encoding='utf-8') as f:
            json.dump(entry, f)
            f.write("\n")
    except OSError:
        pass

def read_undo(undo_path):
    """Read an undo record back. Returns {'operation', 'source', 'entries'}.

    Reads the journal written by this version, and still reads the single
    JSON object that v35 and earlier wrote, so a folder organized with an
    older build can always be undone.

    A torn final line - the only damage an interrupted append can cause -
    is dropped, and everything before it is still restored.
    """
    text = Path(undo_path).read_text(encoding='utf-8')
    try:
        record = json.loads(text)  # v35 and earlier: one object, whole file
        record.setdefault('entries', [])
        return record
    except json.JSONDecodeError:
        pass

    lines = text.splitlines()
    if not lines:
        return {'operation': 'move', 'entries': []}
    record = json.loads(lines[0])
    entries = []
    for line in lines[1:]:
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError:
            break  # a half-written last line: stop, keep everything before it
    record['entries'] = entries
    return record


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


def _claimed(ctx, path):
    """True if something is already at `path`, or is planned to land there."""
    return path.exists() or str(path).lower() in ctx.planned


def _same_as_whatever_lands_at(ctx, candidate, path):
    """True if `candidate` is byte-for-byte the file that will occupy `path`.

    That may be a file already sitting there, or one an earlier operation in
    this same plan is about to put there. The second case matters because
    planning now happens before anything moves: two identical files in
    different folders both aim at the same destination, and the second must
    still be recognised as a duplicate rather than renamed to _1. The old
    loop moved as it went and so saw this for free.
    """
    if path.exists() and files_identical(candidate, path):
        return True
    incoming = ctx.planned.get(str(path).lower())
    return incoming is not None and files_identical(candidate, incoming)


def _plan_set_aside(mf, source_path, folder_name, kind, reason, ctx,
                    settings, dry_run, log_file, new_name=None):
    """Decide to put a duplicate, damaged or misnamed file aside.

    The original folder structure is mirrored inside Duplicates/, Corrupt/ or
    Wrong Extension/, so anything can be put back by hand. Nothing is ever
    deleted. `new_name` renames the file on the way (used to give a misnamed
    file the extension it should have had).
    """
    rel = mf.path.relative_to(source_path)
    tag = {DUPLICATES_FOLDER: "[duplicate]", CORRUPT_FOLDER: "[damaged]",
            WRONG_EXT_FOLDER: "[renamed]"}.get(folder_name, "[aside]")

    if settings.operation == "copy":
        # Copy mode leaves the source untouched and the good original is
        # already there, so making a third copy of a redundant or broken
        # file would only add clutter.
        ctx.progress.log(f"\n{tag} {rel}: {reason}")
        ctx.progress.log("  [skip] Left where it is (copy mode never touches the source)")
        log_file.write(f"{folder_name}: {rel} - {reason} "
                       f"(copy mode, not copied)\n")
        mf.action, mf.reason = 'skipped', reason
        return

    target = source_path / folder_name / rel
    if new_name and new_name != mf.path.name:
        target = target.with_name(new_name)

    if _claimed(ctx, target):
        if _same_as_whatever_lands_at(ctx, mf.path, target):
            ctx.progress.log(f"\n{tag} {rel}: already set aside in "
                             f"{folder_name}/, leaving it alone")
            log_file.write(f"{folder_name}: {rel} already present - skipped\n")
            mf.action, mf.reason = 'skipped', "already set aside"
            return
        base = target
        counter = 1
        while _claimed(ctx, target):
            target = base.parent / f"{base.stem}_{counter}{base.suffix}"
            counter += 1
    ctx.planned[str(target).lower()] = mf.path

    ctx.progress.log(f"\n{tag} {rel}: {reason}")
    relative_target = target.relative_to(source_path)
    mf.action, mf.target, mf.reason = kind, target, reason
    ctx.ops.append(Operation(source=mf.path, target=target, kind=kind,
                             operation="move"))

    if dry_run:
        ctx.progress.log(f"  [plan] would move to: {relative_target}")
        log_file.write(f"{folder_name}: would move {rel} -> "
                       f"{relative_target}\n")


def _plan_one_file(mf, source_path, settings, base_name_to_model, ctx,
                   dry_run, log_file):
    """Decide what should happen to one file.

    Reads the filesystem freely; writes nothing. Everything it concludes goes
    onto the record and, when a file is to be moved, onto ctx.ops. `dry_run`
    reaches this far for one reason only - the wording of the log line. The
    decisions themselves are identical either way, which is precisely the
    point: a preview and a real run now go down the same path.
    """
    file_path = mf.path

    # Files the analysis phases flagged go to a set-aside folder instead of
    # into the organized folders. Duplicates are checked first: a duplicate
    # that also happens to be damaged is still just a duplicate, and its
    # keeper is guaranteed to be the healthier copy.
    if mf.duplicate_of is not None:
        twin = mf.duplicate_of.relative_to(source_path)
        # An embedded clip already explained itself when it was found
        reason = mf.reason or f"identical to {twin}"
        _plan_set_aside(mf, source_path, DUPLICATES_FOLDER, 'duplicate',
                        reason, ctx, settings, dry_run, log_file)
        return

    if settings.check_corrupt and mf.verdict == 'damaged':
        _plan_set_aside(mf, source_path, CORRUPT_FOLDER, 'corrupt',
                        f"damaged - {mf.verdict_reason}",
                        ctx, settings, dry_run, log_file)
        return

    if settings.fix_extensions and mf.verdict == 'misnamed':
        new_name = ((file_path.stem + mf.canonical_ext)
                    if mf.canonical_ext else None)
        _plan_set_aside(mf, source_path, WRONG_EXT_FOLDER, 'wrong_extension',
                        f"wrong file extension - {mf.verdict_reason}",
                        ctx, settings, dry_run, log_file, new_name=new_name)
        return

    # Which camera took it. RAW and video borrow the model of the image they
    # were taken with; only images carry their own.
    match_note = None
    if mf.kind == 'raw':
        file_type = "[raw]"
        mf.camera_model = lookup_model(base_name_to_model, file_path)
        if mf.camera_model:
            match_note = f"  matched RAW to JPEG: {mf.camera_model}"
        else:
            mf.camera_model = camera_from_filename(file_path)
            match_note = (f"  no matching JPEG; filename says {mf.camera_model}"
                          if mf.camera_model else
                          "  no matching JPEG for RAW; filing as Unknown")
    elif mf.kind == 'video':
        file_type = "[video]"
        mf.camera_model = lookup_model(base_name_to_model, file_path)
        if mf.camera_model:
            match_note = f"  matched video to image: {mf.camera_model}"
        else:
            mf.camera_model = camera_from_filename(file_path)
            match_note = (f"  no matching image; filename says {mf.camera_model}"
                          if mf.camera_model else
                          "  no matching image for video; filing as Unknown")
    else:
        file_type = "[image]"
        if (mf.camera_model == "iPhone"
                and file_path.suffix.lower() in HEIC_EXTS
                and mf.date_source != 'exif'):
            match_note = "  HEIC format, assuming iPhone"

    separate_shot = settings.separate_screenshots and mf.is_screenshot
    if separate_shot:
        file_type += " (Screenshot)"

    # Date: metadata (EXIF / video header) if available, else modified time
    if mf.captured_at is None:
        mf.captured_at = datetime.fromtimestamp(file_path.stat().st_mtime)
        mf.date_source = 'mtime'

    target_folder = get_target_folder(source_path, file_path, mf.camera_model,
                                      mf.captured_at, settings, separate_shot)
    target_path = target_folder / file_path.name

    # Recursive re-runs: files already in their correct spot are untouched
    if target_path == file_path:
        ctx.progress.log(f"\n[skip] already organized: "
                         f"{file_path.relative_to(source_path)}")
        log_file.write(f"Already organized - skipped: {file_path.name}\n")
        ctx.stats['already_organized'] += 1
        mf.action, mf.reason = 'skipped', "already organized"
        return

    file_size_mb = mf.size / (1024 * 1024)
    ctx.stats['total_size_mb'] += file_size_mb

    ctx.progress.log(f"\n{file_type} {file_path.name} ({file_size_mb:.2f} MB)")
    log_file.write(f"Processing: {file_path.name}\n")
    if match_note:
        ctx.progress.log(match_note)
    if separate_shot:
        ctx.stats['screenshots'] += 1

    # Update statistics
    ctx.stats['by_year'][str(mf.captured_at.year)] = \
        ctx.stats['by_year'].get(str(mf.captured_at.year), 0) + 1
    if mf.camera_model:
        ctx.stats['by_model'][mf.camera_model] = \
            ctx.stats['by_model'].get(mf.camera_model, 0) + 1
    else:
        ctx.stats['no_metadata'] += 1

    # Name collisions with identical content are skipped entirely; different
    # content gets a _1/_2 rename. planned_targets keeps the numbering right
    # even though nothing has been moved yet.
    if _claimed(ctx, target_path):
        if _same_as_whatever_lands_at(ctx, file_path, target_path):
            ctx.progress.log("  [skip] identical file already at destination"
                             f"{' (left in source)' if settings.operation == 'move' else ''}")
            log_file.write("  Identical duplicate - skipped\n")
            ctx.stats['duplicates'] += 1
            mf.action, mf.reason = 'skipped', "identical file already at destination"
            return
        counter = 1
        while _claimed(ctx, target_path):
            target_path = target_folder / f"{file_path.stem}_{counter}{file_path.suffix}"
            counter += 1
            if _same_as_whatever_lands_at(ctx, file_path, target_path):
                ctx.progress.log("  [skip] identical file already at destination")
                log_file.write("  Identical duplicate - skipped\n")
                ctx.stats['duplicates'] += 1
                mf.action, mf.reason = 'skipped', "identical file already at destination"
                return
        ctx.progress.log(f"  [rename] target taken, will use: {target_path.name}")
    ctx.planned[str(target_path).lower()] = file_path

    mf.action, mf.target = 'organize', target_path
    ctx.ops.append(Operation(source=file_path, target=target_path,
                             kind='organize', operation=settings.operation))

    if dry_run:
        relative_path = target_path.relative_to(source_path)
        ctx.progress.log(f"  [plan] would {settings.operation} to: {relative_path}")
        log_file.write(f"  Would {settings.operation} to: {relative_path}\n")
        ctx.stats['processed'] += 1


def _apply_plan(ops, source_path, progress, stats, log_file, undo_path,
                p0=60, p1=100):
    """Carry out a plan. The only code in the application that moves a file.

    Preview and Execute run exactly the same decisions and then either stop
    here or come through here - which is what stops the two drifting apart.
    They used to be separate engines, and had already grown different
    collision handling.

    Returns (undo_entries, rename_entries).
    """
    undo_entries, rename_entries = [], []
    total = len(ops)
    if not total:
        progress.percent(p1)
        return undo_entries, rename_entries

    operation_text = {"move": "Moving", "copy": "Copying"}
    phase_start = time.monotonic()
    last_status_time = 0.0
    last_pct = -1

    for idx, op in enumerate(ops):
        if progress.cancelled:
            progress.log("\nOperation cancelled by user")
            break

        pct = p0 + int((idx + 1) / total * (p1 - p0))
        if pct != last_pct:
            progress.percent(pct)
            last_pct = pct
        now = time.monotonic()
        if now - last_status_time >= 0.2 or idx + 1 == total:
            elapsed = now - phase_start
            rate = (idx + 1) / elapsed if elapsed > 0 else 0
            if rate > 0.01 and idx + 1 < total:
                remaining = (total - idx - 1) / rate
                eta = f"{int(remaining // 60)}:{int(remaining % 60):02d}"
            else:
                eta = "--:--"
            progress.status(f"Processing {idx + 1}/{total} • {rate:.0f} files/s "
                            f"• ETA {eta} • errors {stats['errors']}")
            last_status_time = now

        source, target = op.source, op.target
        try:
            if not source.exists():
                raise FileNotFoundError("source file vanished")
            # The plan was made against a snapshot. If the destination has
            # been taken since, step aside rather than overwrite - unless it
            # is the very same file, in which case there is nothing to do.
            if target.exists():
                if files_identical(source, target):
                    progress.log(f"  [skip] {source.name}: identical file already "
                                 f"at destination, skipping")
                    log_file.write(f"  Identical duplicate - skipped: {source}\n")
                    stats['duplicates'] += 1
                    continue
                base = target
                counter = 1
                while target.exists():
                    target = base.parent / f"{base.stem}_{counter}{base.suffix}"
                    counter += 1
                progress.log(f"  [rename] {base.name}: destination taken, "
                             f"renaming to {target.name}")

            target.parent.mkdir(parents=True, exist_ok=True)
            # Journalled *before* the move, not after. If the process dies in
            # between, undo finds an entry for a move that never happened,
            # reports "missing, cannot restore" and leaves the file exactly
            # where it already is - harmless. The other order leaves a file
            # moved with no undo entry, which is a file the tool can no longer
            # put back. A spurious entry is always the cheaper mistake.
            _journal_append(undo_path, [str(target), str(source)])
            transfer_file(source, target, op.operation)
            undo_entries.append([str(target), str(source)])
            if op.kind == 'wrong_extension':
                # These also get their own undo record, so the renames can be
                # reversed on their own without touching the rest of the run
                rename_entries.append([str(target), str(source)])
            relative = target.relative_to(source_path)
            # Names the file as well as the destination: deciding and doing
            # are separate passes now, so the action lines are no longer
            # sitting directly under the "Processing X" line they belong to.
            progress.log(f"  [done] {operation_text[op.operation]} {source.name} "
                         f"-> {relative}")
            log_file.write(f"  {operation_text[op.operation]} {source.name} "
                           f"-> {relative}\n")
            stats['processed'] += 1
        except Exception as e:
            progress.log(f"  [error] {source.name}: {e}")
            log_file.write(f"  FILE OPERATION ERROR {source}: {e}\n")
            stats['errors'] += 1

    progress.percent(p1)
    return undo_entries, rename_entries


def execute_cached_plan(plan, settings, progress):
    """Carry out a plan a preview already worked out, without scanning again.

    Returns the run statistics, or None if the folder no longer matches the
    fingerprint taken during the preview - in which case the caller falls
    back to a fresh run rather than acting on a stale plan.

    This is deliberately thin. It used to be a second execution engine, with
    its own collision handling that had already diverged from the real one;
    now it verifies the folder and hands the same operations to the same
    applier.
    """
    source_path = Path(settings.source)
    start_time = datetime.now()

    progress.status("Verifying folder is unchanged since preview...")
    try:
        regular, raw = collect_media_files(
            source_path, settings.include_subfolders)
    except OSError as e:
        progress.log(f"[error] could not read source folder: {e}")
        return None
    if folder_fingerprint(regular + raw) != plan['fingerprint']:
        progress.log("Folder changed since the preview - "
                     "running a fresh analysis instead.")
        return None

    ops = plan['ops']
    operation = settings.operation
    operation_text = "Moving" if operation == "move" else "Copying"

    # Start from the preview's statistics; redo the live counters
    stats = copy.deepcopy(plan['stats'])
    stats['processed'] = 0
    stats['errors'] = 0

    run_stamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    log_filename = f"archiveprep_log_{run_stamp}.txt"
    undo_path = source_path / f"archiveprep_undo_{run_stamp}.jsonl"

    progress.log("=" * 60)
    progress.log(f"ArchivePrep - {operation_text.upper()} "
                 f"(cached preview)")
    progress.log(f"Source: {source_path}")
    progress.log(f"Folder unchanged since preview - executing "
                 f"{len(ops)} planned operation(s) directly")
    progress.log("=" * 60)

    with open(source_path / log_filename, 'w', encoding='utf-8') as log_file:
        log_file.write(f"ArchivePrep run log - {datetime.now()}\n")
        log_file.write(f"Source Folder: {source_path}\n")
        log_file.write(f"Mode: {operation.upper()} (cached preview replay)\n")
        log_file.write("=" * 60 + "\n\n")

        _journal_start(undo_path, operation, source_path)
        undo_entries, rename_entries = _apply_plan(
            ops, source_path, progress, stats, log_file, undo_path, p0=0, p1=100)

        if undo_entries:
            progress.notify("undo_available", str(undo_path))
        else:
            undo_path.unlink(missing_ok=True)

        if rename_entries:
            rename_undo = (source_path / WRONG_EXT_FOLDER /
                           f"archiveprep_undo_renames_{run_stamp}.jsonl")
            _journal_start(rename_undo, "move", source_path)
            for entry in rename_entries:
                _journal_append(rename_undo, entry)
            progress.notify("rename_undo_available", str(rename_undo))

        if operation == "move" and settings.cleanup_empty:
            removed = _sweep_empty_dirs(source_path, log_file)
            stats['empty_folders_removed'] = removed
            if removed:
                progress.log(f"\nRemoved {removed} empty folder(s)")

        duration = (datetime.now() - start_time).total_seconds()
        stats['duration_seconds'] = duration

        progress.log("\n" + "=" * 60)
        progress.log("SUMMARY:")
        progress.log(f"Files processed: {stats['processed']}")
        progress.log(f"Errors: {stats['errors']}")
        progress.log(f"Duration: {duration:.1f} seconds "
                     f"(analysis skipped - cached preview)")
        log_file.write(f"\nSUMMARY: processed {stats['processed']}, "
                       f"errors {stats['errors']}, {duration:.1f}s\n")
        progress.log(f"\nLog file saved: {log_filename}")
        progress.log(f"\nOperation complete! Files were {operation}d "
                     f"successfully.")
        if undo_entries:
            progress.log("This run can be undone with the 'Undo Last Run' "
                         "button.")
        progress.status(f"Operation complete - {stats['processed']} files "
                        f"{operation}d")

    return stats


def run_health_check(settings, progress):
    """Worker: check every media file in the source and report the results.

    Deliberately read-only - nothing is moved, renamed or deleted, so this
    is safe to run on a folder before deciding what to do with it.
    """
    source_path = Path(settings.source)
    start_time = datetime.now()
    stats = _empty_stats()

    # This button always does the full check, whatever the checkboxes say
    settings = copy.copy(settings)
    settings.check_corrupt = True

    progress.log("=" * 60)
    progress.log("ArchivePrep - FILE HEALTH CHECK"
             f"{' (thorough)' if settings.corrupt_thorough else ''}")
    progress.log(f"Source: {source_path}")
    progress.log("Nothing will be moved, renamed or deleted.")
    progress.log("=" * 60)

    try:
        regular, raw = collect_media_files(source_path,
                                           settings.include_subfolders)
    except OSError as e:
        progress.log(f"[error] could not read source folder: {e}")
        progress.status("Error reading source folder")
        return stats

    media_files = regular + raw
    stats['total_files'] = len(media_files)
    if not media_files:
        progress.log("No media files found in the source folder!")
        progress.status("No media files found")
        return stats

    progress.log(f"\nChecking {len(media_files)} file(s)...")
    records = {p: MediaFile(path=p) for p in media_files}
    _check_health(records, settings, progress, p0=0, p1=100)
    health = {mf.path: (mf.verdict, mf.verdict_reason) for mf in records.values()}

    if progress.cancelled:
        progress.log("\nCheck cancelled by user")
        progress.status("Cancelled")
        return stats

    by_name = lambda p: str(p).lower()
    damaged = sorted((f for f, (s, _) in health.items() if s == 'damaged'),
                     key=by_name)
    misnamed = sorted((f for f, (s, _) in health.items() if s == 'misnamed'),
                      key=by_name)
    # Not all wrong names matter equally, and this report is what gets read
    # before deciding whether to touch anything. A photo called .MOV is handed
    # to a video player and simply will not open; a WEBP called .png displays
    # everywhere and needs nothing done to it. Reporting both as one number
    # turns a handful of real problems into a pile of things to worry about.
    breaking = [p for p in misnamed if records[p].extension == 'breaking']
    harmless = [p for p in misnamed if records[p].extension != 'breaking']
    unchecked = [f for f, (s, _) in health.items() if s == 'unchecked']
    healthy = len(health) - len(damaged) - len(misnamed) - len(unchecked)
    stats['damaged'] = len(damaged)
    stats['misnamed'] = len(misnamed)
    stats['unchecked'] = len(unchecked)
    stats['processed'] = len(health)

    run_stamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    report_name = f"archiveprep_health_{run_stamp}.txt"
    try:
        with open(source_path / report_name, 'w', encoding='utf-8') as f:
            f.write(f"ArchivePrep - file health check "
                    f"{datetime.now()}\n")
            f.write(f"Source: {source_path}\n")
            f.write(f"Mode: {'thorough' if settings.corrupt_thorough else 'quick'}\n")
            f.write("=" * 60 + "\n\n")
            f.write(f"Fine: {healthy}\n")
            f.write(f"Damaged: {len(damaged)}\n")
            f.write(f"Wrong file extension: {len(misnamed)} "
                    f"({len(breaking)} will not open, "
                    f"{len(harmless)} open anyway)\n")
            f.write(f"Could not be checked: {len(unchecked)}\n\n")
            if damaged:
                f.write("DAMAGED FILES\n")
                for path in damaged:
                    f.write(f"  {path.relative_to(source_path)} - "
                            f"{health[path][1]}\n")
                f.write("\n")
            if breaking:
                f.write("WRONG FILE EXTENSION - WILL NOT OPEN (the contents "
                        "are fine and these are NOT damaged, but the name "
                        "sends them to the wrong kind of program, so nothing "
                        "opens them. Renaming fixes it.)\n")
                for path in breaking:
                    f.write(f"  {path.relative_to(source_path)} - "
                            f"{health[path][1]}\n")
                f.write("\n")
            if harmless:
                f.write("WRONG FILE EXTENSION - OPENS ANYWAY (the name is "
                        "wrong but points at the same kind of media, so "
                        "viewers display these perfectly well. Nothing here "
                        "needs doing unless you want the names tidy.)\n")
                for path in harmless:
                    f.write(f"  {path.relative_to(source_path)} - "
                            f"{health[path][1]}\n")
                f.write("\n")
            if unchecked:
                f.write("COULD NOT BE CHECKED (format we cannot verify - "
                        "this does NOT mean they are broken)\n")
                for path in sorted(unchecked, key=lambda p: str(p).lower()):
                    f.write(f"  {path.relative_to(source_path)}\n")
    except OSError as e:
        progress.log(f"[warn] could not write the report file: {e}")

    progress.log("\n" + "=" * 60)
    progress.log("RESULTS:")
    progress.log(f"Fine:                  {healthy}")
    progress.log(f"Damaged:               {len(damaged)}")
    progress.log(f"Wrong file extension:  {len(misnamed)} "
             f"- {len(breaking)} will not open at all, "
             f"{len(harmless)} open fine anyway")
    progress.log(f"Could not be checked:  {len(unchecked)} "
             f"(RAW / some video formats - not a sign they are broken)")

    if damaged:
        progress.log("\nDamaged files:")
        for path in damaged[:200]:
            progress.log(f"  [damaged] {path.relative_to(source_path)} - "
                     f"{health[path][1]}")
        if len(damaged) > 200:
            progress.log(f"  ... and {len(damaged) - 200} more "
                     f"(the full list is in the report file)")
    else:
        progress.log("\nNo damaged files found.")

    for heading, paths in (
            ("\nWrong file extension, WILL NOT OPEN (not damaged - the name "
             "sends them to the wrong kind of program):", breaking),
            ("\nWrong file extension, but they open anyway (the name is wrong "
             "and nothing is broken - tidy them only if you want to):",
             harmless)):
        if not paths:
            continue
        progress.log(heading)
        for path in paths[:100]:
            progress.log(f"  [renamed] {path.relative_to(source_path)} - "
                         f"{health[path][1]}")
        if len(paths) > 100:
            progress.log(f"  ... and {len(paths) - 100} more "
                         f"(the full list is in the report file)")

    if damaged or misnamed:
        progress.log("\nTip: tick 'Check files for damage while organizing' to have "
                 f"damaged files moved into a '{CORRUPT_FOLDER}' folder and "
                 f"misnamed ones into '{WRONG_EXT_FOLDER}' on the next run.")

    duration = (datetime.now() - start_time).total_seconds()
    stats['duration_seconds'] = duration
    progress.log(f"\nDuration: {duration:.1f} seconds")
    progress.log(f"Report saved: {report_name}")
    progress.status(f"Check complete - {len(damaged)} damaged, "
                       f"{healthy} fine")

    return stats
def run_undo(undo_file, record, progress, label=None):
    """Worker: revert every operation in the undo record."""
    entries = record.get('entries', [])
    op = record.get('operation', 'move')
    source_root = Path(record.get('source', str(undo_file.parent)))
    total = len(entries)
    restored = 0
    problems = 0

    progress.log("\n" + "=" * 60)
    progress.log(f"UNDOING {label or ('last ' + op + ' run')} ({total} files)")
    progress.log("=" * 60)

    for idx, (target, original) in enumerate(entries):
        if progress.cancelled:
            progress.log("\nUndo cancelled by user")
            break
        target_p, original_p = Path(target), Path(original)
        try:
            if op == "move":
                if not target_p.exists():
                    progress.log(f"  [warn] missing, cannot restore: {target}")
                    problems += 1
                elif original_p.exists():
                    progress.log(f"  [warn] original location occupied, skipping: {original}")
                    problems += 1
                else:
                    original_p.parent.mkdir(parents=True, exist_ok=True)
                    transfer_file(target_p, original_p, "move")
                    restored += 1
            else:
                # Copy run: delete the copies this run made - but only
                # once we can prove a file still *is* one of them. The
                # original it was copied from is untouched by a copy run,
                # so if the two no longer match byte for byte, someone has
                # edited or replaced the copy since. Deleting that would
                # destroy work, which undo must never do.
                if not target_p.exists():
                    pass  # already gone; nothing to undo
                elif not original_p.exists():
                    progress.log(f"  [warn] the original is gone, so this copy is "
                             f"now the only one - keeping it: {target}")
                    problems += 1
                elif not files_identical(target_p, original_p):
                    progress.log(f"  [warn] changed since it was copied - keeping "
                             f"it: {target}")
                    problems += 1
                else:
                    os.remove(str(target_p))
                    restored += 1
        except Exception as e:
            progress.log(f"  [error] {target}: {e}")
            problems += 1

        if (idx + 1) % 20 == 0 or idx + 1 == total:
            progress.percent(int((idx + 1) / total * 100))
            progress.status(f"Undoing {idx + 1}/{total}")

    removed = _sweep_empty_dirs(source_root)

    # Mark the record consumed so it can't be replayed
    try:
        undo_file.rename(undo_file.with_name(undo_file.name + ".undone"))
    except OSError:
        pass
    if label != "renames":
        progress.notify("undo_available", None)
    progress.notify("plan_stale", None)  # the folder just changed

    verb = "restored" if op == "move" else "removed"
    progress.log(f"\nUndo complete: {restored} files {verb}, "
             f"{problems} issue(s), {removed} empty folder(s) cleaned up")
    progress.status(f"Undo complete - {restored} files {verb}")
