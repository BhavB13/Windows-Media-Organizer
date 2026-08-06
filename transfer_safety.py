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
APP_STAGING_DIRECTORY = ".duplicate_transfer_manager_staging"
APP_PARTIAL_SUFFIX = ".dtm-partial"


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

    def complete(
        self,
        source,
        target,
        size,
        digest,
        replaced_backup="",
        hash_algo="",
        hash_mode="",
    ):
        self.data["completed"][source] = {
            "target": target, "size": size, "hash": digest, "completed_at": time.time()
        }
        try:
            self.data["completed"][source]["target_mtime"] = os.path.getmtime(target)
        except OSError:
            # Without a recorded timestamp, resume falls back to full content
            # verification for this entry.
            pass
        if replaced_backup:
            self.data["completed"][source]["replaced_backup"] = replaced_backup
        if hash_algo:
            self.data["completed"][source]["hash_algo"] = hash_algo
        if hash_mode:
            self.data["completed"][source]["hash_mode"] = hash_mode
        self.data["failures"].pop(source, None)
        self._dirty_entries += 1
        self.save()

    def fail(self, source, error, partial_path=""):
        entry = self.data["failures"].setdefault(source, {"attempts": 0})
        entry.update({"attempts": entry["attempts"] + 1, "error": str(error), "updated_at": time.time()})
        if partial_path:
            entry["partial_path"] = os.path.abspath(partial_path)
        self._dirty_entries += 1
        self.save()

    def is_complete(self, source, size, verify_content=False):
        """Report whether a journalled file is still safely complete.

        Size and the modification time recorded at completion are checked
        first. Re-reading every finished file is prohibitively slow when
        resuming a large library, so a target that still matches both is
        trusted. Anything that rewrites a file changes its timestamp, and a
        mismatch falls through to full content verification rather than an
        immediate re-transfer. Callers that need certainty against silent
        corruption pass ``verify_content=True``.
        """

        entry = self.data["completed"].get(source)
        if not entry:
            return False
        target = entry.get("target", "") if entry else ""
        try:
            if entry.get("size") != size or os.path.getsize(target) != size:
                return False
            expected = str(entry.get("hash", ""))
            if not expected:
                return False
            if not verify_content and _target_timestamp_matches(target, entry.get("target_mtime")):
                return True
            algorithm = str(entry.get("hash_algo", "")) or _infer_hash_algorithm(expected)
            mode = str(entry.get("hash_mode", "full"))
            return _hash_local_file(target, algorithm, mode) == expected
        except (OSError, ValueError):
            return False


def _target_timestamp_matches(target, recorded_mtime):
    """Compare a target's timestamp with the one stored at completion.

    The comparison is deliberately exact. Both values come from
    ``os.path.getmtime`` on the same file, so a coarse filesystem such as
    FAT32 or exFAT truncates the recorded and observed values identically and
    still matches. A tolerance window would instead let a rewrite that lands
    inside the window pass as unchanged. Any mismatch, including the one-hour
    FAT/DST shift Windows can produce, falls through to content verification
    rather than being trusted, so drift costs time and never safety.

    Journals written before timestamps were recorded return False, keeping
    those entries on full content verification.
    """

    if recorded_mtime is None:
        return False
    try:
        return abs(os.path.getmtime(target) - float(recorded_mtime)) < 1e-6
    except (OSError, TypeError, ValueError):
        return False


def _infer_hash_algorithm(digest):
    algorithms = {32: "md5", 40: "sha1", 64: "sha256", 128: "sha512"}
    algorithm = algorithms.get(len(str(digest)))
    if not algorithm:
        raise ValueError("Unsupported journal digest length")
    return algorithm


def _hash_local_file(path, algorithm, mode):
    digest = hashlib.new(algorithm)
    size = os.path.getsize(path)
    with open(path, "rb") as stream:
        if mode == "fast" and size > 2097152:
            digest.update(str(size).encode())
            digest.update(stream.read(1048576))
            stream.seek(-1048576, 2)
            digest.update(stream.read(1048576))
        else:
            for chunk in iter(lambda: stream.read(1048576), b""):
                digest.update(chunk)
    return digest.hexdigest()


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
    partial_path="",
    promote=True,
):
    partial = partial_path or f"{target}{APP_PARTIAL_SUFFIX}"
    os.makedirs(os.path.dirname(target), exist_ok=True)
    os.makedirs(os.path.dirname(partial), exist_ok=True)
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
            if promote:
                os.replace(partial, target)
                return target
            return partial
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
    root = os.path.abspath(root)
    owned = set()
    staging_root = os.path.join(root, APP_STAGING_DIRECTORY)
    if os.path.isdir(staging_root):
        for current_root, _, files in os.walk(staging_root):
            for filename in files:
                owned.add(os.path.abspath(os.path.join(current_root, filename)))

    # Version-1 journals remain readable; newer failure entries may additionally
    # identify an app-owned partial path. Never infer ownership from a suffix.
    for journal_path in get_runtime_paths().journals.glob("*.json"):
        try:
            with journal_path.open("r", encoding="utf-8") as handle:
                payload = json.load(handle)
        except (OSError, json.JSONDecodeError):
            continue
        failures = payload.get("failures", {}) if isinstance(payload, dict) else {}
        for entry in failures.values() if isinstance(failures, dict) else ():
            if not isinstance(entry, dict):
                continue
            partial = str(entry.get("partial_path", "")).strip()
            if not partial:
                continue
            candidate = os.path.abspath(partial)
            try:
                if os.path.commonpath([root, candidate]) == root:
                    owned.add(candidate)
            except (OSError, ValueError):
                continue

    removed = []
    for path in sorted(owned):
        try:
            if not os.path.isfile(path):
                continue
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
