#!/usr/bin/env python3
"""Kjegla's Photo Organizer - the window.

Prepares a messy media collection for archival: sorts by camera model, finds
duplicates by content, sets damaged files aside, repairs filenames that lie
about their format, and can undo the lot.

This file is the user interface and nothing else. All of the actual work
lives in organizer_core.py, which knows nothing about tkinter - so the
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

import organizer_core as core

# Sun Valley theme (modern Windows 11 look); optional
try:
    import sv_ttk
    SV_TTK_AVAILABLE = True
except ImportError:
    SV_TTK_AVAILABLE = False


class PhotoOrganizerGUI:
    def __init__(self, root):
        self.root = root
        self.root.title("Kjegla's Photo Organizer")
        self.root.geometry("960x780")

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
        self.dedupe_content = tk.BooleanVar(value=True)
        self.check_corrupt = tk.BooleanVar(value=False)
        self.corrupt_thorough = tk.BooleanVar(value=False)
        self.cleanup_empty = tk.BooleanVar(value=True)
        self.fix_extensions = tk.BooleanVar(value=True)
        self.processing = False
        self.last_undo_file = None
        self.last_rename_undo_file = None  # undo for the extension fixes alone
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
        self.root.minsize(920, 720)
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
        extras_frame.pack(fill=tk.X, pady=(0, 8))

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
            text="🎬 Videos go to a 'Videos' subfolder automatically.",
            font=('Segoe UI', 9), foreground='#888888', justify=tk.LEFT)
        video_note.pack(anchor=tk.W, pady=(6, 2))

        # Cleanup & safety (right column, below Options)
        safety_frame = ttk.LabelFrame(right_col, text="Cleanup & Safety",
                                      padding="10")
        safety_frame.pack(fill=tk.BOTH, expand=True)

        self.dedupe_checkbox = ttk.Checkbutton(
            safety_frame,
            text="♻️ Find duplicates by content (any filename)",
            variable=self.dedupe_content)
        self.dedupe_checkbox.pack(anchor=tk.W, pady=3)

        self.corrupt_checkbox = ttk.Checkbutton(
            safety_frame,
            text="🩺 Check files for damage while organizing",
            variable=self.check_corrupt, command=self._sync_thorough_state)
        self.corrupt_checkbox.pack(anchor=tk.W, pady=3)

        self.thorough_checkbox = ttk.Checkbutton(
            safety_frame,
            text="       └ Thorough check (much slower)",
            variable=self.corrupt_thorough)
        self.thorough_checkbox.pack(anchor=tk.W, pady=(0, 3))

        self.fixext_checkbox = ttk.Checkbutton(
            safety_frame,
            text="🏷️ Fix files whose extension doesn't match their contents",
            variable=self.fix_extensions)
        self.fixext_checkbox.pack(anchor=tk.W, pady=3)

        self.cleanup_checkbox = ttk.Checkbutton(
            safety_frame,
            text="🧹 Delete empty folders left behind (Move only)",
            variable=self.cleanup_empty)
        self.cleanup_checkbox.pack(anchor=tk.W, pady=3)

        safety_note = ttk.Label(
            safety_frame,
            text="Nothing is ever deleted. Duplicates go to 'Duplicates',\n"
                 "damaged files to 'Corrupt', and wrongly-named ones are\n"
                 "renamed into 'Wrong Extension'. Undo puts it all back.",
            font=('Segoe UI', 9), foreground='#888888', justify=tk.LEFT)
        safety_note.pack(anchor=tk.W, pady=(6, 2))

        self._sync_thorough_state()

        # Action buttons
        button_frame = ttk.Frame(main_frame)
        button_frame.grid(row=3, column=0, columnspan=3, pady=10)

        self.preview_btn = ttk.Button(button_frame, text="🔍 Preview (Dry Run)",
                                      command=self.preview_operation)
        self.preview_btn.pack(side=tk.LEFT, padx=4)

        self.check_btn = ttk.Button(button_frame, text="🩺 Check Files",
                                    command=self.check_files_operation)
        self.check_btn.pack(side=tk.LEFT, padx=4)

        self.execute_btn = ttk.Button(button_frame, text="▶️ Execute Operation",
                                      command=self.execute_operation)
        self.execute_btn.pack(side=tk.LEFT, padx=4)

        self.undo_btn = ttk.Button(button_frame, text="↩️ Undo Last Run",
                                   command=self.undo_last_operation, state=tk.DISABLED)
        self.undo_btn.pack(side=tk.LEFT, padx=4)

        self.undo_renames_btn = ttk.Button(button_frame, text="↩️ Undo Renames",
                                           command=self.undo_renames,
                                           state=tk.DISABLED)
        self.undo_renames_btn.pack(side=tk.LEFT, padx=4)

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

    def _sync_thorough_state(self):
        """The thorough toggle only means anything when checking is switched on."""
        self.thorough_checkbox.config(
            state=tk.NORMAL if self.check_corrupt.get() else tk.DISABLED)

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
        report += (f"♻️ Duplicate copies set aside: "
                   f"{self.stats['content_duplicates']} "
                   f"({self.stats['duplicate_bytes'] / (1024 * 1024):.2f} MB)\n")
        report += f"🩹 Damaged files found: {self.stats['damaged']}\n"
        report += f"🏷️ Wrong file extension: {self.stats['misnamed']}\n"
        report += f"❓ Could not be checked: {self.stats['unchecked']}\n"
        report += f"🧹 Empty folders removed: {self.stats['empty_folders_removed']}\n"
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
            # Offer undo if the folder holds a not-yet-undone record. Both
            # patterns are checked so a run made with v35 or earlier, whose
            # record is a single .json object, can still be undone.
            undo_files = sorted(list(Path(folder).glob("kjegla_undo_*.jsonl"))
                                + list(Path(folder).glob("kjegla_undo_*.json")))
            self.last_undo_file = str(undo_files[-1]) if undo_files else None
            rename_undos = sorted(
                list((Path(folder) / core.WRONG_EXT_FOLDER)
                     .glob("kjegla_undo_renames_*.jsonl"))
                + list((Path(folder) / core.WRONG_EXT_FOLDER)
                       .glob("kjegla_undo_renames_*.json")))
            self.last_rename_undo_file = (str(rename_undos[-1])
                                          if rename_undos else None)
            if not self.processing:
                self.undo_btn.config(state=tk.NORMAL if self.last_undo_file else tk.DISABLED)
                self.undo_renames_btn.config(
                    state=tk.NORMAL if self.last_rename_undo_file else tk.DISABLED)

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
                    self.check_btn.config(state=tk.NORMAL)
                    self.cancel_btn.config(state=tk.DISABLED)
                    self.stats_btn.config(state=tk.NORMAL if self.stats['total_files'] > 0 else tk.DISABLED)
                    self.undo_btn.config(state=tk.NORMAL if self.last_undo_file else tk.DISABLED)
                    self.undo_renames_btn.config(
                        state=tk.NORMAL if self.last_rename_undo_file else tk.DISABLED)
                elif action == "disable_buttons":
                    self.preview_btn.config(state=tk.DISABLED)
                    self.execute_btn.config(state=tk.DISABLED)
                    self.check_btn.config(state=tk.DISABLED)
                    self.undo_btn.config(state=tk.DISABLED)
                    self.undo_renames_btn.config(state=tk.DISABLED)
                    self.cancel_btn.config(state=tk.NORMAL)
                elif action == "undo_available":
                    self.last_undo_file = value
                elif action == "rename_undo_available":
                    self.last_rename_undo_file = value
                elif action == "plan_stale":
                    self.cached_plan = None

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
            fix_extensions=self.fix_extensions.get(),
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
                self.log(f"\n❌ Unexpected error: {e}")
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
            f"Include subfolders: {'Yes' if self.include_subfolders.get() else 'No'}\n"
            f"Find duplicates by content: {'Yes' if self.dedupe_content.get() else 'No'}\n"
            f"Check files for damage: {'Yes' if self.check_corrupt.get() else 'No'}\n"
            f"Delete empty folders: {'Yes' if self.cleanup_empty.get() else 'No'}\n\n"
            f"{'Files will be MOVED from source!' if operation == 'move' else 'Original files will remain untouched.'}"
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

    def undo_renames(self):
        """Undo only the extension fixes, leaving the rest of the run alone."""
        if self.processing:
            return
        undo_file = self.last_rename_undo_file
        if not undo_file or not Path(undo_file).exists():
            messagebox.showinfo("Undo Renames", "No renames to undo.")
            self.undo_renames_btn.config(state=tk.DISABLED)
            return

        try:
            record = core.read_undo(undo_file)
        except (OSError, ValueError) as e:
            messagebox.showerror("Undo Renames",
                                 f"Could not read the rename record:\n{e}")
            return

        entries = record.get('entries', [])
        if not entries:
            messagebox.showinfo("Undo Renames", "The rename record is empty.")
            return

        if not messagebox.askyesno(
                "Undo Renames",
                f"Put {len(entries)} renamed file(s) back where they were, "
                f"under their original names?\n\n"
                f"Everything else this run did stays as it is."):
            return

        self.last_rename_undo_file = None
        self._start_undo(undo_file, record, label="renames")

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
    if SV_TTK_AVAILABLE:
        sv_ttk.set_theme("dark")
    app = PhotoOrganizerGUI(root)
    root.mainloop()


if __name__ == "__main__":
    main()
