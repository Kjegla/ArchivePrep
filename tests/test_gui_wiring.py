"""The window's side of the seam.

Everything else in this suite drives organizer_core directly, which is what
makes it fast and screen-free. That leaves exactly one thing unproven: that
the window really does build the settings object the core expects and hand it
over, and really does get the results back.

These tests open a real (hidden) window to check that, and only that. They
are marked `gui` so they can be skipped where no display exists:

    python -m pytest tests/ -m "not gui"
"""
import time

import pytest

from conftest import SCRATCH, build_source, check, make_settings, relpaths
import organizer_core as core
import photo_organizer as po

pytestmark = pytest.mark.gui


def make_app(**settings):
    """The real window, hidden, with its widgets set from make_settings()."""
    import tkinter as tk
    root = tk.Tk()
    root.withdraw()
    app = po.PhotoOrganizerGUI(root)
    s = make_settings(**settings)
    app.source_folder.set(s.source)
    app.operation_mode.set(s.operation)
    app.subfolder_mode.set(s.subfolder_mode)
    app.separate_raw.set(s.separate_raw)
    app.separate_screenshots.set(s.separate_screenshots)
    app.use_multithreading.set(s.use_multithreading)
    app.include_subfolders.set(s.include_subfolders)
    app.dedupe_content.set(s.dedupe_content)
    app.check_corrupt.set(s.check_corrupt)
    app.corrupt_thorough.set(s.corrupt_thorough)
    app.cleanup_empty.set(s.cleanup_empty)
    app.fix_extensions.set(s.fix_extensions)
    return root, app


def wait_for_idle(app, timeout=120):
    deadline = time.time() + timeout
    while app.processing and time.time() < deadline:
        time.sleep(0.02)
    assert not app.processing, "the worker thread never finished"


def test_window_builds_the_settings_the_core_expects():
    """If the window ever stops supplying a field the core reads, every other
    test in this suite would still pass. This is the one that would not."""
    root, app = make_app(operation="move", subfolder="year")
    try:
        from_window = vars(app._snapshot_settings())
        from_tests = vars(make_settings(operation="move", subfolder="year"))
        check(set(from_window) == set(from_tests),
              f"same fields: window has {sorted(set(from_window) - set(from_tests))} "
              f"extra, missing {sorted(set(from_tests) - set(from_window))}")
        for field, value in from_tests.items():
            if field == 'max_threads':
                continue  # the window sizes this from the CPU count
            check(from_window[field] == value,
                  f"{field}: window says {from_window[field]!r}, "
                  f"tests assume {value!r}")
        check(from_window['max_threads'] >= 1, "the window supplies a thread count")
    finally:
        root.destroy()


def test_window_organizes_end_to_end_through_its_own_path():
    """Drives _start_worker, so the settings snapshot, the worker thread, the
    core call and the results coming back are all exercised together."""
    build_source()
    root, app = make_app(operation="move")
    try:
        app._start_worker(dry_run=False)
        wait_for_idle(app)
        files = relpaths()
        check("Samsung Galaxy S23 Ultra/2023/05-May/cam1.jpg" in files,
              f"the window organized the folder (got {files[:3]})")
        check(app.stats['processed'] > 0,
              f"statistics came back to the window (got {app.stats['processed']})")
        check(app.stats['errors'] == 0, f"no errors (got {app.stats['errors']})")
        check(len(list(SCRATCH.glob("kjegla_undo_*.jsonl"))) == 1,
              "an undo record was written")
    finally:
        root.destroy()


def test_preview_caches_a_plan_and_execute_consumes_it():
    build_source()
    root, app = make_app(operation="move")
    try:
        app._start_worker(dry_run=True)
        wait_for_idle(app)
        check(app.cached_plan is not None, "preview left a cached plan on the window")
        check(app.cached_plan['key'] == core.plan_key(app._snapshot_settings()),
              "the cached plan matches the window's current settings")

        app._start_worker(dry_run=False, use_cache=True)
        wait_for_idle(app)
        check(app.cached_plan is None, "the cache is consumed once it has been used")
        check(app.stats['processed'] > 0, "the replay reported what it did")
        top = [p for p in SCRATCH.iterdir()
               if p.is_file() and p.suffix.lower() in core.ALL_MEDIA_EXTS]
        check(top == [], f"the replay emptied the source (got {top})")
    finally:
        root.destroy()


def test_cancel_reaches_the_core():
    """Cancel works by setting an event the core polls; prove the window's
    button is wired to the same event the core is handed."""
    root, app = make_app()
    try:
        check(app.progress.cancelled is False, "not cancelled to begin with")
        app.cancel_operation()
        check(app.progress.cancelled is True,
              "the Cancel button sets the flag the core watches")
        app.progress.cancel.clear()
        check(app.progress.cancelled is False, "and a new run clears it again")
    finally:
        root.destroy()
