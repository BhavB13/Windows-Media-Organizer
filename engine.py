import os
import posixpath
import time
import hashlib
import shutil
import subprocess
from datetime import UTC, datetime
from copy import copy
from concurrent.futures import ThreadPoolExecutor, as_completed
from adb_bridge import ADBBridge, ADBOperationError
from discovery import discover_files
from drive_cache import ADBHashCache, DriveHashCache, default_adb_cache_path, default_cache_path
from models import FileInfo
from transfer_safety import (
    TransferJournal,
    default_journal_path,
    preflight_transfer,
    prevent_windows_sleep,
    pull_with_retries,
    restore_windows_sleep,
    write_transfer_report,
)
from utils import ensure_unique_path, is_hidden_or_system, normalize_excludes, normalize_extensions

def compute_hash(path, settings, stop_event, hash_cache=None, is_adb=False, logger=None):
    if is_adb:
        try:
            digest = ADBBridge.remote_hash(path, settings.hash_algo, serial=getattr(settings, "adb_serial", ""))
        except ADBOperationError as exc:
            if logger:
                logger.log(f"WARNING: {exc}")
            if exc.device_unavailable:
                raise
            return ""
        if not digest and logger:
            logger.log(f"WARNING: Failed to hash ADB file: {path}")
        return digest
    h = hashlib.new(settings.hash_algo)
    try:
        size, mtime = os.path.getsize(path), os.path.getmtime(path)
        if hash_cache:
            cached = hash_cache.get(path, settings.hash_algo, settings.hash_mode, size, mtime)
            if cached: return cached
        with open(path, "rb") as f:
            if settings.hash_mode == "fast" and size > 2097152:
                h.update(str(size).encode()); h.update(f.read(1048576))
                f.seek(-1048576, 2); h.update(f.read(1048576))
            else:
                while not stop_event.is_set():
                    data = f.read(1048576)
                    if not data: break
                    h.update(data)
        digest = h.hexdigest()
        if hash_cache: hash_cache.set(path, settings.hash_algo, settings.hash_mode, size, mtime, digest)
        return digest
    except (OSError, ValueError) as exc:
        if logger:
            logger.log(f"WARNING: Failed to hash file: {path} ({exc})")
        return ""

def normalize_settings(settings):
    settings.extensions = normalize_extensions(getattr(settings, "extensions", []))
    settings.exclude_dirs = normalize_excludes(getattr(settings, "exclude_dirs", []))
    return settings


def _safe_commonpath(paths):
    try:
        return os.path.commonpath(paths)
    except (ValueError, OSError):
        return None

def build_relative_path(source_path, source_root, is_adb=False):
    if not source_root:
        return ""
    try:
        if is_adb:
            normalized_root = posixpath.normpath(source_root)
            normalized_path = posixpath.normpath(source_path)
            rel_path = posixpath.relpath(normalized_path, normalized_root)
        else:
            normalized_root = os.path.abspath(source_root)
            normalized_path = os.path.abspath(source_path)
            rel_path = os.path.relpath(normalized_path, normalized_root)
        if rel_path == "." or rel_path.startswith(".."):
            return ""
        return rel_path
    except ValueError:
        return ""

def build_target_path(
    source_path,
    source_root,
    dest_root,
    preserve_structure,
    is_adb=False,
    avoid_collisions=True,
    destination_template="preserve",
    source_timestamp=0,
):
    filename = os.path.basename(source_path)
    if destination_template == "date":
        try:
            # Use UTC so the folder is stable across machines and daylight-saving changes.
            dated_folder = datetime.fromtimestamp(float(source_timestamp), UTC).strftime("%Y/%m")
        except (OSError, OverflowError, TypeError, ValueError):
            dated_folder = "Unknown date"
        target_path = os.path.join(dest_root, *dated_folder.split("/"), filename)
        os.makedirs(os.path.dirname(target_path), exist_ok=True)
        return ensure_unique_path(target_path) if avoid_collisions else target_path
    if preserve_structure and source_root:
        rel_path = build_relative_path(source_path, source_root, is_adb=is_adb)
        if rel_path:
            target_path = os.path.join(dest_root, *rel_path.split("/")) if is_adb else os.path.join(dest_root, rel_path)
            os.makedirs(os.path.dirname(target_path), exist_ok=True)
            return ensure_unique_path(target_path) if avoid_collisions else target_path

    os.makedirs(dest_root, exist_ok=True)
    target_path = os.path.join(dest_root, filename)
    return ensure_unique_path(target_path) if avoid_collisions else target_path

def resolve_conflict_path(path, policy):
    if not os.path.exists(path):
        return path
    if policy == "replace":
        return path
    if policy == "skip":
        return ""
    return ensure_unique_path(path)

def validate_transfer_paths(settings):
    source_root = settings.source_root.strip()
    compare_root = settings.dest_root.strip()
    output_root = getattr(settings, "output_root", "").strip()
    copy_root = output_root or compare_root

    if not source_root:
        return "Source path is required."
    if not compare_root:
        return "Compare folder is required."
    if getattr(settings, "source_is_adb", False) and not getattr(settings, "adb_serial", "").strip():
        return "Select an ADB device before starting a transfer."
    if getattr(settings, "source_is_adb", False):
        normalized_source = ADBBridge.normalize_remote_path(source_root)
        settings.source_root = normalized_source
        source_root = normalized_source
        device_state = ADBBridge.get_device_state(getattr(settings, "adb_serial", "").strip())
        if device_state and device_state != "device":
            return f"Selected ADB device is {device_state}. Authorize USB debugging on the phone before starting a transfer."
        path_status = ADBBridge.remote_path_status(source_root, serial=getattr(settings, "adb_serial", "").strip())
        if path_status != "dir":
            return f"ADB source folder is not accessible: {source_root} ({path_status}). Try /sdcard/DCIM or /storage/emulated/0/DCIM."
    if getattr(settings, "transfer_mode", "copy") != "copy":
        return "Transfer mode must remain copy-only. Source files are never modified or deleted."
    if not os.path.isdir(compare_root):
        return "Compare folder does not exist."
    if output_root and not os.path.isdir(output_root):
        return "Output folder does not exist."
    if not getattr(settings, "source_is_adb", False) and not os.path.exists(source_root):
        return "Source path does not exist."
    if not getattr(settings, "source_is_adb", False):
        source_abs = os.path.abspath(source_root)
        compare_abs = os.path.abspath(compare_root)
        copy_abs = os.path.abspath(copy_root)
        if source_abs == compare_abs:
            return "Source and compare folder must be different."
        if source_abs == copy_abs:
            return "Source and output folder must be different."
        common_compare = _safe_commonpath([source_abs, compare_abs])
        common_copy = _safe_commonpath([source_abs, copy_abs])
        if common_compare == source_abs:
            return "Compare folder cannot be inside the source folder."
        if common_copy == source_abs:
            return "Output folder cannot be inside the source folder."
    return ""

def validate_scan_paths(settings):
    scan_root = settings.scan_root.strip()
    if not scan_root:
        return "Scan target is required."
    if getattr(settings, "use_adb", False) and not getattr(settings, "adb_serial", "").strip():
        return "Select an ADB device before starting an ADB scan."
    if getattr(settings, "use_adb", False):
        normalized_scan_root = ADBBridge.normalize_remote_path(scan_root)
        settings.scan_root = normalized_scan_root
        scan_root = normalized_scan_root
        device_state = ADBBridge.get_device_state(getattr(settings, "adb_serial", "").strip())
        if device_state and device_state != "device":
            return f"Selected ADB device is {device_state}. Authorize USB debugging on the phone before starting an ADB scan."
        path_status = ADBBridge.remote_path_status(scan_root, serial=getattr(settings, "adb_serial", "").strip())
        if path_status != "dir":
            return f"ADB scan folder is not accessible: {scan_root} ({path_status}). Try /sdcard/DCIM or /storage/emulated/0/DCIM."
    if not getattr(settings, "use_adb", False) and not os.path.exists(scan_root):
        return "Scan target does not exist."
    output_root = getattr(settings, "output_root", "").strip()
    if output_root and not os.path.isdir(output_root):
        return "Output folder does not exist."
    isolate_folder = getattr(settings, "isolate_folder", "").strip()
    if isolate_folder and not os.path.isdir(isolate_folder):
        return "Isolate folder does not exist."
    return ""

def iter_files(root, settings, stop_event, progress_callback=None, logger=None):
    settings = normalize_settings(settings)
    discovery = discover_files(root, settings, stop_event, progress_callback=progress_callback, logger=logger)
    yield from discovery.files

def _transfer_discovery_progress(label, start_time, progress_callback):
    if not progress_callback:
        return None

    def callback(progress):
        elapsed = int(time.time() - start_time)
        current_path = progress.current_path or progress.source or "-"
        message = progress.message or "Scanning"
        text = (
            f"{label}: {progress.files_found} files, "
            f"{progress.folders_scanned} folders, {progress.errors} errors | "
            f"{message}: {current_path} ({elapsed}s)"
        )
        progress_callback(0, 0, text)

    return callback

def build_source_scan_settings(settings):
    scan_settings = copy(settings)
    scan_settings.use_adb = getattr(settings, "source_is_adb", getattr(settings, "use_adb", False))
    scan_settings.source_is_adb = getattr(settings, "source_is_adb", False)
    return scan_settings

def build_compare_scan_settings(settings):
    scan_settings = copy(settings)
    scan_settings.use_adb = False
    scan_settings.source_is_adb = False
    return scan_settings

def build_transfer_hash_settings(settings):
    hash_settings = copy(settings)
    if getattr(settings, "source_is_adb", False):
        hash_settings.hash_mode = "full"
    return hash_settings

def get_drive_cache_path(root, settings):
    return getattr(settings, "drive_cache_path", "").strip() or default_cache_path(root)

def load_drive_cache(root, settings, logger=None):
    if not getattr(settings, "use_dest_cache", False):
        return None
    cache_path = get_drive_cache_path(root, settings)
    cache = DriveHashCache(cache_path)
    cache.load()
    if logger:
        stats = cache.stats()
        logger.log(f"Drive cache loaded: {cache_path} ({stats.files} files, {stats.hashes} hashes, {stats.stale} stale)")
    return cache

def load_adb_cache(settings, logger=None):
    if not getattr(settings, "source_is_adb", False) or not getattr(settings, "use_adb_cache", True):
        return None
    serial = getattr(settings, "adb_serial", "").strip()
    cache_path = getattr(settings, "adb_cache_path", "").strip() or default_adb_cache_path(serial)
    cache = ADBHashCache(cache_path, serial)
    cache.load()
    if logger:
        stats = cache.stats()
        logger.log(f"ADB cache loaded for {serial}: {cache_path} ({stats.files} files, {stats.hashes} hashes, {stats.stale} stale)")
    return cache

def group_duplicates(infos, settings, stop_event, hash_cache, logger, progress_callback=None):
    logger.log("Grouping files by size...")
    size_groups = {}
    for info in infos:
        if stop_event.is_set(): return []
        size_groups.setdefault(info.size, []).append(info)
    
    hash_groups = {}
    items_to_hash = [info for group in size_groups.values() if len(group) > 1 for info in group]
    total_to_hash = len(items_to_hash)
    processed = 0

    if total_to_hash == 0:
        if progress_callback: progress_callback(1, 1, "No duplicates found.")
        return []

    with ThreadPoolExecutor(max_workers=settings.max_hash_workers) as pool:
        futures = {pool.submit(compute_hash, info.path, settings, stop_event, hash_cache, info.is_adb, logger): info 
                   for info in items_to_hash}
        
        for future in as_completed(futures):
            if stop_event.is_set(): break
            processed += 1
            if progress_callback: progress_callback(processed, total_to_hash, "Hashing potential duplicates...")
            try:
                digest = future.result()
                if digest: hash_groups.setdefault(digest, []).append(futures[future])
            except: continue

    return [group for group in hash_groups.values() if len(group) > 1]

def execute_smart_transfer(settings, stop_event, hash_cache, logger, progress_callback=None):
    settings = normalize_settings(settings)
    validation_error = validate_transfer_paths(settings)
    if validation_error:
        logger.log(f"ERROR: {validation_error}")
        if progress_callback:
            progress_callback(0, 1, "Transfer blocked")
        return {"transferred": 0, "duplicates": 0, "isolated": 0, "skipped": 0}

    start_time = time.time()
    compare_root = settings.dest_root
    output_root = settings.output_root.strip() or settings.dest_root
    dry_run_label = "Dry run" if settings.dry_run else "Live run"
    hash_settings = build_transfer_hash_settings(settings)
    source_scan_settings = build_source_scan_settings(hash_settings)
    compare_scan_settings = build_compare_scan_settings(hash_settings)
    drive_cache = load_drive_cache(compare_root, compare_scan_settings, logger)
    adb_cache = load_adb_cache(hash_settings, logger)

    logger.log(f"{dry_run_label}: sync mode is copy-only. Source files will not be modified or deleted.")
    if getattr(settings, "source_is_adb", False) and settings.hash_mode != "full":
        logger.log("ADB transfer detected. Using full-content hashing so phone files match local comparison files.")
    logger.log(f"Scanning Source: {settings.source_root}")
    if progress_callback: progress_callback(0, 0, "Discovering source files... 0 found")
    
    source_files = []
    source_progress = _transfer_discovery_progress("Source scan", start_time, progress_callback)
    for count, f in enumerate(iter_files(settings.source_root, source_scan_settings, stop_event, source_progress, logger), 1):
        source_files.append(f)
        if count % 25 == 0 and progress_callback:
            elapsed = int(time.time() - start_time)
            progress_callback(0, 0, f"Scanning Source: {count} files found ({elapsed}s)")

    if progress_callback:
        progress_callback(0, 0, f"Scanning Source: {len(source_files)} files found")

    preflight_errors, preflight_warnings = preflight_transfer(
        output_root,
        source_files,
        is_adb=getattr(settings, "source_is_adb", False),
        serial=getattr(settings, "adb_serial", ""),
        verify_write=not settings.dry_run,
    )
    for warning in preflight_warnings:
        logger.log(f"WARNING: Preflight: {warning}")
    if preflight_errors:
        for error in preflight_errors:
            logger.log(f"ERROR: Preflight: {error}")
        if progress_callback:
            progress_callback(0, 1, "Preflight failed")
        return {
            "transferred": 0, "duplicates": 0, "isolated": 0, "skipped": 0,
            "errors": len(preflight_errors),
            "preflight_failed": True,
            "preflight_errors": list(preflight_errors),
            "preflight_warnings": list(preflight_warnings),
        }
    logger.log("Preflight passed: device, destination access, and free space checked.")
    sleep_inhibited = prevent_windows_sleep()
    if sleep_inhibited:
        logger.log("Windows sleep prevention enabled for this transfer.")

    adb_cache_count_matches = False
    if adb_cache:
        cached_source_count = adb_cache.active_file_count_under_root(
            settings.source_root,
            algo=hash_settings.hash_algo,
            mode=hash_settings.hash_mode,
        )
        adb_cache_count_matches = cached_source_count == len(source_files)
        logger.log(
            f"ADB cache source count: {cached_source_count} cached file(s), "
            f"{len(source_files)} file(s) currently present."
        )
        if adb_cache_count_matches:
            logger.log("ADB cache count matches the recursive source scan. Cached phone hashes will be used.")
        else:
            logger.log("ADB cache count differs from the recursive source scan. New or changed phone files will be hashed.")
    
    dest_time = time.time()
    logger.log(f"Scanning Compare Folder: {compare_root}")
    if progress_callback: progress_callback(0, 0, "Discovering compare-folder files... 0 found")
    
    dest_files = []
    compare_progress = _transfer_discovery_progress("Compare scan", dest_time, progress_callback)
    for count, f in enumerate(iter_files(compare_root, compare_scan_settings, stop_event, compare_progress, logger), 1):
        dest_files.append(f)
        if count % 25 == 0 and progress_callback:
            elapsed = int(time.time() - dest_time)
            progress_callback(0, 0, f"Scanning Compare: {count} files found ({elapsed}s)")

    if progress_callback:
        progress_callback(0, 0, f"Scanning Compare: {len(dest_files)} files found")
    
    dest_hashes = set()
    total_dest = len(dest_files)
    cache_count_matches = False
    if drive_cache:
        cached_count = drive_cache.active_file_count_under_root(
            compare_root,
            algo=compare_scan_settings.hash_algo,
            mode=compare_scan_settings.hash_mode,
        )
        cache_count_matches = cached_count == total_dest
        logger.log(f"Drive cache compare count: {cached_count} cached file(s), {total_dest} file(s) currently present.")

    if drive_cache and cache_count_matches:
        dest_hashes = drive_cache.hashes_under_root(
            compare_root,
            algo=compare_scan_settings.hash_algo,
            mode=compare_scan_settings.hash_mode,
        )
        logger.log(f"Drive cache count matches compare folder. Using {len(dest_hashes)} cached hash(es); destination hashing skipped.")
        if progress_callback:
            progress_callback(1, 1, f"Using drive cache: {total_dest} compare files")
    else:
        if drive_cache:
            logger.log("Drive cache count does not match compare folder. Hashing compare-folder files to refresh usable cache data.")
        else:
            logger.log("Hashing compare-folder files to build duplicate comparison data.")
        for i, f in enumerate(dest_files):
            if stop_event.is_set(): break
            cached = None
            if drive_cache:
                cached = drive_cache.get_valid_hash(
                    f.path,
                    compare_scan_settings.hash_algo,
                    compare_scan_settings.hash_mode,
                    f.size,
                    f.created,
                )
            h = cached or compute_hash(f.path, compare_scan_settings, stop_event, hash_cache, is_adb=False, logger=logger)
            if h and drive_cache and getattr(settings, "update_drive_cache", True) and not settings.dry_run:
                drive_cache.set_file_hash(
                    f.path,
                    f.size,
                    f.created,
                    h,
                    compare_scan_settings.hash_algo,
                    compare_scan_settings.hash_mode,
                    root=compare_root,
                )
            if h: dest_hashes.add(h)
            if progress_callback: progress_callback(i + 1, total_dest, f"Building destination cache... {i + 1}/{total_dest}")
        if drive_cache and getattr(settings, "update_drive_cache", True) and not settings.dry_run:
            drive_cache.mark_missing_under_root(compare_root, [f.path for f in dest_files])

    logger.log("Processing Source Files. Please wait...")
    logger.log(f"Copy Target Folder: {output_root}")
    logger.log(f"Source files discovered: {len(source_files)}")
    logger.log(f"Compare-folder files discovered: {len(dest_files)}")
    logger.log(
        "Structure-aware sync is enabled. Files are compared recursively across all subfolders "
        "and copied using their relative source paths."
    )
    transferred, isolated, skipped, transfer_errors = 0, 0, 0, 0
    bytes_transferred = 0
    resumed = 0
    failures = []
    adb_device_failed = False
    total_src = len(source_files)
    total_bytes = sum(f.size for f in source_files)
    source_byte_offsets = []
    byte_offset = 0
    for source_info in source_files:
        source_byte_offsets.append(byte_offset)
        byte_offset += source_info.size
    journal = None
    if not settings.dry_run:
        journal_path = getattr(settings, "journal_path", "").strip() or default_journal_path(
            output_root, getattr(settings, "adb_serial", "") or "local"
        )
        journal = TransferJournal(journal_path)
        logger.log(f"Resume journal: {journal_path}")
    previous_stay_awake = None
    if getattr(settings, "source_is_adb", False) and getattr(settings, "keep_device_awake", True):
        previous_stay_awake = ADBBridge.enable_usb_stay_awake(getattr(settings, "adb_serial", ""))
        logger.log("ADB USB keep-awake enabled for this transfer.")
    
    dup_folder_counts = {}
    transferred_folder_counts = {}

    for i, f in enumerate(source_files):
        if stop_event.is_set(): break
        filename = os.path.basename(f.path)
        bytes_before = source_byte_offsets[i]
        staged_path = ""
        if journal and journal.is_complete(f.path, f.size):
            resumed += 1
            if resumed == 1 or resumed % 100 == 0:
                logger.log(f"RESUME: {resumed} previously completed file(s) verified by size.")
            continue
        if progress_callback:
            progress_callback(i, total_src, f"Checking {i}/{total_src}: {filename[:20]} | Copied {transferred}")
        
        h = None
        if adb_cache and f.is_adb:
            h = adb_cache.get_valid_hash(
                f.path,
                hash_settings.hash_algo,
                hash_settings.hash_mode,
                f.size,
                f.created,
            )
        if not h:
            try:
                single_read_import = (
                    f.is_adb
                    and getattr(settings, "transfer_profile", "Balanced") in ("Balanced", "Fast")
                    and not settings.dry_run
                )
                if single_read_import:
                    stage_key = hashlib.sha256(f.path.encode("utf-8")).hexdigest()
                    stage_dir = os.path.join(output_root, ".duplicate_transfer_manager_staging")
                    staged_path = os.path.join(stage_dir, f"{stage_key}_{filename}")
                    pull_with_retries(
                        f.path,
                        staged_path,
                        serial=getattr(settings, "adb_serial", ""),
                        attempts=getattr(settings, "retry_attempts", 2),
                        logger=logger,
                        progress_callback=progress_callback,
                        expected_size=f.size,
                        overall_bytes_before=bytes_before,
                        overall_bytes_total=total_bytes,
                        reconnect_timeout=getattr(settings, "reconnect_timeout", 300),
                        stall_timeout=getattr(settings, "stall_timeout", 180),
                        stop_event=stop_event,
                    )
                    h = compute_hash(staged_path, hash_settings, stop_event, hash_cache, False, logger)
                    if i == 0 or (i + 1) % 25 == 0:
                        logger.log(
                            f"{getattr(settings, 'transfer_profile', 'Balanced')} import: "
                            f"{i + 1}/{total_src} source files processed through single-read staging."
                        )
                else:
                    h = compute_hash(f.path, hash_settings, stop_event, hash_cache, f.is_adb, logger)
            except ADBOperationError:
                transfer_errors += 1
                adb_device_failed = True
                logger.log(
                    "ERROR: ADB device is unavailable or unauthorized. Transfer stopped. "
                    "Unlock the phone, reconnect USB debugging, and accept the authorization prompt."
                )
                if progress_callback:
                    progress_callback(i + 1, total_src, "ADB authorization lost; transfer stopped")
                break
            except OSError as exc:
                transfer_errors += 1
                failures.append({"source": f.path, "error": str(exc)})
                if journal:
                    journal.fail(f.path, exc)
                logger.log(f"WARNING: Single-read import staging failed: {f.path} ({exc})")
                continue
            if (
                h
                and adb_cache
                and f.is_adb
                and getattr(settings, "update_drive_cache", True)
                and not settings.dry_run
            ):
                adb_cache.set_file_hash(
                    f.path,
                    f.size,
                    f.created,
                    h,
                    hash_settings.hash_algo,
                    hash_settings.hash_mode,
                    root=settings.source_root,
                )
        if not h:
            if staged_path:
                try:
                    os.remove(staged_path)
                except OSError:
                    pass
            transfer_errors += 1
            logger.log(f"WARNING: Skipping file because a comparison hash could not be read: {f.path}")
            failures.append({"source": f.path, "error": "Comparison hash could not be read"})
            if journal:
                journal.fail(f.path, "Comparison hash could not be read")
            continue
        
        if h in dest_hashes:
            if staged_path:
                try:
                    os.remove(staged_path)
                except OSError:
                    pass
            folder = os.path.dirname(f.path)
            dup_folder_counts[folder] = dup_folder_counts.get(folder, 0) + 1
            
            if settings.duplicate_policy == "isolate" and settings.isolate_folder:
                os.makedirs(settings.isolate_folder, exist_ok=True)
                target_path = ensure_unique_path(os.path.join(settings.isolate_folder, filename))
                if not settings.dry_run:
                    try:
                        if f.is_adb:
                            ADBBridge.pull(f.path, target_path, serial=getattr(settings, "adb_serial", ""))
                        else:
                            shutil.copy2(f.path, target_path)
                    except (OSError, shutil.Error, subprocess.CalledProcessError, ADBOperationError) as exc:
                        logger.log(f"WARNING: Failed to isolate duplicate: {f.path} -> {target_path} ({exc})")
                        transfer_errors += 1
                        if isinstance(exc, ADBOperationError) and exc.device_unavailable:
                            adb_device_failed = True
                        continue
                isolated += 1
            else:
                skipped += 1
        else:
            target_path = build_target_path(
                f.path,
                settings.source_root,
                output_root,
                hash_settings.preserve_structure,
                is_adb=f.is_adb,
                avoid_collisions=False,
                destination_template=getattr(settings, "destination_template", "preserve"),
                source_timestamp=f.created,
            )
            target_path = resolve_conflict_path(target_path, getattr(settings, "conflict_policy", "rename"))
            if not target_path:
                skipped += 1
                logger.log(f"SKIPPED existing target due to conflict policy: {f.path}")
                continue
            if not settings.dry_run:
                try:
                    if f.is_adb:
                        if staged_path:
                            os.makedirs(os.path.dirname(target_path), exist_ok=True)
                            os.replace(staged_path, target_path)
                        else:
                            pull_with_retries(
                                f.path,
                                target_path,
                                serial=getattr(settings, "adb_serial", ""),
                                attempts=getattr(settings, "retry_attempts", 3),
                                logger=logger,
                                progress_callback=progress_callback,
                                expected_size=f.size,
                                overall_bytes_before=bytes_before,
                                overall_bytes_total=total_bytes,
                                reconnect_timeout=getattr(settings, "reconnect_timeout", 300),
                                stall_timeout=getattr(settings, "stall_timeout", 180),
                                stop_event=stop_event,
                            )
                            verified_hash = compute_hash(
                                target_path, hash_settings, stop_event, hash_cache, False, logger
                            )
                            if verified_hash != h:
                                failed_path = f"{target_path}.partial"
                                os.replace(target_path, failed_path)
                                raise OSError(f"Post-transfer hash verification failed; retained as {failed_path}")
                    else:
                        partial_path = f"{target_path}.partial"
                        shutil.copy2(f.path, partial_path)
                        os.replace(partial_path, target_path)
                        if os.path.getsize(target_path) != f.size:
                            failed_path = f"{target_path}.partial"
                            os.replace(target_path, failed_path)
                            raise OSError(f"Post-transfer size verification failed; retained as {failed_path}")
                except (OSError, shutil.Error, subprocess.CalledProcessError, ADBOperationError) as exc:
                    logger.log(f"WARNING: Failed to copy file: {f.path} -> {target_path} ({exc})")
                    transfer_errors += 1
                    failures.append({"source": f.path, "target": target_path, "error": str(exc)})
                    if journal:
                        journal.fail(f.path, exc)
                    if isinstance(exc, ADBOperationError) and exc.device_unavailable:
                        adb_device_failed = True
                        logger.log(
                            "ERROR: ADB device is unavailable or unauthorized. Transfer stopped. "
                            "Unlock the phone, reconnect USB debugging, and accept the authorization prompt."
                        )
                        if progress_callback:
                            progress_callback(i + 1, total_src, "ADB authorization lost; transfer stopped")
                        break
                    continue
                if journal:
                    journal.complete(f.path, target_path, f.size, h)
                if h and drive_cache and getattr(settings, "update_drive_cache", True) and not settings.dry_run and os.path.exists(target_path):
                    try:
                        drive_cache.set_file_hash(
                            target_path,
                            os.path.getsize(target_path),
                            os.path.getmtime(target_path),
                            h,
                            compare_scan_settings.hash_algo,
                            compare_scan_settings.hash_mode,
                            root=output_root,
                        )
                    except OSError as exc:
                        logger.log(f"WARNING: Failed to update drive cache for copied file: {target_path} ({exc})")
            if h:
                dest_hashes.add(h)
            transferred += 1
            bytes_transferred += f.size
            source_folder = os.path.dirname(f.path)
            transferred_folder_counts[source_folder] = transferred_folder_counts.get(source_folder, 0) + 1
            
        if progress_callback:
            progress_callback(
                bytes_before + f.size,
                total_bytes,
                f"Processed {i + 1}/{total_src}: {filename[:20]} | "
                f"Copied {transferred} | Overall "
                f"{(bytes_before + f.size) / (1024**3):.2f}/{total_bytes / (1024**3):.2f} GB",
            )

        if adb_device_failed:
            break

    if progress_callback:
        progress_callback(1, 1, "Report generation complete" if not settings.dry_run else "Dry Run Complete")
    
    logger.log("\n--- Dry Run Complete ---" if settings.dry_run else "\n--- Transfer Complete ---")
    transfer_label = "Would Transfer to Destination" if settings.dry_run else "Transferred to Destination"
    duplicate_label = "Total Duplicates Found" if settings.dry_run else "Total Duplicates Found"
    skipped_label = "Would Skip as Duplicates" if settings.dry_run else "Duplicates Skipped"
    isolated_label = "Would Isolate Duplicates" if settings.dry_run else "Duplicates Isolated"
    logger.log(f"{transfer_label}: {transferred}")
    logger.log(f"{duplicate_label}: {isolated + skipped}")
    logger.log(f"{skipped_label}: {skipped}")
    logger.log(f"{isolated_label}: {isolated}")
    logger.log(f"Transfer Errors: {transfer_errors}")
    logger.log(f"Resumed From Journal: {resumed}")
    
    if dup_folder_counts:
        logger.log("\nDuplicate Breakdown by Source Folder:")
        for folder, count in sorted(dup_folder_counts.items(), key=lambda x: x[1], reverse=True):
            logger.log(f"  -> {count} duplicate(s) in: {folder}")
    if transferred_folder_counts:
        logger.log("\nTransferred Breakdown by Source Folder:")
        for folder, count in sorted(transferred_folder_counts.items(), key=lambda x: x[1], reverse=True):
            logger.log(f"  -> {count} transferred file(s) from: {folder}")
    if drive_cache and getattr(settings, "update_drive_cache", True) and not settings.dry_run:
        drive_cache.note_root(compare_root, compare_scan_settings, len(dest_files), 0)
        try:
            drive_cache.save()
            logger.log(f"Drive cache updated: {drive_cache.path}")
        except OSError as exc:
            logger.log(f"WARNING: Failed to save drive cache: {drive_cache.path} ({exc})")
    elif drive_cache:
        reason = "dry run is enabled" if settings.dry_run else "update after transfer is disabled"
        logger.log(f"Drive cache was used read-only; {reason}.")
    if adb_cache and getattr(settings, "update_drive_cache", True) and not settings.dry_run:
        adb_cache.mark_missing_under_root(settings.source_root, [f.path for f in source_files])
        adb_cache.note_root(settings.source_root, hash_settings, len(source_files), 0)
        try:
            adb_cache.save()
            logger.log(f"ADB cache updated: {adb_cache.path}")
        except OSError as exc:
            logger.log(f"WARNING: Failed to save ADB cache: {adb_cache.path} ({exc})")
    elif adb_cache:
        reason = "dry run is enabled" if settings.dry_run else "update after transfer is disabled"
        logger.log(f"ADB cache was used read-only; {reason}.")
    if getattr(settings, "source_is_adb", False) and getattr(settings, "keep_device_awake", True):
        ADBBridge.restore_stay_awake(getattr(settings, "adb_serial", ""), previous_stay_awake)
        logger.log("ADB keep-awake setting restored.")
    if journal:
        try:
            journal.save(force=True)
            logger.log("Transfer resume journal checkpoint completed.")
        except OSError as exc:
            logger.log(f"WARNING: Failed to finalize transfer journal: {exc}")
    staging_dir = os.path.join(output_root, ".duplicate_transfer_manager_staging")
    try:
        os.rmdir(staging_dir)
    except OSError:
        pass
    if sleep_inhibited:
        restore_windows_sleep()
        logger.log("Windows sleep prevention released.")
    result = {
        "transferred": transferred,
        "duplicates": isolated + skipped,
        "isolated": isolated,
        "skipped": skipped,
        "resumed": resumed,
        "errors": transfer_errors,
        "bytes_transferred": bytes_transferred,
        "adb_device_failed": adb_device_failed,
        "dry_run": settings.dry_run,
    }
    if not settings.dry_run:
        try:
            result["report_path"] = write_transfer_report(output_root, result, failures)
            logger.log(f"Transfer report written: {result['report_path']}")
        except OSError as exc:
            logger.log(f"WARNING: Failed to write transfer report: {exc}")
    return result
