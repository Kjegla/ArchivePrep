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


def say(progress, log_file, message):
    """Report one event, in one wording, to both places it gets read.

    The window and archiveprep_log_*.txt used to be written by two separate
    calls holding two separately-worded strings, and they drifted apart: the
    window said "[error] processing X" where the file said "ERROR processing
    X", and "[plan] would move to" where the file said "Would move to". So
    reading the file and then searching the window for the line you had just
    read found nothing - the text genuinely was not there.

    One string written to two destinations cannot drift. Where the window
    still needs something the file does not - a separator rule, a blank line
    - call progress.log directly and mean it.
    """
    progress.log(message)
    log_file.write(message.lstrip('\n') + "\n")


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


# Cameras and phone apps that stamp the capture time into the filename, and
# the only names a date is ever read from.
#
# Anchored to the start on purpose. A date found loose inside a name is as
# likely to be wrong as right: a real archive turned up 30 files called
# "Facetune_03-03-2019-21-05-08", which is DD-MM-YYYY and would be read as
# an entirely different month and day. A time of day is required as well,
# because that is what tells a capture stamp apart from a number that merely
# has eight digits in it.
#
# Messaging apps are deliberately absent - "IMG-20230510-WA0001" and
# "signal-2024-01-15" are real shapes, but they change between app versions,
# and the whole value of this list is that every entry is a scheme its maker
# publishes and keeps.
FILENAME_DATE_PATTERNS = (
    # Pixel and most Android cameras:
    #   IMG_20200904_144311  VID_20200904_144311  PXL_20251103_092233580
    re.compile(r'^(?:IMG|VID|MVIMG|PXL|PANO|BURST)[-_]'
               r'(\d{4})(\d{2})(\d{2})[-_](\d{2})(\d{2})(\d{2})'),
    # Samsung, and several export tools:  20240610_101512
    re.compile(r'^(\d{4})(\d{2})(\d{2})_(\d{2})(\d{2})(\d{2})'),
    # Android screenshots:  Screenshot_20240301-142205
    re.compile(r'^Screenshot[-_](\d{4})(\d{2})(\d{2})[-_](\d{2})(\d{2})(\d{2})'),
    # Screenshot_2024-03-01-14-22-05
    re.compile(r'^Screenshot[-_](\d{4})-(\d{2})-(\d{2})'
               r'[-_](\d{2})[-_.](\d{2})[-_.](\d{2})'),
    # macOS:  Screen Shot 2024-03-01 at 14.22.05
    re.compile(r'^Screen[ _]?[Ss]hot[-_ ](\d{4})-(\d{2})-(\d{2})'
               r'[ _]at[ _](\d{2})[.-](\d{2})[.-](\d{2})'),
)


def date_from_filename(file_path):
    """The capture time a camera wrote into the filename, or None.

    The last question asked before giving up, and only ever reached once the
    file's own metadata, its Takeout sidecar and the photo it was captured
    with have all come up empty. So the only thing it can displace is a guess.

    That is what makes it worth doing. On an export the file's modified time
    is the day it was downloaded - one real collection had 7,511 photos
    filed under five days in 2026 for exactly that reason - and a thousand of
    them were carrying their true date in their own name the whole time.

    Only the patterns above are trusted, and only at the start of the name.
    Anything else is None: an honest Unknown Date beats a month read out of a
    number that happened to look like one.
    """
    name = file_path.name
    for pattern in FILENAME_DATE_PATTERNS:
        match = pattern.match(name)
        if not match:
            continue
        try:
            taken = datetime(*(int(part) for part in match.groups()))
        except ValueError:
            continue        # 13 months, the 31st of February, and near misses
        # A camera cannot have taken a photo tomorrow, and a "date" before
        # digital cameras existed is somebody's serial number, not a day.
        if datetime(1990, 1, 1) <= taken <= datetime.now():
            return taken
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
# Nothing writes this folder any more - renaming files whose extension
# disagreed with their contents was removed, because it solved none of the
# problems this application exists for. It stays in the skip list because
# archives organized by an older build still have one, and those files
# should be left exactly where that build put them.
LEGACY_WRONG_EXT_FOLDER = "Wrong Extension"
SET_ASIDE_FOLDERS = {DUPLICATES_FOLDER.lower(), CORRUPT_FOLDER.lower(),
                     LEGACY_WRONG_EXT_FOLDER.lower()}

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


# Magic bytes, used to tell what a file actually is.
#
# Two jobs, both load-bearing. Google Takeout truncates long filenames and
# sometimes chops the extension clean off, so without this those files are
# invisible to the application entirely. And a file is health-checked as what
# it is rather than as what it is called, so a photo that arrived named .dng
# is not handed to a video structure check.
#
# Nothing here renames anything. Each entry is (signature, description,
# extensions it is valid for, the extension that names this format).
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


def file_health(file_path, thorough=False, probe=None):
    """Would this file be safe to put in an archive?

    Returns (status, reason) where status is:
      'healthy' - opened and passed every check we can apply
      'damaged' - definitely broken (empty, unreadable, or truncated)
      'unknown' - a format we cannot verify (most RAW, AVI/MKV/WMV...).
                  Said honestly rather than guessed at, so a good file is
                  never wrongly called broken.

    Three answers, and only three, because there is only one question. They
    are the words the window uses too - a translation layer between what the
    code calls a thing and what the user reads is how the window and the log
    drifted apart once already.

    It used to return a fourth, 'misnamed', for a file whose extension
    disagreed with its contents - which is a fact about a filename, not about
    whether the file is safe to keep. Worse, it returned early, so those files
    were never health-checked at all: 804 of them in one real collection.

    Never raises.
    """
    try:
        size = file_path.stat().st_size
    except OSError as e:
        return 'damaged', f"unreadable ({e})"
    if size == 0:
        return 'damaged', "empty file (0 bytes)"

    # Check the file as what it is, not as what it is called. Cloud exports
    # re-encode on the way out and keep the old name, so a photo can arrive
    # called .dng - and handing that to an MP4 structure check reports
    # nonsense. This decides *how to verify* a file and nothing else: nothing
    # is renamed, and nothing is filed anywhere on the strength of it.
    # A caller that has already read the header passes it in rather than
    # making us read it again.
    real_format, _valid_exts, canonical = probe or probe_file(file_path)
    suffix = (canonical or file_path.suffix).lower()

    if suffix in MVHD_CAPABLE_EXTS:
        ok, reason = _mp4_structure_ok(file_path)
        return ('healthy', "") if ok else ('damaged', reason)

    if suffix in VIDEO_EXTS:
        return 'unknown', "video format we cannot verify without decoding"

    if suffix in RAW_EXTS:
        return 'unknown', "RAW format we cannot verify"

    if suffix not in IMAGE_EXTS:
        return 'unknown', "unrecognized format"

    if not PIL_AVAILABLE:
        return 'unknown', "Pillow is not installed"

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

    return 'healthy', ""


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


# macOS writes a companion "._Photo.jpg" beside a file whenever it copies to a
# volume that cannot hold its metadata natively - FAT32 and exFAT memory cards,
# USB sticks, many network shares. They are small metadata stubs.
APPLEDOUBLE_MAGIC = b'\x00\x05\x16\x07'


def is_appledouble(file_path):
    """True if this is one of macOS's own metadata companion files.

    Judged by content, not by name. A photo really could be called
    "._holiday.jpg", and calling it damaged because of its name would be the
    kind of guess this application does not make - so the name only decides
    which files are worth reading four bytes of.

    Without this they are collected as images (the suffix says .jpg), handed
    to Pillow, and reported as damaged - a false positive in the one check
    that exists because of a genuinely corrupted export, on any collection
    that has ever passed through an SD card.
    """
    if not file_path.name.startswith('._'):
        return False
    try:
        with open(file_path, 'rb') as f:
            return f.read(4) == APPLEDOUBLE_MAGIC
    except OSError:
        return False


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
                 if not f.name.startswith(('kjegla_', 'archiveprep_'))
                 and not is_appledouble(f)]

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
            settings.cleanup_empty)


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
    # Recorded, never acted on. The modified time is not a capture date - see
    # date_from_filename() - but throwing it away would leave no way to check
    # that judgement afterwards. In the manifest you can see for yourself
    # whether a collection's timestamps are the day it was downloaded or
    # something worth going back for.
    modified: float = 0.0

    # what it is
    kind: str = 'image'                    # image | video | raw
    camera_model: str = None
    captured_at: datetime = None
    # Where the date came from, best evidence first. The file's modified time
    # is deliberately not on this list: see date_from_filename().
    date_source: str = 'none'  # exif|video|sidecar|capture|filename|unknown|none
    is_screenshot: bool = False

    # can it be trusted - exactly what file_health() said
    verdict: str = 'unknown'             # healthy | damaged | unknown
    verdict_reason: str = ''

    # what the run decided
    duplicate_of: Path = None
    content_hash: bytes = None
    capture_id: str = ''                   # files from one shutter press
    action: str = ''                       # organize | duplicate | corrupt |
                                           # skipped
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
    kind: str                              # organize | duplicate | corrupt
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
        'unknown': 0,
        # ...and what was decided about them, which is not the same number.
        # Setting a damaged file aside is gated on the user's setting, and a
        # file that is also a duplicate goes to Duplicates instead. The
        # summary reports this, not the count above, or it claims work it was
        # told not to do.
        'damaged_aside': 0,
        # Where the dates came from, for the two rungs worth saying out loud
        'dated_by_filename': 0,
        'no_date': 0,
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
    if mode == "none":
        target_folder = base_folder
    elif photo_date is None:
        # No date could be established from anything - not metadata, not a
        # capture sibling, not even the file's own modified time. "Unknown
        # Date" for the same reason as "Unknown Camera": a folder saying so
        # is worth more than a year picked to fill the gap.
        target_folder = base_folder / "Unknown Date"
    elif mode == "year":
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
            stat = path.stat()
            mf.size, mf.modified = stat.st_size, stat.st_mtime
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


def _check_health(records, settings, progress, p0=25, p1=40):
    """Fill in each record's verdict. Parallel when enabled - it is I/O bound,
    so extra threads help even in quick mode."""
    total = len(records)
    if total == 0:
        return
    thorough = settings.corrupt_thorough
    label = "Thorough check" if thorough else "Checking files"
    last_note = [0.0]
    done = [0]

    def note():
        done[0] += 1
        now = time.monotonic()
        if now - last_note[0] >= 0.2 or done[0] == total:
            progress.percent(p0 + int(done[0] / total * (p1 - p0)))
            progress.status(f"{label} {done[0]}/{total}")
            last_note[0] = now

    values = list(records.values())
    if settings.use_multithreading and total > 1:
        with ThreadPoolExecutor(max_workers=settings.max_threads) as executor:
            futures = {executor.submit(file_health, mf.path, thorough): mf
                       for mf in values}
            for future in as_completed(futures):
                if progress.cancelled:
                    executor.shutdown(wait=False, cancel_futures=True)
                    break
                mf = futures[future]
                try:
                    mf.verdict, mf.verdict_reason = future.result()
                except Exception as e:
                    mf.verdict, mf.verdict_reason = 'damaged', f"check failed ({e})"
                note()
    else:
        for mf in values:
            if progress.cancelled:
                break
            mf.verdict, mf.verdict_reason = file_health(mf.path, thorough)
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
    health_rank = {'healthy': 0, 'unknown': 1, 'damaged': 2}.get(mf.verdict, 1)
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


def mtime_text(stamp):
    """A file's modified time, readable, or blank when it has none we can
    express. Windows refuses timestamps outside its range and real archives
    hold them - a card whose clock was never set, a byte-level recovery."""
    if not stamp:
        return ''
    try:
        return datetime.fromtimestamp(stamp).isoformat(' ', 'seconds')
    except (OSError, ValueError, OverflowError):
        return ''


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
               'date_source', 'modified', 'is_screenshot', 'verdict',
               'verdict_reason', 'duplicate_of', 'capture_id']
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
                    mf.date_source, mtime_text(mf.modified),
                    'yes' if mf.is_screenshot else 'no',
                    mf.verdict, mf.verdict_reason,
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


def _write_run_summary(progress, log_file, stats, duration, dry_run):
    """The end-of-run tally, to the window and to the log file alike."""
    # One summary, written to both. It used to be written twice with two sets
    # of labels - "Total media files" in the window against "Total files" in
    # the file - which made the two disagree about the same run.
    #
    # A preview says "would" and a real run says "did". The summary was the
    # last part of a dry run still written as though it had already happened,
    # under a header that says DRY RUN and above a log where every line says
    # "would move to".
    tense = lambda would, did: would if dry_run else did     # noqa: E731

    progress.log("\n" + "=" * 60)
    log_file.write("\n" + "=" * 60 + "\n")
    say(progress, log_file, "SUMMARY:")
    say(progress, log_file, f"Total media files: {stats['total_files']}")
    say(progress, log_file,
        f"{tense('Files that would be processed', 'Files processed')}: "
        f"{stats['processed']}")
    say(progress, log_file, f"Files without metadata: {stats['no_metadata']}")
    say(progress, log_file, f"Screenshots detected: {stats['screenshots']}")
    if stats['content_duplicates']:
        wasted = stats['duplicate_bytes'] / (1024 * 1024)
        say(progress, log_file,
            f"Duplicate copies {tense('to set aside', 'set aside')}: "
            f"{stats['content_duplicates']} "
            f"({wasted:.2f} MB) -> '{DUPLICATES_FOLDER}' folder")
    if stats['damaged']:
        # Found and dealt with are two different numbers. Setting a damaged
        # file aside only happens when the user asked for damage checking.
        if stats['damaged_aside']:
            say(progress, log_file, f"Damaged files found: {stats['damaged']} "
                f"-> {tense('would go to', 'moved to')} "
                f"'{CORRUPT_FOLDER}' folder")
        else:
            say(progress, log_file, f"Damaged files found: {stats['damaged']} "
                f"- left where they are, because 'Check files for damage' "
                f"is off")
    if stats['dated_by_filename']:
        say(progress, log_file,
            f"Dated from their filename: {stats['dated_by_filename']} "
            f"(nothing in the file itself said when it was taken)")
    if stats['no_date']:
        say(progress, log_file,
            f"No readable date: {stats['no_date']} -> "
            f"{tense('would go to', 'filed under')} 'Unknown Date'")
    if stats['unknown']:
        say(progress, log_file,
            f"Unknown health: {stats['unknown']} "
            f"(RAW / some video formats - not a sign they are broken)")
    if stats['duplicates']:
        say(progress, log_file,
            f"Identical duplicates {tense('to skip', 'skipped')}: "
            f"{stats['duplicates']}")
    if stats['already_organized']:
        say(progress, log_file,
            f"Already organized (untouched): {stats['already_organized']}")
    if stats['empty_folders_removed']:
        say(progress, log_file,
            f"Empty folders removed: {stats['empty_folders_removed']}")
    say(progress, log_file, f"Errors: {stats['errors']}")
    say(progress, log_file, f"Total size: {stats['total_size_mb']:.2f} MB")
    say(progress, log_file, f"Duration: {duration:.1f} seconds")

    if stats['by_model']:
        say(progress, log_file, "\nFiles per camera model:")
        for model, count in sorted(stats['by_model'].items()):
            say(progress, log_file, f"  {model}: {count} files")

    if stats['no_metadata'] > 0:
        say(progress, log_file,
            f"\nUnknown/unmatched files: {stats['no_metadata']} "
            f"({tense('would be moved to', 'moved to')} "
            f"'Unknown Camera' folder)")
    if stats['screenshots'] > 0:
        say(progress, log_file,
            f"\nScreenshots {tense('to separate', 'separated')}: "
            f"{stats['screenshots']} files")


def _run_header(settings, dry_run):
    """What this run was asked to do, as the lines both the window and the log
    file get. Built once here so a fresh run and a replayed preview cannot
    describe the same settings differently."""
    operation = settings.operation
    yes_no = lambda flag: 'Yes' if flag else 'No'          # noqa: E731
    return [
        f"ArchivePrep - {'DRY RUN' if dry_run else operation.upper()}",
        f"Source: {Path(settings.source)}",
        f"Operation: {operation}",
        f"Subfolder mode: {settings.subfolder_mode}",
        f"Separate RAW files: {yes_no(settings.separate_raw)}",
        f"Separate Screenshots: {yes_no(settings.separate_screenshots)}",
        f"Include subfolders: {yes_no(settings.include_subfolders)}",
        f"Multithreading: {yes_no(settings.use_multithreading)}",
        f"Find duplicates by content: {yes_no(settings.dedupe_content)}",
        "Check files for damage: "
        + (('Yes (thorough)' if settings.corrupt_thorough else 'Yes')
           if settings.check_corrupt else 'No'),
        f"Delete empty folders: {yes_no(settings.cleanup_empty)}",
    ]


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

    # Shown to the window now, because the settings are worth seeing before a
    # long scan starts; written into the log file further down, once it exists.
    header = _run_header(settings, dry_run)
    progress.log("=" * 60)
    for line in header:
        progress.log(line)
    progress.log("=" * 60)

    # The run log is only opened once the scan has finished, so everything the
    # scan phase says would otherwise reach the window and nothing else - and
    # some of it is a finding worth keeping, not just status ("N file(s) took
    # their date from the photo they were captured with"). Held here, written
    # into the file the moment it exists.
    preamble = list(header)

    def report(message):
        """Tell the window now, and the run log as soon as there is one."""
        progress.log(message)
        preamble.append(message)

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
        report("No media files found in the source folder!")
        progress.status("No media files found")
        return stats, cached_plan

    report("\nReading media metadata...")
    records = _scan_files(media_files, settings, progress)
    if progress.cancelled:
        report("\nOperation cancelled by user")
        progress.status("Cancelled")
        return stats, cached_plan

    if settings.check_corrupt:
        report("\nChecking files for damage"
               f"{' (thorough)' if settings.corrupt_thorough else ''}...")
        _check_health(records, settings, progress)
        if progress.cancelled:
            report("\nOperation cancelled by user")
            progress.status("Cancelled")
            return stats, cached_plan
        verdicts = [mf.verdict for mf in records.values()]
        stats['damaged'] = verdicts.count('damaged')
        stats['unknown'] = verdicts.count('unknown')
        report(f"  {verdicts.count('healthy')} healthy, "
               f"{stats['damaged']} damaged, {stats['unknown']} unknown")

    # Files from one shutter press belong together, and share a date
    captures = _group_captures(records)
    shared_dates = _share_capture_dates(captures, progress)
    if shared_dates:
        report(f"  {shared_dates} file(s) took their date from the photo "
               f"they were captured with")

    duplicate_groups = []
    if settings.dedupe_content:
        report("\nLooking for duplicate files by content...")
        duplicate_groups = _find_duplicates(records, settings, progress)
        duplicate_groups += _find_embedded_clips(captures, progress)
        if progress.cancelled:
            report("\nOperation cancelled by user")
            progress.status("Cancelled")
            return stats, cached_plan
        dups = [mf for mf in records.values() if mf.duplicate_of is not None]
        stats['content_duplicates'] = len(dups)
        stats['duplicate_bytes'] = sum(mf.size for mf in dups)
        if duplicate_groups:
            wasted = stats['duplicate_bytes'] / (1024 * 1024)
            report(f"  Found {len(duplicate_groups)} set(s) of identical "
                   f"files - {len(dups)} extra copies ({wasted:.2f} MB)")
        else:
            report("  No duplicates found")
    else:
        progress.percent(60)

    base_name_to_model = _build_model_index(records)
    report(f"Found {len(base_name_to_model)} images with camera metadata")
    report(f"Found {len(raw_files)} RAW files and "
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
        report(f"Duplicate report saved: {report_name}")

    with open(log_path, 'w', encoding='utf-8') as log_file:
        log_file.write(f"ArchivePrep run log - {datetime.now()}\n")
        for line in preamble:
            log_file.write(line.lstrip('\n') + "\n")
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
                say(progress, log_file, f"\n[error] processing {mf.path.name}: {e}")
                say(progress, log_file, "   Skipping this file and continuing...")
                stats['errors'] += 1

        # What the summary claims happened has to be what was decided here.
        # This used to be the scan's count - how many files LOOK damaged -
        # while setting them aside is gated on a setting the user can turn
        # off. Counted before APPLY on purpose: a move that then fails gets
        # its own honest block from _report_failures, and is not quietly
        # subtracted from here.
        actions = [mf.action for mf in records.values()]
        stats['damaged_aside'] = actions.count('corrupt')
        # The bottom two rungs of the date ladder, counted here because that
        # is where they are decided. Both change where a file lands, so both
        # are worth saying rather than leaving to be discovered by browsing.
        sources = [mf.date_source for mf in records.values()]
        stats['dated_by_filename'] = sources.count('filename')
        stats['no_date'] = sources.count('unknown')

        # ---- APPLY -------------------------------------------------------
        undo_entries = []
        if not dry_run:
            _journal_start(undo_path, operation, source_path)
            undo_entries, failures = _apply_plan(
                ctx.ops, source_path, progress, stats, log_file, undo_path,
                records=records, p0=80, p1=100)
            _report_failures(failures, source_path, progress, log_file)

            if undo_entries:
                progress.notify("undo_available", str(undo_path))
            else:
                undo_path.unlink(missing_ok=True)

            if operation == "move" and settings.cleanup_empty:
                removed = _sweep_empty_dirs(source_path, log_file)
                stats['empty_folders_removed'] = removed
                if removed:
                    progress.log(f"\nRemoved {removed} empty folder(s)")
                _note_leftover_folders(source_path, progress, log_file)

        duration = (datetime.now() - start_time).total_seconds()
        stats['duration_seconds'] = duration
        _write_run_summary(progress, log_file, stats, duration, dry_run)
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
                # Carried so a replay can write the same manifest a fresh run
                # writes. It is a reference to records that already exist, not
                # a copy - the cost is keeping the scan's memory alive between
                # Preview and Execute, which is the point of caching at all.
                'records': records,
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
        except OSError as e:
            # In use, permission denied, or raced. Say so rather than leaving
            # the user with a folder that looks empty and no reason why.
            if log_file:
                log_file.write(f"Could not remove empty folder "
                               f"{folder.relative_to(source_root)}: {e}\n")
    return removed


# Files an operating system regenerates by itself. Nothing is lost by deleting
# one, which is the entire reason this list exists and is the only reason the
# user is ever offered the option. Deliberately excluded: .aae, .xmp and
# .json. Those are sidecars holding real information about a photo - Apple's
# edit data, ratings, geolocation - and nothing regenerates them.
REGENERABLE_LEFTOVERS = {'thumbs.db', 'ehthumbs.db', 'desktop.ini', '.ds_store'}

# Sidecars that only the device which wrote them can read. An .aae is Apple's
# adjustment data - the edits you made in Photos - and nothing outside Apple's
# ecosystem understands it: not Windows, not a NAS, not Immich. A .thm is a
# camcorder's thumbnail for its own browser. Once the photo has moved to an
# archive, neither travels with any meaning.
#
# Deliberately NOT here, and each for its own reason:
#   .xmp   Lightroom and darktable read these, and they hold ratings and edits
#          that are actively used outside the camera.
#   .json  ArchivePrep itself reads these for a capture date Google stripped
#          from the file. Deleting them would degrade a later re-run.
INERT_SIDECARS = {'.aae', '.thm'}


def is_regenerable_leftover(path):
    """True if a file is an operating system's own cache, not the user's data."""
    return path.name.lower() in REGENERABLE_LEFTOVERS or is_appledouble(path)


def is_inert_sidecar(path):
    """True if a file describes a photo but only its originating device can
    read it - so once the photo is archived elsewhere, it means nothing."""
    return path.suffix.lower() in INERT_SIDECARS


def find_leftover_only_folders(source_root):
    """Folders holding nothing the archive wants: caches, or sidecars whose
    photo has already been moved away.

    After a move these look empty in Explorer and in `dir` - Thumbs.db is
    hidden - but the sweep correctly refuses to touch them, because a folder
    with a file in it is not empty and this application does not decide which
    of your files do not count.

    Returned so the window can *ask*. Nothing here deletes anything.
    """
    found = []
    source_root = Path(source_root)
    for dirpath, _dirs, _names in os.walk(source_root, topdown=False):
        folder = Path(dirpath)
        if folder == source_root:
            continue
        try:
            entries = list(folder.iterdir())
        except OSError:
            continue
        if entries and all(e.is_file() and (is_regenerable_leftover(e)
                                            or is_inert_sidecar(e))
                           for e in entries):
            found.append((folder, entries))
    return found


def remove_leftovers(folders, progress, include_sidecars=False):
    """Delete leftover files in these folders, then the folders themselves.

    The only place this application deletes a file, and it runs only after the
    user has been shown the list and said yes.

    Caches go by default - the operating system recreates them on demand.
    Sidecars are a separate decision the window asks separately, because they
    are not regenerable: an .aae is only redundant once you know the edits are
    already baked into the exported photo, and that is the user's knowledge,
    not ours. A folder is only removed once it is genuinely empty, so
    declining the sidecars leaves both them and their folder alone.

    Returns (files_deleted, folders_removed).
    """
    files_deleted = folders_removed = 0
    for folder, entries in folders:
        try:
            for entry in entries:
                if is_regenerable_leftover(entry) or (
                        include_sidecars and is_inert_sidecar(entry)):
                    entry.unlink()
                    files_deleted += 1
            if not any(folder.iterdir()):
                folder.rmdir()
                folders_removed += 1
        except OSError as e:
            progress.log(f"  [warn] could not clear {folder.name}: {e}")
    return files_deleted, folders_removed


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
                    settings, dry_run, log_file):
    """Decide to put a duplicate or a damaged file aside.

    The original folder structure is mirrored inside Duplicates/ or Corrupt/,
    so anything can be put back by hand, and nothing is ever renamed or
    deleted on the way.
    """
    rel = mf.path.relative_to(source_path)
    tag = {DUPLICATES_FOLDER: "[duplicate]",
           CORRUPT_FOLDER: "[damaged]"}.get(folder_name, "[aside]")

    if settings.operation == "copy":
        # Copy mode leaves the source untouched and the good original is
        # already there, so making a third copy of a redundant or broken
        # file would only add clutter.
        say(ctx.progress, log_file, f"\n{tag} {rel}: {reason}")
        say(ctx.progress, log_file,
            "  [skip] left where it is (copy mode never touches the source)")
        mf.action, mf.reason = 'skipped', reason
        return

    target = source_path / folder_name / rel

    if _claimed(ctx, target):
        if _same_as_whatever_lands_at(ctx, mf.path, target):
            say(ctx.progress, log_file,
                f"\n{tag} {rel}: already set aside in {folder_name}/, "
                f"leaving it alone")
            mf.action, mf.reason = 'skipped', "already set aside"
            return
        base = target
        counter = 1
        while _claimed(ctx, target):
            target = base.parent / f"{base.stem}_{counter}{base.suffix}"
            counter += 1
    ctx.planned[str(target).lower()] = mf.path

    say(ctx.progress, log_file, f"\n{tag} {rel}: {reason}")
    relative_target = target.relative_to(source_path)
    mf.action, mf.target, mf.reason = kind, target, reason
    ctx.ops.append(Operation(source=mf.path, target=target, kind=kind,
                             operation="move"))

    if dry_run:
        say(ctx.progress, log_file,
            f"  [plan] would move to: {relative_target}")


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

    # Last rung of the date ladder. EXIF, the Takeout sidecar and the photo
    # this one was captured with have all been asked by now; the name is what
    # is left. If it says nothing either, the date is honestly unknown.
    #
    # The file's modified time used to sit here and no longer does. On any
    # collection that arrived as a download it is the day it was extracted,
    # not the day the photo was taken - 7,511 files in one real archive were
    # filed under five days in 2026 on that basis. A wrong year disappears
    # into an archive forever; Unknown Date is a pile you can come back to.
    if mf.captured_at is None:
        mf.captured_at = date_from_filename(file_path)
        mf.date_source = 'filename' if mf.captured_at else 'unknown'

    target_folder = get_target_folder(source_path, file_path, mf.camera_model,
                                      mf.captured_at, settings, separate_shot)
    target_path = target_folder / file_path.name

    # Recursive re-runs: files already in their correct spot are untouched
    if target_path == file_path:
        say(ctx.progress, log_file,
            f"\n[skip] already organized: {file_path.relative_to(source_path)}")
        ctx.stats['already_organized'] += 1
        mf.action, mf.reason = 'skipped', "already organized"
        return

    file_size_mb = mf.size / (1024 * 1024)
    ctx.stats['total_size_mb'] += file_size_mb

    say(ctx.progress, log_file,
        f"\n{file_type} {file_path.name} ({file_size_mb:.2f} MB)")
    if match_note:
        say(ctx.progress, log_file, match_note)
    if separate_shot:
        ctx.stats['screenshots'] += 1

    # Update statistics
    year = str(mf.captured_at.year) if mf.captured_at else "Unknown"
    ctx.stats['by_year'][year] = ctx.stats['by_year'].get(year, 0) + 1
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
            say(ctx.progress, log_file,
                "  [skip] identical file already at destination"
                f"{' (left in source)' if settings.operation == 'move' else ''}")
            ctx.stats['duplicates'] += 1
            mf.action, mf.reason = 'skipped', "identical file already at destination"
            return
        counter = 1
        while _claimed(ctx, target_path):
            target_path = target_folder / f"{file_path.stem}_{counter}{file_path.suffix}"
            counter += 1
            if _same_as_whatever_lands_at(ctx, file_path, target_path):
                say(ctx.progress, log_file,
                    "  [skip] identical file already at destination")
                ctx.stats['duplicates'] += 1
                mf.action, mf.reason = 'skipped', "identical file already at destination"
                return
        say(ctx.progress, log_file,
            f"  [rename] target taken, will use: {target_path.name}")
    ctx.planned[str(target_path).lower()] = file_path

    mf.action, mf.target = 'organize', target_path
    ctx.ops.append(Operation(source=file_path, target=target_path,
                             kind='organize', operation=settings.operation))

    if dry_run:
        relative_path = target_path.relative_to(source_path)
        say(ctx.progress, log_file,
            f"  [plan] would {settings.operation} to: {relative_path}")
        ctx.stats['processed'] += 1


def _note_leftover_folders(source_path, progress, log_file):
    """Say which folders were left holding only an operating system's cache,
    and hand the list to the window so it can offer to clear them.

    These look empty in Explorer, because Thumbs.db is hidden, and empty to
    `dir` for the same reason - so being left behind with no explanation is
    genuinely baffling. Nothing is deleted here.
    """
    leftovers = find_leftover_only_folders(source_path)
    if not leftovers:
        return
    say(progress, log_file,
        f"\n{len(leftovers)} folder(s) are empty apart from files "
        f"Windows or macOS regenerate by themselves:")
    for folder, entries in leftovers[:20]:
        names = ", ".join(sorted(e.name for e in entries))
        rel = folder.relative_to(source_path)
        say(progress, log_file, f"  {rel}  ({names})")
    if len(leftovers) > 20:
        say(progress, log_file, f"  ... and {len(leftovers) - 20} more")
    say(progress, log_file,
        "They were left alone because a folder with a file in it is not empty,")
    say(progress, log_file,
        "and this application does not decide which of your files do not count.")
    progress.notify("leftover_folders", leftovers)


def _report_failures(failures, source_path, progress, log_file):
    """Gather what went wrong into one block at the end of the run.

    Individual errors scroll past in the middle of hundreds of successful
    moves, and a summary line saying "Errors: 17" leaves the user to scroll
    back and work out what they have in common. This says it once, at the
    end, where it will be read.
    """
    if not failures:
        return

    denied = [(p, e) for p, e in failures if isinstance(e, PermissionError)
              or getattr(e, 'winerror', None) == 5]
    other = [(p, e) for p, e in failures if (p, e) not in denied]

    progress.log("\n" + "=" * 60)
    say(progress, log_file,
        f"{len(failures)} file(s) could not be moved and are still "
        f"where they were:")

    if denied:
        say(progress, log_file,
            f"\n  {len(denied)} refused by Windows (access denied).")
        for line in (
                "  This usually means the files were copied from another "
                "computer and",
                "  still carry that computer's permissions - your account "
                "can read them",
                "  but not move them. Deleting one in Explorer would raise a "
                "UAC prompt;",
                "  this application does not ask for administrator rights and "
                "will not",
                "  change permissions on your files by itself."):
            say(progress, log_file, line)
        say(progress, log_file,
            "\n  To take ownership, in an administrator terminal:")
        folder = denied[0][0].parent
        say(progress, log_file, f'    takeown /F "{folder}" /R /D Y')
        say(progress, log_file,
            f'    icacls "{folder}" /grant "%USERNAME%":(OI)(CI)F /T')

    for label, group in (("access denied", denied), ("other errors", other)):
        if not group:
            continue
        say(progress, log_file, f"\n  {label}:")
        for path, err in group[:20]:
            try:
                shown = path.relative_to(source_path)
            except ValueError:
                shown = path
            say(progress, log_file, f"    {shown}: {err}")
        if len(group) > 20:
            # The one place the file deliberately says more than the window:
            # it carries the whole list where the window shows the first 20.
            progress.log(f"    ... and {len(group) - 20} more "
                         f"(the full list is in the log file)")
            for path, err in group[20:]:
                try:
                    shown = path.relative_to(source_path)
                except ValueError:
                    shown = path
                log_file.write(f"    {shown}: {err}\n")


def _apply_plan(ops, source_path, progress, stats, log_file, undo_path,
                records=None, p0=60, p1=100):
    """Carry out a plan. The only code in the application that moves a file.

    Preview and Execute run exactly the same decisions and then either stop
    here or come through here - which is what stops the two drifting apart.
    They used to be separate engines, and had already grown different
    collision handling.

    `records` lets a failure be written back onto the file's record, so the
    manifest says what happened rather than what was intended. Without it a
    file that could not be moved was still listed as organized, at a target
    it never reached - which is the one thing a record of the run must never
    do. A cached replay has no records, so it passes None.

    Returns (undo_entries, failures).
    """
    undo_entries, failures = [], []
    total = len(ops)
    if not total:
        progress.percent(p1)
        return undo_entries, failures

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
                    say(progress, log_file,
                        f"  [skip] {source.name}: identical file already "
                        f"at destination, skipping")
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
            relative = target.relative_to(source_path)
            # Names the file as well as the destination: deciding and doing
            # are separate passes now, so the action lines are no longer
            # sitting directly under the "Processing X" line they belong to.
            say(progress, log_file,
                f"  [done] {operation_text[op.operation]} {source.name} "
                f"-> {relative}")
            stats['processed'] += 1
        except Exception as e:
            say(progress, log_file, f"  [error] {source.name}: {e}")
            stats['errors'] += 1
            failures.append((source, e))
            # The manifest is the record of what happened, so a file that did
            # not move must not be listed as though it did.
            mf = (records or {}).get(source)
            if mf is not None:
                mf.action, mf.target = 'failed', None
                mf.reason = str(e)

    progress.percent(p1)
    return undo_entries, failures


def execute_cached_plan(plan, settings, progress):
    """Carry out a plan a preview already worked out, without scanning again.

    Returns the run statistics, or None if the folder no longer matches the
    fingerprint taken during the preview - in which case the caller falls
    back to a fresh run rather than acting on a stale plan.

    This is deliberately thin. It used to be a second execution engine, with
    its own collision handling that had already diverged from the real one;
    now it verifies the folder and hands the same operations to the same
    applier, under the same header, and finishes with the same summary and
    the same manifest.

    That last part was missing. A replay wrote no manifest at all, so the
    documented workflow - Preview, read it, Execute - was the one that
    produced the *least*, and the file the README calls the run's real
    deliverable was the thing it silently skipped.
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
    records = plan['records']
    operation = settings.operation

    # Start from the preview's statistics; redo the live counters
    stats = copy.deepcopy(plan['stats'])
    stats['processed'] = 0
    stats['errors'] = 0

    run_stamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    log_filename = f"archiveprep_log_{run_stamp}.txt"
    undo_path = source_path / f"archiveprep_undo_{run_stamp}.jsonl"

    header = _run_header(settings, dry_run=False)
    progress.log("=" * 60)
    for line in header:
        progress.log(line)
    progress.log(f"Reusing the preview - {len(ops)} operation(s), "
                 f"nothing rescanned")
    progress.log("=" * 60)

    with open(source_path / log_filename, 'w', encoding='utf-8') as log_file:
        log_file.write(f"ArchivePrep run log - {datetime.now()}\n")
        for line in header:
            log_file.write(line + "\n")
        log_file.write(f"Reusing the preview - {len(ops)} operation(s), "
                       f"nothing rescanned\n")
        log_file.write("=" * 60 + "\n\n")

        _journal_start(undo_path, operation, source_path)
        undo_entries, failures = _apply_plan(
            ops, source_path, progress, stats, log_file, undo_path,
            records=records, p0=0, p1=100)
        _report_failures(failures, source_path, progress, log_file)

        if undo_entries:
            progress.notify("undo_available", str(undo_path))
        else:
            undo_path.unlink(missing_ok=True)

        if operation == "move" and settings.cleanup_empty:
            removed = _sweep_empty_dirs(source_path, log_file)
            stats['empty_folders_removed'] = removed
            if removed:
                progress.log(f"\nRemoved {removed} empty folder(s)")
            _note_leftover_folders(source_path, progress, log_file)

        duration = (datetime.now() - start_time).total_seconds()
        stats['duration_seconds'] = duration
        _write_run_summary(progress, log_file, stats, duration, dry_run=False)
        progress.log(f"\nLog file saved: {log_filename}")

    manifest_name = f"archiveprep_manifest_{run_stamp}.csv"
    write_manifest(source_path / manifest_name, records, source_path)
    progress.log(f"Manifest saved: {manifest_name}")

    progress.log(f"\nOperation complete! Files were {operation}d "
                 f"successfully.")
    if undo_entries:
        progress.log("This run can be undone with the 'Undo Last Run' button.")
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
    unknown = [f for f, (s, _) in health.items() if s == 'unknown']
    healthy = len(health) - len(damaged) - len(unknown)
    stats['damaged'] = len(damaged)
    stats['unknown'] = len(unknown)
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
            f.write(f"Healthy: {healthy}\n")
            f.write(f"Damaged: {len(damaged)}\n")
            f.write(f"Unknown: {len(unknown)}\n\n")
            if damaged:
                f.write("DAMAGED - do not archive these without looking\n")
                for path in damaged:
                    f.write(f"  {path.relative_to(source_path)} - "
                            f"{health[path][1]}\n")
                f.write("\n")
            if unknown:
                f.write("UNKNOWN - a format we cannot verify. This does NOT "
                        "mean they are broken.\n")
                for path in sorted(unknown, key=lambda p: str(p).lower()):
                    f.write(f"  {path.relative_to(source_path)}\n")
    except OSError as e:
        progress.log(f"[warn] could not write the report file: {e}")

    progress.log("\n" + "=" * 60)
    progress.log("RESULTS:")
    progress.log(f"Healthy:  {healthy}")
    progress.log(f"Damaged:  {len(damaged)}")
    progress.log(f"Unknown:  {len(unknown)} "
                 f"(RAW / some video formats - not a sign they are broken)")

    if damaged:
        progress.log("\nDamaged files:")
        for path in damaged[:200]:
            progress.log(f"  {path.relative_to(source_path)} - "
                         f"{health[path][1]}")
        if len(damaged) > 200:
            progress.log(f"  ... and {len(damaged) - 200} more "
                         f"(the full list is in the report file)")
        progress.log(f"\nTip: tick 'Check files for damage while organizing' "
                     f"to have these moved into a '{CORRUPT_FOLDER}' folder "
                     f"on the next run.")
    else:
        progress.log("\nNo damaged files found.")

    duration = (datetime.now() - start_time).total_seconds()
    stats['duration_seconds'] = duration
    progress.log(f"\nDuration: {duration:.1f} seconds")
    progress.log(f"Report saved: {report_name}")
    progress.status(f"Check complete - {healthy} healthy, "
                    f"{len(damaged)} damaged")

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
