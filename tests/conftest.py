"""Shared fixtures and file builders for the test suite.

Every test gets a clean scratch folder, so tests can be run one at a time
(``pytest -k undo``) instead of only as one long sequence. That mattered
enough to be worth the conversion: hunting a single failure used to mean
running all 243 checks and reading past the noise.

SCRATCH stays a module-level path rather than pytest's ``tmp_path`` because
the builders below already take explicit paths, and threading a fixture
through all of them would have been a far larger change for the same
isolation. The one thing it costs is parallel runs (``pytest -n``), which
this suite does not use.
"""
import os
import random
import shutil
import struct
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

import pytest

# Import the app from the repo root, wherever this checkout happens to live
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import photo_organizer as po  # noqa: E402

from PIL import Image as PILImage  # noqa: E402

# Kept outside the repo so a failed run never leaves test files behind in it
SCRATCH = Path(tempfile.gettempdir()) / "photo_organizer_tests"

# How many media files build_source() lays down
TOTAL = 15


@pytest.fixture(autouse=True)
def scratch():
    """A clean scratch folder for every test, removed afterwards."""
    if SCRATCH.exists():
        shutil.rmtree(SCRATCH)
    SCRATCH.mkdir(parents=True)
    yield SCRATCH
    if SCRATCH.exists():
        shutil.rmtree(SCRATCH, ignore_errors=True)


_CHECKS = [0]


def check(cond, msg):
    """Assert, naming the behaviour rather than the expression.

    Kept as a function so the suite reads exactly as it did before pytest,
    and because every call already puts the actual value in its message -
    which is the same thing pytest's assertion rewriting would have given us.
    """
    _CHECKS[0] += 1
    assert cond, msg


def pytest_terminal_summary(terminalreporter):
    """Report how many individual checks ran, not just how many tests.

    This suite has always been measured in checks, and one test can carry
    twenty of them - a loop over fifteen expected paths is one line of source
    and fifteen assertions. Counting tests alone would make the coverage look
    like it collapsed when it did not.
    """
    terminalreporter.write_sep("-", f"{_CHECKS[0]} checks executed")


# --------------------------------------------------------------------------
# File builders
# --------------------------------------------------------------------------

def make_img(path, model=None, date=None, size=(640, 480), color='red', fmt=None):
    img = PILImage.new('RGB', size, color)
    exif = PILImage.Exif()
    if model:
        exif[po.TAG_MODEL] = model
    if date:
        exif[po.TAG_DATETIME] = date
    img.save(path, format=fmt, exif=exif.tobytes())


def make_big_img(path, seed=1):
    """A noisy PNG comfortably over the 64 KB head-hash threshold, so the
    full-content-hash stage of duplicate detection actually gets exercised."""
    rnd = random.Random(seed)
    img = PILImage.new('RGB', (400, 400))
    img.putdata([(rnd.randrange(256), rnd.randrange(256), rnd.randrange(256))
                 for _ in range(400 * 400)])
    img.save(path, format='PNG')
    assert path.stat().st_size > po.HEAD_HASH_BYTES, path.stat().st_size


def make_mp4_with_date(path, dt):
    """Craft a minimal ISO-BMFF file with a moov/mvhd creation time."""
    ctime = int(dt.timestamp()) + po.MP4_EPOCH_OFFSET
    mvhd_payload = (bytes(4) + struct.pack('>I', ctime) * 2 +
                    struct.pack('>I', 1000) + struct.pack('>I', 0) + bytes(76))
    mvhd = struct.pack('>I', 8 + len(mvhd_payload)) + b'mvhd' + mvhd_payload
    moov = struct.pack('>I', 8 + len(mvhd)) + b'moov' + mvhd
    ftyp = struct.pack('>I', 16) + b'ftyp' + b'isom\x00\x00\x02\x00'
    mdat = struct.pack('>I', 16) + b'mdat' + bytes(8)
    path.write_bytes(ftyp + moov + mdat)


def truncate(path, drop_bytes):
    data = path.read_bytes()
    path.write_bytes(data[:-drop_bytes])


def build_source():
    """The standard mixed-media source folder: TOTAL files across several
    cameras, plus the awkward cases (RAW with and without a JPEG, a video
    with and without a sibling image, screenshots, fake HEIC)."""
    if SCRATCH.exists():
        shutil.rmtree(SCRATCH)
    SCRATCH.mkdir(parents=True)
    make_img(SCRATCH / "cam1.jpg", model="SM-S918B", date="2023:05:10 14:30:00")
    make_img(SCRATCH / "DSC00001.JPG", model="ILCE-6000", date="2022:01:02 10:00:00")
    (SCRATCH / "DSC00001.ARW").write_bytes(b"fake raw data" * 100)
    (SCRATCH / "DSC09999.ARW").write_bytes(b"fake raw data" * 100)
    make_img(SCRATCH / "clip.jpg", model="SM-S918B", date="2023:06:01 09:00:00")
    (SCRATCH / "clip.mp4").write_bytes(b"fake video" * 1000)
    (SCRATCH / "lonely.mp4").write_bytes(b"fake video" * 1000)
    make_img(SCRATCH / "Screenshot_20240101-120000.png", size=(1080, 2340), color='blue')
    make_img(SCRATCH / "class_photo.jpg")
    make_img(SCRATCH / "IMG_0001.png", color='blue')
    make_img(SCRATCH / "unnamed.png", size=(1080, 2400), color='blue')
    (SCRATCH / "IMG_5555.HEIC").write_bytes(b"fake heic" * 100)
    (SCRATCH / "IMG_5555.MOV").write_bytes(b"fake live photo" * 100)
    make_img(SCRATCH / "real_iphone.heic", model="iPhone15,2",
             date="2023:12:25 10:00:00")
    make_mp4_with_date(SCRATCH / "dated_video.mp4", datetime(2019, 6, 15, 12, 0))

    t = time.mktime(datetime(2021, 7, 15, 12, 0, 0).timetuple())
    for name in ["DSC00001.ARW", "DSC09999.ARW", "clip.mp4", "lonely.mp4",
                 "Screenshot_20240101-120000.png", "class_photo.jpg",
                 "IMG_0001.png", "unnamed.png", "IMG_5555.HEIC", "IMG_5555.MOV"]:
        os.utime(SCRATCH / name, (t, t))


# --------------------------------------------------------------------------
# Driving the real application
# --------------------------------------------------------------------------

def make_app(operation="copy", subfolder="year-month", separate_raw=True,
             separate_screenshots=True, multithread=True,
             include_subfolders=False, dedupe=False, check_corrupt=False,
             thorough=False, cleanup_empty=True, fix_ext=True):
    """The real app, with its window hidden - so what is tested is what
    ships, not a stripped-down copy of the logic."""
    import tkinter as tk
    root = tk.Tk()
    root.withdraw()
    app = po.PhotoOrganizerGUI(root)
    app.source_folder.set(str(SCRATCH))
    app.operation_mode.set(operation)
    app.subfolder_mode.set(subfolder)
    app.separate_raw.set(separate_raw)
    app.separate_screenshots.set(separate_screenshots)
    app.use_multithreading.set(multithread)
    app.include_subfolders.set(include_subfolders)
    app.dedupe_content.set(dedupe)
    app.check_corrupt.set(check_corrupt)
    app.corrupt_thorough.set(thorough)
    app.cleanup_empty.set(cleanup_empty)
    app.fix_extensions.set(fix_ext)
    return root, app


def run_app(dry_run, **kwargs):
    root, app = make_app(**kwargs)
    settings = app._snapshot_settings()
    app.organize_photos(settings, dry_run=dry_run)
    stats = app.stats
    root.destroy()
    return stats


def run_undo(undo_file, record, label=None):
    """Drive a real undo through the app."""
    import tkinter as tk
    root = tk.Tk()
    root.withdraw()
    app = po.PhotoOrganizerGUI(root)
    app._run_undo(undo_file, record, label=label)
    root.destroy()


def relpaths():
    """Every file under the scratch folder, source-relative, slash-separated."""
    return sorted(str(p.relative_to(SCRATCH)).replace("\\", "/")
                  for p in SCRATCH.rglob("*") if p.is_file()
                  and not p.name.startswith("kjegla_"))
