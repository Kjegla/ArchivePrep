"""The window's side of the seam.

Everything else in this suite drives archiveprep_core directly, which is what
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
import archiveprep_core as core
import archiveprep as ap

pytestmark = pytest.mark.gui


@pytest.fixture(scope="module")
def tk_root():
    """One Tk root for the whole module.

    Creating and destroying a root per test is flaky - Tcl intermittently
    fails with "invalid command name tcl_findLibrary" when a second root goes
    up in the same process. Each test gets its own Toplevel on this one
    instead, which is what the window would be in a real session anyway.

    If Tcl cannot start at all, skip rather than fail. These tests are already
    marked `gui` precisely because a display is not always available, and a
    machine where Tk will not initialise is exactly that case.

    The skip is deliberately scoped to *root creation only*. Building the
    window itself happens in the tests, so a genuine bug in ArchivePrepGUI
    still fails loudly - this hides an unusable Tcl, not a broken application.
    """
    import tkinter as tk
    try:
        root = tk.Tk()
    except tk.TclError as e:
        pytest.skip(f"no usable Tk on this machine: {e}")
    root.withdraw()
    yield root
    root.destroy()


def make_app(tk_root, **settings):
    """The real window, hidden, with its widgets set from make_settings()."""
    import tkinter as tk
    root = tk.Toplevel(tk_root)
    root.withdraw()
    app = ap.ArchivePrepGUI(root)
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


def test_window_builds_the_settings_the_core_expects(tk_root):
    """If the window ever stops supplying a field the core reads, every other
    test in this suite would still pass. This is the one that would not."""
    root, app = make_app(tk_root, operation="move", subfolder="year")
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


def test_window_organizes_end_to_end_through_its_own_path(tk_root):
    """Drives _start_worker, so the settings snapshot, the worker thread, the
    core call and the results coming back are all exercised together."""
    build_source()
    root, app = make_app(tk_root, operation="move")
    try:
        app._start_worker(dry_run=False)
        wait_for_idle(app)
        files = relpaths()
        check("Samsung Galaxy S23 Ultra/2023/05-May/cam1.jpg" in files,
              f"the window organized the folder (got {files[:3]})")
        check(app.stats['processed'] > 0,
              f"statistics came back to the window (got {app.stats['processed']})")
        check(app.stats['errors'] == 0, f"no errors (got {app.stats['errors']})")
        check(len(list(SCRATCH.glob("archiveprep_undo_*.jsonl"))) == 1,
              "an undo record was written")
    finally:
        root.destroy()


def test_preview_caches_a_plan_and_execute_consumes_it(tk_root):
    build_source()
    root, app = make_app(tk_root, operation="move")
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


def test_the_defaults_the_window_opens_with(tk_root):
    """What a first-time user gets without touching anything.

    Extension repair in particular must be off: it was never one of the
    problems this application exists to solve, and a maintenance extra should
    not be something you have to notice and switch off.
    """
    import tkinter as tk
    root = tk.Toplevel(tk_root)
    root.withdraw()
    app = ap.ArchivePrepGUI(root)
    try:
        s = app._snapshot_settings()
        check(s.fix_extensions is False,
              f"extension repair is off by default (got {s.fix_extensions})")
        check(s.operation == "move", f"operation defaults to move (got {s.operation})")
        check(s.dedupe_content is True,
              "finding duplicates by content is on - it is a core capability")
        check(s.check_corrupt is False,
              "the damage check is off by default; it is the slower path")
        check(s.cleanup_empty is True, "empty folders are swept after a move")
        check(s.include_subfolders is False,
              "subfolders are not scanned unless asked for")
    finally:
        root.destroy()


def test_log_search_highlights_every_match_and_steps_between_them(tk_root):
    """The log is where you answer "what happened to this file?" during a run.

    Searching it must actually mark the text - a count alone would leave you
    scrolling for the line it counted.
    """
    import tkinter as tk
    root = tk.Toplevel(tk_root)
    root.withdraw()
    app = ap.ArchivePrepGUI(root)
    try:
        app.output_text.insert(tk.END,
                               "[moved] IMG_0001.jpg\n"
                               "[skip] IMG_0002.jpg identical\n"
                               "[duplicate] IMG_0001 (1).jpg\n")

        app.log_search.set("IMG_0001")
        check(len(app._matches) == 2,
              f"both occurrences found (got {len(app._matches)})")
        check(app.match_label.cget('text') == "1 of 2",
              f"the count is shown (got {app.match_label.cget('text')!r})")
        check(app.output_text.tag_ranges('match') != (),
              "the matches are tinted in the widget, not merely counted")

        first = str(app.output_text.tag_ranges('match_current')[0])
        app._step_match(1)
        check(app.match_label.cget('text') == "2 of 2", "Next advances the count")
        check(str(app.output_text.tag_ranges('match_current')[0]) != first,
              "and moves the highlight that marks where you are")
        app._step_match(1)
        check(app.match_label.cget('text') == "1 of 2", "Next wraps at the end")
        app._step_match(-1)
        check(app.match_label.cget('text') == "2 of 2", "Previous wraps the other way")

        app.log_search.set("img_0002")
        check(len(app._matches) == 1,
              f"searching ignores case (got {len(app._matches)})")

        app.log_search.set("no such line")
        check(app._matches == [] and app.match_label.cget('text') == "no matches",
              "a term that is not there says so rather than staying silent")

        app.log_search.set("")
        check(app.output_text.tag_ranges('match') == (),
              "emptying the box takes the highlight off again")
    finally:
        root.destroy()


def test_log_search_keeps_up_while_a_run_is_still_printing(tk_root):
    """A search started mid-run must count lines that arrive after it, and
    must not yank the log back to the first match while it does."""
    import tkinter as tk
    root = tk.Toplevel(tk_root)
    root.withdraw()
    app = ap.ArchivePrepGUI(root)
    try:
        app.output_text.insert(tk.END, "[moved] a.jpg\n[moved] b.jpg\n")
        app.log_search.set("moved")
        app._step_match(1)
        check(app.match_label.cget('text') == "2 of 2", "sitting on the second match")

        app.output_text.insert(tk.END, "[moved] c.jpg\n")
        app._highlight_matches(keep_position=True)
        check(len(app._matches) == 3,
              f"the new line was counted too (got {len(app._matches)})")
        check(app.match_label.cget('text') == "2 of 3",
              f"without jumping back to the first (got "
              f"{app.match_label.cget('text')!r})")

        app.clear_log()
        check(app._matches == [], "clearing the log clears the search with it")
    finally:
        root.destroy()


def test_cancel_reaches_the_core(tk_root):
    """Cancel works by setting an event the core polls; prove the window's
    button is wired to the same event the core is handed."""
    root, app = make_app(tk_root)
    try:
        check(app.progress.cancelled is False, "not cancelled to begin with")
        app.cancel_operation()
        check(app.progress.cancelled is True,
              "the Cancel button sets the flag the core watches")
        app.progress.cancel.clear()
        check(app.progress.cancelled is False, "and a new run clears it again")
    finally:
        root.destroy()
