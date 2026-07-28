"""End-to-end tests for photo_organizer.py, driving the real app headlessly.

Run from anywhere:

    python tests/test_photo_organizer.py

Every test builds real files in a temporary folder and drives the actual
tkinter app with its window hidden, so what is tested is what ships - not a
stripped-down copy of the logic.

Tests 1-10 are the v34 suite, run with the newer features switched off, to
prove nothing regressed. Tests 11+ cover content-based duplicate detection,
the damaged-file check, the empty-folder sweep and extension fixing.
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

# Import the app from the repo root, wherever this checkout happens to live
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import photo_organizer as po

from PIL import Image as PILImage

# Kept outside the repo so a failed run never leaves test files behind in it
SCRATCH = Path(tempfile.gettempdir()) / "photo_organizer_tests"
FAILURES = []


def check(cond, msg):
    if cond:
        print(f"  PASS: {msg}")
    else:
        print(f"  FAIL: {msg}")
        FAILURES.append(msg)


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


def make_app(operation="copy", subfolder="year-month", separate_raw=True,
             separate_screenshots=True, multithread=True,
             include_subfolders=False, dedupe=False, check_corrupt=False,
             thorough=False, cleanup_empty=True, fix_ext=True):
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


def relpaths():
    return sorted(str(p.relative_to(SCRATCH)).replace("\\", "/")
                  for p in SCRATCH.rglob("*") if p.is_file()
                  and not p.name.startswith("kjegla_"))


TOTAL = 15

# ---------------------------------------------------------------------------
# Regression: the v34 suite, with the new v35 features off
# ---------------------------------------------------------------------------

print("=== Test 1: dry run (nothing should change) ===")
build_source()
before = relpaths()
stats = run_app(dry_run=True)
after = relpaths()
check(before == after, "dry run leaves source untouched")
check(stats['total_files'] == TOTAL, f"total_files == {TOTAL} (got {stats['total_files']})")
check(stats['processed'] == TOTAL, f"dry run processed all {TOTAL} (got {stats['processed']})")
check(stats['errors'] == 0, f"no errors (got {stats['errors']})")
check(stats['screenshots'] == 2, f"2 screenshots detected (got {stats['screenshots']})")
check(stats['by_model'].get('Samsung Galaxy S23 Ultra') == 3,
      f"S23 Ultra count 3 (got {stats['by_model']})")
check(stats['by_model'].get('Sony A6000') == 2, "Sony A6000 count 2")
check(stats['by_model'].get('iPhone') == 2, "generic iPhone count 2 (fake HEIC + MOV)")
check(stats['by_model'].get('iPhone 14 Pro') == 1,
      f"real HEIC mapped to iPhone 14 Pro (got {stats['by_model']})")
check(stats['by_year'].get('2019') == 1, f"mvhd video dated 2019 (got {stats['by_year']})")

print("=== Test 2: copy run, year-month ===")
stats = run_app(dry_run=False, operation="copy")
files = relpaths()
expected = [
    "Samsung Galaxy S23 Ultra/2023/05-May/cam1.jpg",
    "Samsung Galaxy S23 Ultra/2023/06-June/clip.jpg",
    "Samsung Galaxy S23 Ultra/2021/07-July/Videos/clip.mp4",
    "Sony A6000/2022/01-January/DSC00001.JPG",
    "Sony A6000/2021/07-July/RAW/DSC00001.ARW",
    "Unknown Camera/2021/07-July/RAW/DSC09999.ARW",
    "Unknown Camera/2021/07-July/Videos/lonely.mp4",
    "Unknown Camera/2019/06-June/Videos/dated_video.mp4",
    "Unknown Camera/2021/07-July/Screenshots/Screenshot_20240101-120000.png",
    "Unknown Camera/2021/07-July/Screenshots/unnamed.png",
    "Unknown Camera/2021/07-July/class_photo.jpg",
    "Unknown Camera/2021/07-July/IMG_0001.png",
    "iPhone/2021/07-July/IMG_5555.HEIC",
    "iPhone/2021/07-July/Videos/IMG_5555.MOV",
    "iPhone 14 Pro/2023/12-December/real_iphone.heic",
]
for e in expected:
    check(e in files, f"created {e}")
check((SCRATCH / "cam1.jpg").exists(), "copy keeps originals")
undo_files = list(SCRATCH.glob("kjegla_undo_*.json"))
check(len(undo_files) == 1, f"undo record written (got {len(undo_files)})")

print("=== Test 3: identical duplicates are skipped, changed files renamed ===")
stats = run_app(dry_run=True, operation="copy")
check(stats['duplicates'] == TOTAL,
      f"dry re-run: all {TOTAL} identical dupes skipped (got {stats['duplicates']})")
check(stats['processed'] == 0, f"nothing to process (got {stats['processed']})")
make_img(SCRATCH / "cam1.jpg", model="SM-S918B", date="2023:05:10 14:30:00",
         color='green')
stats = run_app(dry_run=False, operation="copy")
check(stats['duplicates'] == TOTAL - 1,
      f"{TOTAL - 1} identical dupes skipped (got {stats['duplicates']})")
check((SCRATCH / "Samsung Galaxy S23 Ultra/2023/05-May/cam1_1.jpg").exists(),
      "changed cam1.jpg copied as cam1_1.jpg")

print("=== Test 4: move run empties source top level ===")
build_source()
stats = run_app(dry_run=False, operation="move", multithread=False)
top_media = [p for p in SCRATCH.iterdir()
             if p.is_file() and p.suffix.lower() in po.ALL_MEDIA_EXTS]
check(top_media == [], f"no media left at top level (got {top_media})")
check(stats['processed'] == TOTAL, f"move processed {TOTAL} (got {stats['processed']})")
check(stats['errors'] == 0, f"move errors 0 (got {stats['errors']})")

print("=== Test 5: undo restores the move run ===")
undo_files = sorted(SCRATCH.glob("kjegla_undo_*.json"))
check(len(undo_files) == 1, "one undo record present")
import json as _json
import tkinter as tk
record = _json.loads(undo_files[0].read_text(encoding='utf-8'))
check(record['operation'] == 'move', "undo record has operation=move")
check(len(record['entries']) == TOTAL, f"undo record has {TOTAL} entries")
root = tk.Tk(); root.withdraw()
app = po.PhotoOrganizerGUI(root)
app._run_undo(undo_files[0], record)
root.destroy()
top_media = sorted(p.name for p in SCRATCH.iterdir()
                   if p.is_file() and p.suffix.lower() in po.ALL_MEDIA_EXTS)
check(len(top_media) == TOTAL, f"all {TOTAL} files restored to top level (got {len(top_media)})")
check(not (SCRATCH / "Samsung Galaxy S23 Ultra").exists(), "camera folder cleaned up")
check(not (SCRATCH / "Unknown Camera").exists(), "Unknown Camera folder cleaned up")
check(list(SCRATCH.glob("kjegla_undo_*.json")) == [], "undo record consumed")
check(len(list(SCRATCH.glob("kjegla_undo_*.undone"))) == 1, "undo record renamed .undone")

print("=== Test 6: recursive scan + idempotent re-run + empty dir cleanup ===")
if SCRATCH.exists():
    shutil.rmtree(SCRATCH)
SCRATCH.mkdir(parents=True)
(SCRATCH / "trip").mkdir()
make_img(SCRATCH / "trip" / "nested.jpg", model="SM-S918B", date="2023:05:10 14:30:00")
make_img(SCRATCH / "top.jpg", model="SM-S918B", date="2023:06:01 09:00:00", color='blue')
stats = run_app(dry_run=False, operation="move", include_subfolders=True)
files = relpaths()
check("Samsung Galaxy S23 Ultra/2023/05-May/nested.jpg" in files,
      f"nested file organized (got {files})")
check("Samsung Galaxy S23 Ultra/2023/06-June/top.jpg" in files, "top-level file organized")
check(not (SCRATCH / "trip").exists(), "emptied 'trip' folder removed")
before = relpaths()
stats = run_app(dry_run=False, operation="move", include_subfolders=True)
check(stats['already_organized'] == 2,
      f"re-run: 2 files recognized as already organized (got {stats['already_organized']})")
check(stats['errors'] == 0, "re-run: no errors")
check(relpaths() == before, "re-run changes nothing")

print("=== Test 7: recursive duplicate handling (same name, both cases) ===")
if SCRATCH.exists():
    shutil.rmtree(SCRATCH)
SCRATCH.mkdir(parents=True)
(SCRATCH / "a").mkdir(); (SCRATCH / "b").mkdir()
make_img(SCRATCH / "a" / "dup.jpg", model="SM-S918B", date="2023:05:10 14:30:00")
shutil.copy2(SCRATCH / "a" / "dup.jpg", SCRATCH / "b" / "dup.jpg")
make_img(SCRATCH / "a" / "diff.jpg", model="SM-S918B", date="2023:05:10 14:30:00")
make_img(SCRATCH / "b" / "diff.jpg", model="SM-S918B", date="2023:05:10 14:30:00",
         color='green')
stats = run_app(dry_run=False, operation="move", include_subfolders=True)
files = relpaths()
check(stats['duplicates'] == 1, f"1 identical duplicate skipped (got {stats['duplicates']})")
check("Samsung Galaxy S23 Ultra/2023/05-May/dup.jpg" in files, "dup.jpg moved once")
check(sum(1 for f in files if "dup" in f) == 2,
      f"identical dup left in source, not renamed (files: {files})")
check("Samsung Galaxy S23 Ultra/2023/05-May/diff.jpg" in files, "diff.jpg moved")
check("Samsung Galaxy S23 Ultra/2023/05-May/diff_1.jpg" in files,
      "different-content diff.jpg renamed to diff_1.jpg")

print("=== Test 8: unit checks (v34 behaviour) ===")
check(po.friendly_camera_name("SM-S918B") == "Samsung Galaxy S23 Ultra", "model mapping S23 Ultra")
check(po.friendly_camera_name("iPhone15,2") == "iPhone 14 Pro", "iPhone15,2 -> iPhone 14 Pro")
check(po.model_for_image(Path("x.heic"), None) == "iPhone", "unreadable HEIC -> iPhone")
check(po.looks_like_screenshot(Path("class_photo.jpg"), None) is False, "class_photo not screenshot")
check(po.looks_like_screenshot(Path("Screenshot_x.png"), None) is True, "Screenshot_ prefix detected")
tmp_mp4 = SCRATCH / "unit_test.mp4"
make_mp4_with_date(tmp_mp4, datetime(2015, 3, 1, 12, 0))
d = po.read_video_date(tmp_mp4)
check(d is not None and d.year == 2015, f"read_video_date parses mvhd (got {d})")
bad_mp4 = SCRATCH / "bad.mp4"
bad_mp4.write_bytes(b"not a real mp4 at all")
check(po.read_video_date(bad_mp4) is None, "read_video_date safe on garbage")
f1 = SCRATCH / "h1.bin"; f2 = SCRATCH / "h2.bin"; f3 = SCRATCH / "h3.bin"
f1.write_bytes(b"same content"); f2.write_bytes(b"same content"); f3.write_bytes(b"other stuff!")
check(po.files_identical(f1, f2) is True, "files_identical true for same content")
check(po.files_identical(f1, f3) is False, "files_identical false for different content")
check(po.HEIF_AVAILABLE, "pillow-heif is active")

print("=== Test 9: cached preview -> execute replay ===")
build_source()
root, app = make_app(operation="move")
settings = app._snapshot_settings()
app.organize_photos(settings, dry_run=True)
check(app.cached_plan is not None, "dry run stores a cached plan")
check(len(app.cached_plan['ops']) == TOTAL, f"plan has {TOTAL} ops")
check(app.cached_plan['key'] == po.plan_key(settings), "plan key matches settings")
ok = app._execute_cached_plan(app.cached_plan, settings)
check(ok is True, "cached plan executes when folder unchanged")
check(app.stats['processed'] == TOTAL, f"replay processed {TOTAL} (got {app.stats['processed']})")
check(app.stats['errors'] == 0, "replay had no errors")
check(app.cached_plan is None, "cache consumed after execute")
files = relpaths()
check("Samsung Galaxy S23 Ultra/2023/05-May/cam1.jpg" in files, "replay: cam1.jpg at planned target")
check("iPhone 14 Pro/2023/12-December/real_iphone.heic" in files, "replay: HEIC at planned target")
top_media = [p for p in SCRATCH.iterdir()
             if p.is_file() and p.suffix.lower() in po.ALL_MEDIA_EXTS]
check(top_media == [], "replay: source top level emptied (move)")
check(len(list(SCRATCH.glob("kjegla_undo_*.json"))) == 1, "replay wrote an undo record")
root.destroy()

print("=== Test 10: cache invalidation ===")
build_source()
root, app = make_app(operation="move")
settings = app._snapshot_settings()
app.organize_photos(settings, dry_run=True)
check(app.cached_plan is not None, "plan cached")
os.utime(SCRATCH / "cam1.jpg", None)
ok = app._execute_cached_plan(app.cached_plan, settings)
check(ok is False, "modified file -> replay refuses, falls back")
before = relpaths()
check(all("/" not in f for f in before), "fallback refused: nothing was moved")
app.organize_photos(settings, dry_run=True)
app.subfolder_mode.set("year")
settings2 = app._snapshot_settings()
check(app.cached_plan['key'] != po.plan_key(settings2),
      "different settings -> key mismatch")
app.subfolder_mode.set("year-month")
settings3 = app._snapshot_settings()
app.organize_photos(settings3, dry_run=True)
make_img(SCRATCH / "newphoto.jpg", model="SM-S918B", date="2024:01:01 10:00:00")
ok = app._execute_cached_plan(app.cached_plan, settings3)
check(ok is False, "added file -> replay refuses")
root.destroy()

# ---------------------------------------------------------------------------
# New in v35
# ---------------------------------------------------------------------------

print("=== Test 11: file_health unit checks ===")
if SCRATCH.exists():
    shutil.rmtree(SCRATCH)
SCRATCH.mkdir(parents=True)

good_jpg = SCRATCH / "good.jpg"
make_img(good_jpg, model="SM-S918B", date="2023:05:10 14:30:00")
check(po.file_health(good_jpg)[0] == 'ok',
      f"valid JPEG -> ok (got {po.file_health(good_jpg)})")

cut_jpg = SCRATCH / "cut.jpg"
shutil.copy2(good_jpg, cut_jpg)
truncate(cut_jpg, 200)
check(po.file_health(cut_jpg)[0] == 'damaged',
      f"truncated JPEG -> damaged (got {po.file_health(cut_jpg)})")

good_png = SCRATCH / "good.png"
make_img(good_png, color='blue', fmt='PNG')
check(po.file_health(good_png)[0] == 'ok', "valid PNG -> ok")
cut_png = SCRATCH / "cut.png"
shutil.copy2(good_png, cut_png)
truncate(cut_png, 20)
check(po.file_health(cut_png)[0] == 'damaged', "truncated PNG -> damaged")

empty = SCRATCH / "empty.jpg"
empty.write_bytes(b"")
check(po.file_health(empty) == ('damaged', 'empty file (0 bytes)'),
      f"0-byte file -> damaged (got {po.file_health(empty)})")

junk = SCRATCH / "junk.jpg"
junk.write_bytes(b"this is definitely not a jpeg")
check(po.file_health(junk)[0] == 'damaged', "garbage with .jpg extension -> damaged")

raw = SCRATCH / "photo.arw"
raw.write_bytes(b"fake raw data" * 100)
check(po.file_health(raw)[0] == 'unchecked',
      f"RAW -> unchecked, never damaged (got {po.file_health(raw)})")

avi = SCRATCH / "clip.avi"
avi.write_bytes(b"fake avi" * 100)
check(po.file_health(avi)[0] == 'unchecked', "AVI -> unchecked (cannot verify)")

good_mp4 = SCRATCH / "good.mp4"
make_mp4_with_date(good_mp4, datetime(2019, 6, 15, 12, 0))
check(po.file_health(good_mp4)[0] == 'ok',
      f"well-formed MP4 -> ok (got {po.file_health(good_mp4)})")

cut_mp4 = SCRATCH / "cut.mp4"
shutil.copy2(good_mp4, cut_mp4)
truncate(cut_mp4, 6)
check(po.file_health(cut_mp4)[0] == 'damaged',
      f"truncated MP4 -> damaged (got {po.file_health(cut_mp4)})")

nomoov = SCRATCH / "nomoov.mp4"
ftyp = struct.pack('>I', 16) + b'ftyp' + b'isom\x00\x00\x02\x00'
mdat = struct.pack('>I', 16) + b'mdat' + bytes(8)
nomoov.write_bytes(ftyp + mdat)
check(po.file_health(nomoov)[0] == 'damaged', "MP4 without a moov box -> damaged")

check(po.file_health(good_jpg, thorough=True)[0] == 'ok', "thorough mode: good JPEG ok")
check(po.file_health(cut_jpg, thorough=True)[0] == 'damaged',
      "thorough mode: truncated JPEG damaged")

print("=== Test 11b: real-world cases that must NOT be called damaged ===")
# Recovery tools pad a complete file out to a block boundary with zero bytes,
# pushing the end marker megabytes away from the end of the file.
padded = SCRATCH / "padded.jpg"
padded.write_bytes(good_jpg.read_bytes() + b"\x00" * (3 * 1024 * 1024))
check(po.file_health(padded)[0] == 'ok',
      f"complete JPEG + 3 MB of zero padding -> ok (got {po.file_health(padded)})")
check(po.file_health(padded, thorough=True)[0] == 'ok',
      "...and thorough mode agrees")

# iPhone Portrait/dual-camera photos are MPO: a complete photo followed by a
# second embedded image, and it is usually that second image that got cut off.
mpo = SCRATCH / "portrait.jpg"
mpo.write_bytes(good_jpg.read_bytes() + good_jpg.read_bytes()[:400])
check(po.file_health(mpo)[0] == 'ok',
      f"complete photo + truncated 2nd embedded image -> ok "
      f"(got {po.file_health(mpo)})")

# ...but a photo whose own data never finished is still caught
really_cut = SCRATCH / "really_cut.jpg"
data = good_jpg.read_bytes()
really_cut.write_bytes(data[:len(data) // 2])
check(po.file_health(really_cut)[0] == 'damaged',
      f"genuinely truncated photo still caught (got {po.file_health(really_cut)})")

print("=== Test 11b2: motion photos (video appended to a JPEG) ===")
# Google Pixel (PXL_*.MP.jpg) and Samsung phones append a short video clip to
# the end of an ordinary JPEG. The photo is complete; something just follows it.
pixel_mp = SCRATCH / "PXL_20251103_094531578.MP.jpg"
pixel_mp.write_bytes(good_jpg.read_bytes()
                     + struct.pack('>I', 16) + b'ftyp' + b'isom\x00\x00\x02\x00'
                     + bytes(40000))
check(po.file_health(pixel_mp)[0] == 'ok',
      f"Pixel motion photo (MP4 appended) -> ok (got {po.file_health(pixel_mp)})")

samsung_mp = SCRATCH / "20240629_073655.jpg"
samsung_mp.write_bytes(good_jpg.read_bytes()
                       + b'\x00\x00\x01\x0a\x0e\x00\x00\x00Image_UTC_Data'
                       + bytes(1500))
check(po.file_health(samsung_mp)[0] == 'ok',
      f"Samsung motion photo (trailer appended) -> ok "
      f"(got {po.file_health(samsung_mp)})")

noise_tail = SCRATCH / "noisetail.jpg"
noise_tail.write_bytes(good_jpg.read_bytes()
                       + bytes(range(256)) * 200)
check(po.file_health(noise_tail)[0] == 'ok',
      "a complete photo with arbitrary data appended is still ok")

print("=== Test 11b3: a thumbnail's end marker must not excuse a truncated photo ===")
# Cameras embed a small preview inside the EXIF block near the start of the
# file, and that preview has an end marker of its own. If the main photo is
# then cut off, that stray marker must not be mistaken for proof it finished.
original = good_jpg.read_bytes()
fake_preview = (b'Exif\x00\x00' + b'\xff\xd8\xff\xdb' + bytes(64) + b'\xff\xd9')
app1 = b'\xff\xe1' + struct.pack('>H', len(fake_preview) + 2) + fake_preview
with_preview = original[:2] + app1 + original[2:]
thumb_only = SCRATCH / "thumb_only.jpg"
thumb_only.write_bytes(with_preview[:2 + len(app1) + 400])
check(thumb_only.read_bytes().count(b'\xff\xd9') >= 1,
      "the test file really does contain a preview end marker")
check(po.file_health(thumb_only)[0] == 'damaged',
      f"truncated photo is caught even though its preview has an end marker "
      f"(got {po.file_health(thumb_only)})")

intact_with_preview = SCRATCH / "intact_preview.jpg"
intact_with_preview.write_bytes(with_preview)
check(po.file_health(intact_with_preview)[0] == 'ok',
      f"...while the same photo left intact is fine "
      f"(got {po.file_health(intact_with_preview)})")

start = po._jpeg_scan_start(good_jpg, good_jpg.stat().st_size)
check(start > 0, f"_jpeg_scan_start locates the photo's data (got {start})")
check(start < good_jpg.stat().st_size, "...and it is inside the file")

print("=== Test 11c: wrong file extension is not damage ===")
jpeg_as_mov = SCRATCH / "IMG_0607.MOV"
jpeg_as_mov.write_bytes(good_jpg.read_bytes())
status, reason = po.file_health(jpeg_as_mov)
check(status == 'misnamed', f"JPEG named .MOV -> misnamed (got {status})")
check("JPEG image" in reason, f"...and the reason names the real format ({reason})")

png_as_jpg = SCRATCH / "shot.jpg"
png_as_jpg.write_bytes(good_png.read_bytes())
check(po.file_health(png_as_jpg)[0] == 'misnamed', "PNG named .jpg -> misnamed")

# Google Takeout exports some pictures as WEBP but names them .png
webp_as_png = SCRATCH / "04165a3a49aaf42842f7f02dc088f4f3.png"
webp_as_png.write_bytes(b'RIFF' + struct.pack('<I', 100) + b'WEBPVP8 '
                        + bytes(96))
status, reason = po.file_health(webp_as_png)
check(status == 'misnamed', f"WEBP named .png -> misnamed (got {status})")
check("WEBP" in reason, f"...and it says so ({reason})")

jpeg_as_heic = SCRATCH / "IMG_0803.HEIC"
jpeg_as_heic.write_bytes(good_jpg.read_bytes())
check(po.file_health(jpeg_as_heic)[0] == 'misnamed', "JPEG named .HEIC -> misnamed")

check(po.file_health(good_mp4)[0] == 'ok', "a real .mp4 named .mp4 is not misnamed")
mov_copy = SCRATCH / "real.mov"
mov_copy.write_bytes(good_mp4.read_bytes())
check(po.file_health(mov_copy)[0] == 'ok', "a real video named .mov is not misnamed")
check(po.sniff_real_format(b'\xff\xd8\xff\xe0' + b'x' * 12)[0] == 'JPEG image',
      "sniff detects JPEG")
check(po.sniff_real_format(b'RIFF' + b'\x00' * 4 + b'WEBP' + b'x' * 4)[0] == 'WEBP image',
      "sniff detects WEBP without confusing it with AVI")
check(po.sniff_real_format(b'total garbage!!!')[0] is None,
      "sniff says nothing rather than guessing on an unknown format")

print("=== Test 12: copy-name detection unit checks ===")
for stem in ["IMG_1234 (1)", "IMG_1234 (12)", "IMG_1234(1)", "photo - Copy",
             "photo - copy", "photo - Copy (2)", "photo copy 2",
             "photo - kopi", "vacation copy"]:
    check(po.looks_like_copy_name(stem) is True, f"'{stem}' looks like a copy")
# Camera/phone filenames must NOT be mistaken for copies - a trailing "_2" is
# how nearly every camera names its files, so it can't be a copy marker.
for stem in ["IMG_1234", "DSC00001", "DSC-0001", "2023-05-10 vacation", "photo",
             "IMG_20230510_143000", "photo_2", "photo-3", "photocopy",
             "Sunset (2 of 3)"]:
    check(po.looks_like_copy_name(stem) is False, f"'{stem}' does not look like a copy")

print("=== Test 13: duplicates by content, different filenames ===")
if SCRATCH.exists():
    shutil.rmtree(SCRATCH)
SCRATCH.mkdir(parents=True)
make_img(SCRATCH / "IMG_1234.jpg", model="SM-S918B", date="2023:05:10 14:30:00")
shutil.copy2(SCRATCH / "IMG_1234.jpg", SCRATCH / "IMG_1234 (1).jpg")
shutil.copy2(SCRATCH / "IMG_1234.jpg", SCRATCH / "IMG_1234 - Copy.jpg")
stats = run_app(dry_run=False, operation="move", dedupe=True)
files = relpaths()
check(stats['content_duplicates'] == 2,
      f"2 extra copies found (got {stats['content_duplicates']})")
check("Samsung Galaxy S23 Ultra/2023/05-May/IMG_1234.jpg" in files,
      f"the clean-named original is the keeper (got {files})")
check("Duplicates/IMG_1234 (1).jpg" in files, "'(1)' copy set aside")
check("Duplicates/IMG_1234 - Copy.jpg" in files, "'- Copy' copy set aside")
check(stats['duplicate_bytes'] > 0, "wasted space is measured")
check(len(list(SCRATCH.glob("kjegla_duplicates_*.txt"))) == 1,
      "a duplicate report was written")

print("=== Test 14: same size but different content is NOT a duplicate ===")
if SCRATCH.exists():
    shutil.rmtree(SCRATCH)
SCRATCH.mkdir(parents=True)
# under the 64 KB head-hash threshold
(SCRATCH / "small_a.jpg").write_bytes(b"A" * 1000)
(SCRATCH / "small_b.jpg").write_bytes(b"B" * 1000)
# over it, so the full-content-hash stage has to reject them
(SCRATCH / "big_a.jpg").write_bytes(b"A" * 200_000)
(SCRATCH / "big_b.jpg").write_bytes(b"B" * 200_000)
# same first 64 KB, different tail: only the full hash can tell these apart
(SCRATCH / "head_a.jpg").write_bytes(b"S" * 100_000 + b"tail-one")
(SCRATCH / "head_b.jpg").write_bytes(b"S" * 100_000 + b"tail-two")
stats = run_app(dry_run=True, operation="move", dedupe=True)
check(stats['content_duplicates'] == 0,
      f"no false duplicates among same-size files (got {stats['content_duplicates']})")
check(stats['processed'] == 6, f"all 6 files planned normally (got {stats['processed']})")

print("=== Test 15: large identical files (full-hash stage) ===")
if SCRATCH.exists():
    shutil.rmtree(SCRATCH)
SCRATCH.mkdir(parents=True)
make_big_img(SCRATCH / "noise.png", seed=1)
shutil.copy2(SCRATCH / "noise.png", SCRATCH / "noise (1).png")
make_big_img(SCRATCH / "other.png", seed=2)
stats = run_app(dry_run=False, operation="move", dedupe=True,
                separate_screenshots=False)
files = relpaths()
check(stats['content_duplicates'] == 1,
      f"1 large duplicate found (got {stats['content_duplicates']})")
check("Duplicates/noise (1).png" in files, f"large '(1)' copy set aside (got {files})")
check(any(f.endswith("/noise.png") for f in files), "large original organized")
check(any(f.endswith("/other.png") for f in files),
      "the different large file was left alone")

print("=== Test 16: damaged files routed to Corrupt, never beating a good copy ===")
if SCRATCH.exists():
    shutil.rmtree(SCRATCH)
SCRATCH.mkdir(parents=True)
(SCRATCH / "a").mkdir(); (SCRATCH / "b").mkdir()
make_img(SCRATCH / "a" / "photo.jpg", model="SM-S918B", date="2023:05:10 14:30:00")
shutil.copy2(SCRATCH / "a" / "photo.jpg", SCRATCH / "b" / "photo.jpg")
truncate(SCRATCH / "b" / "photo.jpg", 200)  # same name, damaged content
(SCRATCH / "b" / "zero.jpg").write_bytes(b"")
stats = run_app(dry_run=False, operation="move", include_subfolders=True,
                dedupe=True, check_corrupt=True)
files = relpaths()
check(stats['damaged'] == 2, f"2 damaged files found (got {stats['damaged']})")
check("Samsung Galaxy S23 Ultra/2023/05-May/photo.jpg" in files,
      f"the healthy copy was organized (got {files})")
check("Corrupt/b/photo.jpg" in files,
      "the damaged same-name copy went to Corrupt, mirroring its folder")
check("Corrupt/b/zero.jpg" in files, "the 0-byte file went to Corrupt")
check(not any(f.endswith("photo_1.jpg") for f in files),
      "the damaged copy never landed beside the good one as photo_1.jpg")
check(not (SCRATCH / "a").exists(), "emptied folder 'a' swept away")

print("=== Test 16b: misnamed files go to Wrong Extension, not Corrupt ===")
if SCRATCH.exists():
    shutil.rmtree(SCRATCH)
SCRATCH.mkdir(parents=True)
(SCRATCH / "vids").mkdir()
make_img(SCRATCH / "real.jpg", model="SM-S918B", date="2023:05:10 14:30:00")
# a photo that got saved with a video extension - intact, just misnamed.
# Deliberately different content from real.jpg, so it is not also a duplicate.
make_img(SCRATCH / "vids" / "IMG_0607.MOV", model="SM-S918B",
         date="2023:05:11 09:00:00", color='green', fmt='JPEG')
(SCRATCH / "vids" / "broken.jpg").write_bytes(b"")
stats = run_app(dry_run=False, operation="move", include_subfolders=True,
                dedupe=True, check_corrupt=True)
files = relpaths()
check(stats['misnamed'] == 1, f"1 misnamed file found (got {stats['misnamed']})")
check(stats['damaged'] == 1, f"the empty file is still damaged (got {stats['damaged']})")
check("Wrong Extension/vids/IMG_0607.jpg" in files,
      f"misnamed file renamed to .jpg and moved to Wrong Extension, "
      f"mirroring its folder (got {files})")
check("Corrupt/vids/broken.jpg" in files, "the genuinely broken file went to Corrupt")
check(not any(f.startswith("Corrupt/vids/IMG_0607") for f in files),
      "the misnamed file was NOT called damaged")

# the renames get their own undo record, kept inside the folder they affect
rename_records = list((SCRATCH / "Wrong Extension").glob("kjegla_undo_renames_*.json"))
check(len(rename_records) == 1,
      f"a separate rename undo record was written inside Wrong Extension/ "
      f"(got {len(rename_records)})")
rec = _json.loads(rename_records[0].read_text(encoding='utf-8'))
check(len(rec['entries']) == 1, "the rename record covers the 1 rename")
check(rec['entries'][0][1].endswith("IMG_0607.MOV"),
      "...and remembers the original name")

before_misnamed = relpaths()
stats = run_app(dry_run=False, operation="move", include_subfolders=True,
                dedupe=True, check_corrupt=True)
check(relpaths() == before_misnamed,
      "re-run leaves Wrong Extension/ alone")

print("=== Test 16c: 'Undo Renames' reverses only the renames ===")
root = tk.Tk(); root.withdraw()
app = po.PhotoOrganizerGUI(root)
app._run_undo(rename_records[0], rec, label="renames")
root.destroy()
files = relpaths()
check("vids/IMG_0607.MOV" in files,
      f"the renamed file is back under its original name (got {files})")
check("Samsung Galaxy S23 Ultra/2023/05-May/real.jpg" in files,
      "the rest of the run was left untouched")
check("Corrupt/vids/broken.jpg" in files,
      "the damaged file stayed in Corrupt - only renames were undone")
check(len(list((SCRATCH / "Wrong Extension").glob("*.undone"))) == 1,
      "the rename record was marked used so it cannot run twice")

print("=== Test 16d: files with an unrecognised extension are found by content ===")
if SCRATCH.exists():
    shutil.rmtree(SCRATCH)
SCRATCH.mkdir(parents=True)
make_img(SCRATCH / "normal.jpg", model="SM-S918B", date="2023:05:10 14:30:00")
# Google Takeout truncates long names, chopping ".jpg"/".mp4" off the end
make_img(SCRATCH / "PXL_20250507_050944066.RAW-01.MP.COVER", model="SM-S918B",
         date="2023:05:10 14:30:00", color='purple', fmt='JPEG')
make_mp4_with_date(SCRATCH / "PXL_20251103_094531578.MP", datetime(2019, 6, 15, 12, 0))
(SCRATCH / "notes.txt").write_text("not a photo")
(SCRATCH / "metadata.json").write_text('{"title": "x"}')

reg, raw = po.collect_media_files(SCRATCH, False, sniff_unknown=True)
found = sorted(p.name for p in reg + raw)
check("PXL_20250507_050944066.RAW-01.MP.COVER" in found,
      f"the .COVER file is picked up by its contents (got {found})")
check("PXL_20251103_094531578.MP" in found, "the .MP video is picked up too")
check("notes.txt" not in found, "a text file is still ignored")
check("metadata.json" not in found, "a Takeout json sidecar is still ignored")

reg2, raw2 = po.collect_media_files(SCRATCH, False, sniff_unknown=False)
check(len(reg2 + raw2) == 1,
      f"with the option off, only normal.jpg is seen (got {len(reg2 + raw2)})")

stats = run_app(dry_run=False, operation="move", check_corrupt=True)
files = relpaths()
check("Wrong Extension/PXL_20250507_050944066.RAW-01.MP.jpg" in files,
      f"the truncated photo name was fixed to .jpg (got {files})")
check("Wrong Extension/PXL_20251103_094531578.mp4" in files,
      "the truncated video name was fixed to .mp4")
check((SCRATCH / "notes.txt").exists(), "the text file was left alone")
check((SCRATCH / "metadata.json").exists(), "the json sidecar was left alone")

print("=== Test 17: re-run ignores Duplicates/ and Corrupt/ ===")
before = relpaths()
stats = run_app(dry_run=False, operation="move", include_subfolders=True,
                dedupe=True, check_corrupt=True)
check(relpaths() == before, "second run leaves the set-aside folders alone")
check(stats['damaged'] == 0,
      f"files already in Corrupt/ are not rescanned (got {stats['damaged']})")
check(stats['content_duplicates'] == 0, "files already in Duplicates/ are not rescanned")

print("=== Test 18: undo restores files out of Duplicates/ and Corrupt/ ===")
if SCRATCH.exists():
    shutil.rmtree(SCRATCH)
SCRATCH.mkdir(parents=True)
(SCRATCH / "src").mkdir()
make_img(SCRATCH / "src" / "IMG_1.jpg", model="SM-S918B", date="2023:05:10 14:30:00")
shutil.copy2(SCRATCH / "src" / "IMG_1.jpg", SCRATCH / "src" / "IMG_1 (1).jpg")
(SCRATCH / "src" / "broken.jpg").write_bytes(b"")
before = relpaths()
stats = run_app(dry_run=False, operation="move", include_subfolders=True,
                dedupe=True, check_corrupt=True)
files = relpaths()
check("Duplicates/src/IMG_1 (1).jpg" in files,
      f"duplicate mirrored its source folder (got {files})")
check("Corrupt/src/broken.jpg" in files, "damaged file mirrored its source folder")
undo_files = sorted(SCRATCH.glob("kjegla_undo_*.json"))
check(len(undo_files) == 1, "undo record written")
record = _json.loads(undo_files[0].read_text(encoding='utf-8'))
check(len(record['entries']) == 3,
      f"undo record covers all 3 moves incl. set-aside ones (got {len(record['entries'])})")
root = tk.Tk(); root.withdraw()
app = po.PhotoOrganizerGUI(root)
app._run_undo(undo_files[0], record)
root.destroy()
check(relpaths() == before, f"undo restored the folder exactly (got {relpaths()})")
check(not (SCRATCH / "Duplicates").exists(), "empty Duplicates folder cleaned up by undo")
check(not (SCRATCH / "Corrupt").exists(), "empty Corrupt folder cleaned up by undo")

print("=== Test 19: copy mode never copies duplicates or damaged files ===")
if SCRATCH.exists():
    shutil.rmtree(SCRATCH)
SCRATCH.mkdir(parents=True)
make_img(SCRATCH / "IMG_9.jpg", model="SM-S918B", date="2023:05:10 14:30:00")
shutil.copy2(SCRATCH / "IMG_9.jpg", SCRATCH / "IMG_9 (1).jpg")
(SCRATCH / "dead.jpg").write_bytes(b"")
stats = run_app(dry_run=False, operation="copy", dedupe=True, check_corrupt=True)
files = relpaths()
check(not (SCRATCH / "Duplicates").exists(),
      "copy mode makes no Duplicates folder")
check(not (SCRATCH / "Corrupt").exists(), "copy mode makes no Corrupt folder")
check((SCRATCH / "IMG_9 (1).jpg").exists(), "the duplicate is left exactly where it was")
check((SCRATCH / "dead.jpg").exists(), "the damaged file is left exactly where it was")
check("Samsung Galaxy S23 Ultra/2023/05-May/IMG_9.jpg" in files,
      "the good original was still copied into place")
check(stats['content_duplicates'] == 1, "the duplicate is still counted and reported")
check(stats['damaged'] == 1, "the damaged file is still counted and reported")

print("=== Test 20: empty-folder sweep ===")
if SCRATCH.exists():
    shutil.rmtree(SCRATCH)
SCRATCH.mkdir(parents=True)
(SCRATCH / "deep" / "deeper" / "deepest").mkdir(parents=True)
(SCRATCH / "keepme").mkdir()
(SCRATCH / "keepme" / "notes.txt").write_text("hello")
(SCRATCH / "junkonly").mkdir()
(SCRATCH / "junkonly" / "Thumbs.db").write_bytes(b"x")
removed = po.PhotoOrganizerGUI._sweep_empty_dirs(SCRATCH)
check(removed == 3, f"3 nested empty folders removed in one pass (got {removed})")
check(not (SCRATCH / "deep").exists(), "whole empty nest collapsed")
check((SCRATCH / "keepme" / "notes.txt").exists(), "folder with a file untouched")
check((SCRATCH / "junkonly" / "Thumbs.db").exists(),
      "folder holding only Thumbs.db is left alone - no file is ever deleted")
check(SCRATCH.exists(), "the source root itself is never removed")

print("=== Test 21: cleanup_empty off leaves folders alone ===")
if SCRATCH.exists():
    shutil.rmtree(SCRATCH)
SCRATCH.mkdir(parents=True)
(SCRATCH / "trip").mkdir()
make_img(SCRATCH / "trip" / "x.jpg", model="SM-S918B", date="2023:05:10 14:30:00")
run_app(dry_run=False, operation="move", include_subfolders=True,
        cleanup_empty=False)
check((SCRATCH / "trip").exists(), "emptied folder kept when cleanup is switched off")
check(not any(SCRATCH.joinpath("trip").iterdir()), "...and it really is empty")

print("=== Test 22: keeper ranking unit checks ===")
if SCRATCH.exists():
    shutil.rmtree(SCRATCH)
SCRATCH.mkdir(parents=True)
p_good = SCRATCH / "a.jpg"; p_good.write_bytes(b"x")
p_bad = SCRATCH / "b.jpg"; p_bad.write_bytes(b"x")
rank = po.PhotoOrganizerGUI._keeper_rank
health = {p_good: ('ok', ''), p_bad: ('damaged', 'truncated')}
check(rank(p_good, {}, health) < rank(p_bad, {}, health),
      "a healthy copy always outranks a damaged one")
meta = {p_good: {'model': 'X', 'date': datetime.now()}, p_bad: {}}
check(rank(p_good, meta, {}) < rank(p_bad, meta, {}),
      "a copy with camera info and a date outranks one with neither")
p_orig = SCRATCH / "IMG_1.jpg"; p_orig.write_bytes(b"x")
p_copy = SCRATCH / "IMG_1 (1).jpg"; p_copy.write_bytes(b"x")
check(rank(p_orig, {}, {}) < rank(p_copy, {}, {}),
      "a clean filename outranks a '(1)' one")
check(rank(p_orig, {}, {}) == rank(p_orig, {}, {}),
      "ranking is stable for the same file")

print("=== Test 23: standalone Check Files run is read-only ===")
if SCRATCH.exists():
    shutil.rmtree(SCRATCH)
SCRATCH.mkdir(parents=True)
make_img(SCRATCH / "fine.jpg", model="SM-S918B", date="2023:05:10 14:30:00")
bad = SCRATCH / "bad.jpg"
shutil.copy2(SCRATCH / "fine.jpg", bad)
truncate(bad, 300)
(SCRATCH / "raw.arw").write_bytes(b"fake raw" * 100)
before = relpaths()
root, app = make_app(operation="move", check_corrupt=True)
app._run_health_check(app._snapshot_settings())
stats = app.stats
root.destroy()
check(relpaths() == before, "check run moved nothing")
check(stats['damaged'] == 1, f"1 damaged file reported (got {stats['damaged']})")
check(stats['unchecked'] == 1, f"1 unchecked RAW reported (got {stats['unchecked']})")
check(stats['total_files'] == 3, f"3 files scanned (got {stats['total_files']})")
reports = list(SCRATCH.glob("kjegla_health_*.txt"))
check(len(reports) == 1, "a health report was written")
report_text = reports[0].read_text(encoding='utf-8')
check("bad.jpg" in report_text, "the damaged file is named in the report")
check("raw.arw" in report_text, "the unchecked RAW is listed separately")

print("=== Test 24: new settings invalidate the cached preview ===")
build_source()
root, app = make_app(operation="move")
settings = app._snapshot_settings()
app.organize_photos(settings, dry_run=True)
base_key = app.cached_plan['key']
for name, var in [("dedupe_content", app.dedupe_content),
                  ("check_corrupt", app.check_corrupt),
                  ("corrupt_thorough", app.corrupt_thorough),
                  ("cleanup_empty", app.cleanup_empty),
                  ("fix_extensions", app.fix_extensions)]:
    var.set(not var.get())
    check(base_key != po.plan_key(app._snapshot_settings()),
          f"toggling {name} invalidates the cached preview")
    var.set(not var.get())
check(base_key == po.plan_key(app._snapshot_settings()),
      "restoring every setting makes the cached preview valid again")
root.destroy()

print("=== Test 25: thorough mode end-to-end ===")
if SCRATCH.exists():
    shutil.rmtree(SCRATCH)
SCRATCH.mkdir(parents=True)
make_img(SCRATCH / "ok1.jpg", model="SM-S918B", date="2023:05:10 14:30:00")
make_img(SCRATCH / "ok2.png", color='blue', fmt='PNG')
bad = SCRATCH / "bad.jpg"
shutil.copy2(SCRATCH / "ok1.jpg", bad)
truncate(bad, 300)
stats = run_app(dry_run=False, operation="move", check_corrupt=True,
                thorough=True, multithread=False)
files = relpaths()
check(stats['damaged'] == 1, f"thorough run flags the 1 bad file (got {stats['damaged']})")
check("Corrupt/bad.jpg" in files, f"bad file set aside (got {files})")
check(stats['errors'] == 0, "thorough run had no errors")

if SCRATCH.exists():
    shutil.rmtree(SCRATCH)

print()
if FAILURES:
    print(f"{len(FAILURES)} FAILURES:")
    for f in FAILURES:
        print(" -", f)
    sys.exit(1)
print("ALL TESTS PASSED")
