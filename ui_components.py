import tkinter as tk
from tkinter import ttk, filedialog, messagebox
from tkinterdnd2 import DND_FILES
import threading
import os
import shutil
import time

try:
    from PIL import Image, ImageTk
    HAS_PIL = True
except ImportError:
    HAS_PIL = False

from adb_bridge import ADBBridge
from engine import (
    execute_smart_transfer,
    group_duplicates,
    normalize_settings,
    validate_scan_paths,
    validate_transfer_paths,
)
from drive_cache import default_adb_cache_path
from discovery import discover_files, ScanProgress
from utils import (
    DEFAULT_EXCLUDES,
    DEFAULT_MEDIA_EXTS,
    SessionLogger,
    ensure_unique_path,
    get_local_storage_info,
    normalize_extensions,
)
from models import Settings, TransferSettings
from transfer_safety import cleanup_partial_files


def get_adb_device_by_serial(serial):
    if not serial:
        return None
    return next((device for device in ADBBridge.list_devices() if device["serial"] == serial), None)


def apply_dark_theme(root):
    style = ttk.Style(root)
    style.theme_use('clam')

    bg_color = "#11161c"
    fg_color = "#e3e8ef"
    input_bg = "#1b2430"
    accent_color = "#7dcfff"
    border_color = "#2a3442"

    root.configure(bg=bg_color)

    style.configure('.', background=bg_color, foreground=fg_color)
    style.configure('TFrame', background=bg_color)
    style.configure('TLabel', background=bg_color, foreground=fg_color)

    style.configure('TEntry', fieldbackground=input_bg, foreground=fg_color, bordercolor=border_color)
    style.configure('TCombobox', fieldbackground=input_bg, foreground=fg_color, background=bg_color)
    style.map('TCombobox', fieldbackground=[('readonly', input_bg)], selectbackground=[('readonly', accent_color)])
    style.configure('TSpinbox', fieldbackground=input_bg, foreground=fg_color, background=bg_color)

    style.configure('TButton', background='#223041', foreground=fg_color, borderwidth=1, bordercolor=border_color, padding=(10, 6))
    style.map('TButton', background=[('active', '#2d4158')])

    style.configure('TLabelframe', background=bg_color, foreground=accent_color, bordercolor=border_color)
    style.configure('TLabelframe.Label', background=bg_color, foreground=accent_color, font=("Segoe UI", 9, "bold"))

    style.configure('TCheckbutton', background=bg_color, foreground=fg_color)
    style.map('TCheckbutton', background=[('active', bg_color)])

    style.configure('Horizontal.TProgressbar', background=accent_color, troughcolor='#2d2d30')

    style.configure('Treeview', background=input_bg, foreground=fg_color, fieldbackground=input_bg, borderwidth=0)
    style.configure('Treeview.Heading', background='#253244', foreground=fg_color, font=("Segoe UI", 9, "bold"))
    style.map('Treeview', background=[('selected', accent_color)])


class ADBExplorerDialog(tk.Toplevel):
    def __init__(self, parent, start_path="/sdcard", device_serial=""):
        super().__init__(parent)
        self.title("ADB Device Explorer")
        self.geometry("600x500")
        self.configure(bg="#11161c")
        self.result = None
        self.current_path = start_path
        self.device_serial = device_serial
        self._build_ui()
        self._load_directory(start_path)
        self.grab_set()

    def _build_ui(self):
        nav = ttk.Frame(self, padding=5)
        nav.pack(fill="x")
        ttk.Button(nav, text="Up", command=self._go_up).pack(side="left")
        self.path_entry = ttk.Entry(nav)
        self.path_entry.pack(side="left", fill="x", expand=True, padx=5)
        self.tree = ttk.Treeview(self, show="tree")
        self.tree.pack(fill="both", expand=True, padx=10)
        self.tree.bind("<Double-1>", lambda e: self._load_directory(self.tree.identify_row(e.y)))
        ttk.Button(self, text="Select Folder", command=self._confirm).pack(pady=10)

    def _load_directory(self, path):
        self.current_path = path
        self.path_entry.delete(0, tk.END)
        self.path_entry.insert(0, path)
        for item in self.tree.get_children():
            self.tree.delete(item)
        for directory in ADBBridge.get_directory_structure(path, serial=self.device_serial):
            self.tree.insert("", "end", iid=directory['path'], text=f"  [DIR] {directory['name']}")

    def _go_up(self):
        parent_path = "/".join(self.current_path.rstrip("/").split("/")[:-1])
        self._load_directory(parent_path if parent_path else "/")

    def _confirm(self):
        selection = self.tree.selection()
        self.result = selection[0] if selection else self.current_path
        self.destroy()


class StatusHeader(ttk.Frame):
    def __init__(self, parent):
        super().__init__(parent, padding=10)
        self.parent = parent
        self.device_options = {}
        self.conn_label = ttk.Label(self, text="ADB: Disconnected", font=("Segoe UI", 9, "bold"))
        self.conn_label.pack(side="left", padx=20)
        ttk.Label(self, text="ADB Device:").pack(side="left")
        self.device_var = tk.StringVar(value="No devices found")
        self.device_combo = ttk.Combobox(self, textvariable=self.device_var, state="readonly", width=35)
        self.device_combo.pack(side="left", padx=5)
        self.device_combo.bind("<<ComboboxSelected>>", self._on_device_selected)
        ttk.Label(self, text="Device Storage:").pack(side="left", padx=(20, 0))
        self.phone_bar = ttk.Progressbar(self, length=150)
        self.phone_bar.pack(side="left", padx=5)
        ttk.Label(self, text="Local Drive:").pack(side="left", padx=(20, 5))
        self.usb_bar = ttk.Progressbar(self, length=150)
        self.usb_bar.pack(side="left", padx=5)
        self.refresh()

    def _on_device_selected(self, _event=None):
        label = self.device_var.get()
        self.parent.set_selected_device_serial(self.device_options.get(label, ""))
        self.refresh()

    def _refresh_devices(self):
        devices = ADBBridge.list_devices()
        self.device_options = {
            f"{device['model']} ({device['serial']}) [{device['status']}]": device["serial"]
            for device in devices
        }
        values = list(self.device_options.keys())
        self.device_combo["values"] = values

        selected_serial = self.parent.get_selected_device_serial()
        if selected_serial and selected_serial not in self.device_options.values():
            self.parent.set_selected_device_serial("")
            selected_serial = ""

        if not values:
            self.device_var.set("No devices found")
            self.device_combo.state(["disabled"])
            return ""

        self.device_combo.state(["!disabled"])
        if not selected_serial:
            selected_serial = self.device_options[values[0]]
            self.parent.set_selected_device_serial(selected_serial)

        selected_label = next(
            (label for label, serial in self.device_options.items() if serial == selected_serial),
            values[0],
        )
        self.device_var.set(selected_label)
        return selected_serial

    def refresh(self):
        serial = self._refresh_devices()
        info = ADBBridge.get_device_info(serial=serial)
        color = "#4caf50" if info['status'] == "Connected" else "#f44336"
        if info["status"] == "Connected":
            self.conn_label.config(text=f"ADB: Connected to {info['model']}", foreground=color)
        elif info["status"] in {"unauthorized", "offline"}:
            self.conn_label.config(text=f"ADB: {info['model']} is {info['status']}", foreground=color)
        else:
            self.conn_label.config(text="ADB: Disconnected", foreground=color)
        _, _, phone_usage = ADBBridge.get_storage_info(serial=serial)
        self.phone_bar['value'] = phone_usage * 100
        local_root = f"{os.environ.get('SystemDrive', 'C:')}\\"
        _, _, local_usage = get_local_storage_info(local_root)
        self.usb_bar['value'] = local_usage * 100
        self.after(5000, self.refresh)


class Sidebar(ttk.Frame):
    def __init__(self, parent, app):
        super().__init__(parent, width=200, padding=10)
        self.app = app
        apply_dark_theme(parent)

        ttk.Label(self, text="NAVIGATION", font=("Segoe UI", 10, "bold"), foreground="#569cd6").pack(anchor="w", pady=(0, 10))
        ttk.Button(self, text="Home", command=lambda: self.app.show_frame("HomeFrame")).pack(fill="x", pady=2)
        ttk.Button(self, text="Duplicate Organizer", command=lambda: self.app.show_frame("OrganizerFrame")).pack(fill="x", pady=2)
        ttk.Button(self, text="Smart Sync Transfer", command=lambda: self.app.show_frame("TransferFrame")).pack(fill="x", pady=2)

        ttk.Label(self, text="QUICK PATHS", font=("Segoe UI", 10, "bold"), foreground="#569cd6").pack(anchor="w", pady=(20, 10))
        ttk.Button(self, text="Android Camera (ADB)", command=lambda: self._set_path("/sdcard/DCIM/Camera", True)).pack(fill="x", pady=2)
        ttk.Button(self, text="External USB-C", command=lambda: self._set_path("D:/", False)).pack(fill="x", pady=2)

    def _set_path(self, path, is_adb):
        frame = self.app.frames["TransferFrame"]
        frame.src_var.set(path)
        frame.adb_src_var.set(is_adb)
        self.app.show_frame("TransferFrame")


class HomeFrame(ttk.Frame):
    def __init__(self, parent, app):
        super().__init__(parent)
        ttk.Label(self, text="Media Organizer Pro", font=("Segoe UI", 28, "bold"), foreground="#569cd6").pack(pady=(80, 10))
        ttk.Label(self, text="Manage Android and Local Storage Seamlessly", font=("Segoe UI", 12)).pack()
        ttk.Label(self, text="Select a module from the sidebar to begin.", font=("Segoe UI", 10, "italic"), foreground="#888888").pack(pady=20)


class OrganizerFrame(ttk.Frame):
    def __init__(self, parent, app):
        super().__init__(parent)
        self.app = app
        self.scan_var = tk.StringVar()
        self.log_dir_var = tk.StringVar()
        self.isolate_dir_var = tk.StringVar()
        self.adb_var = tk.BooleanVar(value=False)
        self.keep_policy_var = tk.StringVar(value="Oldest")
        self.hash_algo_var = tk.StringVar(value="sha256")
        self.hash_mode_var = tk.StringVar(value="full")
        self.only_media_var = tk.BooleanVar(value=True)
        self.skip_hidden_var = tk.BooleanVar(value=True)
        self.dry_run_var = tk.BooleanVar(value=False)
        self.min_size_var = tk.IntVar(value=0)
        self.threads_var = tk.IntVar(value=4)
        self.ai_blurry_var = tk.BooleanVar(value=False)
        self.ai_semantic_var = tk.BooleanVar(value=False)

        self.stop_event = threading.Event()
        self.start_time = 0
        self.scan_thread = None
        self.scan_running = False
        self.scan_last_update = 0.0
        self.scan_heartbeat_job = None
        self.scan_terminal_status = "Ready to scan"
        self.scan_source_var = tk.StringVar(value="Local")
        self.scan_path_var = tk.StringVar()
        self.scan_status_var = tk.StringVar(value="Ready to scan")
        self.scan_detail_var = tk.StringVar(value="Pick a folder, drive root, or ADB path.")
        self.scan_folders_var = tk.StringVar(value="Folders scanned: 0")
        self.scan_files_var = tk.StringVar(value="Files found: 0")
        self.scan_errors_var = tk.StringVar(value="Errors: 0")
        self.scan_current_path_var = tk.StringVar(value="Current path: -")
        self.scan_mode_hint_var = tk.StringVar(value="Recursive discovery only. Nothing is modified during scanning.")
        self._build_ui()
        self._sync_scan_mode()

    def _build_ui(self):
        title_frame = ttk.Frame(self, padding=(10, 10, 10, 0))
        title_frame.pack(fill="x")
        ttk.Label(title_frame, text="Media Discovery Workspace", font=("Segoe UI", 24, "bold"), foreground="#7dcfff").pack(anchor="w")
        ttk.Label(title_frame, textvariable=self.scan_mode_hint_var, foreground="#9aa0a6").pack(anchor="w", pady=(2, 0))
        ttk.Label(title_frame, text="Uncheck Media Only to scan all files.", foreground="#6f7b88").pack(anchor="w", pady=(2, 0))

        path_frame = ttk.LabelFrame(self, text="Scan Source", padding=10)
        path_frame.pack(fill="x", padx=10, pady=8)
        source_row = ttk.Frame(path_frame)
        source_row.pack(fill="x")
        ttk.Label(source_row, text="Source:").pack(side="left")
        source_combo = ttk.Combobox(
            source_row,
            textvariable=self.scan_source_var,
            values=["Local", "ADB"],
            width=10,
            state="readonly",
        )
        source_combo.pack(side="left", padx=(6, 10))
        source_combo.bind("<<ComboboxSelected>>", lambda _e: self._sync_scan_mode())
        ttk.Checkbutton(source_row, text="ADB", variable=self.adb_var, command=self._toggle_adb_source).pack(side="left", padx=(0, 10))
        ttk.Button(source_row, text="Browse", command=self._browse).pack(side="left")
        ttk.Button(source_row, text="Use Current Drive", command=self._use_current_drive).pack(side="left", padx=5)

        entry_row = ttk.Frame(path_frame)
        entry_row.pack(fill="x", pady=(10, 0))
        self.scan_entry = ttk.Entry(entry_row, textvariable=self.scan_var)
        self.scan_entry.pack(side="left", padx=(0, 8), fill="x", expand=True)
        self.scan_entry.drop_target_register(DND_FILES)
        self.scan_entry.dnd_bind('<<Drop>>', lambda event: self.scan_var.set(event.data.strip('{}')))
        ttk.Button(entry_row, text="Root /", command=lambda: self.scan_var.set("/" if os.name != "nt" else os.path.splitdrive(os.getcwd())[0] + "\\")).pack(side="left", padx=2)
        ttk.Button(entry_row, text="Media /sdcard", command=lambda: self.scan_var.set("/sdcard")).pack(side="left", padx=2)

        config_container = ttk.Frame(self)
        config_container.pack(fill="x", padx=10, pady=5)

        pref_frame = ttk.LabelFrame(config_container, text="Hashing Preferences", padding=10)
        pref_frame.pack(side="left", fill="x", expand=True, padx=(0, 5))
        ttk.Label(pref_frame, text="Hash Algo:").grid(row=0, column=0, sticky="w")
        ttk.Combobox(pref_frame, textvariable=self.hash_algo_var, values=["sha256", "md5"], width=8, state="readonly").grid(row=0, column=1, sticky="w", padx=5)
        ttk.Label(pref_frame, text="Threads:").grid(row=0, column=2, sticky="w", padx=5)
        ttk.Spinbox(pref_frame, from_=1, to=16, textvariable=self.threads_var, width=5).grid(row=0, column=3, sticky="w")
        ttk.Checkbutton(pref_frame, text="Media Only (images/videos)", variable=self.only_media_var).grid(row=1, column=0, columnspan=2, sticky="w", pady=5)
        ttk.Checkbutton(pref_frame, text="Dry Run", variable=self.dry_run_var).grid(row=1, column=2, columnspan=2, sticky="w")

        ai_frame = ttk.LabelFrame(config_container, text="Smart AI Processing (Experimental)", padding=10)
        ai_frame.pack(side="left", fill="x", expand=True, padx=(5, 0))
        ttk.Checkbutton(ai_frame, text="Flag Blurry Media (Local ML)", variable=self.ai_blurry_var).pack(anchor="w", pady=2)
        ttk.Checkbutton(ai_frame, text="Semantic Deduplication", variable=self.ai_semantic_var).pack(anchor="w", pady=2)

        route_frame = ttk.LabelFrame(self, text="Routing & Sorting Options", padding=10)
        route_frame.pack(fill="x", padx=10, pady=5)
        ttk.Label(route_frame, text="Keep:").grid(row=0, column=0, sticky="w")
        ttk.Combobox(route_frame, textvariable=self.keep_policy_var, values=["Oldest", "Newest"], state="readonly", width=15).grid(row=0, column=1, sticky="w", padx=5)
        ttk.Label(route_frame, text="Move Duplicates To:").grid(row=1, column=0, sticky="w", pady=5)
        ttk.Entry(route_frame, textvariable=self.isolate_dir_var).grid(row=1, column=1, sticky="we", padx=5)
        ttk.Button(route_frame, text="Browse", command=lambda: self.isolate_dir_var.set(filedialog.askdirectory())).grid(row=1, column=2)
        route_frame.columnconfigure(1, weight=1)

        ctrl_frame = ttk.Frame(self, padding=(10, 8))
        ctrl_frame.pack(fill="x")
        ttk.Button(ctrl_frame, text="Find & Sort Duplicates", command=self._start_scan).pack(side="left")
        ttk.Button(ctrl_frame, text="Cancel Scan", command=self._cancel_scan).pack(side="left", padx=5)
        ttk.Label(ctrl_frame, textvariable=self.scan_status_var, font=("Segoe UI", 10, "bold"), foreground="#7dcfff").pack(side="right")

        prog_frame = ttk.LabelFrame(self, text="Live Scan Status", padding=10)
        prog_frame.pack(fill="x")
        top_status = ttk.Frame(prog_frame)
        top_status.pack(fill="x")
        self.status_label = ttk.Label(top_status, textvariable=self.scan_detail_var, width=40)
        self.status_label.pack(side="left", padx=5)
        ttk.Label(top_status, textvariable=self.scan_folders_var).pack(side="left", padx=(15, 0))
        ttk.Label(top_status, textvariable=self.scan_files_var).pack(side="left", padx=10)
        ttk.Label(top_status, textvariable=self.scan_errors_var).pack(side="left", padx=10)
        ttk.Label(top_status, textvariable=self.scan_current_path_var, foreground="#9aa0a6").pack(side="right", padx=5)
        self.progress_var = tk.DoubleVar()
        self.progress_bar = ttk.Progressbar(prog_frame, variable=self.progress_var, maximum=100)
        self.progress_bar.pack(fill="x", expand=True, padx=5, pady=(8, 0))
        self.pct_label = ttk.Label(prog_frame, text="0% | 0 items/s | ETA: --:--:--", anchor="e")
        self.pct_label.pack(side="left", padx=5, pady=(6, 0))

        self.paned_window = ttk.PanedWindow(self, orient=tk.HORIZONTAL)
        self.paned_window.pack(fill="both", expand=True, padx=10, pady=(0, 10))

        results_frame = ttk.Frame(self.paned_window)
        self.paned_window.add(results_frame, weight=2)
        self.results_tree = ttk.Treeview(results_frame, columns=("Group", "Path"), show="headings")
        self.results_tree.heading("Group", text="Hash Group")
        self.results_tree.heading("Path", text="File Path")
        self.results_tree.column("Group", width=100, stretch=False)
        self.results_tree.pack(side="left", fill="both", expand=True)
        self.results_tree.bind("<<TreeviewSelect>>", self._show_thumbnail)

        self.preview_label = tk.Label(results_frame, text="Select file for preview\n(Requires PIL)", bg="#1e1e1e", fg="#888888", width=30)
        self.preview_label.pack(side="right", fill="y", padx=5)

        log_frame = ttk.Frame(self.paned_window)
        self.paned_window.add(log_frame, weight=1)
        self.log_widget = tk.Text(log_frame, bg="#0d0d0d", fg="#00ff00", font=("Consolas", 9))
        self.log_widget.pack(fill="both", expand=True)

    def _browse(self):
        if self.adb_var.get():
            serial = self.app.get_selected_device_serial()
            if not serial:
                messagebox.showerror("ADB Device Required", "Connect a device and select it in the header before browsing over ADB.")
                return
            device = get_adb_device_by_serial(serial)
            if not device or device["status"] != "device":
                status = device["status"] if device else "disconnected"
                messagebox.showerror("ADB Device Unavailable", f"Selected device is {status}. Authorize USB debugging on the phone before browsing over ADB.")
                return
            explorer = ADBExplorerDialog(self, device_serial=serial)
            self.wait_window(explorer)
            if explorer.result:
                self.scan_var.set(explorer.result)
        else:
            path = filedialog.askdirectory()
            self.scan_var.set(path) if path else None

    def _sync_scan_mode(self):
        use_adb = self.scan_source_var.get() == "ADB"
        self.adb_var.set(use_adb)
        self.scan_source_var.set("ADB" if use_adb else "Local")
        if use_adb:
            self.scan_mode_hint_var.set("ADB discovery reads the selected device path recursively and never modifies files.")
        else:
            self.scan_mode_hint_var.set("Local discovery scans folders, full drives, and nested subfolders recursively.")

    def _toggle_adb_source(self):
        self.scan_source_var.set("ADB" if self.adb_var.get() else "Local")
        self._sync_scan_mode()

    def _use_current_drive(self):
        if os.name != "nt":
            self.scan_var.set("/")
            return
        drive = os.path.splitdrive(os.getcwd())[0]
        self.scan_var.set(f"{drive}\\")

    def _cancel_scan(self):
        self.stop_event.set()
        self.scan_status_var.set("Cancelling...")
        self.scan_detail_var.set("Cancel requested")
        self.scan_running = True
        self.scan_last_update = time.time()

    def _apply_scan_progress(self, progress: ScanProgress):
        self.scan_last_update = time.time()
        phase = progress.phase
        is_adb = progress.is_adb
        message = progress.message or "Scanning..."
        folders_scanned = progress.folders_scanned
        files_found = progress.files_found
        errors = progress.errors
        current_path = progress.current_path or "-"
        self.after(0, lambda phase_label="ADB scan" if is_adb else "Local scan": self.scan_status_var.set(phase_label))
        self.after(0, lambda detail=message: self.scan_detail_var.set(detail))
        self.after(0, lambda count=folders_scanned: self.scan_folders_var.set(f"Folders scanned: {count}"))
        self.after(0, lambda count=files_found: self.scan_files_var.set(f"Files found: {count}"))
        self.after(0, lambda count=errors: self.scan_errors_var.set(f"Errors: {count}"))
        self.after(0, lambda path=current_path: self.scan_current_path_var.set(f"Current path: {path}"))
        if phase in {"complete", "cancelled"}:
            self.after(0, lambda: self.progress_var.set(100 if phase == "complete" else 0))

    def _scan_heartbeat(self):
        if not self.scan_running:
            self.scan_status_var.set(self.scan_terminal_status)
            if self.scan_heartbeat_job is not None:
                self.scan_heartbeat_job = None
            return

        elapsed = int(time.time() - self.start_time) if self.start_time else 0
        state = "Running"
        if self.stop_event.is_set():
            state = "Stopping"
        source = "ADB" if self.adb_var.get() else "Local"
        self.scan_status_var.set(f"{state} ({source}, {elapsed}s)")
        if time.time() - self.scan_last_update > 1.0:
            self.scan_detail_var.set(f"Working... {elapsed}s elapsed")
        self.scan_heartbeat_job = self.after(1000, self._scan_heartbeat)

    def _update_progress(self, current, total, text=""):
        if total > 0:
            pct = (current / total) * 100
            elapsed = time.time() - self.start_time
            speed = current / elapsed if elapsed > 0 else 0
            eta = (total - current) / speed if speed > 0 else 0
            eta_str = time.strftime('%H:%M:%S', time.gmtime(eta)) if current > 0 else "--:--:--"

            self.after(0, lambda: self.progress_var.set(pct))
            self.after(0, lambda: self.pct_label.config(text=f"{int(pct)}% | {speed:.1f} items/s | ETA: {eta_str}"))
        if text:
            short_text = (text[:40] + '..') if len(text) > 40 else text
            self.after(0, lambda: self.scan_detail_var.set(short_text))

    def _show_thumbnail(self, event):
        if not HAS_PIL:
            return
        selected = self.results_tree.selection()
        if not selected:
            return
        path = self.results_tree.item(selected[0], 'values')[1]

        if self.adb_var.get() or path.startswith("/sdcard"):
            self.preview_label.config(image='', text="ADB Preview Unavailable")
            return

        try:
            image = Image.open(path)
            image.thumbnail((250, 250))
            photo = ImageTk.PhotoImage(image)
            self.preview_label.config(image=photo, text="")
            self.preview_label.image = photo
        except Exception:
            self.preview_label.config(image='', text="No Preview")

    def _start_scan(self):
        if self.isolate_dir_var.get() and not self.dry_run_var.get():
            confirmed = messagebox.askyesno(
                "Confirm Duplicate Isolation",
                "Live duplicate isolation will move duplicate files into the isolate folder.\n\n"
                "Scanning itself never modifies files. Continue with the move step after the scan?",
            )
            if not confirmed:
                return
        self.log_widget.delete(1.0, tk.END)
        for item in self.results_tree.get_children():
            self.results_tree.delete(item)
        self.stop_event.clear()
        self.progress_bar.stop()
        self.start_time = time.time()
        self.scan_running = True
        self.scan_last_update = self.start_time
        self.scan_terminal_status = "Running"
        self.scan_status_var.set("Scanning...")
        self.scan_detail_var.set("Preparing discovery engine")
        self.scan_folders_var.set("Folders scanned: 0")
        self.scan_files_var.set("Files found: 0")
        self.scan_errors_var.set("Errors: 0")
        self.scan_current_path_var.set("Current path: -")
        self.progress_var.set(0)
        self._sync_scan_mode()
        if self.scan_heartbeat_job is None:
            self._scan_heartbeat()
        threading.Thread(target=self._run_scan, daemon=True).start()

    def _run_scan(self):
        logger = SessionLogger(self.log_widget, self.log_dir_var.get())
        try:
            settings = Settings(
                scan_root=self.scan_var.get(),
                output_root="",
                criteria="hash",
                hash_algo=self.hash_algo_var.get(),
                hash_mode=self.hash_mode_var.get(),
                only_media=self.only_media_var.get(),
                extensions=DEFAULT_MEDIA_EXTS,
                min_size_kb=self.min_size_var.get(),
                exclude_dirs=DEFAULT_EXCLUDES,
                skip_hidden_system=self.skip_hidden_var.get(),
                dry_run=self.dry_run_var.get(),
                preserve_structure=True,
                max_hash_workers=self.threads_var.get(),
                use_adb=self.adb_var.get(),
                adb_serial=self.app.get_selected_device_serial(),
                isolate_folder=self.isolate_dir_var.get(),
            )
            settings = normalize_settings(settings)
            validation_error = validate_scan_paths(settings)
            if validation_error:
                logger.log(f"ERROR: {validation_error}")
                self.scan_terminal_status = "Blocked"
                self.scan_detail_var.set(validation_error)
                self._update_progress(0, 1, "Scan blocked")
                self.after(0, lambda: messagebox.showerror("Invalid Scan Settings", validation_error))
                return

            if self.ai_blurry_var.get() or self.ai_semantic_var.get():
                logger.log("WARNING: AI modules selected but not yet implemented. Proceeding with Hash...")

            self.start_time = time.time()
            self._apply_scan_progress(ScanProgress(phase="discovering", source=settings.scan_root, current_path=settings.scan_root, message="Discovering files...", is_adb=settings.use_adb))

            discovery = discover_files(
                settings.scan_root,
                settings,
                self.stop_event,
                progress_callback=self._apply_scan_progress,
                logger=logger,
            )

            if discovery.cancelled or self.stop_event.is_set():
                logger.log("Scan cancelled.")
                self.scan_terminal_status = "Cancelled"
                self._apply_scan_progress(ScanProgress(
                    phase="cancelled",
                    source=settings.scan_root,
                    current_path=settings.scan_root,
                    folders_scanned=discovery.folders_scanned,
                    files_found=discovery.files_found,
                    errors=len(discovery.errors),
                    message="Scan cancelled",
                    is_adb=settings.use_adb,
                    cancelled=True,
                ))
                self.after(0, self.progress_bar.stop)
                return

            files = discovery.files
            self.scan_status_var.set("Hashing")
            logger.log(f"Discovery complete: {discovery.files_found} files across {discovery.folders_scanned} folders.")
            if discovery.errors:
                logger.log(f"Discovery reported {len(discovery.errors)} error(s).")

            self.start_time = time.time()
            self._update_progress(0, 1, f"Found {len(files)} files. Grouping...")

            duplicates = group_duplicates(files, settings, self.stop_event, self.app.hash_cache, logger, self._update_progress)

            if self.stop_event.is_set():
                logger.log("Scan stopped.")
                self.scan_terminal_status = "Stopped"
                self._update_progress(0, 1, "Stopped.")
                self.after(0, self.progress_bar.stop)
                return

            logger.log("Processing and sorting duplicates. Please wait...")
            isolated_count = 0
            policy = self.keep_policy_var.get()

            for idx, group in enumerate(duplicates):
                group.sort(key=lambda x: x.created, reverse=(policy == "Newest"))
                group_id = f"Group {idx + 1}"
                for file_info in group:
                    self.after(0, lambda g=group_id, p=file_info.path: self.results_tree.insert("", "end", values=(g, p)))

                for dup in group[1:]:
                    if self.isolate_dir_var.get():
                        os.makedirs(self.isolate_dir_var.get(), exist_ok=True)
                        target = ensure_unique_path(os.path.join(self.isolate_dir_var.get(), os.path.basename(dup.path)))
                        if settings.dry_run:
                            logger.log(f"WOULD MOVE: {dup.path} -> {target}")
                        else:
                            try:
                                if dup.is_adb:
                                    ADBBridge.pull(dup.path, target, serial=settings.adb_serial)
                                    logger.log(f"MOVED (pull-only for ADB): {dup.path} -> {target}")
                                else:
                                    shutil.move(dup.path, target)
                                    logger.log(f"MOVED: {dup.path} -> {target}")
                            except Exception as exc:
                                logger.log(f"WARNING: Failed to move duplicate: {dup.path} -> {target} ({exc})")
                                continue
                    isolated_count += 1

            logger.log("\n--- Scan Complete ---")
            if self.isolate_dir_var.get() and settings.dry_run:
                action = "Would Move"
            elif self.isolate_dir_var.get():
                action = "Moved"
            else:
                action = "Found"
            logger.log(f"Total Duplicates {action}: {isolated_count}")
            logger.log(f"Duplicate Groups Found: {len(duplicates)}")
            self.app.hash_cache.save()
            self._update_progress(1, 1, "Scan Complete.")
            self.after(0, self.progress_bar.stop)
            self.scan_terminal_status = "Complete"
            self.scan_status_var.set("Complete")
        finally:
            if self.stop_event.is_set() and self.scan_terminal_status == "Running":
                self.scan_terminal_status = "Stopped"
                self.scan_detail_var.set("Scan stopped")
            self.scan_running = False
            self.scan_last_update = time.time()
            self.after(0, self.progress_bar.stop)
            self.after(0, self._scan_heartbeat)


class TransferFrame(ttk.Frame):
    def __init__(self, parent, app):
        super().__init__(parent)
        self.app = app
        self.src_var = tk.StringVar()
        self.dst_var = tk.StringVar()
        self.out_var = tk.StringVar()
        self.adb_src_var = tk.BooleanVar(value=False)
        self.log_dir_var = tk.StringVar()

        self.pics_var = tk.BooleanVar(value=True)
        self.vids_var = tk.BooleanVar(value=True)
        self.audio_var = tk.BooleanVar(value=False)
        self.custom_ext_var = tk.StringVar()

        self.hash_algo_var = tk.StringVar(value="sha256")
        self.hash_mode_var = tk.StringVar(value="fast")
        self.skip_hidden_var = tk.BooleanVar(value=True)
        self.dry_run_var = tk.BooleanVar(value=False)
        self.cache_location_var = tk.StringVar(
            value=f"Automatic caches: {os.path.abspath(os.path.join(os.getcwd(), 'drive_caches'))}"
        )
        self.min_size_var = tk.IntVar(value=0)
        self.threads_var = tk.IntVar(value=4)
        self.transfer_profile_var = tk.StringVar(value="Balanced")
        self.retry_attempts_var = tk.IntVar(value=3)
        self.conflict_policy_var = tk.StringVar(value="rename")
        self.keep_awake_var = tk.BooleanVar(value=True)

        self.stop_event = threading.Event()
        self.start_time = 0
        self.transfer_running = False
        self.transfer_last_update = 0.0
        self.transfer_heartbeat_job = None
        self.transfer_terminal_status = "Ready"
        self.pull_progress_key = ""
        self.pull_progress_started = 0.0
        self.pull_progress_baseline = 0
        self._build_ui()

    def _build_ui(self):
        path_frame = ttk.LabelFrame(self, text="Smart Sync Transfer", padding=10)
        path_frame.pack(fill="x", padx=10, pady=5)
        ttk.Label(path_frame, text="Source Drive/Folder:").grid(row=0, column=0, sticky="w")
        source_entry = ttk.Entry(path_frame, textvariable=self.src_var)
        source_entry.grid(row=0, column=1, sticky="we", padx=5)
        source_entry.drop_target_register(DND_FILES)
        source_entry.dnd_bind('<<Drop>>', lambda e: self.src_var.set(e.data.strip('{}')))
        ttk.Checkbutton(path_frame, text="ADB Source", variable=self.adb_src_var).grid(row=0, column=2)
        ttk.Button(path_frame, text="Browse", command=lambda: self._browse(self.src_var, self.adb_src_var.get())).grid(row=0, column=3, padx=5)

        ttk.Label(path_frame, text="Compare Against Folder:").grid(row=1, column=0, sticky="w", pady=5)
        compare_entry = ttk.Entry(path_frame, textvariable=self.dst_var)
        compare_entry.grid(row=1, column=1, sticky="we", padx=5)
        compare_entry.drop_target_register(DND_FILES)
        compare_entry.dnd_bind('<<Drop>>', lambda e: self.dst_var.set(e.data.strip('{}')))
        ttk.Button(path_frame, text="Browse", command=lambda: self._browse(self.dst_var, False)).grid(row=1, column=3, padx=5)
        ttk.Label(path_frame, text="Optional Output Folder:").grid(row=2, column=0, sticky="w", pady=5)
        output_entry = ttk.Entry(path_frame, textvariable=self.out_var)
        output_entry.grid(row=2, column=1, sticky="we", padx=5)
        output_entry.drop_target_register(DND_FILES)
        output_entry.dnd_bind('<<Drop>>', lambda e: self.out_var.set(e.data.strip('{}')))
        ttk.Button(path_frame, text="Browse", command=lambda: self._browse(self.out_var, False)).grid(row=2, column=3, padx=5)
        path_frame.columnconfigure(1, weight=1)

        filter_frame = ttk.LabelFrame(self, text="File Extraction Filters (What to pull from Source)", padding=10)
        filter_frame.pack(fill="x", padx=10, pady=5)

        ttk.Checkbutton(filter_frame, text="Pictures (.jpg, .png, .heic...)", variable=self.pics_var).grid(row=0, column=0, sticky="w", padx=(0, 15))
        ttk.Checkbutton(filter_frame, text="Videos (.mp4, .mkv, .mov...)", variable=self.vids_var).grid(row=0, column=1, sticky="w", padx=(0, 15))
        ttk.Checkbutton(filter_frame, text="Audio (.mp3, .wav...)", variable=self.audio_var).grid(row=0, column=2, sticky="w", padx=(0, 15))

        ttk.Label(filter_frame, text="Custom Extensions (comma separated, e.g., .pdf, .docx):").grid(row=1, column=0, columnspan=2, sticky="w", pady=(10, 0))
        ttk.Entry(filter_frame, textvariable=self.custom_ext_var).grid(row=1, column=2, columnspan=2, sticky="we", pady=(10, 0), padx=5)
        filter_frame.columnconfigure(3, weight=1)

        pref_frame = ttk.LabelFrame(self, text="Advanced Sync Engine", padding=10)
        pref_frame.pack(fill="x", padx=10, pady=5)
        ttk.Label(pref_frame, text="Hash Algo:").grid(row=0, column=0, sticky="w")
        ttk.Combobox(pref_frame, textvariable=self.hash_algo_var, values=["sha256", "md5"], width=8, state="readonly").grid(row=0, column=1, sticky="w", padx=5)
        ttk.Label(pref_frame, text="Hash Mode:").grid(row=0, column=2, sticky="w", padx=5)
        ttk.Combobox(pref_frame, textvariable=self.hash_mode_var, values=["full", "fast"], width=8, state="readonly").grid(row=0, column=3, sticky="w", padx=5)
        ttk.Label(pref_frame, text="Max Threads:").grid(row=0, column=4, sticky="w", padx=5)
        ttk.Spinbox(pref_frame, from_=1, to=16, textvariable=self.threads_var, width=5).grid(row=0, column=5, sticky="w")
        ttk.Checkbutton(pref_frame, text="Skip Hidden/System", variable=self.skip_hidden_var).grid(row=1, column=0, columnspan=2, sticky="w", pady=5)
        ttk.Checkbutton(pref_frame, text="Dry Run (Test Sync)", variable=self.dry_run_var).grid(row=1, column=2, columnspan=2, sticky="w")
        ttk.Label(pref_frame, text="*Checks every subfolder in the compare folder, then copies only new files while preserving source folder structure.", font=("Segoe UI", 8, "italic"), foreground="#888888").grid(row=1, column=4, columnspan=2, sticky="w")
        ttk.Label(pref_frame, text="Profile:").grid(row=2, column=0, sticky="w")
        profile = ttk.Combobox(pref_frame, textvariable=self.transfer_profile_var, values=["Reliable", "Balanced", "Fast"], width=10, state="readonly")
        profile.grid(row=2, column=1, sticky="w", padx=5)
        profile.bind("<<ComboboxSelected>>", self._apply_transfer_profile)
        ttk.Label(pref_frame, text="Retries:").grid(row=2, column=2, sticky="w")
        ttk.Spinbox(pref_frame, from_=1, to=8, textvariable=self.retry_attempts_var, width=5).grid(row=2, column=3, sticky="w", padx=5)
        ttk.Label(pref_frame, text="Existing target:").grid(row=2, column=4, sticky="w")
        ttk.Combobox(pref_frame, textvariable=self.conflict_policy_var, values=["rename", "skip", "replace"], width=9, state="readonly").grid(row=2, column=5, sticky="w")
        ttk.Checkbutton(pref_frame, text="Keep phone awake over USB", variable=self.keep_awake_var).grid(row=3, column=0, columnspan=3, sticky="w", pady=5)
        ttk.Label(pref_frame, textvariable=self.cache_location_var, foreground="#9aa0a6").grid(
            row=3, column=3, columnspan=2, sticky="e", padx=5
        )
        ttk.Button(pref_frame, text="Open Cache Folder", command=self._open_cache_folder).grid(
            row=3, column=5, sticky="e"
        )

        ctrl_frame = ttk.Frame(self, padding=10)
        ctrl_frame.pack(fill="x")
        ttk.Button(ctrl_frame, text="Start Sync", command=self._start).pack(side="left")
        ttk.Button(ctrl_frame, text="Stop", command=lambda: self.stop_event.set()).pack(side="left", padx=5)
        ttk.Button(ctrl_frame, text="ADB Diagnostics", command=self._show_adb_diagnostics).pack(side="left", padx=5)
        ttk.Button(ctrl_frame, text="Clean Partials", command=self._clean_partials).pack(side="left", padx=5)

        prog_frame = ttk.Frame(self, padding=10)
        prog_frame.pack(fill="x")
        self.status_label = ttk.Label(prog_frame, text="Ready", width=24, anchor="w")
        self.status_label.grid(row=0, column=0, sticky="w", padx=(5, 10))
        self.progress_var = tk.DoubleVar()
        self.progress_bar = ttk.Progressbar(prog_frame, variable=self.progress_var, maximum=100)
        self.progress_bar.grid(row=0, column=1, sticky="ew", padx=5)
        self.pct_label = ttk.Label(prog_frame, text="0% | 0 items/s | ETA: --:--:--", width=34, anchor="e")
        self.pct_label.grid(row=0, column=2, sticky="e", padx=5)
        self.transfer_status_var = tk.StringVar(value="Ready")
        self.transfer_detail_var = tk.StringVar(value="Compare checks recurse through nested subfolders.")
        ttk.Label(
            prog_frame,
            textvariable=self.transfer_status_var,
            font=("Segoe UI", 10, "bold"),
            foreground="#7dcfff",
            width=24,
            anchor="w",
        ).grid(row=1, column=0, sticky="nw", padx=(5, 10), pady=(6, 0))
        ttk.Label(
            prog_frame,
            textvariable=self.transfer_detail_var,
            foreground="#c2cad4",
            anchor="w",
            justify="left",
            wraplength=980,
        ).grid(row=1, column=1, columnspan=2, sticky="ew", padx=5, pady=(6, 0))
        prog_frame.columnconfigure(1, weight=1)

        log_frame = ttk.LabelFrame(self, text="Live Activity", padding=5)
        log_frame.pack(fill="both", expand=True, padx=10, pady=(0, 10))
        self.log_widget = tk.Text(
            log_frame,
            bg="#0d0d0d",
            fg="#00ff00",
            font=("Consolas", 10),
            height=24,
            wrap="none",
        )
        log_scroll_y = ttk.Scrollbar(log_frame, orient="vertical", command=self.log_widget.yview)
        log_scroll_x = ttk.Scrollbar(log_frame, orient="horizontal", command=self.log_widget.xview)
        self.log_widget.configure(yscrollcommand=log_scroll_y.set, xscrollcommand=log_scroll_x.set)
        self.log_widget.grid(row=0, column=0, sticky="nsew")
        log_scroll_y.grid(row=0, column=1, sticky="ns")
        log_scroll_x.grid(row=1, column=0, sticky="ew")
        log_frame.rowconfigure(0, weight=1)
        log_frame.columnconfigure(0, weight=1)

    def _browse(self, var, use_adb):
        if use_adb:
            serial = self.app.get_selected_device_serial()
            if not serial:
                messagebox.showerror("ADB Device Required", "Connect a device and select it in the header before browsing over ADB.")
                return
            device = get_adb_device_by_serial(serial)
            if not device or device["status"] != "device":
                status = device["status"] if device else "disconnected"
                messagebox.showerror("ADB Device Unavailable", f"Selected device is {status}. Authorize USB debugging on the phone before browsing over ADB.")
                return
            explorer = ADBExplorerDialog(self, device_serial=serial)
            self.wait_window(explorer)
            if explorer.result:
                var.set(explorer.result)
        else:
            path = filedialog.askdirectory()
            var.set(path) if path else None

    def _open_cache_folder(self):
        cache_dir = os.path.abspath(os.path.join(os.getcwd(), "drive_caches"))
        os.makedirs(cache_dir, exist_ok=True)
        try:
            os.startfile(cache_dir)
        except (AttributeError, OSError) as exc:
            messagebox.showerror("Cache Folder", f"Could not open:\n{cache_dir}\n\n{exc}")

    def _apply_transfer_profile(self, _event=None):
        profile = self.transfer_profile_var.get()
        if profile == "Reliable":
            self.retry_attempts_var.set(5)
            self.threads_var.set(2)
            self.keep_awake_var.set(True)
        elif profile == "Fast":
            self.retry_attempts_var.set(2)
            self.threads_var.set(8)
            self.keep_awake_var.set(True)
        else:
            self.retry_attempts_var.set(3)
            self.threads_var.set(4)
            self.keep_awake_var.set(True)

    def _show_adb_diagnostics(self):
        serial = self.app.get_selected_device_serial()
        if not serial:
            messagebox.showwarning("ADB Diagnostics", "No ADB device is selected.")
            return
        ready, detail = ADBBridge.probe_device(serial)
        state = ADBBridge.get_device_state(serial) or "not detected"
        used, total, _ = ADBBridge.get_storage_info(serial=serial)
        cache_path = default_adb_cache_path(serial)
        messagebox.showinfo(
            "ADB Diagnostics",
            f"Serial: {serial}\nState: {state}\nShell: {'ready' if ready else detail}\n"
            f"Storage: {used / (1024**3):.1f} / {total / (1024**3):.1f} GB\n"
            f"Cache: {cache_path}",
        )

    def _clean_partials(self):
        root = self.out_var.get().strip() or self.dst_var.get().strip()
        if not root or not os.path.isdir(root):
            messagebox.showerror("Clean Partials", "Choose a valid output or comparison folder first.")
            return
        if not messagebox.askyesno("Clean Partials", f"Delete .partial files recursively under:\n{root}?"):
            return
        removed = cleanup_partial_files(root)
        messagebox.showinfo("Clean Partials", f"Removed {len(removed)} partial file(s).")

    def _update_progress(self, current, total, text=""):
        self.transfer_last_update = time.time()
        if total > 0:
            pct = (current / total) * 100
            is_pull_progress = text.startswith("Pulling ")
            is_byte_progress = is_pull_progress or "Overall " in text
            if is_pull_progress:
                progress_key = text.rsplit(":", 1)[0]
                if progress_key != self.pull_progress_key:
                    self.pull_progress_key = progress_key
                    self.pull_progress_started = time.time()
                    self.pull_progress_baseline = current
                elapsed = time.time() - self.pull_progress_started
                speed_current = max(0, current - self.pull_progress_baseline)
            else:
                elapsed = time.time() - self.start_time
                speed_current = current
            speed = speed_current / elapsed if elapsed > 0 else 0
            eta = (total - current) / speed if speed > 0 else 0
            eta_str = time.strftime('%H:%M:%S', time.gmtime(eta)) if current > 0 else "--:--:--"
            rate_text = f"{speed / (1024**2):.1f} MB/s" if is_byte_progress else f"{speed:.1f} items/s"

            self.after(0, lambda: self.progress_var.set(pct))
            self.after(0, lambda: self.pct_label.config(text=f"{int(pct)}% | {rate_text} | ETA: {eta_str}"))
        if text:
            phase_text = text.split(":", 1)[0]
            short_text = (phase_text[:30] + "..") if len(phase_text) > 30 else phase_text
            detail_text = (text[:300] + "..") if len(text) > 300 else text
            self.after(0, lambda: self.status_label.config(text=short_text))
            self.after(0, lambda: self.transfer_detail_var.set(detail_text))

    def _transfer_heartbeat(self):
        if not self.transfer_running:
            self.transfer_status_var.set(self.transfer_terminal_status)
            self.transfer_heartbeat_job = None
            return

        elapsed = int(time.time() - self.start_time) if self.start_time else 0
        state = "Running"
        if self.stop_event.is_set():
            state = "Stopping"
        source_mode = "ADB" if self.adb_src_var.get() else "Local"
        self.transfer_status_var.set(f"{state} ({source_mode}, {elapsed}s)")
        if time.time() - self.transfer_last_update > 1.0:
            self.transfer_detail_var.set(f"Still working... {elapsed}s elapsed")
        self.transfer_heartbeat_job = self.after(1000, self._transfer_heartbeat)

    def _start(self):
        if not self.dry_run_var.get():
            if not messagebox.askyesno(
                "Confirm Live Transfer",
                "Start a live copy transfer?\n\nSource files will not be modified or deleted. "
                "New destination files may be created or replaced according to the selected conflict policy.",
            ):
                return
        self.log_widget.delete(1.0, tk.END)
        self.stop_event.clear()
        self.transfer_running = True
        self.transfer_last_update = time.time()
        self.transfer_terminal_status = "Running"
        self.start_time = time.time()
        self.transfer_status_var.set("Scanning...")
        self.transfer_detail_var.set("Preparing transfer comparison")
        if self.transfer_heartbeat_job is None:
            self._transfer_heartbeat()
        threading.Thread(target=self._run_transfer, daemon=True).start()

    def _run_transfer(self):
        logger = SessionLogger(self.log_widget, self.log_dir_var.get())
        try:
            active_exts = []
            if self.pics_var.get():
                active_exts.extend(['.jpg', '.jpeg', '.png', '.gif', '.bmp', '.tiff', '.heic', '.webp'])
            if self.vids_var.get():
                active_exts.extend(['.mp4', '.mov', '.avi', '.mkv', '.wmv', '.flv', '.mpeg', '.mpg', '.3gp', '.mts', '.m2ts', '.hevc'])
            if self.audio_var.get():
                active_exts.extend(['.mp3', '.wav', '.flac', '.aac', '.ogg', '.m4a'])

            custom = self.custom_ext_var.get().strip()
            if custom:
                active_exts.extend([item.strip().lower() for item in custom.split(',') if item.strip()])
            active_exts = normalize_extensions(active_exts)

            if not active_exts:
                logger.log("WARNING: No file types selected! Please check Pictures, Videos, or enter a Custom Extension.")
                self.transfer_terminal_status = "Failed"
                self.transfer_detail_var.set("No file types selected")
                self._update_progress(0, 1, "Failed: No File Types Selected")
                return

            settings = TransferSettings(
                source_root=self.src_var.get(),
                dest_root=self.dst_var.get(),
                output_root=self.out_var.get(),
                criteria="hash",
                hash_algo=self.hash_algo_var.get(),
                hash_mode=self.hash_mode_var.get(),
                only_media=True,
                extensions=active_exts,
                min_size_kb=self.min_size_var.get(),
                exclude_dirs=DEFAULT_EXCLUDES,
                skip_hidden_system=self.skip_hidden_var.get(),
                dry_run=self.dry_run_var.get(),
                preserve_structure=True,
                max_hash_workers=self.threads_var.get(),
                transfer_mode="copy",
                duplicate_policy="skip",
                use_dest_cache=True,
                source_is_adb=self.adb_src_var.get(),
                adb_serial=self.app.get_selected_device_serial(),
                log_folder=self.log_dir_var.get(),
                isolate_folder="",
                drive_cache_path="",
                update_drive_cache=True,
                use_adb_cache=True,
                adb_cache_path="",
                transfer_profile=self.transfer_profile_var.get(),
                retry_attempts=self.retry_attempts_var.get(),
                conflict_policy=self.conflict_policy_var.get(),
                keep_device_awake=self.keep_awake_var.get(),
            )
            settings.use_dest_cache = True
            settings = normalize_settings(settings)
            validation_error = validate_transfer_paths(settings)
            if validation_error:
                logger.log(f"ERROR: {validation_error}")
                self.transfer_terminal_status = "Blocked"
                self.transfer_detail_var.set(validation_error)
                self._update_progress(0, 1, "Transfer blocked")
                self.after(0, lambda: messagebox.showerror("Invalid Transfer Settings", validation_error))
                return

            logger.log("Comparison is recursive across the entire compare folder, including nested subfolders.")
            result = execute_smart_transfer(settings, self.stop_event, self.app.hash_cache, logger, self._update_progress)
            self.app.hash_cache.save()
            if result:
                self.transfer_terminal_status = "Complete"
                action_word = "Would Copy" if result.get("dry_run") else "Copied"
                summary = (
                    f"Done | {action_word}: {result['transferred']} | "
                    f"Duplicates: {result['duplicates']} | Skipped: {result['skipped']} | "
                    f"Errors: {result.get('errors', 0)}"
                )
                if result.get("adb_device_failed"):
                    self.transfer_terminal_status = "ADB Disconnected"
                    summary = "ADB authorization lost | Unlock phone, reconnect USB debugging, and retry"
                self.transfer_detail_var.set(summary)
                self._update_progress(1, 1, summary)
        finally:
            if self.stop_event.is_set() and self.transfer_terminal_status == "Running":
                self.transfer_terminal_status = "Stopped"
                self.transfer_detail_var.set("Transfer stopped")
            self.transfer_running = False
            self.transfer_last_update = time.time()
            self.after(0, self._transfer_heartbeat)
