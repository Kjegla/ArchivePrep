#!/usr/bin/env python3
"""
Kjegla's Photo Organizer by Camera Model - Customized Edition (v34)
Safely organizes photos into folders based on camera model metadata.
Can either MOVE or COPY files with date-based subfolders.

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


def model_for_image(file_path, meta):
    """Camera model for an image, assuming iPhone for unreadable HEIC files."""
    model = friendly_camera_name(meta.get('model') if meta else None)
    if not model and file_path.suffix.lower() in HEIC_EXTS:
        model = "iPhone"
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


def read_media_metadata(file_path):
    """Metadata for any media file: EXIF for images, mvhd date for videos."""
    if is_video_file(file_path):
        return {'model': None, 'date': read_video_date(file_path),
                'width': None, 'height': None, 'software': None}
    return read_metadata(file_path)


def collect_media_files(source_path, include_subfolders):
    """List media files in the source. Shared by the organizer and the
    preview-cache validation so both always see the same file set.

    Returns (regular_media_files, raw_files).
    """
    if include_subfolders:
        all_files = [Path(dirpath) / name
                     for dirpath, _dirs, names in os.walk(source_path)
                     for name in names]
    else:
        all_files = [f for f in source_path.iterdir() if f.is_file()]
    # Never touch our own log/undo files
    all_files = [f for f in all_files if not f.name.startswith('kjegla_')]
    raw_files = [f for f in all_files if f.suffix.lower() in RAW_EXTS]
    regular = [f for f in all_files
               if f.suffix.lower() in (IMAGE_EXTS | VIDEO_EXTS)]
    return regular, raw_files


def plan_key(settings):
    """Settings that affect where files go. If any of these differ between
    preview and execute, the cached plan is invalid."""
    return (settings.source, settings.operation, settings.subfolder_mode,
            settings.separate_raw, settings.separate_screenshots,
            settings.include_subfolders)


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
        self.root.geometry("900x700")

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
        self.processing = False
        self.cancel_requested = False
        self.last_undo_file = None
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
        self.root.minsize(860, 640)
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
        extras_frame.pack(fill=tk.BOTH, expand=True)

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
            text="🎬 Videos go to a 'Videos' subfolder automatically.\n"
                 "♻️ Identical duplicates are skipped, never overwritten.",
            font=('Segoe UI', 9), foreground='#888888', justify=tk.LEFT)
        video_note.pack(anchor=tk.W, pady=(6, 2))

        # Action buttons
        button_frame = ttk.Frame(main_frame)
        button_frame.grid(row=3, column=0, columnspan=3, pady=10)

        self.preview_btn = ttk.Button(button_frame, text="🔍 Preview (Dry Run)",
                                      command=self.preview_operation)
        self.preview_btn.pack(side=tk.LEFT, padx=4)

        self.execute_btn = ttk.Button(button_frame, text="▶️ Execute Operation",
                                      command=self.execute_operation)
        self.execute_btn.pack(side=tk.LEFT, padx=4)

        self.undo_btn = ttk.Button(button_frame, text="↩️ Undo Last Run",
                                   command=self.undo_last_operation, state=tk.DISABLED)
        self.undo_btn.pack(side=tk.LEFT, padx=4)

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
            if not self.processing:
                self.undo_btn.config(state=tk.NORMAL if self.last_undo_file else tk.DISABLED)

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
                    self.cancel_btn.config(state=tk.DISABLED)
                    self.stats_btn.config(state=tk.NORMAL if self.stats['total_files'] > 0 else tk.DISABLED)
                    self.undo_btn.config(state=tk.NORMAL if self.last_undo_file else tk.DISABLED)
                elif action == "disable_buttons":
                    self.preview_btn.config(state=tk.DISABLED)
                    self.execute_btn.config(state=tk.DISABLED)
                    self.undo_btn.config(state=tk.DISABLED)
                    self.cancel_btn.config(state=tk.NORMAL)
                elif action == "undo_available":
                    self.last_undo_file = value

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

    def _extract_metadata(self, files, settings):
        """Phase A: read metadata for images and videos (0-30%% progress).

        Returns {Path: meta dict}. Parallelized when multithreading is on.
        """
        metadata = {}
        total = len(files)
        if total == 0:
            return metadata

        def note_progress(done):
            self.update_progress(int(done / total * 30))
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
        self.log("=" * 60)

        try:
            regular_media_files, raw_files = collect_media_files(
                source_path, settings.include_subfolders)
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

        # Map base filenames to camera models for RAW/video matching
        base_name_to_model = {}
        for file_path, meta in metadata.items():
            model = model_for_image(file_path, meta)
            if model:
                base_name_to_model[file_path.stem] = model

        self.log(f"Found {len(base_name_to_model)} images with camera metadata")
        self.log(f"Found {len(raw_files)} RAW files and "
                 f"{sum(1 for f in media_files if is_video_file(f))} video files")

        # Create log file (and matching undo record for real runs)
        run_stamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        log_filename = f"kjegla_media_log_{run_stamp}.txt"
        log_path = source_path / log_filename
        undo_path = source_path / f"kjegla_undo_{run_stamp}.json"

        # Per-run context: planned targets make dry-run duplicate renames
        # accurate; undo_entries and moved_from feed undo / folder cleanup;
        # plan_ops collects [source, target] pairs for the preview cache
        ctx = SimpleNamespace(planned_targets=set(), undo_entries=[],
                              moved_from=set(), plan_ops=[])

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

                # Phase B occupies 30-100% of the progress bar
                progress = 30 + int((idx + 1) / total * 70)
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

            # After a recursive move, tidy up folders we emptied
            if not dry_run and operation == "move" and settings.include_subfolders:
                removed = self._remove_empty_dirs(ctx.moved_from, source_path)
                if removed:
                    self.log(f"\n🧹 Removed {removed} empty folder(s)")

            self.log("\n" + "=" * 60)
            self.log("SUMMARY:")
            self.log(f"Total media files: {self.stats['total_files']}")
            self.log(f"Files processed: {self.stats['processed']}")
            self.log(f"Files without metadata: {self.stats['no_metadata']}")
            self.log(f"Screenshots detected: {self.stats['screenshots']}")
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
    def _remove_empty_dirs(folders, source_root):
        """Remove now-empty folders bottom-up; never the source root itself."""
        removed = 0
        for folder in sorted(set(folders), key=lambda p: len(p.parts), reverse=True):
            current = folder
            while current != source_root and current.is_relative_to(source_root):
                try:
                    if current.exists() and not any(current.iterdir()):
                        current.rmdir()
                        removed += 1
                        current = current.parent
                    else:
                        break
                except OSError:
                    break
        return removed

    def _process_one_file(self, file_path, source_path, settings, base_name_to_model,
                          metadata, ctx, operation, operation_text,
                          dry_run, log_file):
        """Classify one file, compute its target, and move/copy (or preview) it."""
        meta = metadata.get(file_path)

        # Classify the file and determine its camera model
        camera_model = None
        is_screenshot = False
        match_note = None

        if is_raw_file(file_path):
            file_type = "📸 RAW"
            # RAW metadata is never read directly - match to a sibling image
            camera_model = base_name_to_model.get(file_path.stem)
            match_note = (f"  🔗 Matched RAW to JPEG: {camera_model}" if camera_model
                          else "  ⚠️  No matching JPEG for RAW, will move to Unknown")
        elif is_video_file(file_path):
            file_type = "🎬 Video"
            camera_model = base_name_to_model.get(file_path.stem)
            match_note = (f"  🔗 Matched video to image: {camera_model}" if camera_model
                          else "  ⚠️  No matching image for video, will move to Unknown")
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
                    ctx.moved_from.add(file_path.parent)
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
            regular, raw = collect_media_files(source_path,
                                               settings.include_subfolders)
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
        moved_from = set()
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
                        moved_from.add(src_p.parent)
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

            if operation == "move" and settings.include_subfolders:
                removed = self._remove_empty_dirs(moved_from, source_path)
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
            f"Include subfolders: {'Yes' if self.include_subfolders.get() else 'No'}\n\n"
            f"{'Files will be MOVED from source!' if operation == 'move' else 'Original files will remain untouched.'}"
            f"{cache_note}")

        if not result:
            return
        self._start_worker(dry_run=False, use_cache=True)

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

    def _run_undo(self, undo_file, record):
        """Worker: revert every operation in the undo record."""
        entries = record.get('entries', [])
        op = record.get('operation', 'move')
        source_root = Path(record.get('source', str(undo_file.parent)))
        total = len(entries)
        restored = 0
        problems = 0

        self.log("\n" + "=" * 60)
        self.log(f"↩️ UNDOING last {op} run ({total} files)")
        self.log("=" * 60)

        touched_folders = set()
        for idx, (target, original) in enumerate(entries):
            if self.cancel_requested:
                self.log("\n⏹️ Undo cancelled by user")
                break
            target_p = Path(target)
            touched_folders.add(target_p.parent)
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

        removed = self._remove_empty_dirs(touched_folders, source_root)

        # Mark the record consumed so it can't be replayed
        try:
            undo_file.rename(undo_file.with_name(undo_file.name + ".undone"))
        except OSError:
            pass
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
   python -m PyInstaller --onefile --windowed --noconfirm --collect-all sv_ttk --name "PhotoOrganizerV34" photo_organizer_v34.py

3. Optional: Add VLC cone icon (download vlc_cone.ico):
   add --icon=vlc_cone.ico to the command above

The executable will be in the 'dist' folder.
"""
