#!/usr/bin/env python3
"""ArchivePrep - the window.

Prepares a messy media collection for archival: sorts by camera model, finds
duplicates by content, sets damaged files aside, repairs filenames that lie
about their format, and can undo the lot.

This file is the user interface and nothing else. All of the actual work
lives in archiveprep_core.py, which knows nothing about tkinter - so the
behaviour can be tested, and driven, without a screen. What crosses between
them is a Progress object going in, and statistics coming back.
"""
import multiprocessing
import queue
import threading
import tkinter as tk
from pathlib import Path
from tkinter import ttk, filedialog, messagebox, scrolledtext
from types import SimpleNamespace

import archiveprep_core as core

# The window uses whatever ttk theme the operating system provides - on Windows
# that means the widgets are drawn by the OS itself.
#
# It previously used sv-ttk, which imitates Windows 11 by compositing every
# widget from sprite images in software. Measured with the real window: an
# empty window cost 5ms to maximise, but this one - 47 widgets - cost 2,245ms,
# against 136ms with the native theme, and 783ms of startup against 226ms. The
# cost is per widget, so it grew with the window: a maximise on a 1440p screen
# took around five seconds and repainted blank while it worked.
#
# Losing it also lost dark mode, which was a real trade and a deliberate one.


class ArchivePrepGUI:
    def __init__(self, root):
        self.root = root
        self.root.title("ArchivePrep")
        self.root.geometry("960x780")

        # Variables
        self.source_folder = tk.StringVar()
        self.operation_mode = tk.StringVar(value="move")
        self.subfolder_mode = tk.StringVar(value="none")  # none, year, month, year-month
        self.separate_raw = tk.BooleanVar(value=False)
        self.separate_screenshots = tk.BooleanVar(value=False)
        self.use_multithreading = tk.BooleanVar(value=True)
        self.include_subfolders = tk.BooleanVar(value=False)
        self.dedupe_content = tk.BooleanVar(value=True)
        self.check_corrupt = tk.BooleanVar(value=False)
        self.corrupt_thorough = tk.BooleanVar(value=False)
        self.cleanup_empty = tk.BooleanVar(value=True)
        # Log search state: where every match starts, and which one Next and
        # Previous are currently sitting on.
        self.log_search = tk.StringVar()
        self._matches = []
        self._match_index = -1
        self._dropped_lines = 0
        self.processing = False
        self.last_undo_file = None
        self.cached_plan = None  # preview results reusable by Execute
        self.queue = queue.Queue()

        # Handed to the core for every run: it reports through this, and
        # Cancel works by setting its event.
        self.progress = core.Progress(self.queue)

        self.max_threads = min(multiprocessing.cpu_count(), 12)

        # Statistics tracking
        self.stats = core._empty_stats()

        if not core.PIL_AVAILABLE:
            self.show_dependency_error()
            return

        self.setup_ui()

        # Start queue checker
        self.root.after(100, self.check_queue)

    def show_dependency_error(self):
        """Show error message if the Pillow library is not installed."""
        error_frame = ttk.Frame(self.root, padding="20")
        error_frame.pack(fill=tk.BOTH, expand=True)

        ttk.Label(error_frame, text="Required library missing",
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
        self.root.minsize(920, 720)
        main_frame.columnconfigure(1, weight=1)

        # The window's own title bar already says ArchivePrep; repeating it
        # in 16pt bold underneath was decoration.
        ttk.Label(main_frame, text="Source folder").grid(
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
        extras_frame.pack(fill=tk.X, pady=(0, 8))

        self.raw_checkbox = ttk.Checkbutton(
            extras_frame,
            text="Separate RAW files into 'RAW' subfolder",
            variable=self.separate_raw)
        self.raw_checkbox.pack(anchor=tk.W, pady=3)

        self.screenshot_checkbox = ttk.Checkbutton(
            extras_frame,
            text="Separate screenshots",
            variable=self.separate_screenshots)
        self.screenshot_checkbox.pack(anchor=tk.W, pady=3)

        self.subfolders_checkbox = ttk.Checkbutton(
            extras_frame,
            text="Include subfolders (scan recursively)",
            variable=self.include_subfolders)
        self.subfolders_checkbox.pack(anchor=tk.W, pady=3)

        video_note = ttk.Label(
            extras_frame,
            text="Videos go to a 'Videos' subfolder automatically.",
            font=('Segoe UI', 9), foreground='#888888', justify=tk.LEFT)
        video_note.pack(anchor=tk.W, pady=(6, 2))

        # Cleanup & safety (right column, below Options)
        safety_frame = ttk.LabelFrame(right_col, text="Cleanup & Safety",
                                      padding="10")
        safety_frame.pack(fill=tk.BOTH, expand=True)

        self.dedupe_checkbox = ttk.Checkbutton(
            safety_frame,
            text="Find duplicates by content (any filename)",
            variable=self.dedupe_content)
        self.dedupe_checkbox.pack(anchor=tk.W, pady=3)

        self.corrupt_checkbox = ttk.Checkbutton(
            safety_frame,
            text="Check files for damage while organizing",
            variable=self.check_corrupt, command=self._sync_thorough_state)
        self.corrupt_checkbox.pack(anchor=tk.W, pady=3)

        self.thorough_checkbox = ttk.Checkbutton(
            safety_frame,
            text="Thorough check (much slower)",
            variable=self.corrupt_thorough)
        self.thorough_checkbox.pack(anchor=tk.W, padx=(20, 0), pady=(0, 3))

        self.cleanup_checkbox = ttk.Checkbutton(
            safety_frame,
            text="Delete empty folders left behind (Move only)",
            variable=self.cleanup_empty)
        self.cleanup_checkbox.pack(anchor=tk.W, pady=3)

        safety_note = ttk.Label(
            safety_frame,
            text="Nothing is ever deleted. Duplicates go to 'Duplicates'\n"
                 "and damaged files to 'Corrupt'. Undo puts it all back.",
            font=('Segoe UI', 9), foreground='#888888', justify=tk.LEFT)
        safety_note.pack(anchor=tk.W, pady=(6, 2))

        self._sync_thorough_state()

        # Action buttons
        button_frame = ttk.Frame(main_frame)
        button_frame.grid(row=3, column=0, columnspan=3, pady=10)

        self.preview_btn = ttk.Button(button_frame, text="Preview (Dry Run)",
                                      command=self.preview_operation)
        self.preview_btn.pack(side=tk.LEFT, padx=4)

        self.check_btn = ttk.Button(button_frame, text="Check Files",
                                    command=self.check_files_operation)
        self.check_btn.pack(side=tk.LEFT, padx=4)

        self.execute_btn = ttk.Button(button_frame, text="Execute",
                                      command=self.execute_operation)
        self.execute_btn.pack(side=tk.LEFT, padx=4)

        self.undo_btn = ttk.Button(button_frame, text="Undo Last Run",
                                   command=self.undo_last_operation, state=tk.DISABLED)
        self.undo_btn.pack(side=tk.LEFT, padx=4)

        self.stats_btn = ttk.Button(button_frame, text="Summary",
                                    command=self.show_statistics, state=tk.DISABLED)
        self.stats_btn.pack(side=tk.LEFT, padx=4)

        self.cancel_btn = ttk.Button(button_frame, text="Cancel",
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

        # Finding a line without leaving the window. A run against a real
        # archive prints thousands of lines, and until now the only way to
        # answer "what happened to this one file?" was to open the .txt on
        # disk - which is the log you go to afterwards, not the one in front
        # of you.
        search_frame = ttk.Frame(output_frame)
        search_frame.grid(row=1, column=0, sticky=(tk.W, tk.E), pady=(5, 0))

        ttk.Label(search_frame, text="Find:").pack(side=tk.LEFT)
        self.log_search.trace_add('write', lambda *_: self._highlight_matches())
        search_entry = ttk.Entry(search_frame, textvariable=self.log_search,
                                 width=32)
        search_entry.pack(side=tk.LEFT, padx=(4, 6))
        search_entry.bind('<Return>', lambda _e: self._step_match(1))
        search_entry.bind('<Shift-Return>', lambda _e: self._step_match(-1))
        search_entry.bind('<Escape>', lambda _e: self.log_search.set(''))

        ttk.Button(search_frame, text="Previous", width=10,
                   command=lambda: self._step_match(-1)).pack(side=tk.LEFT)
        ttk.Button(search_frame, text="Next", width=10,
                   command=lambda: self._step_match(1)).pack(side=tk.LEFT,
                                                             padx=(4, 8))

        self.match_label = ttk.Label(search_frame, text="")
        self.match_label.pack(side=tk.LEFT)

        ttk.Button(search_frame, text="Clear Log",
                   command=self.clear_log).pack(side=tk.RIGHT)

        # Every match is tinted, and the one being stepped through is tinted
        # more strongly - otherwise Next and Previous have nothing visible to
        # move between.
        self.output_text.tag_config('match', background='#ffe9a3')
        self.output_text.tag_config('match_current', background='#ffab2e')
        self.output_text.tag_raise('match_current', 'match')

        self.root.bind('<Control-f>', lambda _e: self._focus_search(search_entry))

        main_frame.rowconfigure(6, weight=1)

    def _focus_search(self, entry):
        """Ctrl+F puts the cursor in the find box with the old term selected."""
        entry.focus_set()
        entry.select_range(0, tk.END)

    def _highlight_matches(self, keep_position=False):
        """Tint every occurrence of the search term in the log.

        Called on every keystroke, and again whenever new lines arrive while a
        search is active - so the count stays true during a run rather than
        going stale the moment the log grows.
        """
        self.output_text.tag_remove('match', '1.0', tk.END)
        self.output_text.tag_remove('match_current', '1.0', tk.END)

        needle = self.log_search.get()
        self._matches = []
        if not needle:
            self.match_label.config(text="")
            self._match_index = -1
            return

        idx = '1.0'
        while True:
            hit = self.output_text.search(needle, idx, stopindex=tk.END,
                                          nocase=True)
            if not hit:
                break
            end = f"{hit}+{len(needle)}c"
            self.output_text.tag_add('match', hit, end)
            self._matches.append(hit)
            idx = end

        if not self._matches:
            self.match_label.config(text="no matches")
            self._match_index = -1
            return

        # Typing jumps to the first match; new lines arriving mid-run must not,
        # or the log would yank itself around while you are reading it.
        if not keep_position or not 0 <= self._match_index < len(self._matches):
            self._match_index = 0
            keep_position = False
        self._show_current_match(scroll=not keep_position)

    def _show_current_match(self, scroll=True):
        """Mark which match Next and Previous are sitting on, and count them."""
        self.output_text.tag_remove('match_current', '1.0', tk.END)
        start = self._matches[self._match_index]
        self.output_text.tag_add('match_current', start,
                                 f"{start}+{len(self.log_search.get())}c")
        if scroll:
            self.output_text.see(start)
        self.match_label.config(
            text=f"{self._match_index + 1} of {len(self._matches)}")

    def _step_match(self, direction):
        """Move to the next or previous match, wrapping at either end."""
        if not self._matches:
            return
        self._match_index = (self._match_index + direction) % len(self._matches)
        self._show_current_match()

    def _sync_thorough_state(self):
        """The thorough toggle only means anything when checking is switched on."""
        self.thorough_checkbox.config(
            state=tk.NORMAL if self.check_corrupt.get() else tk.DISABLED)

    def show_statistics(self):
        """What the run found, in one place.

        This is where the tally lives. The run log says what changed as it
        goes; this answers "so what did it do?" afterwards, and it is short on
        purpose - five numbers you would actually act on, then the breakdown
        by camera and year.
        """
        if not self.stats['total_files']:
            messagebox.showinfo(
                "Summary", "Nothing to summarise yet - run a preview first.")
            return

        stats_window = tk.Toplevel(self.root)
        stats_window.title("Run Summary")
        stats_window.geometry("460x560")

        stats_text = scrolledtext.ScrolledText(stats_window, wrap=tk.WORD,
                                               width=56, height=28,
                                               font=('Consolas', 10),
                                               borderwidth=0)
        stats_text.pack(padx=10, pady=10, fill=tk.BOTH, expand=True)

        s = self.stats
        lines = [
            f"Files scanned      {s['total_files']}",
            f"Files organized    {s['processed']}",
            f"Duplicates found   {s['content_duplicates']}"
            f"  ({s['duplicate_bytes'] / (1024 * 1024):.0f} MB)",
            f"Damaged files      {s['damaged']}",
            f"Unknown dates      {s['no_date']}",
        ]
        if s['already_organized']:
            lines.append(f"Already in place   {s['already_organized']}")
        if s['errors']:
            lines.append(f"Errors             {s['errors']}")

        report = "\n".join(lines) + "\n"

        if s['by_model']:
            report += "\nBy camera\n"
            for model, count in sorted(s['by_model'].items(),
                                       key=lambda x: x[1], reverse=True):
                report += f"  {model}: {count}\n"

        if s['by_year']:
            report += "\nBy year\n"
            for year, count in sorted(s['by_year'].items()):
                report += f"  {year}: {count}\n"

        stats_text.insert(1.0, report)
        stats_text.config(state=tk.DISABLED)

        ttk.Button(stats_window, text="Close",
                   command=stats_window.destroy).pack(pady=10)

    def browse_folder(self):
        """Open folder browser dialog."""
        folder = filedialog.askdirectory(title="Select Source Folder")
        if folder:
            self.source_folder.set(folder)
            # Offer undo if the folder holds a not-yet-undone record. Both
            # patterns are checked so a run made with v35 or earlier, whose
            # record is a single .json object, can still be undone.
            undo_files = sorted(list(Path(folder).glob("archiveprep_undo_*.jsonl"))
                                + list(Path(folder).glob("archiveprep_undo_*.json")))
            self.last_undo_file = str(undo_files[-1]) if undo_files else None
            if not self.processing:
                self.undo_btn.config(
                    state=tk.NORMAL if self.last_undo_file else tk.DISABLED)

    def clear_log(self):
        """Clear the output log."""
        self.output_text.delete(1.0, tk.END)
        self._matches = []
        self._match_index = -1
        self._dropped_lines = 0
        self.match_label.config(text="" if not self.log_search.get()
                                else "no matches")

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
        leftover_folders = None
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
                    self.check_btn.config(state=tk.NORMAL)
                    self.cancel_btn.config(state=tk.DISABLED)
                    self.stats_btn.config(state=tk.NORMAL if self.stats['total_files'] > 0 else tk.DISABLED)
                    self.undo_btn.config(state=tk.NORMAL if self.last_undo_file else tk.DISABLED)
                elif action == "disable_buttons":
                    self.preview_btn.config(state=tk.DISABLED)
                    self.execute_btn.config(state=tk.DISABLED)
                    self.check_btn.config(state=tk.DISABLED)
                    self.undo_btn.config(state=tk.DISABLED)
                    self.cancel_btn.config(state=tk.NORMAL)
                elif action == "undo_available":
                    self.last_undo_file = value
                elif action == "plan_stale":
                    self.cached_plan = None
                elif action == "leftover_folders":
                    leftover_folders = value

        except queue.Empty:
            pass

        if log_lines:
            self.output_text.insert(tk.END, "\n".join(log_lines) + "\n")
            # Keep the widget bounded so very large runs stay responsive. It
            # says so when it drops lines: silently discarding them makes the
            # log lie about the run, and searching for something that scrolled
            # off would fail with no explanation.
            line_count = int(self.output_text.index('end-1c').split('.')[0])
            if line_count > 6000:
                dropped = line_count - 5000
                self.output_text.delete('1.0', f'{dropped}.0')
                # One of those deleted lines was the previous notice, not a
                # line of the run.
                self._dropped_lines += dropped - (1 if self._dropped_lines else 0)
                self.output_text.insert(
                    '1.0',
                    f"({self._dropped_lines} earlier line(s) are not "
                    f"shown here - the run log on disk has all of them)\n")
            # Following the tail is only helpful while nobody is reading. A
            # search means someone is: scrolling to the end here would drag
            # the view off the match they just found, 100ms after they found
            # it, which looked exactly like Find not working.
            if self.log_search.get():
                self._highlight_matches(keep_position=True)
            else:
                self.output_text.see(tk.END)
        if last_status is not None:
            self.status_label.config(text=last_status)
        if last_progress is not None:
            self.progress_var.set(last_progress)
        if leftover_folders:
            self._offer_to_clear_leftovers(leftover_folders)

        self.root.after(100, self.check_queue)

    def _offer_to_clear_leftovers(self, folders):
        """Ask whether to delete what the archive left behind.

        The only place this application deletes a file, and it never happens
        without being asked. Two questions rather than one: caches an
        operating system regenerates are a different decision from sidecars it
        does not, and bundling them would let one click discard something
        nothing brings back.
        """
        entries = [e for _folder, es in folders for e in es]
        caches = [e for e in entries if core.is_regenerable_leftover(e)]
        sidecars = [e for e in entries if core.is_inert_sidecar(e)]

        listed = "\n".join(f"  {f.name}" for f, _ in folders[:15])
        if len(folders) > 15:
            listed += f"\n  ... and {len(folders) - 15} more"

        parts = [f"{len(folders)} folder(s) hold nothing your archive wants:",
                 "", listed, ""]
        if caches:
            kinds = ", ".join(sorted({e.name for e in caches}))
            parts.append(f"{len(caches)} file(s) Windows or macOS regenerate "
                         f"by themselves ({kinds}). These are why the folders "
                         f"look empty in Explorer - the files are hidden.")
        if sidecars:
            kinds = ", ".join(sorted({e.suffix.lower() for e in sidecars}))
            parts.append(f"{len(sidecars)} sidecar file(s) ({kinds}) whose "
                         f"photo has already moved to the archive.")
        parts += ["", "Delete them and remove the folders?"]

        if not messagebox.askyesno("Leftover folders", "\n".join(parts)):
            return

        include_sidecars = False
        if sidecars:
            kinds = ", ".join(sorted({e.suffix.lower() for e in sidecars}))
            include_sidecars = messagebox.askyesno(
                "Sidecar files",
                f"Also delete the {len(sidecars)} sidecar file(s) ({kinds})?\n\n"
                f"An .aae holds the edits you made in Apple Photos. Nothing "
                f"outside Apple reads it - not Windows, not a NAS, not Immich "
                f"- and if you exported edited copies, those edits are already "
                f"in the photo itself.\n\n"
                f"Unlike the caches, nothing regenerates these. Answer No and "
                f"they stay exactly where they are, along with their folders.")

        def work(progress):
            deleted, removed = core.remove_leftovers(
                folders, progress, include_sidecars=include_sidecars)
            progress.log(f"\nRemoved {removed} folder(s) and {deleted} "
                         f"leftover file(s)")

        self._run_in_background(work)

    def _snapshot_settings(self):
        """Read all tkinter variables on the main thread into a plain object.

        Everything the core needs to decide what to do, and nothing it could
        read only by touching a widget from the worker thread.
        """
        return SimpleNamespace(
            source=self.source_folder.get(),
            operation=self.operation_mode.get(),
            subfolder_mode=self.subfolder_mode.get(),
            separate_raw=self.separate_raw.get(),
            separate_screenshots=self.separate_screenshots.get(),
            use_multithreading=self.use_multithreading.get(),
            include_subfolders=self.include_subfolders.get(),
            dedupe_content=self.dedupe_content.get(),
            check_corrupt=self.check_corrupt.get(),
            corrupt_thorough=self.corrupt_thorough.get(),
            cleanup_empty=self.cleanup_empty.get(),
            max_threads=self.max_threads,
        )

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

    def _run_in_background(self, work):
        """Run one piece of core work on a worker thread, with the buttons
        disabled while it goes. `work` takes the Progress object."""
        self.processing = True
        self.progress.cancel.clear()
        self.queue.put(("disable_buttons", None, None))

        def run():
            try:
                work(self.progress)
            except Exception as e:
                self.log(f"\nERROR unexpected: {e}")
            finally:
                self.processing = False
                self.queue.put(("enable_buttons", None, None))
                self.update_progress(0)

        threading.Thread(target=run, daemon=True).start()

    def _start_worker(self, dry_run, use_cache=False):
        """Snapshot settings and launch the organizing run."""
        settings = self._snapshot_settings()

        def work(progress):
            if use_cache and not dry_run:
                plan = self.cached_plan
                if plan and plan['key'] == core.plan_key(settings):
                    stats = core.execute_cached_plan(plan, settings, progress)
                    if stats is not None:
                        self.stats = stats
                        self.cached_plan = None
                        return
            self.stats, self.cached_plan = core.organize_photos(
                settings, progress, dry_run=dry_run)

        self._run_in_background(work)

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
                       self.cached_plan['key'] == core.plan_key(self._snapshot_settings()))
        cache_note = ("\n\nYour preview will be reused - no re-scan needed "
                      "(unless the folder changed since then)." if cache_ready else "")
        # The settings are already listed at the top of the log, and were on
        # screen when you ticked them. What this dialog is for is the one
        # thing you cannot undo by closing it: whether your originals move.
        consequence = ("Your files will be MOVED out of the source folder."
                       if operation == "move" else
                       "Your originals stay where they are; copies are made.")
        result = messagebox.askyesno(
            f"{operation.capitalize()} files?",
            f"{consequence}\n\n{self.source_folder.get()}\n\n"
            f"Everything this does can be reversed with Undo Last Run."
            f"{cache_note}")

        if not result:
            return
        self._start_worker(dry_run=False, use_cache=True)

    def check_files_operation(self):
        """Run the damage check on its own - reports only, moves nothing."""
        if self.processing:
            return
        if self._validate_source() is None:
            return

        settings = self._snapshot_settings()

        def work(progress):
            self.stats = core.run_health_check(settings, progress)

        self._run_in_background(work)

    def _start_undo(self, undo_file, record, label=None):
        """Reverse a recorded run on a worker thread."""
        def work(progress):
            core.run_undo(Path(undo_file), record, progress, label=label)

        self._run_in_background(work)

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
            record = core.read_undo(undo_file)
        except (OSError, ValueError) as e:
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

        self._start_undo(undo_file, record)

    def cancel_operation(self):
        """Cancel the current operation."""
        self.progress.cancel.set()
        self.update_status("Cancelling...")


def main():
    root = tk.Tk()
    app = ArchivePrepGUI(root)
    root.mainloop()


if __name__ == "__main__":
    main()
