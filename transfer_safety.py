import json
import hashlib
import ctypes
import os
import shutil
import time
from datetime import datetime

from adb_bridge import ADBBridge, ADBOperationError
from runtime_paths import get_runtime_paths


ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001
ES_AWAYMODE_REQUIRED = 0x00000040


def prevent_windows_sleep():
    if os.name != "nt":
        return False
    try:
        result = ctypes.windll.kernel32.SetThreadExecutionState(
            ES_CONTINUOUS | ES_SYSTEM_REQUIRED | ES_AWAYMODE_REQUIRED
        )
        return bool(result)
    except (AttributeError, OSError):
        return False


def restore_windows_sleep():
    if os.name != "nt":
        return
    try:
        ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS)
    except (AttributeError, OSError):
        pass


class TransferJournal:
    def __init__(self, path):
        self.path = path
        self.data = {"version": 1, "completed": {}, "failures": {}, "updated_at": 0}
        self._dirty_entries = 0
        self._last_save = time.monotonic()
        self.load()

    def load(self):
        try:
            with open(self.path, "r", encoding="utf-8") as handle:
                loaded = json.load(handle)
            if loaded.get("version") == 1:
                self.data = loaded
        except (OSError, json.JSONDecodeError):
            pass

    def save(self, force=False):
        if not force and self._dirty_entries < 25 and time.monotonic() - self._last_save < 5:
            return
        os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
        self.data["updated_at"] = time.time()
        temp_path = f"{self.path}.tmp"
        with open(temp_path, "w", encoding="utf-8") as handle:
            json.dump(self.data, handle, indent=2)
        os.replace(temp_path, self.path)
        self._dirty_entries = 0
        self._last_save = time.monotonic()

    def complete(self, source, target, size, digest):
        self.data["completed"][source] = {
            "target": target, "size": size, "hash": digest, "completed_at": time.time()
        }
        self.data["failures"].pop(source, None)
        self._dirty_entries += 1
        self.save()

    def fail(self, source, error):
        entry = self.data["failures"].setdefault(source, {"attempts": 0})
        entry.update({"attempts": entry["attempts"] + 1, "error": str(error), "updated_at": time.time()})
        self._dirty_entries += 1
        self.save()

    def is_complete(self, source, size):
        entry = self.data["completed"].get(source)
        if not entry:
            return False
        target = entry.get("target", "") if entry else ""
        try:
            return entry.get("size") == size and os.path.getsize(target) == size
        except OSError:
            return False


def default_journal_path(output_root, serial="local"):
    safe_serial = "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in serial) or "local"
    root_tag = hashlib.sha256(os.path.abspath(output_root).encode("utf-8")).hexdigest()[:10]
    return str(get_runtime_paths().journals / f"{safe_serial}_{root_tag}.journal.json")


def preflight_transfer(output_root, source_files, is_adb=False, serial="", verify_write=True):
    errors = []
    warnings = []
    if is_adb and verify_write:
        ready, detail = ADBBridge.probe_device(serial)
        if not ready:
            errors.append(f"ADB device is not ready: {detail}")
    if verify_write:
        try:
            probe = os.path.join(output_root, ".duplicate_transfer_manager_write_test")
            with open(probe, "wb") as handle:
                handle.write(b"")
            os.remove(probe)
        except OSError as exc:
            errors.append(f"Output folder is not writable: {exc}")
    try:
        required = sum(info.size for info in source_files)
        free = shutil.disk_usage(output_root).free
        if free < required:
            errors.append(f"Insufficient free space: need up to {required} bytes, have {free} bytes.")
        elif free < required * 1.1:
            warnings.append("Destination free space is within 10% of the maximum transfer size.")
    except OSError as exc:
        warnings.append(f"Could not check destination free space: {exc}")
    return errors, warnings


def pull_with_retries(
    source,
    target,
    serial,
    attempts=3,
    logger=None,
    progress_callback=None,
    expected_size=0,
    overall_bytes_before=0,
    overall_bytes_total=0,
    reconnect_timeout=300,
    stall_timeout=180,
    stop_event=None,
):
    partial = f"{target}.partial"
    os.makedirs(os.path.dirname(target), exist_ok=True)
    last_error = None
    for attempt in range(1, max(1, attempts) + 1):
        try:
            destination_root = os.path.splitdrive(os.path.abspath(target))[0] + os.sep
            wait_started = time.monotonic()
            while destination_root and not os.path.exists(destination_root):
                if stop_event and stop_event.is_set():
                    raise OSError("Transfer cancelled while waiting for the destination drive.")
                waited = int(time.monotonic() - wait_started)
                if waited >= reconnect_timeout:
                    raise OSError(f"Destination drive unavailable after {waited}s: {destination_root}")
                if progress_callback:
                    progress_callback(0, 0, f"Waiting for destination drive: {destination_root} ({waited}s)")
                time.sleep(2)
            if os.path.exists(partial):
                os.remove(partial)
            if progress_callback:
                progress_callback(0, 0, f"Pull attempt {attempt}/{attempts}: {os.path.basename(source)}")
            compressed_media = {
                ".jpg", ".jpeg", ".png", ".gif", ".webp", ".heic",
                ".mp4", ".mov", ".mkv", ".avi", ".3gp", ".mp3", ".aac", ".m4a", ".zip",
            }
            disable_compression = os.path.splitext(source)[1].lower() in compressed_media
            ADBBridge.pull(
                source,
                partial,
                serial=serial,
                disable_compression=disable_compression,
                stall_timeout=stall_timeout,
                progress_callback=(
                    lambda current: progress_callback(
                        overall_bytes_before + current,
                        overall_bytes_total or expected_size,
                        f"Pulling {os.path.basename(source)}: "
                        f"{current / (1024**2):.1f}/{expected_size / (1024**2):.1f} MB | "
                        f"Overall {(overall_bytes_before + current) / (1024**3):.2f}/"
                        f"{(overall_bytes_total or expected_size) / (1024**3):.2f} GB",
                    )
                    if progress_callback and expected_size
                    else None
                ),
            )
            os.replace(partial, target)
            return target
        except (OSError, ADBOperationError) as exc:
            last_error = exc
            if logger:
                logger.log(f"WARNING: ADB pull attempt {attempt}/{attempts} failed: {exc}")
            if isinstance(exc, ADBOperationError) and exc.device_unavailable:
                ready, detail = ADBBridge.wait_for_device(
                    serial,
                    timeout=reconnect_timeout,
                    stop_event=stop_event,
                )
                if not ready and logger:
                    logger.log(f"WARNING: ADB reconnect wait failed: {detail}")
            if attempt < attempts:
                time.sleep(min(2 ** (attempt - 1), 8))
    raise last_error


def cleanup_partial_files(root, dry_run=False):
    removed = []
    for current_root, _, files in os.walk(root):
        for filename in files:
            if not filename.endswith(".partial"):
                continue
            path = os.path.join(current_root, filename)
            try:
                if not dry_run:
                    os.remove(path)
                removed.append(path)
            except OSError:
                pass
    return removed


def write_transfer_report(output_root, result, failures):
    report_dir = get_runtime_paths().reports
    path = report_dir / f"transfer_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    payload = dict(result)
    payload["failures"] = failures
    payload["created_at"] = datetime.now().isoformat(timespec="seconds")
    with path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2)
    return str(path)
