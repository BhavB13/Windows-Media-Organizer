import os
import queue
import subprocess
import threading
import time
from dataclasses import dataclass

from models import FileInfo
from utils import is_hidden_or_system, normalize_extensions, normalize_excludes


ADB_CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0
ADB_TEXT_KWARGS = {"text": True, "encoding": "utf-8", "errors": "replace"}


@dataclass
class ScanProgress:
    phase: str
    source: str
    current_path: str = ""
    folders_scanned: int = 0
    files_found: int = 0
    errors: int = 0
    current: int = 0
    total: int = 0
    message: str = ""
    is_adb: bool = False
    cancelled: bool = False


@dataclass
class DiscoveryResult:
    files: list
    folders_scanned: int
    files_found: int
    errors: list
    cancelled: bool


def _normalize_settings(settings):
    settings.extensions = normalize_extensions(getattr(settings, "extensions", []))
    settings.exclude_dirs = normalize_excludes(getattr(settings, "exclude_dirs", []))
    return settings


def _normalize_local_root(root):
    root = os.path.abspath(os.path.normpath(root))
    if os.name != "nt":
        return root
    if root.startswith("\\\\?\\") or root.startswith("\\\\"):
        return root
    if len(root) < 240:
        return root
    if root[1:3] == ":\\":
        return f"\\\\?\\{root}"
    return root


def _is_excluded_local(path, settings):
    if settings.skip_hidden_system and is_hidden_or_system(path):
        return True
    name = os.path.basename(path).lower()
    return name in set(settings.exclude_dirs)


def _is_excluded_remote(path, settings):
    name = os.path.basename(path.rstrip("/")).lower()
    return name in set(settings.exclude_dirs)


def _should_include_file(path, size, settings):
    if size < settings.min_size_kb * 1024:
        return False
    if getattr(settings, "only_media", False):
        ext = os.path.splitext(path)[1].lower()
        return ext in settings.extensions
    return True


def _emit_progress(progress_callback, progress, last_emit, force=False):
    if not progress_callback:
        return last_emit
    now = time.monotonic()
    should_emit = force or (now - last_emit) >= 0.15
    if should_emit:
        progress_callback(progress)
        return now
    return last_emit


def _drain_stderr(process, errors, progress_callback, progress, logger=None):
    try:
        for raw_line in iter(process.stderr.readline, ""):
            if not raw_line:
                break
            line = raw_line.strip()
            if not line:
                continue
            errors.append(line)
            progress.errors = len(errors)
            progress.message = line
            if logger:
                logger.log(f"WARNING: {line}")
            if progress_callback:
                progress_callback(progress)
    except Exception:
        pass


def _adb_shell_command(serial, script):
    cmd = ["adb"]
    if serial:
        cmd.extend(["-s", serial])
    cmd.extend(["exec-out", "sh", "-c", script])
    return cmd


def _shell_quote(value):
    return "'" + str(value).replace("'", "'\"'\"'") + "'"


def _canonical_adb_walk_root(root):
    if root == "/sdcard":
        return "/storage/emulated/0"
    if root.startswith("/sdcard/"):
        return f"/storage/emulated/0/{root[len('/sdcard/'):]}"
    return root


def _display_adb_path(path, walk_root, display_root):
    if walk_root != display_root and (path == walk_root or path.startswith(f"{walk_root}/")):
        return f"{display_root}{path[len(walk_root):]}"
    return path


def _build_remote_find_script(root, settings, find_type, follow_links=True):
    quoted_root = _shell_quote(root)
    excluded = [name for name in getattr(settings, "exclude_dirs", []) if name]
    prune = ""
    if excluded:
        name_expr = " -o ".join(f"-name {_shell_quote(name)}" for name in excluded)
        prune = f"\\( -type d \\( {name_expr} \\) -prune \\) -o "
    link_flag = "-L " if follow_links else ""
    return f"find {link_flag}{quoted_root} {prune}-type {find_type} -print"


def _build_remote_walk_script(root, settings, follow_links=True):
    quoted_root = _shell_quote(root)
    excluded = [name for name in getattr(settings, "exclude_dirs", []) if name]
    prune = ""
    if excluded:
        name_expr = " -o ".join(f"-name {_shell_quote(name)}" for name in excluded)
        prune = f"\\( -type d \\( {name_expr} \\) -prune \\) -o "
    link_flag = "-L " if follow_links else ""
    return (
        f"find {link_flag}{quoted_root} {prune}\\( -type d -o -type f \\) -print | "
        "while IFS= read -r path; do "
        "if [ -d \"$path\" ]; then printf 'D|%s\\n' \"$path\"; "
        "elif [ -f \"$path\" ]; then stat -c 'F|%s|%Y|%n' \"$path\" 2>/dev/null; fi; "
        "done"
    )


def scan_local_tree(root, settings, stop_event, progress_callback=None, logger=None):
    settings = _normalize_settings(settings)
    root = _normalize_local_root(root)
    files = []
    errors = []
    folders_scanned = 0
    files_found = 0
    progress = ScanProgress(
        phase="discovering",
        source=root,
        current_path=root,
        is_adb=False,
    )
    last_emit = 0.0
    stack = [root]
    seen_dirs = set()

    while stack and not stop_event.is_set():
        current_dir = stack.pop()
        if current_dir in seen_dirs:
            continue
        seen_dirs.add(current_dir)
        folders_scanned += 1
        progress.folders_scanned = folders_scanned
        progress.current_path = current_dir
        progress.message = "Scanning folder"
        last_emit = _emit_progress(progress_callback, progress, last_emit, force=(folders_scanned == 1))

        subdirs = []
        try:
            with os.scandir(current_dir) as iterator:
                for entry in iterator:
                    if stop_event.is_set():
                        break
                    try:
                        entry_path = entry.path
                        if entry.is_dir(follow_symlinks=False):
                            if _is_excluded_local(entry_path, settings):
                                continue
                            subdirs.append(entry_path)
                            continue
                        if not entry.is_file(follow_symlinks=False):
                            continue
                        if _is_excluded_local(entry_path, settings):
                            continue
                        stat_result = entry.stat(follow_symlinks=False)
                        size = int(stat_result.st_size)
                        if not _should_include_file(entry_path, size, settings):
                            continue
                        files.append(FileInfo(entry_path, size, float(stat_result.st_mtime)))
                        files_found += 1
                        progress.files_found = files_found
                        progress.current_path = entry_path
                        progress.message = "File discovered"
                        last_emit = _emit_progress(progress_callback, progress, last_emit)
                    except (OSError, ValueError) as exc:
                        error_msg = f"{entry.path}: {exc}"
                        errors.append(error_msg)
                        progress.errors = len(errors)
                        progress.message = error_msg
                        if logger:
                            logger.log(f"WARNING: {error_msg}")
                        last_emit = _emit_progress(progress_callback, progress, last_emit)
        except (OSError, ValueError) as exc:
            error_msg = f"{current_dir}: {exc}"
            errors.append(error_msg)
            progress.errors = len(errors)
            progress.message = error_msg
            if logger:
                logger.log(f"WARNING: {error_msg}")
            last_emit = _emit_progress(progress_callback, progress, last_emit, force=True)

        for subdir in reversed(subdirs):
            stack.append(subdir)

    progress.cancelled = stop_event.is_set()
    progress.phase = "complete" if not progress.cancelled else "cancelled"
    progress.current_path = root
    progress.folders_scanned = folders_scanned
    progress.files_found = files_found
    progress.message = "Discovery finished" if not progress.cancelled else "Discovery cancelled"
    _emit_progress(progress_callback, progress, last_emit, force=True)
    return DiscoveryResult(files, folders_scanned, files_found, errors, progress.cancelled)


def _stream_lines(process):
    for raw_line in iter(process.stdout.readline, ""):
        if not raw_line:
            break
        line = raw_line.strip()
        if line:
            yield line


def _enqueue_stdout_lines(process, output_queue):
    try:
        for raw_line in iter(process.stdout.readline, ""):
            if not raw_line:
                break
            line = raw_line.strip()
            if line:
                output_queue.put(line)
    finally:
        output_queue.put(None)


def _scan_remote_paths(root, settings, stop_event, find_type, progress, progress_callback=None, logger=None):
    serial = getattr(settings, "adb_serial", "")
    script = _build_remote_find_script(root, settings, find_type)
    last_emit = 0.0
    process = subprocess.Popen(
        _adb_shell_command(serial, script),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        **ADB_TEXT_KWARGS,
        creationflags=ADB_CREATE_NO_WINDOW,
    )
    errors = []
    stderr_thread = threading.Thread(
        target=_drain_stderr,
        args=(process, errors, progress_callback, progress, logger),
        daemon=True,
    )
    stderr_thread.start()
    try:
        for path in _stream_lines(process):
            if stop_event.is_set():
                process.terminate()
                break
            if find_type == "d":
                progress.folders_scanned += 1
                progress.current_path = path
                progress.message = "Scanning folder"
                last_emit = _emit_progress(progress_callback, progress, last_emit)
            else:
                progress.files_found += 1
                progress.current_path = path
                progress.message = "File discovered"
                last_emit = _emit_progress(progress_callback, progress, last_emit)
                yield path
    finally:
        try:
            process.wait(timeout=5)
        except Exception:
            try:
                process.kill()
            except Exception:
                pass
        stderr_thread.join(timeout=2)
        if errors:
            progress.errors = len(errors)


def scan_adb_tree(root, settings, stop_event, progress_callback=None, logger=None):
    settings = _normalize_settings(settings)
    from adb_bridge import ADBBridge

    root = ADBBridge.normalize_remote_path(root)
    files = []
    errors = []
    progress = ScanProgress(
        phase="discovering",
        source=root,
        current_path=root,
        is_adb=True,
    )
    last_emit = 0.0

    if stop_event.is_set():
        progress.cancelled = True
        progress.phase = "cancelled"
        progress.message = "Discovery cancelled"
        _emit_progress(progress_callback, progress, last_emit, force=True)
        return DiscoveryResult(files, 0, 0, errors, True)

    probe_ok, probe_message = (True, "")
    try:
        probe_ok, probe_message = ADBBridge.probe_device(getattr(settings, "adb_serial", ""))
    except Exception as exc:
        probe_ok = False
        probe_message = str(exc)

    if not probe_ok:
        error_msg = f"ADB device not ready: {probe_message}".strip()
        if logger:
            logger.log(f"ERROR: {error_msg}")
        errors.append(error_msg)
        progress.errors = len(errors)
        progress.message = error_msg
        progress.phase = "complete"
        _emit_progress(progress_callback, progress, last_emit, force=True)
        return DiscoveryResult(files, 0, 0, errors, False)

    path_status = ADBBridge.remote_path_status(root, serial=getattr(settings, "adb_serial", ""))
    if path_status != "dir":
        error_msg = f"ADB path is not accessible as a folder: {root} ({path_status})"
        if logger:
            logger.log(f"ERROR: {error_msg}")
        errors.append(error_msg)
        progress.errors = len(errors)
        progress.current_path = root
        progress.message = error_msg
        progress.phase = "complete"
        _emit_progress(progress_callback, progress, last_emit, force=True)
        return DiscoveryResult(files, 0, 0, errors, False)

    walk_root = _canonical_adb_walk_root(root)
    follow_links = walk_root == root
    if walk_root != root:
        walk_status = ADBBridge.remote_path_status(walk_root, serial=getattr(settings, "adb_serial", ""))
        if walk_status != "dir":
            if logger:
                logger.log(f"WARNING: Canonical ADB path unavailable: {walk_root} ({walk_status}); falling back to {root}")
            walk_root = root
            follow_links = True
    progress.message = f"Waiting for ADB file list from {root}"
    last_emit = _emit_progress(progress_callback, progress, last_emit, force=True)
    serial = getattr(settings, "adb_serial", "")
    script = _build_remote_walk_script(walk_root, settings, follow_links=follow_links)
    try:
        process = subprocess.Popen(
            _adb_shell_command(serial, script),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            **ADB_TEXT_KWARGS,
            creationflags=ADB_CREATE_NO_WINDOW,
        )
    except (FileNotFoundError, OSError, subprocess.SubprocessError) as exc:
        error_msg = f"ADB discovery failed: {exc}"
        if logger:
            logger.log(f"ERROR: {error_msg}")
        errors.append(error_msg)
        progress.errors = len(errors)
        progress.message = error_msg
        progress.phase = "complete"
        _emit_progress(progress_callback, progress, last_emit, force=True)
        return DiscoveryResult(files, progress.folders_scanned, progress.files_found, errors, False)
    stderr_thread = threading.Thread(
        target=_drain_stderr,
        args=(process, errors, progress_callback, progress, logger),
        daemon=True,
    )
    stderr_thread.start()
    output_queue = queue.Queue()
    stdout_thread = threading.Thread(target=_enqueue_stdout_lines, args=(process, output_queue), daemon=True)
    stdout_thread.start()
    last_output = time.monotonic()
    idle_notice_interval = 5.0

    try:
        stdout_done = False
        while not stdout_done:
            if stop_event.is_set():
                process.terminate()
                break
            try:
                raw_line = output_queue.get(timeout=1.0)
            except queue.Empty:
                idle_for = int(time.monotonic() - last_output)
                if idle_for >= idle_notice_interval:
                    progress.message = f"ADB is still scanning; no new paths for {idle_for}s"
                    last_emit = _emit_progress(progress_callback, progress, last_emit, force=True)
                    last_output = time.monotonic()
                continue
            if raw_line is None:
                stdout_done = True
                continue
            last_output = time.monotonic()
            if "|" not in raw_line:
                continue
            if raw_line.startswith("D|"):
                path = _display_adb_path(raw_line[2:], walk_root, root)
                if _is_excluded_remote(path, settings):
                    continue
                progress.folders_scanned += 1
                progress.current_path = path
                progress.message = "Scanning folder"
                last_emit = _emit_progress(progress_callback, progress, last_emit)
                continue
            if not raw_line.startswith("F|"):
                continue
            try:
                _, size_str, mtime_str, raw_path = raw_line.split("|", 3)
                path = _display_adb_path(raw_path, walk_root, root)
                size = int(size_str)
                mtime = float(mtime_str)
                if _is_excluded_remote(path, settings):
                    continue
                if not _should_include_file(path, size, settings):
                    continue
                files.append(FileInfo(path, size, mtime, is_adb=True))
                progress.files_found = len(files)
                progress.current_path = path
                progress.message = "File discovered"
                last_emit = _emit_progress(progress_callback, progress, last_emit)
            except (OSError, ValueError) as exc:
                error_msg = f"{raw_line}: {exc}"
                errors.append(error_msg)
                progress.errors = len(errors)
                progress.message = error_msg
                if logger:
                    logger.log(f"WARNING: {error_msg}")
                last_emit = _emit_progress(progress_callback, progress, last_emit)
    finally:
        try:
            process.wait(timeout=5)
        except Exception:
            try:
                process.kill()
            except Exception:
                pass
        stdout_thread.join(timeout=2)
        stderr_thread.join(timeout=2)

    progress.cancelled = stop_event.is_set()
    progress.phase = "complete" if not progress.cancelled else "cancelled"
    progress.message = "Discovery finished" if not progress.cancelled else "Discovery cancelled"
    _emit_progress(progress_callback, progress, last_emit, force=True)
    return DiscoveryResult(files, progress.folders_scanned, len(files), errors, progress.cancelled)


def discover_files(root, settings, stop_event, progress_callback=None, logger=None):
    if getattr(settings, "use_adb", False) or getattr(settings, "source_is_adb", False):
        return scan_adb_tree(root, settings, stop_event, progress_callback, logger)
    return scan_local_tree(root, settings, stop_event, progress_callback, logger)
