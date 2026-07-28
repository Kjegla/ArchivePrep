"""End-to-end tests for photo_organizer.py, driving the real app headlessly.

    python -m pytest tests/ -q

Every test builds real files in a temporary folder and drives the actual
tkinter app with its window hidden, so what is tested is what ships - not a
stripped-down copy of the logic.

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
import shutil
import struct
import tkinter as tk
from datetime import datetime
from pathlib import Path

import photo_organizer as po
from conftest import (SCRATCH, TOTAL, build_source, check, make_app, make_big_img,
                      make_img, make_mp4_with_date, relpaths, run_app, run_undo,
                      truncate)


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
                 if p.is_file() and p.suffix.lower() in po.ALL_MEDIA_EXTS]
    check(top_media == [], f"no media left at top level (got {top_media})")
    check(stats['processed'] == TOTAL,
          f"move processed {TOTAL} (got {stats['processed']})")
    check(stats['errors'] == 0, f"move errors 0 (got {stats['errors']})")

    undo_files = sorted(SCRATCH.glob("kjegla_undo_*.jsonl"))
    check(len(undo_files) == 1, "one undo record present")
    record = po.PhotoOrganizerGUI._read_undo(undo_files[0])
    check(record['operation'] == 'move', "undo record has operation=move")
    check(len(record['entries']) == TOTAL, f"undo record has {TOTAL} entries")

    run_undo(undo_files[0], record)
    top_media = sorted(p.name for p in SCRATCH.iterdir()
                       if p.is_file() and p.suffix.lower() in po.ALL_MEDIA_EXTS)
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
    check(po.friendly_camera_name("SM-S918B") == "Samsung Galaxy S23 Ultra",
          "model mapping S23 Ultra")
    check(po.friendly_camera_name("iPhone15,2") == "iPhone 14 Pro",
          "iPhone15,2 -> iPhone 14 Pro")
    check(po.model_for_image(Path("x.heic"), None) == "iPhone",
          "unreadable HEIC -> iPhone")
    check(po.looks_like_screenshot(Path("class_photo.jpg"), None) is False,
          "class_photo not screenshot")
    check(po.looks_like_screenshot(Path("Screenshot_x.png"), None) is True,
          "Screenshot_ prefix detected")

    tmp_mp4 = SCRATCH / "unit_test.mp4"
    make_mp4_with_date(tmp_mp4, datetime(2015, 3, 1, 12, 0))
    d = po.read_video_date(tmp_mp4)
    check(d is not None and d.year == 2015, f"read_video_date parses mvhd (got {d})")
    bad_mp4 = SCRATCH / "bad.mp4"
    bad_mp4.write_bytes(b"not a real mp4 at all")
    check(po.read_video_date(bad_mp4) is None, "read_video_date safe on garbage")

    f1, f2, f3 = SCRATCH / "h1.bin", SCRATCH / "h2.bin", SCRATCH / "h3.bin"
    f1.write_bytes(b"same content")
    f2.write_bytes(b"same content")
    f3.write_bytes(b"other stuff!")
    check(po.files_identical(f1, f2) is True, "files_identical true for same content")
    check(po.files_identical(f1, f3) is False,
          "files_identical false for different content")
    check(po.HEIF_AVAILABLE, "pillow-heif is active")


def test_copy_name_detection():
    for stem in ["IMG_1234 (1)", "IMG_1234 (12)", "IMG_1234(1)", "photo - Copy",
                 "photo - copy", "photo - Copy (2)", "photo copy 2",
                 "photo - kopi", "vacation copy"]:
        check(po.looks_like_copy_name(stem) is True, f"'{stem}' looks like a copy")
    # Camera/phone filenames must NOT be mistaken for copies - a trailing "_2" is
    # how nearly every camera names its files, so it can't be a copy marker.
    for stem in ["IMG_1234", "DSC00001", "DSC-0001", "2023-05-10 vacation", "photo",
                 "IMG_20230510_143000", "photo_2", "photo-3", "photocopy",
                 "Sunset (2 of 3)"]:
        check(po.looks_like_copy_name(stem) is False,
              f"'{stem}' does not look like a copy")


def test_keeper_ranking():
    p_good = SCRATCH / "a.jpg"
    p_good.write_bytes(b"x")
    p_bad = SCRATCH / "b.jpg"
    p_bad.write_bytes(b"x")
    rank = po.PhotoOrganizerGUI._keeper_rank
    health = {p_good: ('ok', ''), p_bad: ('damaged', 'truncated')}
    check(rank(p_good, {}, health) < rank(p_bad, {}, health),
          "a healthy copy always outranks a damaged one")
    meta = {p_good: {'model': 'X', 'date': datetime.now()}, p_bad: {}}
    check(rank(p_good, meta, {}) < rank(p_bad, meta, {}),
          "a copy with camera info and a date outranks one with neither")
    p_orig = SCRATCH / "IMG_1.jpg"
    p_orig.write_bytes(b"x")
    p_copy = SCRATCH / "IMG_1 (1).jpg"
    p_copy.write_bytes(b"x")
    check(rank(p_orig, {}, {}) < rank(p_copy, {}, {}),
          "a clean filename outranks a '(1)' one")
    check(rank(p_orig, {}, {}) == rank(p_orig, {}, {}),
          "ranking is stable for the same file")


def test_empty_folder_sweep():
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


# ---------------------------------------------------------------------------
# Preview caching
# ---------------------------------------------------------------------------

def test_cached_preview_replay():
    build_source()
    root, app = make_app(operation="move")
    settings = app._snapshot_settings()
    app.organize_photos(settings, dry_run=True)
    check(app.cached_plan is not None, "dry run stores a cached plan")
    check(len(app.cached_plan['ops']) == TOTAL, f"plan has {TOTAL} ops")
    check(app.cached_plan['key'] == po.plan_key(settings), "plan key matches settings")
    ok = app._execute_cached_plan(app.cached_plan, settings)
    check(ok is True, "cached plan executes when folder unchanged")
    check(app.stats['processed'] == TOTAL,
          f"replay processed {TOTAL} (got {app.stats['processed']})")
    check(app.stats['errors'] == 0, "replay had no errors")
    check(app.cached_plan is None, "cache consumed after execute")
    files = relpaths()
    check("Samsung Galaxy S23 Ultra/2023/05-May/cam1.jpg" in files,
          "replay: cam1.jpg at planned target")
    check("iPhone 14 Pro/2023/12-December/real_iphone.heic" in files,
          "replay: HEIC at planned target")
    top_media = [p for p in SCRATCH.iterdir()
                 if p.is_file() and p.suffix.lower() in po.ALL_MEDIA_EXTS]
    check(top_media == [], "replay: source top level emptied (move)")
    check(len(list(SCRATCH.glob("kjegla_undo_*.jsonl"))) == 1,
          "replay wrote an undo record")
    root.destroy()


def test_cache_invalidation():
    import os
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


def test_settings_invalidate_cached_preview():
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
    rec = po.PhotoOrganizerGUI._read_undo(rename_records[0])
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
    record = po.PhotoOrganizerGUI._read_undo(undo_files[0])
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
    root, app = make_app(operation="move", check_corrupt=True)
    app._run_health_check(app._snapshot_settings())
    stats = app.stats
    root.destroy()
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
    record = po.PhotoOrganizerGUI._read_undo(journals[0])
    check(record['operation'] == 'move', "the header still reads after a torn tail")
    check(len(record['entries']) == TOTAL - 1,
          f"the torn line is dropped and everything before it survives "
          f"(got {len(record['entries'])})")

    run_undo(journals[0], record)
    restored = [p for p in SCRATCH.iterdir()
                if p.is_file() and p.suffix.lower() in po.ALL_MEDIA_EXTS]
    check(len(restored) == TOTAL - 1,
          f"undo restored every file the journal still held (got {len(restored)})")


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
    record = po.PhotoOrganizerGUI._read_undo(legacy)
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
    record = po.PhotoOrganizerGUI._read_undo(journals[0])
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


def test_transfers_are_verified_and_a_short_write_keeps_the_original():
    src = SCRATCH / "src.bin"
    src.write_bytes(b"important photo bytes" * 500)
    po.transfer_file(src, SCRATCH / "copied.bin", "copy")
    check((SCRATCH / "copied.bin").read_bytes() == src.read_bytes(),
          "copy arrives byte for byte")
    check(src.exists(), "copy leaves the source where it was")
    po.transfer_file(src, SCRATCH / "moved.bin", "move")
    check(not src.exists(), "move removes the source")
    check((SCRATCH / "moved.bin").read_bytes()
          == (SCRATCH / "copied.bin").read_bytes(), "move arrives byte for byte")

    # Force the cross-volume path and make the copy come up short, the way a
    # full disk or a network drive dropping out would. shutil is patched
    # globally here and restored in the finally, so nothing else is affected.
    victim = SCRATCH / "victim.bin"
    victim.write_bytes(b"the only copy of this photo" * 100)
    real_copy2, real_same_volume = po.shutil.copy2, po._same_volume
    po.shutil.copy2 = lambda s, d: Path(d).write_bytes(Path(s).read_bytes()[:10])
    po._same_volume = lambda a, b: False
    raised = False
    try:
        try:
            po.transfer_file(victim, SCRATCH / "landed.bin", "move")
        except OSError:
            raised = True
    finally:
        po.shutil.copy2, po._same_volume = real_copy2, real_same_volume
    check(raised, "a short write is caught and raised rather than passing silently")
    check(victim.exists(), "the original survives a failed move")
    check(victim.stat().st_size == 2700,
          f"...intact, not half-written (got {victim.stat().st_size} bytes)")
