"""End-to-end tests for organizer_core.py - the whole application except the
window.

    python -m pytest tests/ -q

Every test builds real files in a temporary folder and runs the real code
against them, then asserts where each file ended up. Nothing here needs a
screen: the core takes the same settings object the window builds and reports
through the same Progress object, so these tests drive exactly what ships.

The one thing they skip - that the window really does build that settings
object and hand it over - is covered in test_gui_wiring.py, which does open a
real window.

Each test starts from a clean scratch folder (see conftest.py), so any one of
them can be run on its own:

    python -m pytest tests/ -k undo

Where a test has several steps, that is deliberate: some behaviour only exists
across a sequence, such as "a second run leaves the set-aside folders alone".
Those read as one test with numbered steps rather than as separate tests that
secretly depend on each other.

The byte-level verdicts of file_health() live in test_golden_corpus.py.
"""
import json
import os
import shutil
import struct
from datetime import datetime
from pathlib import Path

import organizer_core as core
from conftest import (SCRATCH, TOTAL, build_source, check, make_big_img,
                      make_img, make_mp4_with_date, make_settings, relpaths,
                      run_app, run_undo, truncate)


# ---------------------------------------------------------------------------
# Core organizing behaviour
# ---------------------------------------------------------------------------

def test_dry_run_then_copy_then_rerun():
    """Preview changes nothing; the copy that follows lands where the preview
    said; running it again recognises every file as already there."""
    # 1. A dry run must not touch a thing
    build_source()
    before = relpaths()
    stats = run_app(dry_run=True)
    check(before == relpaths(), "dry run leaves source untouched")
    check(stats['total_files'] == TOTAL,
          f"total_files == {TOTAL} (got {stats['total_files']})")
    check(stats['processed'] == TOTAL,
          f"dry run processed all {TOTAL} (got {stats['processed']})")
    check(stats['errors'] == 0, f"no errors (got {stats['errors']})")
    check(stats['screenshots'] == 2,
          f"2 screenshots detected (got {stats['screenshots']})")
    check(stats['by_model'].get('Samsung Galaxy S23 Ultra') == 3,
          f"S23 Ultra count 3 (got {stats['by_model']})")
    check(stats['by_model'].get('Sony A6000') == 2, "Sony A6000 count 2")
    check(stats['by_model'].get('iPhone') == 2,
          "generic iPhone count 2 (fake HEIC + MOV)")
    check(stats['by_model'].get('iPhone 14 Pro') == 1,
          f"real HEIC mapped to iPhone 14 Pro (got {stats['by_model']})")
    check(stats['by_year'].get('2019') == 1,
          f"mvhd video dated 2019 (got {stats['by_year']})")

    # 2. The real copy run
    stats = run_app(dry_run=False, operation="copy")
    files = relpaths()
    expected = [
        "Samsung Galaxy S23 Ultra/2023/05-May/cam1.jpg",
        "Samsung Galaxy S23 Ultra/2023/06-June/clip.jpg",
        # The video and the RAW below now land in the same date folder as the
        # image they were captured with. Both used to be filed by their file
        # modified time - 2021/07-July - which put one shutter press in two
        # different years. That split was asserted as correct until V4 M1.
        "Samsung Galaxy S23 Ultra/2023/06-June/Videos/clip.mp4",
        "Sony A6000/2022/01-January/DSC00001.JPG",
        "Sony A6000/2022/01-January/RAW/DSC00001.ARW",
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
    undo_files = list(SCRATCH.glob("kjegla_undo_*.jsonl"))
    check(len(undo_files) == 1, f"undo record written (got {len(undo_files)})")

    # 3. Re-running finds everything already there; only a changed file moves
    stats = run_app(dry_run=True, operation="copy")
    check(stats['duplicates'] == TOTAL,
          f"dry re-run: all {TOTAL} identical dupes skipped "
          f"(got {stats['duplicates']})")
    check(stats['processed'] == 0, f"nothing to process (got {stats['processed']})")
    make_img(SCRATCH / "cam1.jpg", model="SM-S918B", date="2023:05:10 14:30:00",
             color='green')
    stats = run_app(dry_run=False, operation="copy")
    check(stats['duplicates'] == TOTAL - 1,
          f"{TOTAL - 1} identical dupes skipped (got {stats['duplicates']})")
    check((SCRATCH / "Samsung Galaxy S23 Ultra/2023/05-May/cam1_1.jpg").exists(),
          "changed cam1.jpg copied as cam1_1.jpg")


def test_move_then_undo():
    """A move empties the source; undo puts every file back and tidies up."""
    build_source()
    stats = run_app(dry_run=False, operation="move", multithread=False)
    top_media = [p for p in SCRATCH.iterdir()
                 if p.is_file() and p.suffix.lower() in core.ALL_MEDIA_EXTS]
    check(top_media == [], f"no media left at top level (got {top_media})")
    check(stats['processed'] == TOTAL,
          f"move processed {TOTAL} (got {stats['processed']})")
    check(stats['errors'] == 0, f"move errors 0 (got {stats['errors']})")

    undo_files = sorted(SCRATCH.glob("kjegla_undo_*.jsonl"))
    check(len(undo_files) == 1, "one undo record present")
    record = core.read_undo(undo_files[0])
    check(record['operation'] == 'move', "undo record has operation=move")
    check(len(record['entries']) == TOTAL, f"undo record has {TOTAL} entries")

    run_undo(undo_files[0], record)
    top_media = sorted(p.name for p in SCRATCH.iterdir()
                       if p.is_file() and p.suffix.lower() in core.ALL_MEDIA_EXTS)
    check(len(top_media) == TOTAL,
          f"all {TOTAL} files restored to top level (got {len(top_media)})")
    check(not (SCRATCH / "Samsung Galaxy S23 Ultra").exists(),
          "camera folder cleaned up")
    check(not (SCRATCH / "Unknown Camera").exists(), "Unknown Camera folder cleaned up")
    check(list(SCRATCH.glob("kjegla_undo_*.jsonl")) == [], "undo record consumed")
    check(len(list(SCRATCH.glob("kjegla_undo_*.undone"))) == 1,
          "undo record renamed .undone")


def test_recursive_scan_and_idempotent_rerun():
    (SCRATCH / "trip").mkdir()
    make_img(SCRATCH / "trip" / "nested.jpg", model="SM-S918B",
             date="2023:05:10 14:30:00")
    make_img(SCRATCH / "top.jpg", model="SM-S918B", date="2023:06:01 09:00:00",
             color='blue')
    run_app(dry_run=False, operation="move", include_subfolders=True)
    files = relpaths()
    check("Samsung Galaxy S23 Ultra/2023/05-May/nested.jpg" in files,
          f"nested file organized (got {files})")
    check("Samsung Galaxy S23 Ultra/2023/06-June/top.jpg" in files,
          "top-level file organized")
    check(not (SCRATCH / "trip").exists(), "emptied 'trip' folder removed")

    before = relpaths()
    stats = run_app(dry_run=False, operation="move", include_subfolders=True)
    check(stats['already_organized'] == 2,
          f"re-run: 2 files recognized as already organized "
          f"(got {stats['already_organized']})")
    check(stats['errors'] == 0, "re-run: no errors")
    check(relpaths() == before, "re-run changes nothing")


def test_recursive_duplicate_handling():
    """Same filename in two folders: identical content is skipped, different
    content gets renamed."""
    (SCRATCH / "a").mkdir()
    (SCRATCH / "b").mkdir()
    make_img(SCRATCH / "a" / "dup.jpg", model="SM-S918B", date="2023:05:10 14:30:00")
    shutil.copy2(SCRATCH / "a" / "dup.jpg", SCRATCH / "b" / "dup.jpg")
    make_img(SCRATCH / "a" / "diff.jpg", model="SM-S918B", date="2023:05:10 14:30:00")
    make_img(SCRATCH / "b" / "diff.jpg", model="SM-S918B", date="2023:05:10 14:30:00",
             color='green')
    stats = run_app(dry_run=False, operation="move", include_subfolders=True)
    files = relpaths()
    check(stats['duplicates'] == 1,
          f"1 identical duplicate skipped (got {stats['duplicates']})")
    check("Samsung Galaxy S23 Ultra/2023/05-May/dup.jpg" in files, "dup.jpg moved once")
    check(sum(1 for f in files if "dup" in f) == 2,
          f"identical dup left in source, not renamed (files: {files})")
    check("Samsung Galaxy S23 Ultra/2023/05-May/diff.jpg" in files, "diff.jpg moved")
    check("Samsung Galaxy S23 Ultra/2023/05-May/diff_1.jpg" in files,
          "different-content diff.jpg renamed to diff_1.jpg")


# ---------------------------------------------------------------------------
# Unit checks
# ---------------------------------------------------------------------------

def test_camera_naming_and_metadata():
    check(core.friendly_camera_name("SM-S918B") == "Samsung Galaxy S23 Ultra",
          "model mapping S23 Ultra")
    check(core.friendly_camera_name("iPhone15,2") == "iPhone 14 Pro",
          "iPhone15,2 -> iPhone 14 Pro")
    check(core.model_for_image(Path("x.heic"), None) == "iPhone",
          "unreadable HEIC -> iPhone")
    check(core.looks_like_screenshot(Path("class_photo.jpg"), None) is False,
          "class_photo not screenshot")
    check(core.looks_like_screenshot(Path("Screenshot_x.png"), None) is True,
          "Screenshot_ prefix detected")

    tmp_mp4 = SCRATCH / "unit_test.mp4"
    make_mp4_with_date(tmp_mp4, datetime(2015, 3, 1, 12, 0))
    d = core.read_video_date(tmp_mp4)
    check(d is not None and d.year == 2015, f"read_video_date parses mvhd (got {d})")
    bad_mp4 = SCRATCH / "bad.mp4"
    bad_mp4.write_bytes(b"not a real mp4 at all")
    check(core.read_video_date(bad_mp4) is None, "read_video_date safe on garbage")

    f1, f2, f3 = SCRATCH / "h1.bin", SCRATCH / "h2.bin", SCRATCH / "h3.bin"
    f1.write_bytes(b"same content")
    f2.write_bytes(b"same content")
    f3.write_bytes(b"other stuff!")
    check(core.files_identical(f1, f2) is True, "files_identical true for same content")
    check(core.files_identical(f1, f3) is False,
          "files_identical false for different content")
    check(core.HEIF_AVAILABLE, "pillow-heif is active")


def test_copy_name_detection():
    for stem in ["IMG_1234 (1)", "IMG_1234 (12)", "IMG_1234(1)", "photo - Copy",
                 "photo - copy", "photo - Copy (2)", "photo copy 2",
                 "photo - kopi", "vacation copy"]:
        check(core.looks_like_copy_name(stem) is True, f"'{stem}' looks like a copy")
    # Camera/phone filenames must NOT be mistaken for copies - a trailing "_2" is
    # how nearly every camera names its files, so it can't be a copy marker.
    for stem in ["IMG_1234", "DSC00001", "DSC-0001", "2023-05-10 vacation", "photo",
                 "IMG_20230510_143000", "photo_2", "photo-3", "photocopy",
                 "Sunset (2 of 3)"]:
        check(core.looks_like_copy_name(stem) is False,
              f"'{stem}' does not look like a copy")


def test_keeper_ranking():
    def record(name, **fields):
        path = SCRATCH / name
        path.write_bytes(b"x")
        return core.MediaFile(path=path, **fields)

    rank = core._keeper_rank
    healthy = record("a.jpg", verdict='ok')
    damaged = record("b.jpg", verdict='damaged', verdict_reason='truncated')
    check(rank(healthy) < rank(damaged),
          "a healthy copy always outranks a damaged one")

    rich = record("c.jpg", camera_model='X', captured_at=datetime.now())
    bare = record("d.jpg")
    check(rank(rich) < rank(bare),
          "a copy with camera info and a date outranks one with neither")

    original = record("IMG_1.jpg")
    copied = record("IMG_1 (1).jpg")
    check(rank(original) < rank(copied), "a clean filename outranks a '(1)' one")
    check(rank(original) == rank(original), "ranking is stable for the same file")


def test_empty_folder_sweep():
    (SCRATCH / "deep" / "deeper" / "deepest").mkdir(parents=True)
    (SCRATCH / "keepme").mkdir()
    (SCRATCH / "keepme" / "notes.txt").write_text("hello")
    (SCRATCH / "junkonly").mkdir()
    (SCRATCH / "junkonly" / "Thumbs.db").write_bytes(b"x")
    removed = core._sweep_empty_dirs(SCRATCH)
    check(removed == 3, f"3 nested empty folders removed in one pass (got {removed})")
    check(not (SCRATCH / "deep").exists(), "whole empty nest collapsed")
    check((SCRATCH / "keepme" / "notes.txt").exists(), "folder with a file untouched")
    check((SCRATCH / "junkonly" / "Thumbs.db").exists(),
          "folder holding only Thumbs.db is left alone - no file is ever deleted")
    check(SCRATCH.exists(), "the source root itself is never removed")


# ---------------------------------------------------------------------------
# Preview caching
# ---------------------------------------------------------------------------

def test_cached_preview_replay():
    build_source()
    settings = make_settings(operation="move")
    _stats, plan = core.organize_photos(settings, core.Progress(), dry_run=True)
    check(plan is not None, "dry run stores a cached plan")
    check(len(plan['ops']) == TOTAL, f"plan has {TOTAL} ops")
    check(plan['key'] == core.plan_key(settings), "plan key matches settings")

    stats = core.execute_cached_plan(plan, settings, core.Progress())
    check(stats is not None, "cached plan executes when folder unchanged")
    check(stats['processed'] == TOTAL,
          f"replay processed {TOTAL} (got {stats['processed']})")
    check(stats['errors'] == 0, "replay had no errors")
    files = relpaths()
    check("Samsung Galaxy S23 Ultra/2023/05-May/cam1.jpg" in files,
          "replay: cam1.jpg at planned target")
    check("iPhone 14 Pro/2023/12-December/real_iphone.heic" in files,
          "replay: HEIC at planned target")
    top_media = [p for p in SCRATCH.iterdir()
                 if p.is_file() and p.suffix.lower() in core.ALL_MEDIA_EXTS]
    check(top_media == [], "replay: source top level emptied (move)")
    check(len(list(SCRATCH.glob("kjegla_undo_*.jsonl"))) == 1,
          "replay wrote an undo record")


def test_cache_invalidation():
    build_source()
    settings = make_settings(operation="move")
    _stats, plan = core.organize_photos(settings, core.Progress(), dry_run=True)
    check(plan is not None, "plan cached")

    os.utime(SCRATCH / "cam1.jpg", None)
    check(core.execute_cached_plan(plan, settings, core.Progress()) is None,
          "modified file -> replay refuses, falls back")
    before = relpaths()
    check(all("/" not in f for f in before), "fallback refused: nothing was moved")

    _stats, plan = core.organize_photos(settings, core.Progress(), dry_run=True)
    other = make_settings(operation="move", subfolder="year")
    check(plan['key'] != core.plan_key(other), "different settings -> key mismatch")

    _stats, plan = core.organize_photos(settings, core.Progress(), dry_run=True)
    make_img(SCRATCH / "newphoto.jpg", model="SM-S918B", date="2024:01:01 10:00:00")
    check(core.execute_cached_plan(plan, settings, core.Progress()) is None,
          "added file -> replay refuses")


def test_settings_invalidate_cached_preview():
    build_source()
    settings = make_settings(operation="move")
    _stats, plan = core.organize_photos(settings, core.Progress(), dry_run=True)
    base_key = plan['key']
    for name, changed in [("dedupe_content", {"dedupe": True}),
                          ("check_corrupt", {"check_corrupt": True}),
                          ("corrupt_thorough", {"thorough": True}),
                          ("cleanup_empty", {"cleanup_empty": False}),
                          ("fix_extensions", {"fix_ext": False})]:
        check(base_key != core.plan_key(make_settings(operation="move", **changed)),
              f"toggling {name} invalidates the cached preview")
    check(base_key == core.plan_key(make_settings(operation="move")),
          "the same settings still match the cached preview")


# ---------------------------------------------------------------------------
# Duplicates by content
# ---------------------------------------------------------------------------

def test_duplicates_by_content():
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


def test_same_size_different_content_is_not_a_duplicate():
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
          f"no false duplicates among same-size files "
          f"(got {stats['content_duplicates']})")
    check(stats['processed'] == 6, f"all 6 files planned normally "
          f"(got {stats['processed']})")


def test_large_identical_files_full_hash_stage():
    make_big_img(SCRATCH / "noise.png", seed=1)
    shutil.copy2(SCRATCH / "noise.png", SCRATCH / "noise (1).png")
    make_big_img(SCRATCH / "other.png", seed=2)
    stats = run_app(dry_run=False, operation="move", dedupe=True,
                    separate_screenshots=False)
    files = relpaths()
    check(stats['content_duplicates'] == 1,
          f"1 large duplicate found (got {stats['content_duplicates']})")
    check("Duplicates/noise (1).png" in files,
          f"large '(1)' copy set aside (got {files})")
    check(any(f.endswith("/noise.png") for f in files), "large original organized")
    check(any(f.endswith("/other.png") for f in files),
          "the different large file was left alone")


# ---------------------------------------------------------------------------
# Damage, misnaming and the set-aside folders
# ---------------------------------------------------------------------------

def test_damaged_files_routed_to_corrupt():
    (SCRATCH / "a").mkdir()
    (SCRATCH / "b").mkdir()
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


def test_misnamed_files_and_undo_renames():
    """Misnamed files go to Wrong Extension (not Corrupt), get their own undo
    record, survive a re-run, and 'Undo Renames' reverses only them."""
    # 1. A misnamed file and a genuinely broken one must part company
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
    check(stats['damaged'] == 1,
          f"the empty file is still damaged (got {stats['damaged']})")
    check("Wrong Extension/vids/IMG_0607.jpg" in files,
          f"misnamed file renamed to .jpg and moved to Wrong Extension, "
          f"mirroring its folder (got {files})")
    check("Corrupt/vids/broken.jpg" in files,
          "the genuinely broken file went to Corrupt")
    check(not any(f.startswith("Corrupt/vids/IMG_0607") for f in files),
          "the misnamed file was NOT called damaged")

    # 2. The renames get their own undo record, inside the folder they affect
    rename_records = list((SCRATCH / "Wrong Extension")
                          .glob("kjegla_undo_renames_*.jsonl"))
    check(len(rename_records) == 1,
          f"a separate rename undo record was written inside Wrong Extension/ "
          f"(got {len(rename_records)})")
    rec = core.read_undo(rename_records[0])
    check(len(rec['entries']) == 1, "the rename record covers the 1 rename")
    check(rec['entries'][0][1].endswith("IMG_0607.MOV"),
          "...and remembers the original name")

    # 3. A second run leaves the set-aside folder alone
    before_misnamed = relpaths()
    run_app(dry_run=False, operation="move", include_subfolders=True,
            dedupe=True, check_corrupt=True)
    check(relpaths() == before_misnamed, "re-run leaves Wrong Extension/ alone")

    # 4. Undo Renames reverses the renames and nothing else
    run_undo(rename_records[0], rec, label="renames")
    files = relpaths()
    check("vids/IMG_0607.MOV" in files,
          f"the renamed file is back under its original name (got {files})")
    check("Samsung Galaxy S23 Ultra/2023/05-May/real.jpg" in files,
          "the rest of the run was left untouched")
    check("Corrupt/vids/broken.jpg" in files,
          "the damaged file stayed in Corrupt - only renames were undone")
    check(len(list((SCRATCH / "Wrong Extension").glob("*.undone"))) == 1,
          "the rename record was marked used so it cannot run twice")


def test_unrecognised_extensions_found_by_content_then_rerun():
    """Google Takeout truncates long names, chopping the extension clean off.
    Those files are found by their contents - and a second run then leaves the
    set-aside folders alone."""
    # 1. Files with no usable extension are still seen
    make_img(SCRATCH / "normal.jpg", model="SM-S918B", date="2023:05:10 14:30:00")
    make_img(SCRATCH / "PXL_20250507_050944066.RAW-01.MP.COVER", model="SM-S918B",
             date="2023:05:10 14:30:00", color='purple', fmt='JPEG')
    make_mp4_with_date(SCRATCH / "PXL_20251103_094531578.MP",
                       datetime(2019, 6, 15, 12, 0))
    (SCRATCH / "notes.txt").write_text("not a photo")
    (SCRATCH / "metadata.json").write_text('{"title": "x"}')

    reg, raw = core.collect_media_files(SCRATCH, False)
    found = sorted(p.name for p in reg + raw)
    check("PXL_20250507_050944066.RAW-01.MP.COVER" in found,
          f"the .COVER file is picked up by its contents (got {found})")
    check("PXL_20251103_094531578.MP" in found, "the .MP video is picked up too")
    check("notes.txt" not in found, "a text file is still ignored")
    check("metadata.json" not in found, "a Takeout json sidecar is still ignored")

    # Finding these files is no longer optional (STATUS.md #4). Turning off
    # "fix wrong extensions" used to hide them entirely - never sorted, never
    # checked, never deduplicated - rather than merely leaving them alone.
    stats = run_app(dry_run=True, operation="move", fix_ext=False)
    check(stats['total_files'] == 3,
          f"all 3 media files are seen even with extension fixing off "
          f"(got {stats['total_files']})")

    # 2. Organizing renames them to what they actually are
    run_app(dry_run=False, operation="move", check_corrupt=True)
    files = relpaths()
    check("Wrong Extension/PXL_20250507_050944066.RAW-01.MP.jpg" in files,
          f"the truncated photo name was fixed to .jpg (got {files})")
    check("Wrong Extension/PXL_20251103_094531578.mp4" in files,
          "the truncated video name was fixed to .mp4")
    check((SCRATCH / "notes.txt").exists(), "the text file was left alone")
    check((SCRATCH / "metadata.json").exists(), "the json sidecar was left alone")

    # 3. Re-running must not drag them back out
    before = relpaths()
    stats = run_app(dry_run=False, operation="move", include_subfolders=True,
                    dedupe=True, check_corrupt=True)
    check(relpaths() == before, "second run leaves the set-aside folders alone")
    check(stats['damaged'] == 0,
          f"files already in Corrupt/ are not rescanned (got {stats['damaged']})")
    check(stats['content_duplicates'] == 0,
          "files already in Duplicates/ are not rescanned")


def test_undo_restores_out_of_set_aside_folders():
    (SCRATCH / "src").mkdir()
    make_img(SCRATCH / "src" / "IMG_1.jpg", model="SM-S918B",
             date="2023:05:10 14:30:00")
    shutil.copy2(SCRATCH / "src" / "IMG_1.jpg", SCRATCH / "src" / "IMG_1 (1).jpg")
    (SCRATCH / "src" / "broken.jpg").write_bytes(b"")
    before = relpaths()
    run_app(dry_run=False, operation="move", include_subfolders=True,
            dedupe=True, check_corrupt=True)
    files = relpaths()
    check("Duplicates/src/IMG_1 (1).jpg" in files,
          f"duplicate mirrored its source folder (got {files})")
    check("Corrupt/src/broken.jpg" in files, "damaged file mirrored its source folder")
    undo_files = sorted(SCRATCH.glob("kjegla_undo_*.jsonl"))
    check(len(undo_files) == 1, "undo record written")
    record = core.read_undo(undo_files[0])
    check(len(record['entries']) == 3,
          f"undo record covers all 3 moves incl. set-aside ones "
          f"(got {len(record['entries'])})")
    run_undo(undo_files[0], record)
    check(relpaths() == before, f"undo restored the folder exactly (got {relpaths()})")
    check(not (SCRATCH / "Duplicates").exists(),
          "empty Duplicates folder cleaned up by undo")
    check(not (SCRATCH / "Corrupt").exists(), "empty Corrupt folder cleaned up by undo")


def test_copy_mode_never_copies_duplicates_or_damaged():
    make_img(SCRATCH / "IMG_9.jpg", model="SM-S918B", date="2023:05:10 14:30:00")
    shutil.copy2(SCRATCH / "IMG_9.jpg", SCRATCH / "IMG_9 (1).jpg")
    (SCRATCH / "dead.jpg").write_bytes(b"")
    stats = run_app(dry_run=False, operation="copy", dedupe=True, check_corrupt=True)
    files = relpaths()
    check(not (SCRATCH / "Duplicates").exists(), "copy mode makes no Duplicates folder")
    check(not (SCRATCH / "Corrupt").exists(), "copy mode makes no Corrupt folder")
    check((SCRATCH / "IMG_9 (1).jpg").exists(),
          "the duplicate is left exactly where it was")
    check((SCRATCH / "dead.jpg").exists(),
          "the damaged file is left exactly where it was")
    check("Samsung Galaxy S23 Ultra/2023/05-May/IMG_9.jpg" in files,
          "the good original was still copied into place")
    check(stats['content_duplicates'] == 1,
          "the duplicate is still counted and reported")
    check(stats['damaged'] == 1, "the damaged file is still counted and reported")


def test_cleanup_empty_off_leaves_folders_alone():
    (SCRATCH / "trip").mkdir()
    make_img(SCRATCH / "trip" / "x.jpg", model="SM-S918B", date="2023:05:10 14:30:00")
    run_app(dry_run=False, operation="move", include_subfolders=True,
            cleanup_empty=False)
    check((SCRATCH / "trip").exists(),
          "emptied folder kept when cleanup is switched off")
    check(not any(SCRATCH.joinpath("trip").iterdir()), "...and it really is empty")


def test_standalone_check_files_is_read_only():
    make_img(SCRATCH / "fine.jpg", model="SM-S918B", date="2023:05:10 14:30:00")
    bad = SCRATCH / "bad.jpg"
    shutil.copy2(SCRATCH / "fine.jpg", bad)
    truncate(bad, 300)
    (SCRATCH / "raw.arw").write_bytes(b"fake raw" * 100)
    before = relpaths()
    stats = core.run_health_check(
        make_settings(operation="move", check_corrupt=True), core.Progress())
    check(relpaths() == before, "check run moved nothing")
    check(stats['damaged'] == 1, f"1 damaged file reported (got {stats['damaged']})")
    check(stats['unchecked'] == 1,
          f"1 unchecked RAW reported (got {stats['unchecked']})")
    check(stats['total_files'] == 3, f"3 files scanned (got {stats['total_files']})")
    reports = list(SCRATCH.glob("kjegla_health_*.txt"))
    check(len(reports) == 1, "a health report was written")
    report_text = reports[0].read_text(encoding='utf-8')
    check("bad.jpg" in report_text, "the damaged file is named in the report")
    check("raw.arw" in report_text, "the unchecked RAW is listed separately")


def test_thorough_mode_end_to_end():
    make_img(SCRATCH / "ok1.jpg", model="SM-S918B", date="2023:05:10 14:30:00")
    make_img(SCRATCH / "ok2.png", color='blue', fmt='PNG')
    bad = SCRATCH / "bad.jpg"
    shutil.copy2(SCRATCH / "ok1.jpg", bad)
    truncate(bad, 300)
    stats = run_app(dry_run=False, operation="move", check_corrupt=True,
                    thorough=True, multithread=False)
    files = relpaths()
    check(stats['damaged'] == 1,
          f"thorough run flags the 1 bad file (got {stats['damaged']})")
    check("Corrupt/bad.jpg" in files, f"bad file set aside (got {files})")
    check(stats['errors'] == 0, "thorough run had no errors")


# ---------------------------------------------------------------------------
# Safety: the undo journal, verified transfers (V4 Tier 1)
# ---------------------------------------------------------------------------

def test_undo_journal_is_append_only_and_survives_a_torn_tail():
    build_source()
    run_app(dry_run=False, operation="move")
    journals = sorted(SCRATCH.glob("kjegla_undo_*.jsonl"))
    check(len(journals) == 1, f"a journal was written (got {len(journals)})")
    lines = journals[0].read_text(encoding='utf-8').splitlines()
    check(json.loads(lines[0]).get('operation') == 'move',
          "the first line is the header")
    entries = [json.loads(line) for line in lines[1:]]
    check(all(isinstance(e, list) and len(e) == 2 for e in entries),
          "every later line is one [target, original] pair")
    check(len(entries) == TOTAL,
          f"one line per file moved, written as it happened (got {len(entries)})")

    # Simulate a crash part-way through an append: chop the final line in half.
    # The old whole-file rewrite would have left unreadable JSON here, taking
    # the entire run's undo with it.
    text = journals[0].read_text(encoding='utf-8')
    cut = text.rstrip("\n").rfind("\n")
    journals[0].write_text(text[:cut + 1] + '["half a writ', encoding='utf-8')
    record = core.read_undo(journals[0])
    check(record['operation'] == 'move', "the header still reads after a torn tail")
    check(len(record['entries']) == TOTAL - 1,
          f"the torn line is dropped and everything before it survives "
          f"(got {len(record['entries'])})")

    run_undo(journals[0], record)
    restored = [p for p in SCRATCH.iterdir()
                if p.is_file() and p.suffix.lower() in core.ALL_MEDIA_EXTS]
    check(len(restored) == TOTAL - 1,
          f"undo restored every file the journal still held (got {len(restored)})")


def test_a_move_is_journalled_before_it_is_made():
    """Order matters here, and it is not the obvious one.

    Journalling after the move leaves a window - however small - where a file
    has moved and nothing records it, which is a file the tool can no longer
    put back. Journalling first means a crash in that window leaves an entry
    for a move that never happened, and undo simply reports it as missing and
    moves on. A spurious entry is always the cheaper mistake.

    Proven here by making the move itself fail: the journal entry must still
    be there afterwards.
    """
    make_img(SCRATCH / "fine.jpg", model="SM-S918B", date="2023:05:10 14:30:00")
    make_img(SCRATCH / "doomed.jpg", model="SM-S918B", date="2023:05:11 09:00:00",
             color='green')

    real_transfer = core.transfer_file

    def fail_on_doomed(src, dst, operation):
        if src.name == "doomed.jpg":
            raise OSError("simulated failure part-way through the move")
        return real_transfer(src, dst, operation)

    core.transfer_file = fail_on_doomed
    try:
        stats = run_app(dry_run=False, operation="move")
    finally:
        core.transfer_file = real_transfer

    check(stats['errors'] == 1, f"the failed move was counted (got {stats['errors']})")
    check((SCRATCH / "doomed.jpg").exists(),
          "the file that failed to move is still exactly where it was")

    journal = sorted(SCRATCH.glob("kjegla_undo_*.jsonl"))[-1]
    record = core.read_undo(journal)
    origins = [Path(origin).name for _target, origin in record['entries']]
    check("doomed.jpg" in origins,
          f"the move was journalled before it was attempted (got {origins})")

    # And undo copes with the entry describing something that never happened
    run_undo(journal, record)
    check((SCRATCH / "doomed.jpg").exists(),
          "undo left the un-moved file alone rather than damaging it")
    check((SCRATCH / "fine.jpg").exists(),
          "...and still restored the file that really did move")


def test_undo_record_from_an_older_build_still_works():
    (SCRATCH / "Old Camera").mkdir()
    (SCRATCH / "Old Camera" / "moved.jpg").write_bytes(b"the photo")
    legacy = SCRATCH / "kjegla_undo_20240101_000000.json"
    legacy.write_text(json.dumps({
        'operation': 'move',
        'created': '2024-01-01T00:00:00',
        'source': str(SCRATCH),
        'entries': [[str(SCRATCH / "Old Camera" / "moved.jpg"),
                     str(SCRATCH / "moved.jpg")]]}), encoding='utf-8')
    record = core.read_undo(legacy)
    check(record['operation'] == 'move', "legacy record: operation read")
    check(len(record['entries']) == 1, "legacy record: entry read")
    run_undo(legacy, record)
    check((SCRATCH / "moved.jpg").exists(),
          "a folder organized with an older build can still be undone")
    check(not (SCRATCH / "Old Camera").exists(), "its emptied folder was swept")


def test_copy_mode_undo_never_deletes_an_edited_copy():
    make_img(SCRATCH / "keep.jpg", model="SM-S918B", date="2023:05:10 14:30:00")
    make_img(SCRATCH / "edited.jpg", model="SM-S918B", date="2023:05:11 09:00:00",
             color='green')
    run_app(dry_run=False, operation="copy")
    journals = sorted(SCRATCH.glob("kjegla_undo_*.jsonl"))
    check(len(journals) == 1, "the copy run wrote an undo record")
    record = core.read_undo(journals[0])
    check(record['operation'] == 'copy', "the record knows it was a copy run")
    by_origin = {Path(o).name: Path(t) for t, o in record['entries']}
    edited_copy, kept_copy = by_origin['edited.jpg'], by_origin['keep.jpg']
    check(edited_copy.exists() and kept_copy.exists(), "both copies were made")
    # The user opens one of the copies and works on it, then changes their mind
    # about the run. Undo must not destroy that work.
    edited_copy.write_bytes(edited_copy.read_bytes() + b"work the user did")
    run_undo(journals[0], record)
    check(edited_copy.exists(), "the edited copy was kept, not deleted")
    check(edited_copy.read_bytes().endswith(b"work the user did"),
          "...with the user's work still in it")
    check(not kept_copy.exists(), "the untouched copy was removed as normal")
    check((SCRATCH / "keep.jpg").exists() and (SCRATCH / "edited.jpg").exists(),
          "copy mode left both originals alone throughout")


def _takeout_sidecar(media_name, taken, extra_suffix=".supplemental-metadata"):
    """What Google Takeout writes next to a photo."""
    (SCRATCH / f"{media_name}{extra_suffix}.json").write_text(json.dumps({
        "title": media_name,
        "photoTakenTime": {"timestamp": str(int(taken.timestamp()))},
        "creationTime": {"timestamp": str(int(datetime(2025, 1, 1).timestamp()))},
    }), encoding='utf-8')


def test_takeout_sidecar_supplies_the_date_when_exif_is_gone():
    """Takeout strips EXIF from some files. Without the sidecar those photos
    fall back to the file's modified time, which on an export is the date you
    downloaded it - so they get filed under the wrong year entirely."""
    stripped = SCRATCH / "PXL_20190612_101500000.jpg"
    make_img(stripped, color='purple')          # no EXIF date at all
    _takeout_sidecar(stripped.name, datetime(2019, 6, 12, 10, 15))

    stats = run_app(dry_run=False, operation="move")
    files = relpaths()
    check(any("2019/06-June" in f and f.endswith(".jpg") for f in files),
          f"the photo was filed under the year it was taken (got {files})")
    check(stats['by_year'].get('2019') == 1,
          f"...and counted there (got {stats['by_year']})")
    check((SCRATCH / f"{stripped.name}.supplemental-metadata.json").exists(),
          "the sidecar itself was left exactly where it was")


def test_a_files_own_exif_always_beats_its_sidecar():
    """The sidecar only answers when the file cannot. Google's record of a
    photo is good evidence; the photo's own is better."""
    photo = SCRATCH / "IMG_2020.jpg"
    make_img(photo, model="SM-S918B", date="2020:03:04 11:00:00")
    _takeout_sidecar(photo.name, datetime(2011, 1, 1, 9, 0))   # deliberately wrong

    run_app(dry_run=False, operation="move")
    files = relpaths()
    check(any("2020/03-March" in f for f in files),
          f"EXIF won (got {files})")
    check(not any("2011" in f for f in files),
          "the sidecar did not override the file's own metadata")


def test_sidecar_is_found_even_when_takeout_truncated_its_name():
    """Takeout truncates long names, sometimes part-way through
    '.supplemental-metadata', so the sidecar cannot be found by guessing an
    exact filename."""
    photo = SCRATCH / "PXL_20180101_120000000.jpg"
    make_img(photo, color='red')
    _takeout_sidecar(photo.name, datetime(2018, 1, 1, 12, 0),
                     extra_suffix=".supplemental-me")   # chopped mid-word

    run_app(dry_run=False, operation="move")
    check(any("2018/01-January" in f for f in relpaths()),
          f"the truncated sidecar was still matched (got {relpaths()})")

    check(core.read_sidecar_date(SCRATCH / "nope.json") is None,
          "a missing sidecar is not an error")
    bad = SCRATCH / "bad.json"
    bad.write_text("{not json at all", encoding='utf-8')
    check(core.read_sidecar_date(bad) is None, "an unreadable sidecar is not an error")


def test_a_capture_keeps_its_files_together():
    """One shutter press, one destination. A RAW carries no date this
    application reads, so it used to be filed by its file time and land in a
    different year from the JPEG it was taken with."""
    make_img(SCRATCH / "DSC01234.JPG", model="ILCE-6000", date="2016:08:09 14:00:00")
    (SCRATCH / "DSC01234.ARW").write_bytes(b"raw bytes for 1234" * 50)
    # a second, unrelated capture in the same folder
    make_img(SCRATCH / "DSC05555.JPG", model="ILCE-6000", date="2021:02:03 10:00:00")
    (SCRATCH / "DSC05555.ARW").write_bytes(b"raw bytes for 5555" * 50)

    run_app(dry_run=False, operation="move")
    files = relpaths()
    check("Sony A6000/2016/08-August/DSC01234.JPG" in files,
          f"the JPEG went by its EXIF date (got {files})")
    check("Sony A6000/2016/08-August/RAW/DSC01234.ARW" in files,
          "its RAW followed it, rather than being filed by the file time")
    check("Sony A6000/2021/02-February/RAW/DSC05555.ARW" in files,
          "and the other capture's RAW followed its own JPEG, not this one")


def test_captures_do_not_reach_across_folders():
    """Two folders can easily both hold an IMG_1234.jpg from different years.
    Treating those as one capture would hand a photo the wrong date, so a
    capture is deliberately scoped to a single folder."""
    (SCRATCH / "holiday").mkdir()
    (SCRATCH / "work").mkdir()
    make_img(SCRATCH / "holiday" / "IMG_1234.jpg", model="SM-S918B",
             date="2016:07:01 12:00:00")
    (SCRATCH / "work" / "IMG_1234.dng").write_bytes(b"unrelated raw" * 60)

    run_app(dry_run=False, operation="move", include_subfolders=True)
    files = relpaths()
    check("Samsung Galaxy S23 Ultra/2016/07-July/IMG_1234.jpg" in files,
          f"the photo went by its own date (got {files})")
    check(not any("2016/07-July" in f and f.endswith(".dng") for f in files),
          "the unrelated RAW in another folder did NOT inherit that date")


def test_a_clip_already_inside_its_photo_is_redundant():
    """A motion photo is one file: a complete JPEG with a short MP4 welded on
    the back. Takeout writes that MP4 out a second time as its own file, and
    truncation strips its extension - so the same bytes are stored twice.
    Matched by content, never by name."""
    clip_bytes = (struct.pack('>I', 16) + b'ftyp' + b'isom\x00\x00\x02\x00'
                  + bytes(range(256)) * 120)
    photo = SCRATCH / "PXL_20250615_013103222.MP.jpg"
    make_img(photo, model="SM-S918B", date="2025:06:15 01:31:00")
    photo.write_bytes(photo.read_bytes() + clip_bytes)
    # the separate copy Takeout also wrote, extension chopped off
    (SCRATCH / "PXL_20250615_013103222.MP").write_bytes(clip_bytes)
    # a genuine video of its own, which must not be touched
    make_mp4_with_date(SCRATCH / "real_video.mp4", datetime(2025, 6, 15, 2, 0))

    stats = run_app(dry_run=False, operation="move", dedupe=True)
    files = relpaths()
    check(any(f.startswith("Duplicates/") and "MP" in f for f in files),
          f"the redundant clip was set aside (got {files})")
    check(any(f.endswith("PXL_20250615_013103222.MP.jpg") for f in files),
          "the motion photo itself was organized as normal")
    check(any(f.endswith("real_video.mp4") and not f.startswith("Duplicates/")
              for f in files),
          "a genuine video was left completely alone")

    reports = list(SCRATCH.glob("kjegla_duplicates_*.txt"))
    check(len(reports) == 1, "the duplicate report covers it")


def test_a_video_is_only_redundant_when_it_really_is_inside_the_photo():
    """The rule is about bytes, not names. A clip that merely shares a photo
    name is not a spare copy of anything."""
    photo = SCRATCH / "PXL_20250615_013103222.MP.jpg"
    make_img(photo, model="SM-S918B", date="2025:06:15 01:31:00")
    photo.write_bytes(photo.read_bytes() + bytes(range(256)) * 100)
    # same capture name, but its bytes are NOT the tail of the photo
    make_mp4_with_date(SCRATCH / "PXL_20250615_013103222.MP.mp4",
                       datetime(2025, 6, 15, 1, 31))

    run_app(dry_run=False, operation="move", dedupe=True)
    files = relpaths()
    check(not any(f.startswith("Duplicates/") for f in files),
          f"nothing was called redundant on the strength of its name (got {files})")


def test_preview_and_a_fresh_execute_agree_on_every_file():
    """The defect this exists to prevent: preview and execute used to be two
    separate implementations, and had already grown different collision
    handling - a preview would say "identical file already there, skipping"
    where the execute made a _1 copy instead. They now share one decision
    pass, so the plan a preview shows must be the plan an execute carries out,
    file for file.

    The awkward cases are deliberately in the fixture: two identical files
    aimed at one destination, and two different files aimed at one destination.
    """
    (SCRATCH / "a").mkdir()
    (SCRATCH / "b").mkdir()
    make_img(SCRATCH / "a" / "dup.jpg", model="SM-S918B", date="2023:05:10 14:30:00")
    shutil.copy2(SCRATCH / "a" / "dup.jpg", SCRATCH / "b" / "dup.jpg")
    make_img(SCRATCH / "a" / "diff.jpg", model="SM-S918B", date="2023:05:10 14:30:00")
    make_img(SCRATCH / "b" / "diff.jpg", model="SM-S918B", date="2023:05:10 14:30:00",
             color='green')
    settings = make_settings(operation="move", include_subfolders=True)

    before = relpaths()
    _stats, plan = core.organize_photos(settings, core.Progress(), dry_run=True)
    previewed = sorted((str(op.source), str(op.target)) for op in plan['ops'])
    check(relpaths() == before, "the preview moved nothing")
    check(previewed, "the preview planned something")

    core.organize_photos(settings, core.Progress(), dry_run=False)
    journal = sorted(SCRATCH.glob("kjegla_undo_*.jsonl"))[-1]
    executed = sorted((origin, target)
                      for target, origin in core.read_undo(journal)['entries'])
    check(previewed == executed,
          f"a fresh execute did exactly what the preview said\n"
          f"  previewed: {previewed}\n  executed:  {executed}")


def test_preview_and_a_cached_replay_agree_on_every_file():
    """Same guarantee for the other path: replaying a cached preview must land
    every file exactly where the preview said, not merely somewhere sensible."""
    (SCRATCH / "a").mkdir()
    (SCRATCH / "b").mkdir()
    make_img(SCRATCH / "a" / "dup.jpg", model="SM-S918B", date="2023:05:10 14:30:00")
    shutil.copy2(SCRATCH / "a" / "dup.jpg", SCRATCH / "b" / "dup.jpg")
    make_img(SCRATCH / "a" / "diff.jpg", model="SM-S918B", date="2023:05:10 14:30:00")
    make_img(SCRATCH / "b" / "diff.jpg", model="SM-S918B", date="2023:05:10 14:30:00",
             color='green')
    settings = make_settings(operation="move", include_subfolders=True)

    _stats, plan = core.organize_photos(settings, core.Progress(), dry_run=True)
    previewed = sorted((str(op.source), str(op.target)) for op in plan['ops'])

    stats = core.execute_cached_plan(plan, settings, core.Progress())
    check(stats is not None, "the replay ran")
    journal = sorted(SCRATCH.glob("kjegla_undo_*.jsonl"))[-1]
    executed = sorted((origin, target)
                      for target, origin in core.read_undo(journal)['entries'])
    check(previewed == executed,
          f"the replay did exactly what the preview said\n"
          f"  previewed: {previewed}\n  executed:  {executed}")


def test_manifest_records_every_file_and_what_became_of_it():
    """The manifest is the run's deliverable alongside the moved files - what
    you read when merging the organised batch into an existing archive."""
    import csv
    build_source()
    run_app(dry_run=False, operation="move", dedupe=True)
    manifests = list(SCRATCH.glob("kjegla_manifest_*.csv"))
    check(len(manifests) == 1, f"a manifest was written (got {len(manifests)})")

    rows = list(csv.DictReader(manifests[0].read_text(encoding='utf-8').splitlines()))
    check(len(rows) == TOTAL, f"one row per file scanned (got {len(rows)})")
    by_name = {Path(r['original_path']).name: r for r in rows}

    cam1 = by_name['cam1.jpg']
    check(cam1['action'] == 'organize', f"cam1.jpg was organized (got {cam1['action']})")
    check(cam1['camera_model'] == 'Samsung Galaxy S23 Ultra',
          f"...under its camera (got {cam1['camera_model']})")
    check(cam1['date_source'] == 'exif',
          f"...dated from EXIF (got {cam1['date_source']})")
    check(cam1['target_path'].startswith('Samsung Galaxy S23 Ultra'),
          f"...and the manifest says where it went (got {cam1['target_path']})")
    check(cam1['verdict'] in ('ok', 'unchecked'),
          f"...with its integrity verdict (got {cam1['verdict']})")

    # A RAW has no EXIF this application reads, so it takes both its date and
    # its camera from the JPEG it was captured with - and the manifest says so
    raw = by_name['DSC00001.ARW']
    check(raw['date_source'] == 'capture',
          f"a RAW's date is recorded as coming from its capture "
          f"(got {raw['date_source']})")
    check(raw['camera_model'] == 'Sony A6000',
          "...along with its camera, from the JPEG it was taken with")
    check(raw['capture_id'] == by_name['DSC00001.JPG']['capture_id'],
          "...and both are recorded as one capture")
    # STATUS.md #2: a RAW with no surviving JPEG has no capture siblings, so
    # it inherits nothing rather than being assumed into the others' folder
    check(by_name['DSC09999.ARW']['date_source'] != 'capture',
          f"a RAW with no surviving JPEG inherits no date from anywhere "
          f"(got {by_name['DSC09999.ARW']['date_source']})")
    check(by_name['DSC09999.ARW']['capture_id']
          != by_name['DSC00001.ARW']['capture_id'],
          "...and is not lumped into someone else's capture")

    screenshot = by_name['Screenshot_20240101-120000.png']
    check(screenshot['is_screenshot'] == 'yes',
          f"a screenshot is recorded as one (got {screenshot['is_screenshot']})")


def test_manifest_carries_the_hashes_the_duplicate_hunt_already_paid_for():
    """These are what let the organised batch be compared against an existing
    archive without reading every byte a second time."""
    import csv
    make_img(SCRATCH / "IMG_1234.jpg", model="SM-S918B", date="2023:05:10 14:30:00")
    shutil.copy2(SCRATCH / "IMG_1234.jpg", SCRATCH / "IMG_1234 (1).jpg")
    make_img(SCRATCH / "alone.jpg", model="SM-S918B", date="2023:05:11 09:00:00",
             color='green')
    run_app(dry_run=False, operation="move", dedupe=True)

    manifest = list(SCRATCH.glob("kjegla_manifest_*.csv"))[0]
    rows = {Path(r['original_path']).name: r
            for r in csv.DictReader(manifest.read_text(encoding='utf-8').splitlines())}
    check(rows['IMG_1234.jpg']['content_hash'],
          "the keeper of a duplicate set has its hash recorded")
    check(rows['IMG_1234 (1).jpg']['content_hash']
          == rows['IMG_1234.jpg']['content_hash'],
          "...and the copy has the same one, because that is why they matched")
    check(rows['IMG_1234 (1).jpg']['action'] == 'duplicate',
          f"the copy is marked a duplicate "
          f"(got {rows['IMG_1234 (1).jpg']['action']})")
    check(rows['IMG_1234 (1).jpg']['duplicate_of'] == 'IMG_1234.jpg',
          f"...naming the file it duplicates "
          f"(got {rows['IMG_1234 (1).jpg']['duplicate_of']})")
    check(rows['alone.jpg']['content_hash'] == '',
          "a file with a unique size was never read, so it has no hash - "
          "nothing is hashed just to fill in a column")


def test_transfers_are_verified_and_a_short_write_keeps_the_original():
    src = SCRATCH / "src.bin"
    src.write_bytes(b"important photo bytes" * 500)
    core.transfer_file(src, SCRATCH / "copied.bin", "copy")
    check((SCRATCH / "copied.bin").read_bytes() == src.read_bytes(),
          "copy arrives byte for byte")
    check(src.exists(), "copy leaves the source where it was")
    core.transfer_file(src, SCRATCH / "moved.bin", "move")
    check(not src.exists(), "move removes the source")
    check((SCRATCH / "moved.bin").read_bytes()
          == (SCRATCH / "copied.bin").read_bytes(), "move arrives byte for byte")

    # Force the cross-volume path and make the copy come up short, the way a
    # full disk or a network drive dropping out would. shutil is patched
    # globally here and restored in the finally, so nothing else is affected.
    victim = SCRATCH / "victim.bin"
    victim.write_bytes(b"the only copy of this photo" * 100)
    real_copy2, real_same_volume = core.shutil.copy2, core._same_volume
    core.shutil.copy2 = lambda s, d: Path(d).write_bytes(Path(s).read_bytes()[:10])
    core._same_volume = lambda a, b: False
    raised = False
    try:
        try:
            core.transfer_file(victim, SCRATCH / "landed.bin", "move")
        except OSError:
            raised = True
    finally:
        core.shutil.copy2, core._same_volume = real_copy2, real_same_volume
    check(raised, "a short write is caught and raised rather than passing silently")
    check(victim.exists(), "the original survives a failed move")
    check(victim.stat().st_size == 2700,
          f"...intact, not half-written (got {victim.stat().st_size} bytes)")
