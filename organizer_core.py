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
import json
import time
import shutil
import hashlib
import threading
from pathlib import Path
from datetime import datetime
from types import SimpleNamespace
from concurrent.futures import ThreadPoolExecutor, as_completed

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


def _extract_metadata(files, settings, progress, p0=0, p1=25):
    """Phase A: read metadata for images and videos (p0-p1%% progress).

    Returns {Path: meta dict}. Parallelized when multithreading is on.
    """
    metadata = {}
    total = len(files)
    if total == 0:
        return metadata

    def note_progress(done):
        progress.percent(p0 + int(done / total * (p1 - p0)))
        progress.status(f"Reading metadata {done}/{total}")

    if settings.use_multithreading and total > 1:
        with ThreadPoolExecutor(max_workers=settings.max_threads) as executor:
            futures = {executor.submit(read_media_metadata, f): f for f in files}
            done = 0
            for future in as_completed(futures):
                if progress.cancelled:
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
            if progress.cancelled:
                break
            metadata[file_path] = read_media_metadata(file_path)
            if done % 25 == 0 or done == total:
                note_progress(done)

    return metadata


def _check_health(files, settings, progress, p0=25, p1=40):
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
            progress.percent(p0 + int(done / total * (p1 - p0)))
            progress.status(f"{label} {done}/{total}")
            last_note[0] = now

    if settings.use_multithreading and total > 1:
        with ThreadPoolExecutor(max_workers=settings.max_threads) as executor:
            futures = {executor.submit(file_health, f, thorough,
                                       format_only): f for f in files}
            for future in as_completed(futures):
                if progress.cancelled:
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
            if progress.cancelled:
                break
            health[file_path] = file_health(file_path, thorough,
                                            format_only)
            note(len(health))

    return health


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


def _find_duplicates(files, metadata, health, settings, progress,
                     p0=40, p1=60):
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
        progress.percent(p1)
        return {}, []

    progress.log(f"\n♻️ Comparing {len(candidates)} files that share a size "
             f"with at least one other ({len(files) - len(candidates)} "
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

    head_hashes = _hash_many(candidates, _hash_head, settings, progress, note_head)

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
            progress.percent(mid + int(done / full_total * (p1 - mid)))
            progress.status(f"Verifying possible duplicates "
                               f"{done}/{full_total}")
            last_note[0] = now

    full_hashes = _hash_many(full_candidates, _hash_file, settings, progress,
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
                         key=lambda p: _keeper_rank(p, metadata, health))
        keeper, rest = ordered[0], ordered[1:]
        for dup in rest:
            duplicate_of[dup] = keeper
        groups.append((keeper, rest))

    progress.percent(p1)
    return duplicate_of, groups


def _write_duplicate_report(report_path, groups, source_path):
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


def organize_photos(settings, progress, dry_run=True):
    """Organize photos and videos from source folder by camera model.

    Runs on a worker thread; must not touch tkinter widgets/variables.
    """
    start_time = datetime.now()
    source_path = Path(settings.source)

    # Reset statistics
    stats = _empty_stats()
    cached_plan = None

    operation = settings.operation
    operation_text = "Moving" if operation == "move" else "Copying"

    progress.log("=" * 60)
    progress.log(f"Kjegla's Photo Organizer - {'DRY RUN' if dry_run else operation_text.upper()}")
    progress.log(f"Source: {source_path}")
    progress.log(f"Operation: {operation}")
    progress.log(f"Subfolder mode: {settings.subfolder_mode}")
    progress.log(f"Separate RAW files: {'Yes' if settings.separate_raw else 'No'}")
    progress.log(f"Separate Screenshots: {'Yes' if settings.separate_screenshots else 'No'}")
    progress.log(f"Include subfolders: {'Yes' if settings.include_subfolders else 'No'}")
    progress.log(f"Multithreading: {'Yes' if settings.use_multithreading else 'No'}")
    progress.log(f"Find duplicates by content: "
             f"{'Yes' if settings.dedupe_content else 'No'}")
    progress.log(f"Check files for damage: "
             f"{('Yes (thorough)' if settings.corrupt_thorough else 'Yes') if settings.check_corrupt else 'No'}")
    progress.log(f"Delete empty folders: "
             f"{'Yes' if settings.cleanup_empty else 'No'}")
    progress.log("=" * 60)

    try:
        regular_media_files, raw_files = collect_media_files(
            source_path, settings.include_subfolders,
            sniff_unknown=settings.fix_extensions)
    except OSError as e:
        progress.log(f"❌ Could not read source folder: {e}")
        progress.status("Error reading source folder")
        return stats, cached_plan

    # Process regular files first so RAW/video matching can use their models
    media_files = regular_media_files + raw_files

    # Snapshot the folder state now; a dry run stores this so Execute can
    # later prove nothing changed since the preview
    fingerprint = folder_fingerprint(media_files) if dry_run else None

    stats['total_files'] = len(media_files)

    if not media_files:
        progress.log("No media files found in the source folder!")
        progress.status("No media files found")
        return stats, cached_plan

    # Phase A: one metadata read per image/video (parallel if enabled)
    progress.log("\n📋 Reading media metadata...")
    metadata = _extract_metadata(regular_media_files, settings, progress)

    if progress.cancelled:
        progress.log("\n⏹️ Operation cancelled by user")
        progress.status("Cancelled")
        return stats, cached_plan

    # Phase A2: damage check and/or extension check (either can be off)
    health = {}
    if settings.check_corrupt or settings.fix_extensions:
        if settings.check_corrupt:
            progress.log("\n🩺 Checking files for damage"
                     f"{' (thorough)' if settings.corrupt_thorough else ''}...")
        else:
            progress.log("\n🏷️ Checking whether file extensions match their "
                     "contents...")
        health = _check_health(media_files, settings, progress)
        if progress.cancelled:
            progress.log("\n⏹️ Operation cancelled by user")
            progress.status("Cancelled")
            return stats, cached_plan
        stats['damaged'] = sum(1 for s, _ in health.values()
                                    if s == 'damaged')
        stats['misnamed'] = sum(1 for s, _ in health.values()
                                     if s == 'misnamed')
        stats['unchecked'] = sum(1 for s, _ in health.values()
                                      if s == 'unchecked')
        fine = (len(health) - stats['damaged']
                - stats['misnamed'] - stats['unchecked'])
        if settings.check_corrupt:
            progress.log(f"  {stats['damaged']} damaged, "
                     f"{stats['misnamed']} with the wrong file extension, "
                     f"{stats['unchecked']} could not be checked, "
                     f"{fine} fine")
        else:
            progress.log(f"  {stats['misnamed']} file(s) have an extension "
                     f"that doesn't match their contents")

    # Phase A3: content-based duplicate detection (optional)
    duplicate_of, duplicate_groups = {}, []
    if settings.dedupe_content:
        progress.log("\n♻️ Looking for duplicate files by content...")
        duplicate_of, duplicate_groups = _find_duplicates(
            media_files, metadata, health, settings, progress)
        if progress.cancelled:
            progress.log("\n⏹️ Operation cancelled by user")
            progress.status("Cancelled")
            return stats, cached_plan
        stats['content_duplicates'] = len(duplicate_of)
        for dup in duplicate_of:
            try:
                stats['duplicate_bytes'] += dup.stat().st_size
            except OSError:
                pass
        if duplicate_groups:
            wasted = stats['duplicate_bytes'] / (1024 * 1024)
            progress.log(f"  Found {len(duplicate_groups)} set(s) of identical "
                     f"files - {len(duplicate_of)} extra copies "
                     f"({wasted:.2f} MB)")
        else:
            progress.log("  No duplicates found")
    else:
        progress.percent(60)

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

    progress.log(f"Found {len(base_name_to_model)} images with camera metadata")
    progress.log(f"Found {len(raw_files)} RAW files and "
             f"{sum(1 for f in media_files if is_video_file(f))} video files")

    # Create log file (and matching undo record for real runs)
    run_stamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    log_filename = f"kjegla_media_log_{run_stamp}.txt"
    log_path = source_path / log_filename
    undo_path = source_path / f"kjegla_undo_{run_stamp}.jsonl"

    # Per-run context: planned targets make dry-run duplicate renames
    # accurate; undo_entries feeds undo; plan_ops collects [source, target]
    # pairs for the preview cache; duplicate_of/health carry the results of
    # the two analysis phases into the per-file loop; undo_path is the
    # journal each move is appended to as it happens
    ctx = SimpleNamespace(planned_targets=set(), undo_entries=[],
                          plan_ops=[], duplicate_of=duplicate_of,
                          health=health, rename_entries=[],
                          stats=stats, progress=progress,
                          undo_path=undo_path)

    if not dry_run:
        _journal_start(undo_path, operation, source_path)

    if duplicate_groups:
        report_name = f"kjegla_duplicates_{run_stamp}.txt"
        _write_duplicate_report(source_path / report_name,
                                     duplicate_groups, source_path)
        progress.log(f"📄 Duplicate report saved: {report_name}")

    with open(log_path, 'w', encoding='utf-8') as log_file:
        log_file.write(f"Kjegla's Media Organization Log - {datetime.now()}\n")
        log_file.write(f"Source Folder: {source_path}\n")
        log_file.write(f"Mode: {'DRY RUN' if dry_run else operation.upper()}\n")
        log_file.write(f"Subfolder organization: {settings.subfolder_mode}\n")
        log_file.write(f"Separate RAW files: {'Yes' if settings.separate_raw else 'No'}\n")
        log_file.write(f"Separate Screenshots: {'Yes' if settings.separate_screenshots else 'No'}\n")
        log_file.write("=" * 60 + "\n\n")

        total = stats['total_files']
        last_pct = -1
        phase_start = time.monotonic()
        last_status_time = 0.0

        for idx, file_path in enumerate(media_files):
            if progress.cancelled:
                progress.log("\n⏹️ Operation cancelled by user")
                break

            # Phase B occupies 60-100% of the progress bar
            pct = 60 + int((idx + 1) / total * 40)
            if pct != last_pct:
                progress.percent(pct)
                last_pct = pct

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
                progress.status(
                    f"Processing {idx + 1}/{total} • {rate:.0f} files/s "
                    f"• ETA {eta} • errors {stats['errors']}")
                last_status_time = now

            try:
                _process_one_file(file_path, source_path, settings,
                                       base_name_to_model, metadata,
                                       ctx, operation,
                                       operation_text, dry_run, log_file)
            except Exception as e:
                progress.log(f"\n❌ Error processing {file_path.name}: {e}")
                progress.log("   Skipping this file and continuing...")
                log_file.write(f"ERROR processing {file_path.name}: {e}\n")
                stats['errors'] += 1

        # Summary
        duration = (datetime.now() - start_time).total_seconds()
        stats['duration_seconds'] = duration

        # Every move was journalled as it happened, so there is nothing
        # left to write - only an empty journal to clear away.
        if not dry_run:
            if ctx.undo_entries:
                progress.notify("undo_available", str(undo_path))
            else:
                undo_path.unlink(missing_ok=True)

        # Renames get their own undo record, kept inside the folder they
        # affect, so they can be reversed without unpicking the whole run
        if not dry_run and ctx.rename_entries:
            rename_undo = (source_path / WRONG_EXT_FOLDER /
                           f"kjegla_undo_renames_{run_stamp}.jsonl")
            _journal_start(rename_undo, "move", source_path)
            for entry in ctx.rename_entries:
                _journal_append(rename_undo, entry)
            progress.notify("rename_undo_available", str(rename_undo))
            progress.log(f"\n↩️ {len(ctx.rename_entries)} rename(s) can be "
                     f"undone on their own with 'Undo Renames'.")

        # After a move, tidy up every folder left empty behind us
        if not dry_run and operation == "move" and settings.cleanup_empty:
            removed = _sweep_empty_dirs(source_path, log_file)
            stats['empty_folders_removed'] = removed
            if removed:
                progress.log(f"\n🧹 Removed {removed} empty folder(s)")

        progress.log("\n" + "=" * 60)
        progress.log("SUMMARY:")
        progress.log(f"Total media files: {stats['total_files']}")
        progress.log(f"Files processed: {stats['processed']}")
        progress.log(f"Files without metadata: {stats['no_metadata']}")
        progress.log(f"Screenshots detected: {stats['screenshots']}")
        if stats['content_duplicates']:
            wasted = stats['duplicate_bytes'] / (1024 * 1024)
            progress.log(f"Duplicate copies set aside: "
                     f"{stats['content_duplicates']} ({wasted:.2f} MB) "
                     f"→ '{DUPLICATES_FOLDER}' folder")
        if stats['damaged']:
            progress.log(f"Damaged files found: {stats['damaged']} "
                     f"→ '{CORRUPT_FOLDER}' folder")
        if stats['misnamed']:
            progress.log(f"Wrongly-named files fixed: {stats['misnamed']} "
                     f"→ renamed to the right extension and moved to "
                     f"'{WRONG_EXT_FOLDER}' (their contents were fine)")
        if stats['unchecked']:
            progress.log(f"Files that could not be checked: "
                     f"{stats['unchecked']} (RAW / some video formats)")
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
            progress.log(f"\n📂 Unknown/unmatched files: {stats['no_metadata']} "
                     f"(moved to 'Unknown Camera' folder)")

        if stats['screenshots'] > 0:
            progress.log(f"\n📱 Screenshots separated: {stats['screenshots']} files")

        progress.log(f"\n📄 Log file saved: {log_filename}")

        if dry_run:
            progress.log("\n⚠️  This was a DRY RUN - no files were actually moved/copied!")
            if not progress.cancelled:
                cached_plan = {
                    'key': plan_key(settings),
                    'fingerprint': fingerprint,
                    'ops': ctx.plan_ops,
                    'stats': copy.deepcopy(stats),
                }
                progress.log("⚡ Preview cached - Execute will reuse it "
                         "without re-scanning (as long as nothing changes).")
            progress.status("Dry run complete")
        else:
            cached_plan = None  # the folder just changed
            progress.log(f"\n✅ Operation complete! Files were {operation}d successfully.")
            if ctx.undo_entries:
                progress.log("↩️ This run can be undone with the 'Undo Last Run' button.")
            progress.status(f"Operation complete - {stats['processed']} files {operation}d")

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


def _set_aside_file(file_path, source_path, folder_name, reason,
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
        ctx.progress.log(f"\n{icon} {rel}: {reason}")
        ctx.progress.log("  ⏭️ Left where it is (copy mode never touches the source)")
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
            ctx.progress.log(f"\n{icon} {rel}: already set aside in "
                     f"{folder_name}/, leaving it alone")
            log_file.write(f"{folder_name}: {rel} already present - skipped\n")
            return
        base = target
        counter = 1
        while claimed(target):
            target = base.parent / f"{base.stem}_{counter}{base.suffix}"
            counter += 1
    ctx.planned_targets.add(str(target).lower())

    ctx.progress.log(f"\n{icon} {rel}: {reason}")
    relative_target = target.relative_to(source_path)

    if dry_run:
        ctx.plan_ops.append([str(file_path), str(target)])
        ctx.progress.log(f"  🔍 Would move to: {relative_target}")
        log_file.write(f"{folder_name}: would move {rel} -> "
                       f"{relative_target}\n")
        return

    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        transfer_file(file_path, target, "move")
        ctx.undo_entries.append([str(target), str(file_path)])
        _journal_append(ctx.undo_path, [str(target), str(file_path)])
        if folder_name == WRONG_EXT_FOLDER:
            # These also get their own undo record, so the renames can be
            # reversed on their own without touching the rest of the run
            ctx.rename_entries.append([str(target), str(file_path)])
        ctx.progress.log(f"  ➡️ Moved to: {relative_target}")
        log_file.write(f"{folder_name}: {rel} -> {relative_target}\n")
    except Exception as e:
        ctx.progress.log(f"  ❌ Could not set aside: {e}")
        log_file.write(f"ERROR setting aside {rel}: {e}\n")
        ctx.stats['errors'] += 1


def _process_one_file(file_path, source_path, settings, base_name_to_model,
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
        _set_aside_file(
            file_path, source_path, DUPLICATES_FOLDER,
            f"identical to {keeper.relative_to(source_path)}",
            ctx, settings, dry_run, log_file)
        return

    if settings.check_corrupt:
        status, reason = ctx.health.get(file_path, ('unchecked', ''))
        if status == 'damaged':
            _set_aside_file(file_path, source_path, CORRUPT_FOLDER,
                                 f"damaged - {reason}",
                                 ctx, settings, dry_run, log_file)
            return
    if settings.fix_extensions:
        status, reason = ctx.health.get(file_path, ('ok', ''))
        if status == 'misnamed':
            new_ext = canonical_extension_for(file_path)
            new_name = (file_path.stem + new_ext) if new_ext else None
            _set_aside_file(file_path, source_path, WRONG_EXT_FOLDER,
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

    target_folder = get_target_folder(source_path, file_path, camera_model,
                                           photo_date, settings, is_screenshot)
    target_path = target_folder / file_path.name

    # Recursive re-runs: files already in their correct spot are untouched
    if target_path == file_path:
        ctx.progress.log(f"\n✔️ Already organized: "
                 f"{file_path.relative_to(source_path)}")
        log_file.write(f"Already organized - skipped: {file_path.name}\n")
        ctx.stats['already_organized'] += 1
        return

    file_size_mb = file_path.stat().st_size / (1024 * 1024)
    ctx.stats['total_size_mb'] += file_size_mb

    ctx.progress.log(f"\n{file_type} Processing: {file_path.name} ({file_size_mb:.2f} MB)")
    log_file.write(f"Processing: {file_path.name}\n")
    if match_note:
        ctx.progress.log(match_note)
    if is_screenshot:
        ctx.stats['screenshots'] += 1

    # Update statistics
    ctx.stats['by_year'][str(photo_date.year)] = \
        ctx.stats['by_year'].get(str(photo_date.year), 0) + 1
    if camera_model:
        ctx.stats['by_model'][camera_model] = \
            ctx.stats['by_model'].get(camera_model, 0) + 1
    else:
        ctx.stats['no_metadata'] += 1

    # Handle duplicates. Name collisions with identical content are
    # skipped entirely; different content gets a _1/_2 rename.
    # (ctx.planned_targets makes dry-run renames accurate.)
    def claimed(path):
        return path.exists() or str(path).lower() in ctx.planned_targets

    if claimed(target_path):
        if target_path.exists() and files_identical(file_path, target_path):
            ctx.progress.log(f"  ♻️ Identical file already at destination, skipping"
                     f"{' (left in source)' if operation == 'move' and not dry_run else ''}")
            log_file.write("  Identical duplicate - skipped\n")
            ctx.stats['duplicates'] += 1
            return
        counter = 1
        while claimed(target_path):
            target_path = target_folder / f"{file_path.stem}_{counter}{file_path.suffix}"
            counter += 1
            if target_path.exists() and files_identical(file_path, target_path):
                ctx.progress.log("  ♻️ Identical file already at destination, skipping")
                log_file.write("  Identical duplicate - skipped\n")
                ctx.stats['duplicates'] += 1
                return
        ctx.progress.log(f"  🔄 Duplicate found, will rename to: {target_path.name}")
    ctx.planned_targets.add(str(target_path).lower())

    if not dry_run:
        target_folder.mkdir(parents=True, exist_ok=True)
        try:
            transfer_file(file_path, target_path, operation)
            ctx.undo_entries.append([str(target_path), str(file_path)])
            _journal_append(ctx.undo_path,
                                 [str(target_path), str(file_path)])
            relative_path = target_path.relative_to(source_path)
            ctx.progress.log(f"  ✅ {operation_text} to: {relative_path}")
            log_file.write(f"  {operation_text} to: {relative_path}\n")
            ctx.stats['processed'] += 1
        except Exception as e:
            ctx.progress.log(f"  ❌ File operation error: {e}")
            log_file.write(f"  FILE OPERATION ERROR: {e}\n")
            ctx.stats['errors'] += 1
    else:
        ctx.plan_ops.append([str(file_path), str(target_path)])
        relative_path = target_path.relative_to(source_path)
        ctx.progress.log(f"  🔍 Would {operation} to: {relative_path}")
        log_file.write(f"  Would {operation} to: {relative_path}\n")
        ctx.stats['processed'] += 1


def execute_cached_plan(plan, settings, progress):
    """Execute a cached preview plan without re-analyzing anything.

    Returns True if the plan ran; False if the folder no longer matches
    the preview fingerprint (caller then runs a fresh full analysis).
    """
    source_path = Path(settings.source)
    start_time = datetime.now()

    progress.status("Verifying folder is unchanged since preview...")
    try:
        regular, raw = collect_media_files(
            source_path, settings.include_subfolders,
            sniff_unknown=settings.fix_extensions)
    except OSError as e:
        progress.log(f"❌ Could not read source folder: {e}")
        return None
    if folder_fingerprint(regular + raw) != plan['fingerprint']:
        progress.log("⚠️ Folder changed since the preview - "
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
    log_filename = f"kjegla_media_log_{run_stamp}.txt"
    undo_path = source_path / f"kjegla_undo_{run_stamp}.jsonl"
    _journal_start(undo_path, operation, source_path)

    progress.log("=" * 60)
    progress.log(f"Kjegla's Photo Organizer - {operation_text.upper()} (cached preview)")
    progress.log(f"Source: {source_path}")
    progress.log(f"⚡ Folder unchanged since preview - executing "
             f"{len(ops)} planned operation(s) directly")
    progress.log("=" * 60)

    undo_entries = []
    total = len(ops)
    phase_start = time.monotonic()
    last_status_time = 0.0
    last_pct = -1

    with open(source_path / log_filename, 'w', encoding='utf-8') as log_file:
        log_file.write(f"Kjegla's Media Organization Log - {datetime.now()}\n")
        log_file.write(f"Source Folder: {source_path}\n")
        log_file.write(f"Mode: {operation.upper()} (cached preview replay)\n")
        log_file.write("=" * 60 + "\n\n")

        for idx, (src, dst) in enumerate(ops):
            if progress.cancelled:
                progress.log("\n⏹️ Operation cancelled by user")
                break

            pct = int((idx + 1) / total * 100) if total else 100
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
                progress.status(
                    f"Processing {idx + 1}/{total} • {rate:.0f} files/s "
                    f"• ETA {eta} • errors {stats['errors']}")
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
                    progress.log(f"  🔄 {base.name}: destination taken, "
                             f"renaming to {dst_p.name}")
                dst_p.parent.mkdir(parents=True, exist_ok=True)
                transfer_file(src_p, dst_p, operation)
                undo_entries.append([str(dst_p), str(src_p)])
                _journal_append(undo_path, [str(dst_p), str(src_p)])
                relative = dst_p.relative_to(source_path)
                progress.log(f"  ✅ {operation_text}: {src_p.name} → {relative}")
                log_file.write(f"{operation_text}: {src} -> {dst_p}\n")
                stats['processed'] += 1
            except Exception as e:
                progress.log(f"  ❌ {src_p.name}: {e}")
                log_file.write(f"ERROR {src}: {e}\n")
                stats['errors'] += 1

        if undo_entries:
            progress.notify("undo_available", str(undo_path))
        else:
            undo_path.unlink(missing_ok=True)

        if operation == "move" and settings.cleanup_empty:
            removed = _sweep_empty_dirs(source_path, log_file)
            stats['empty_folders_removed'] = removed
            if removed:
                progress.log(f"\n🧹 Removed {removed} empty folder(s)")

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
        progress.log(f"\n📄 Log file saved: {log_filename}")
        progress.log(f"\n✅ Operation complete! Files were {operation}d successfully.")
        if undo_entries:
            progress.log("↩️ This run can be undone with the 'Undo Last Run' button.")
        progress.status(
            f"Operation complete - {stats['processed']} files {operation}d")

    cached_plan = None
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
    progress.log("Kjegla's Photo Organizer - FILE HEALTH CHECK"
             f"{' (thorough)' if settings.corrupt_thorough else ''}")
    progress.log(f"Source: {source_path}")
    progress.log("Nothing will be moved, renamed or deleted.")
    progress.log("=" * 60)

    try:
        regular, raw = collect_media_files(source_path,
                                           settings.include_subfolders,
                                           sniff_unknown=True)
    except OSError as e:
        progress.log(f"❌ Could not read source folder: {e}")
        progress.status("Error reading source folder")
        return stats

    media_files = regular + raw
    stats['total_files'] = len(media_files)
    if not media_files:
        progress.log("No media files found in the source folder!")
        progress.status("No media files found")
        return stats

    progress.log(f"\n🩺 Checking {len(media_files)} file(s)...")
    health = _check_health(media_files, settings, progress, p0=0, p1=100)

    if progress.cancelled:
        progress.log("\n⏹️ Check cancelled by user")
        progress.status("Cancelled")
        return stats

    by_name = lambda p: str(p).lower()
    damaged = sorted((f for f, (s, _) in health.items() if s == 'damaged'),
                     key=by_name)
    misnamed = sorted((f for f, (s, _) in health.items() if s == 'misnamed'),
                      key=by_name)
    unchecked = [f for f, (s, _) in health.items() if s == 'unchecked']
    healthy = len(health) - len(damaged) - len(misnamed) - len(unchecked)
    stats['damaged'] = len(damaged)
    stats['misnamed'] = len(misnamed)
    stats['unchecked'] = len(unchecked)
    stats['processed'] = len(health)

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
        progress.log(f"⚠️ Could not write the report file: {e}")

    progress.log("\n" + "=" * 60)
    progress.log("RESULTS:")
    progress.log(f"✅ Fine: {healthy}")
    progress.log(f"🩹 Damaged: {len(damaged)}")
    progress.log(f"🏷️ Wrong file extension: {len(misnamed)} "
             f"(contents are fine - renaming fixes these)")
    progress.log(f"❓ Could not be checked: {len(unchecked)} "
             f"(RAW / some video formats - not a sign they are broken)")

    if damaged:
        progress.log("\nDamaged files:")
        for path in damaged[:200]:
            progress.log(f"  🩹 {path.relative_to(source_path)} - "
                     f"{health[path][1]}")
        if len(damaged) > 200:
            progress.log(f"  ... and {len(damaged) - 200} more "
                     f"(the full list is in the report file)")
    else:
        progress.log("\n🎉 No damaged files found.")

    if misnamed:
        progress.log("\nWrong file extension (these are NOT damaged - the "
                 "contents are fine, the name just lies about the format):")
        for path in misnamed[:100]:
            progress.log(f"  🏷️ {path.relative_to(source_path)} - "
                     f"{health[path][1]}")
        if len(misnamed) > 100:
            progress.log(f"  ... and {len(misnamed) - 100} more "
                     f"(the full list is in the report file)")

    if damaged or misnamed:
        progress.log("\n💡 Tick 'Check files for damage while organizing' to have "
                 f"damaged files moved into a '{CORRUPT_FOLDER}' folder and "
                 f"misnamed ones into '{WRONG_EXT_FOLDER}' on the next run.")

    duration = (datetime.now() - start_time).total_seconds()
    stats['duration_seconds'] = duration
    progress.log(f"\nDuration: {duration:.1f} seconds")
    progress.log(f"📄 Report saved: {report_name}")
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
    progress.log(f"↩️ UNDOING {label or ('last ' + op + ' run')} ({total} files)")
    progress.log("=" * 60)

    for idx, (target, original) in enumerate(entries):
        if progress.cancelled:
            progress.log("\n⏹️ Undo cancelled by user")
            break
        target_p, original_p = Path(target), Path(original)
        try:
            if op == "move":
                if not target_p.exists():
                    progress.log(f"  ⚠️ Missing, cannot restore: {target}")
                    problems += 1
                elif original_p.exists():
                    progress.log(f"  ⚠️ Original location occupied, skipping: {original}")
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
                    progress.log(f"  ⚠️ The original is gone, so this copy is "
                             f"now the only one - keeping it: {target}")
                    problems += 1
                elif not files_identical(target_p, original_p):
                    progress.log(f"  ⚠️ Changed since it was copied - keeping "
                             f"it: {target}")
                    problems += 1
                else:
                    os.remove(str(target_p))
                    restored += 1
        except Exception as e:
            progress.log(f"  ❌ {target}: {e}")
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
    progress.log(f"\n✅ Undo complete: {restored} files {verb}, "
             f"{problems} issue(s), {removed} empty folder(s) cleaned up")
    progress.status(f"Undo complete - {restored} files {verb}")
