import os
import json
import threading
import shutil
import ctypes
import queue
from datetime import datetime

from runtime_paths import get_runtime_paths

DEFAULT_MEDIA_EXTS = [
    ".jpg", ".jpeg", ".png", ".gif", ".bmp", ".tiff", ".heic", ".webp",
    ".dng", ".cr2", ".cr3", ".nef", ".arw", ".raf", ".orf", ".rw2",
    ".mp4", ".mov", ".avi", ".mkv", ".wmv", ".flv", ".mpeg", ".mpg",
    ".3gp", ".mts", ".m2ts", ".hevc"
]

DEFAULT_EXCLUDES = [
    "$RECYCLE.BIN",
    "System Volume Information",
    "Windows",
    ".git",
    ".venv",
    ".media_organizer_staging",
    ".duplicate_transfer_manager_staging",
    ".duplicate_transfer_manager_backups",
]

class SessionLogger:
    def __init__(self, ui_text_widget, log_folder=""):
        self.ui_widget = ui_text_widget
        self.log_file = None
        self._queue = queue.Queue()
        if not log_folder:
            log_folder = str(get_runtime_paths().logs)
        if os.path.exists(log_folder):
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            self.log_file = os.path.join(log_folder, f"duplicate_transfer_manager_{timestamp}.log")
        if self.ui_widget:
            self._schedule_flush()

    def _schedule_flush(self):
        try:
            self.ui_widget.after(100, self._flush_ui_queue)
        except Exception:
            pass

    def _flush_ui_queue(self):
        messages = []
        try:
            while True:
                messages.append(self._queue.get_nowait())
        except queue.Empty:
            pass
        finally:
            if messages:
                self.ui_widget.insert("end", "\n".join(messages) + "\n")
                self.ui_widget.see("end")
            self._schedule_flush()

    def log(self, msg):
        formatted_msg = f"[{datetime.now().strftime('%H:%M:%S')}] {msg}"
        if self.ui_widget:
            self._queue.put(formatted_msg)
        if self.log_file:
            try:
                with open(self.log_file, "a", encoding="utf-8") as f:
                    f.write(f"{formatted_msg}\n")
            except: pass

class HashCache:
    def __init__(self, path):
        self.path = path
        self.lock = threading.Lock()
        self.data = {}

    def load(self):
        if os.path.exists(self.path):
            try:
                with open(self.path, "r", encoding="utf-8") as f:
                    self.data = json.load(f)
            except: self.data = {}

    def save(self):
        try:
            directory = os.path.dirname(os.path.abspath(self.path))
            if directory:
                os.makedirs(directory, exist_ok=True)
            with open(self.path, "w", encoding="utf-8") as f:
                json.dump(self.data, f)
        except: pass

    def get(self, path, algo, mode, size, mtime):
        key = f"{path}|{algo}|{mode}"
        with self.lock:
            entry = self.data.get(key)
            if entry and entry.get("size") == size and entry.get("mtime") == mtime:
                return entry.get("hash")
        return None

    def set(self, path, algo, mode, size, mtime, digest):
        key = f"{path}|{algo}|{mode}"
        with self.lock:
            self.data[key] = {"size": size, "mtime": mtime, "hash": digest}

def format_bytes(num_bytes):
    for unit in ['B','KB','MB','GB','TB']:
        if num_bytes < 1024: return f"{num_bytes:.2f} {unit}"
        num_bytes /= 1024
    return f"{num_bytes:.2f} PB"

def get_local_storage_info(path):
    try:
        usage = shutil.disk_usage(os.path.splitdrive(os.path.abspath(path))[0] + os.sep)
        return usage.used, usage.total, (usage.used / usage.total)
    except: return 0, 1, 0

def is_hidden_or_system(path):
    if os.name == 'nt':
        try:
            attrs = ctypes.windll.kernel32.GetFileAttributesW(path)
            return bool(attrs & (0x2 | 0x4))
        except: return False
    return os.path.basename(path).startswith('.')

def normalize_extensions(extensions):
    normalized = []
    for ext in extensions or []:
        ext = str(ext).strip().lower()
        if not ext:
            continue
        if not ext.startswith('.'):
            ext = f".{ext}"
        if ext not in normalized:
            normalized.append(ext)
    return normalized

def normalize_excludes(exclude_dirs):
    return [str(name).strip().lower() for name in (exclude_dirs or []) if str(name).strip()]

def ensure_unique_path(path):
    if not os.path.exists(path):
        return path

    directory, filename = os.path.split(path)
    stem, ext = os.path.splitext(filename)
    counter = 1
    while True:
        candidate = os.path.join(directory, f"{stem} ({counter}){ext}")
        if not os.path.exists(candidate):
            return candidate
        counter += 1
