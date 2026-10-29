import os
import posixpath
import time
import hashlib
import shutil
import subprocess
from datetime import UTC, datetime
from copy import copy
from concurrent.futures import ThreadPoolExecutor, as_completed
from adb_bridge import ADBBridge, ADBOperationError, describe_device_state
from discovery import discover_files
from drive_cache import ADBHashCache, DriveHashCache, default_adb_cache_path, default_cache_path
from models import FileInfo
from transfer_safety import (
    APP_PARTIAL_SUFFIX,
    APP_STAGING_DIRECTORY,
    TransferJournal,
    default_journal_path,
    files_too_large_for_destination,
    preflight_transfer,
    prevent_windows_sleep,
    pull_with_retries,
    restore_windows_sleep,
    too_large_message,
    write_transfer_report,
)
from utils import ensure_unique_path, is_hidden_or_system, normalize_excludes, normalize_extensions


class HashCancelled(RuntimeError):
    """Raised when a content hash stops before reading the complete file."""


def _hash_cancelled(stop_event):
    return bool(stop_event and stop_event.is_set())


def batch_adb_hashes(items, settings, stop_event, logger=None):
    """Hash device files in grouped commands before per-file work starts.

    Hashing a phone one file at a time spends most of its wall clock spawning
    an adb client rather than reading files. Anything not returned here simply
    falls back to the per-file path, so a device that cannot run the batched
    command behaves exactly as before.
    """

    adb_paths = [info.path for info in items if getattr(info, "is_adb", False)]
    if not adb_paths:
        return {}
    try:
        digests = ADBBridge.remote_hash_many(
            adb_paths,
            settings.hash_algo,
            serial=getattr(settings, "adb_serial", ""),
            stop_event=stop_event,
        )
    except ADBOperationError as exc:
        if logger:
            logger.log(f"WARNING: Grouped device hashing unavailable ({exc}). Falling back to per-file hashing.")
        return {}
    if logger and digests:
        logger.log(
            f"Grouped device hashing: {len(digests)} of {len(adb_paths)} device file(s) "
            "hashed without a per-file adb call."
        )
    return digests

def compute_hash(path, settings, stop_event, hash_cache=None, is_adb=False, logger=None):
    if _hash_cancelled(stop_event):
        raise HashCancelled(path)
    if is_adb:
        try:
            digest = ADBBridge.remote_hash(path, settings.hash_algo, serial=getattr(settings, "adb_serial", ""))
        except ADBOperationError as exc:
            if logger:
                logger.log(f"WARNING: {exc}")
            if exc.device_unavailable:
                raise
            return ""
        if _hash_cancelled(stop_event):
            raise HashCancelled(path)
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
                while True:
                    if _hash_cancelled(stop_event):
                        raise HashCancelled(path)
                    data = f.read(1048576)
                    if not data: break
                    h.update(data)
        if _hash_cancelled(stop_event):
            raise HashCancelled(path)
        digest = h.hexdigest()
        if hash_cache: hash_cache.set(path, settings.hash_algo, settings.hash_mode, size, mtime, digest)
        return digest
    except HashCancelled:
        raise
    except (OSError, ValueError) as exc:
        if logger:
            logger.log(f"WARNING: Failed to hash file: {path} ({exc})")
        return ""

def normalize_settings(settings):
    settings.extensions = normalize_extensions(getattr(settings, "extensions", []))
    settings.exclude_dirs = normalize_excludes(getattr(settings, "exclude_dirs", []))
    return settings


DESTINATION_DISCONNECTED_MESSAGE = (
    "ERROR: The destination drive is no longer available. Transfer stopped. "
    "Nothing on the source was changed and completed files are recorded; "
    "reconnect the drive and run the transfer again to resume."
)


def _destination_gone(output_root):
    """True when the destination root itself has disappeared.

    A failed copy normally means one file. When the root is gone too, the drive
    was unplugged or dropped off the bus, and carrying on would fail every
    remaining file one by one and report each as an error.
    """

    try:
        return not os.path.isdir(output_root)
    except OSError:
        return True


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
    create_dirs=True,
):
    """Compute the destination path for a source file.

    ``create_dirs`` exists so a dry run can answer "where would this go?"
    without answering it by creating the folder. Building the path used to
    ``os.makedirs`` unconditionally, so a run the UI describes as making no
    changes left an empty destination tree behind. It defaults to True to keep
    every existing caller behaving exactly as before.
    """

    filename = os.path.basename(source_path)

    def _prepare(path):
        if create_dirs:
            parent = os.path.dirname(path)
            if parent:
                os.makedirs(parent, exist_ok=True)
        return ensure_unique_path(path) if avoid_collisions else path

    if destination_template == "date":
        try:
            # Use UTC so the folder is stable across machines and daylight-saving changes.
            dated_folder = datetime.fromtimestamp(float(source_timestamp), UTC).strftime("%Y/%m")
        except (OSError, OverflowError, TypeError, ValueError):
            dated_folder = "Unknown date"
        return _prepare(os.path.join(dest_root, *dated_folder.split("/"), filename))
    if preserve_structure and source_root:
        rel_path = build_relative_path(source_path, source_root, is_adb=is_adb)
        if rel_path:
            target_path = os.path.join(dest_root, *rel_path.split("/")) if is_adb else os.path.join(dest_root, rel_path)
            return _prepare(target_path)

    if create_dirs:
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


def transfer_staging_path(output_root, target_path, label="partial"):
    stage_dir = os.path.join(output_root, APP_STAGING_DIRECTORY)
    os.makedirs(stage_dir, exist_ok=True)
    token = hashlib.sha256(os.path.abspath(target_path).encode("utf-8", "replace")).hexdigest()
    filename = os.path.basename(target_path) or "transfer"
    return os.path.join(stage_dir, f"{token}_{label}_{filename}{APP_PARTIAL_SUFFIX}")


def _copy_no_clobber(source, target):
    """Copy an app-owned staged file without ever replacing an existing target."""

    created = False
    try:
        with open(source, "rb") as input_stream, open(target, "xb") as output_stream:
            created = True
            shutil.copyfileobj(input_stream, output_stream, 1024 * 1024)
            output_stream.flush()
            os.fsync(output_stream.fileno())
        shutil.copystat(source, target)
        os.remove(source)
    except Exception:
        if created:
            try:
                os.remove(target)
            except OSError:
                pass
        raise


def _move_no_clobber(source, target):
    """Move a file to a free name without replacing anything already there."""

    if os.name == "nt":
        # This app targets Windows, where rename fails when the destination
        # exists. Unlike os.replace, it therefore closes the check/move race.
        os.rename(source, target)
        return
    # Keep the non-Windows compatibility path fail-closed and atomic to other
    # writers. If links are unavailable, the caller reports the failure rather
    # than risking a replace after an existence check.
    os.link(source, target)
    try:
        os.remove(source)
    except OSError:
        os.remove(target)
        raise


def promote_transfer_file(staged_path, target_path, policy, output_root):
    """Promote staged content while preserving the reviewed conflict policy."""

    policy = policy if policy in {"rename", "skip", "replace"} else "rename"
    staged_path = os.path.abspath(staged_path)
    target_path = os.path.abspath(target_path)
    if policy == "replace":
        backup = ""
        if os.path.exists(target_path):
            backup_root = os.path.join(output_root, ".duplicate_transfer_manager_backups")
            os.makedirs(backup_root, exist_ok=True)
            backup_base = os.path.join(backup_root, os.path.basename(target_path))
            while True:
                backup = ensure_unique_path(backup_base)
                try:
                    _move_no_clobber(target_path, backup)
                    break
                except FileExistsError:
                    # The name became occupied after ensure_unique_path looked.
                    # Keep that file and choose another backup name.
                    continue
        try:
            candidate = target_path
            while True:
                try:
                    _move_no_clobber(staged_path, candidate)
                    target_path = candidate
                    break
                except FileExistsError:
                    # Something arrived after the reviewed incumbent was moved.
                    # It is not the file the user approved replacing, so retain
                    # it and publish the incoming file under a unique name.
                    candidate = ensure_unique_path(candidate)
        except Exception:
            restore_replaced_original(target_path, backup)
            raise
        return target_path, backup

    candidate = target_path
    while True:
        try:
            try:
                os.link(staged_path, candidate)
                os.remove(staged_path)
            except (AttributeError, NotImplementedError):
                _copy_no_clobber(staged_path, candidate)
            return candidate, ""
        except FileExistsError:
            if policy == "skip":
                try:
                    os.remove(staged_path)
                except OSError:
                    pass
                return "", ""
            candidate = ensure_unique_path(candidate)
        except OSError as exc:
            # Some filesystems do not support hard links. Exclusive creation is
            # still no-clobber, though promotion is no longer atomic to readers.
            if getattr(exc, "winerror", None) not in {1, 17, 50} and exc.errno not in {
                getattr(os, "EXDEV", 18), 1, 13, 17, 18, 95,
            }:
                raise
            try:
                _copy_no_clobber(staged_path, candidate)
                return candidate, ""
            except FileExistsError:
                if policy == "skip":
                    try:
                        os.remove(staged_path)
                    except OSError:
                        pass
                    return "", ""
                candidate = ensure_unique_path(candidate)

def _is_separate_output_tree(output_root, compare_root):
    """Report whether the output folder needs a comparison scan of its own.

    False when the two are the same folder, or when the output folder already
    sits inside the compare folder and was therefore covered by that scan.
    """

    if not output_root or not compare_root:
        return False
    output_abs = os.path.abspath(output_root)
    compare_abs = os.path.abspath(compare_root)
    if output_abs == compare_abs:
        return False
    common = _safe_commonpath([output_abs, compare_abs])
    return common != compare_abs


def restore_replaced_original(target_path, replaced_backup, logger=None):
    """Put a displaced original back after a replace-policy transfer failed.

    ``promote_transfer_file`` moves the existing destination file into the
    backups folder before writing over its path. If anything afterwards fails,
    that original is the user's only copy and its own path is now empty, so it
    has to go back. Returns the backup path that is still outstanding: empty
    once the original has been restored, unchanged when there was nothing to
    restore or the target path is occupied again.
    """

    if not replaced_backup or not target_path:
        return replaced_backup
    if not os.path.exists(replaced_backup):
        return replaced_backup
    parent = os.path.dirname(target_path)
    try:
        if parent:
            os.makedirs(parent, exist_ok=True)
    except OSError as exc:
        return _report_failed_restore(target_path, replaced_backup, exc, logger)

    # Never ``os.replace``. Checking that the target is free and then replacing
    # it is two steps, and anything that lands in between -- a retry, a second
    # window, the user putting a file back -- is destroyed by the second step.
    # A hard link is created only if the name is still free, and the exclusive
    # -creation copy used where links are unavailable has the same guarantee.
    try:
        try:
            os.link(replaced_backup, target_path)
        except (AttributeError, NotImplementedError):
            _copy_no_clobber(replaced_backup, target_path)
        else:
            os.remove(replaced_backup)
    except FileExistsError:
        # Something occupies the name again. The original stays in the backup
        # folder rather than being forced over whatever is now there.
        return _report_failed_restore(
            target_path,
            replaced_backup,
            "the destination name was taken again",
            logger,
        )
    except OSError as exc:
        try:
            _copy_no_clobber(replaced_backup, target_path)
        except FileExistsError:
            return _report_failed_restore(
                target_path, replaced_backup, "the destination name was taken again", logger
            )
        except OSError as copy_exc:
            return _report_failed_restore(target_path, replaced_backup, copy_exc, logger)
        else:
            del exc
    if logger:
        logger.log(f"Restored the original file at {target_path} after a failed replacement.")
    return ""


def _recovery_record(target_path, quarantined_path, unrestored_backup):
    """Describe where a failed transfer left files, for the journal and report.

    Only the paths a person would need to act on. An empty record means nothing
    was left behind and the destination is as it was.
    """

    record = {}
    if quarantined_path:
        record["quarantined_path"] = os.path.abspath(quarantined_path)
    if unrestored_backup:
        record["unrestored_original_backup"] = os.path.abspath(unrestored_backup)
        if target_path:
            record["unrestored_original_target"] = os.path.abspath(target_path)
    return record


def _report_failed_restore(target_path, replaced_backup, reason, logger=None):
    """Say loudly where an original was left when it could not be put back."""

    if logger:
        logger.log(
            f"ERROR: Could not restore the original file to {target_path} ({reason}). "
            f"The original is preserved at {replaced_backup} and is recorded in the "
            "transfer report and resume journal."
        )
    return replaced_backup


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
            return f"The selected phone is not ready ({device_state}). {describe_device_state(device_state)}"
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

        # The checks above compare the paths as typed. A junction, symlink or
        # subst drive can make two different-looking paths one folder, so the
        # same rules are applied again to where each path actually leads.
        source_real = os.path.realpath(source_abs)
        compare_real = os.path.realpath(compare_abs)
        copy_real = os.path.realpath(copy_abs)
        if source_real in (compare_real, copy_real):
            return "Source and destination resolve to the same folder through a link or mapped drive."
        if _safe_commonpath([source_real, compare_real]) == source_real:
            return "Compare folder cannot be inside the source folder."
        if _safe_commonpath([source_real, copy_real]) == source_real:
            return "Output folder cannot be inside the source folder."
        # The reverse nesting was allowed and gave a false answer: the
        # comparison scan of the library also reads the source inside it, every
        # source file matches itself, and new photos were reported as already in
        # the library while nothing was imported.
        if _safe_commonpath([source_real, compare_real]) == compare_real:
            return (
                "Source folder cannot be inside the compare folder: its files would be "
                "compared against themselves and reported as already in the library. "
                "Move the source folder out, or compare against a folder that does not contain it."
            )
        if _safe_commonpath([source_real, copy_real]) == copy_real:
            return (
                "Source folder cannot be inside the output folder: imported files would land "
                "beside the originals and be picked up again by the next import."
            )
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
            return f"The selected phone is not ready ({device_state}). {describe_device_state(device_state)}"
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

def iter_files(root, settings, stop_event, progress_callback=None, logger=None, on_result=None):
    """Yield discovered files, handing the full result to ``on_result`` first.

    The discovery result carries errors and an ``incomplete`` flag. Dropping
    those on the floor is how a partially listed phone becomes an import that
    reports success while leaving files behind, so callers are given the chance
    to record them.
    """

    settings = normalize_settings(settings)
    discovery = discover_files(root, settings, stop_event, progress_callback=progress_callback, logger=logger)
    if on_result is not None:
        on_result(discovery)
    elif discovery.incomplete and logger:
        logger.log(
            f"WARNING: The listing of {root} was incomplete; "
            f"{len(discovery.errors)} problem(s) were reported and some files may be missing."
        )
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
        # The phone-side digest is the only identity available before writing.
        # Do not allow legacy MD5/SHA-1 choices to turn a collision into a
        # silently skipped source file.
        hash_settings.hash_algo = "sha256"
        hash_settings.hash_mode = "full"
    return hash_settings


def confirm_duplicate_by_content(source_path, dest_path, settings, stop_event, hash_cache=None, logger=None):
    """Prove two files are identical before treating the source as a duplicate.

    A sampled digest covers only the size plus the first and last megabyte, so
    two different large files can share one. Skipping an import on that basis
    silently leaves a file behind, which is the one outcome an import must
    never produce. Duplicate grouping already confirms its candidates the same
    way; this is the transfer path's equivalent.
    """

    if not dest_path or not os.path.exists(dest_path):
        return False
    full_settings = copy(settings)
    full_settings.hash_algo = "sha256"
    full_settings.hash_mode = "full"
    source_digest = compute_hash(source_path, full_settings, stop_event, hash_cache, False, logger)
    if not source_digest:
        return False
    dest_digest = compute_hash(dest_path, full_settings, stop_event, hash_cache, False, logger)
    return bool(dest_digest) and source_digest == dest_digest

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

    batched = batch_adb_hashes(items_to_hash, settings, stop_event, logger)
    for info in items_to_hash:
        digest = batched.get(info.path)
        if digest:
            hash_groups.setdefault(digest, []).append(info)
    processed = len(batched)
    if processed and progress_callback:
        progress_callback(processed, total_to_hash, "Hashing potential duplicates...")

    with ThreadPoolExecutor(max_workers=settings.max_hash_workers) as pool:
        futures = {pool.submit(compute_hash, info.path, settings, stop_event, hash_cache, info.is_adb, logger): info
                   for info in items_to_hash if info.path not in batched}

        for future in as_completed(futures):
            if stop_event.is_set(): break
            processed += 1
            if progress_callback: progress_callback(processed, total_to_hash, "Hashing potential duplicates...")
            try:
                digest = future.result()
                if digest: hash_groups.setdefault(digest, []).append(futures[future])
            except HashCancelled:
                break
            except Exception:
                continue

    candidates = [group for group in hash_groups.values() if len(group) > 1]
    needs_sha256_confirmation = (
        getattr(settings, "hash_mode", "full") == "fast"
        or str(getattr(settings, "hash_algo", "sha256")).lower() != "sha256"
    )
    if not needs_sha256_confirmation or not candidates or stop_event.is_set():
        return candidates

    # A sampled digest is only a candidate filter. Destructive duplicate review
    # must be based on a complete SHA-256 read of every candidate file.
    full_settings = copy(settings)
    full_settings.hash_algo = "sha256"
    full_settings.hash_mode = "full"
    full_groups = {}
    candidates_to_hash = [info for group in candidates for info in group]
    total_candidates = len(candidates_to_hash)
    if progress_callback:
        progress_callback(0, total_candidates, "Confirming duplicate candidates with full-content SHA-256...")
    confirmed_batch = batch_adb_hashes(candidates_to_hash, full_settings, stop_event, logger)
    for info in candidates_to_hash:
        digest = confirmed_batch.get(info.path)
        if digest:
            full_groups.setdefault(digest, []).append(info)
    with ThreadPoolExecutor(max_workers=settings.max_hash_workers) as pool:
        futures = {
            pool.submit(
                compute_hash,
                info.path,
                full_settings,
                stop_event,
                hash_cache,
                info.is_adb,
                logger,
            ): info
            for info in candidates_to_hash
            if info.path not in confirmed_batch
        }
        confirmed = len(confirmed_batch)
        for future in as_completed(futures):
            if stop_event.is_set():
                break
            try:
                digest = future.result()
            except HashCancelled:
                break
            except Exception:
                continue
            confirmed += 1
            if digest:
                full_groups.setdefault(digest, []).append(futures[future])
            if progress_callback:
                progress_callback(
                    confirmed,
                    total_candidates,
                    "Confirming duplicate candidates with full-content SHA-256...",
                )
    return [group for group in full_groups.values() if len(group) > 1]

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
    # Used to prove a copy landed intact when the comparison digest is only a
    # sample. Duplicate grouping already keeps a full-content settings object
    # for the same reason; this is the transfer path's.
    full_verify_settings = copy(hash_settings)
    full_verify_settings.hash_algo = "sha256"
    full_verify_settings.hash_mode = "full"
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
    source_discovery = {}
    source_progress = _transfer_discovery_progress("Source scan", start_time, progress_callback)
    for count, f in enumerate(
        iter_files(
            settings.source_root,
            source_scan_settings,
            stop_event,
            source_progress,
            logger,
            on_result=lambda result: source_discovery.update(
                {
                    "incomplete": result.incomplete,
                    "errors": list(result.errors),
                    "unreadable": list(result.unreadable),
                }
            ),
        ),
        1,
    ):
        source_files.append(f)
        if count % 25 == 0 and progress_callback:
            elapsed = int(time.time() - start_time)
            progress_callback(0, 0, f"Scanning Source: {count} files found ({elapsed}s)")

    if progress_callback:
        progress_callback(0, 0, f"Scanning Source: {len(source_files)} files found")

    if source_discovery.get("incomplete"):
        # Say so loudly. An import that copies everything it saw is still an
        # incomplete import if the source listing itself was truncated, and the
        # user has no other way to discover that.
        logger.log(
            f"WARNING: The source listing was incomplete. {len(source_discovery.get('errors', []))} "
            "problem(s) were reported, so some files may not have been offered for import. "
            "Re-run the import after resolving them."
        )
        for unreadable_path in source_discovery.get("unreadable", [])[:20]:
            logger.log(f"WARNING: Source file could not be read: {unreadable_path}")

    # Built before the preflight rather than after it: the free-space check has
    # to know which sources the journal already completed, or a resume is
    # hard-blocked precisely when the destination is nearly full.
    # Loaded for a dry run too. Reading the journal writes nothing, and skipping
    # it made the preview disagree with the live run it exists to predict:
    # already-completed files were reported as "would transfer" and their bytes
    # were charged against the free-space estimate. ``write_journal`` is the
    # handle used for every mutation, so a dry run stays strictly read-only.
    journal_path = getattr(settings, "journal_path", "").strip() or default_journal_path(
        output_root, getattr(settings, "adb_serial", "") or "local"
    )
    journal = TransferJournal(journal_path)
    write_journal = None if settings.dry_run else journal
    logger.log(
        f"Resume journal: {journal_path}"
        f"{' (read-only for this dry run)' if settings.dry_run else ''}"
    )
    verify_resumed_files = bool(getattr(settings, "verify_resumed_files", False))

    preflight_errors, preflight_warnings = preflight_transfer(
        output_root,
        source_files,
        is_adb=getattr(settings, "source_is_adb", False),
        serial=getattr(settings, "adb_serial", ""),
        verify_write=not settings.dry_run,
        journal=journal,
        verify_resumed=verify_resumed_files,
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

    # Every counter the summary and the teardown need, initialised before the
    # try so an early failure still produces a result instead of a traceback.
    transferred = isolated = skipped = transfer_errors = 0
    conflict_skipped = bytes_transferred = resumed = too_large_skipped = 0
    failures = []
    adb_device_failed = False
    destination_disconnected = False
    dest_files = []
    previous_stay_awake = None
    dup_folder_counts = {}
    transferred_folder_counts = {}

    try:
        if adb_cache:
            cached_source_count = adb_cache.active_file_count_under_root(
                settings.source_root,
                algo=hash_settings.hash_algo,
                mode=hash_settings.hash_mode,
            )
            # Reported, never used as a decision. Each phone file is looked up
            # individually and its cached hash is accepted only while size and
            # timestamp still match; a matching total says nothing about whether
            # the same files are present.
            logger.log(
                f"ADB cache holds {cached_source_count} entry(s) for this folder; "
                f"{len(source_files)} file(s) are present. Each is validated individually."
            )
    
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

        # The output folder is part of the comparison set whenever it is a tree
        # of its own. Only the compare folder used to be scanned, so files this
        # app had already copied into a separate "Save to" folder were invisible:
        # any run whose journal was missing -- a hard kill loses up to 25
        # completions to the journal's write throttle, and the journal name
        # itself depends on the device serial -- re-imported every one of them
        # under a " (1)" suffix. Held apart from ``dest_files`` so the compare
        # folder's cache bookkeeping stays about the compare folder.
        output_files = []
        if _is_separate_output_tree(output_root, compare_root):
            output_time = time.time()
            logger.log(f"Scanning Output Folder: {output_root}")
            output_progress = _transfer_discovery_progress("Output scan", output_time, progress_callback)
            for count, f in enumerate(
                iter_files(output_root, compare_scan_settings, stop_event, output_progress, logger), 1
            ):
                output_files.append(f)
                if count % 25 == 0 and progress_callback:
                    elapsed = int(time.time() - output_time)
                    progress_callback(0, 0, f"Scanning Output: {count} files found ({elapsed}s)")
            if progress_callback:
                progress_callback(0, 0, f"Scanning Output: {len(output_files)} files found")
            logger.log(
                f"Output folder differs from the compare folder; {len(output_files)} "
                "already-imported file(s) join the duplicate comparison set."
            )

        dest_hashes = set()
        # One representative destination per digest. A sampled "fast" digest is
        # only a shortlist, so skipping a source as an existing duplicate has to be
        # confirmed against a real file rather than against the digest alone.
        dest_hash_paths = {}
        # Each entry carries the root it belongs to so the drive cache records
        # output-folder files under the output root rather than the compare root.
        comparison_entries = [(f, compare_root) for f in dest_files]
        comparison_entries.extend((f, output_root) for f in output_files)
        total_dest = len(comparison_entries)
        # There is deliberately no count-based shortcut here. Comparing the number
        # of cached entries with the number of files on disk says nothing about
        # whether they are the same files: deleting one file and adding another
        # leaves the count identical, and the surviving stale hash then made a
        # source file look like an existing copy, so it was skipped and never
        # imported. The per-file lookup below already returns a cached hash without
        # re-reading the file whenever size and timestamp still match, so the
        # shortcut bought nothing and only removed the validation.
        cache_hits = 0
        if drive_cache:
            logger.log(f"Validating {total_dest} compare file(s) against cached size and timestamp.")
        else:
            logger.log("Hashing compare-folder files to build duplicate comparison data.")
        for i, (f, entry_root) in enumerate(comparison_entries):
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
                if cached:
                    cache_hits += 1
            try:
                h = cached or compute_hash(
                    f.path,
                    compare_scan_settings,
                    stop_event,
                    hash_cache,
                    is_adb=False,
                    logger=logger,
                )
            except HashCancelled:
                break
            if h and drive_cache and getattr(settings, "update_drive_cache", True) and not settings.dry_run:
                drive_cache.set_file_hash(
                    f.path,
                    f.size,
                    f.created,
                    h,
                    compare_scan_settings.hash_algo,
                    compare_scan_settings.hash_mode,
                    root=entry_root,
                )
            if h:
                dest_hashes.add(h)
                dest_hash_paths.setdefault(h, f.path)
            if progress_callback: progress_callback(i + 1, total_dest, f"Building destination cache... {i + 1}/{total_dest}")
        if drive_cache and getattr(settings, "update_drive_cache", True) and not settings.dry_run:
            drive_cache.mark_missing_under_root(compare_root, [f.path for f in dest_files])
            if output_files:
                drive_cache.mark_missing_under_root(output_root, [f.path for f in output_files])
        if drive_cache:
            logger.log(f"Drive cache served {cache_hits} of {total_dest} comparison hash(es); the rest were read.")

        logger.log("Processing Source Files. Please wait...")
        logger.log(f"Copy Target Folder: {output_root}")
        logger.log(f"Source files discovered: {len(source_files)}")
        logger.log(f"Compare-folder files discovered: {len(dest_files)}")
        logger.log(
            "Structure-aware sync is enabled. Files are compared recursively across all subfolders "
            "and copied using their relative source paths."
        )
        transferred, isolated, skipped, transfer_errors = 0, 0, 0, 0
        # Counted apart from duplicates: these files are unique and were simply not
        # copied because the destination name was already taken.
        conflict_skipped = 0
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
        batched_source_hashes = {}
        uses_staged_import = (
            getattr(settings, "source_is_adb", False)
            and getattr(settings, "transfer_profile", "Balanced") in ("Balanced", "Fast")
            and not settings.dry_run
        )
        if getattr(settings, "source_is_adb", False) and not uses_staged_import:
            # Staged imports hash the local copy after pulling, so batching would
            # add an entire extra device-side read. Only the profiles that hash on
            # the phone benefit from grouping those calls.
            pending_device_hashes = [
                source_info
                for source_info in source_files
                if not (
                    adb_cache
                    and adb_cache.get_valid_hash(
                        source_info.path,
                        hash_settings.hash_algo,
                        hash_settings.hash_mode,
                        source_info.size,
                        source_info.created,
                    )
                )
            ]
            batched_source_hashes = batch_adb_hashes(pending_device_hashes, hash_settings, stop_event, logger)
        previous_stay_awake = None
        if getattr(settings, "source_is_adb", False) and getattr(settings, "keep_device_awake", True):
            previous_stay_awake = ADBBridge.enable_usb_stay_awake(getattr(settings, "adb_serial", ""))
            logger.log("ADB USB keep-awake enabled for this transfer.")
    
        dup_folder_counts = {}
        transferred_folder_counts = {}
        too_large, destination_filesystem = files_too_large_for_destination(output_root, source_files)
        too_large_paths = {info.path for info in too_large}

        for i, f in enumerate(source_files):
            if stop_event.is_set(): break
            filename = os.path.basename(f.path)
            bytes_before = source_byte_offsets[i]
            staged_path = ""
            partial_path = ""
            published_unverified = False
            if journal and journal.is_complete(f.path, f.size, verify_resumed_files):
                resumed += 1
                if resumed == 1 or resumed % 100 == 0:
                    logger.log(
                        f"RESUME: {resumed} previously completed file(s) verified by "
                        f"{'full content' if verify_resumed_files else 'size and timestamp'}."
                    )
                continue
            if f.path in too_large_paths:
                # Checked before anything is staged or hashed: an Android import
                # stages each file on the destination first, so even staging a
                # 4 GB video onto FAT32 fails after writing most of it.
                message = too_large_message(destination_filesystem)
                too_large_skipped += 1
                failures.append({"source": f.path, "error": message, "reason": "too_large_for_filesystem"})
                logger.log(f"SKIPPED {f.path}: {message}")
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
            if not h and f.is_adb:
                h = batched_source_hashes.get(f.path)
            if not h:
                try:
                    single_read_import = (
                        f.is_adb
                        and getattr(settings, "transfer_profile", "Balanced") in ("Balanced", "Fast")
                        and not settings.dry_run
                    )
                    if single_read_import:
                        stage_key = hashlib.sha256(f.path.encode("utf-8")).hexdigest()
                        stage_dir = os.path.join(output_root, APP_STAGING_DIRECTORY)
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
                            partial_path=f"{staged_path}{APP_PARTIAL_SUFFIX}",
                        )
                        h = compute_hash(staged_path, hash_settings, stop_event, hash_cache, False, logger)
                        if i == 0 or (i + 1) % 25 == 0:
                            logger.log(
                                f"{getattr(settings, 'transfer_profile', 'Balanced')} import: "
                                f"{i + 1}/{total_src} source files processed through single-read staging."
                            )
                    else:
                        h = compute_hash(f.path, hash_settings, stop_event, hash_cache, f.is_adb, logger)
                except HashCancelled:
                    if write_journal:
                        write_journal.fail(
                            f.path,
                            "Hashing cancelled",
                            partial_path or (f"{staged_path}{APP_PARTIAL_SUFFIX}" if staged_path else ""),
                        )
                    break
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
                    if write_journal:
                        write_journal.fail(f.path, exc, partial_path or (f"{staged_path}{APP_PARTIAL_SUFFIX}" if staged_path else ""))
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
                if write_journal:
                    write_journal.fail(f.path, "Comparison hash could not be read")
                continue
        
            is_duplicate = h in dest_hashes
            if (
                is_duplicate
                and not f.is_adb
                and (
                    hash_settings.hash_mode == "fast"
                    or str(hash_settings.hash_algo).lower() != "sha256"
                )
            ):
                # A sampled or legacy-digest match is a shortlist entry, not
                # proof. Confirm with full SHA-256 before skipping the import.
                if not confirm_duplicate_by_content(
                    f.path, dest_hash_paths.get(h, ""), hash_settings, stop_event, hash_cache, logger
                ):
                    is_duplicate = False
                    logger.log(
                        f"Sampled hash matched but full contents differ; importing anyway: {f.path}"
                    )
            if is_duplicate:
                if staged_path:
                    try:
                        os.remove(staged_path)
                    except OSError:
                        pass
                folder = os.path.dirname(f.path)
                dup_folder_counts[folder] = dup_folder_counts.get(folder, 0) + 1
            
                if settings.duplicate_policy == "isolate" and settings.isolate_folder:
                    # Creating the isolate folder is a change to the user's disk,
                    # so a dry run only computes where the duplicate would land.
                    if not settings.dry_run:
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
                    create_dirs=not settings.dry_run,
                )
                conflict_policy = getattr(settings, "conflict_policy", "rename")
                target_path = resolve_conflict_path(target_path, conflict_policy)
                if not target_path:
                    # A staged import has already pulled the whole file to the
                    # staging directory by this point. Every other exit removes it;
                    # this one used to leak a full copy per skipped file, which then
                    # made the os.rmdir of the staging directory fail silently.
                    if staged_path:
                        try:
                            os.remove(staged_path)
                        except OSError as exc:
                            logger.log(f"WARNING: Could not remove staged copy {staged_path}: {exc}")
                    conflict_skipped += 1
                    logger.log(f"SKIPPED existing target due to conflict policy: {f.path}")
                    continue
                if not settings.dry_run:
                    replaced_backup = ""
                    # Every copy is verified against a complete read of its
                    # source, whatever the profile. Under "Fast" the comparison
                    # digest ``h`` covers only the size and the first and last
                    # megabyte, so a corrupted middle -- the usual shape of a bad
                    # sector or a flaky USB enclosure -- verified clean and was
                    # published as a good import. ``h`` is reused only when it is
                    # already a full digest, so the careful profiles read nothing
                    # extra. ADB sources are always full (see
                    # ``build_transfer_hash_settings``), so the extra read here is
                    # of a local file.
                    verify_settings = (
                        hash_settings
                        if hash_settings.hash_mode == "full"
                        and str(hash_settings.hash_algo).lower() == "sha256"
                        else full_verify_settings
                    )
                    try:
                        expected_digest = (
                            h
                            if verify_settings is hash_settings
                            else compute_hash(
                                f.path, full_verify_settings, stop_event, hash_cache, f.is_adb, logger
                            )
                        )
                        if not expected_digest:
                            raise OSError(
                                "Could not read the source completely to verify the copy; "
                                "nothing was written."
                            )
                        if f.is_adb:
                            if staged_path:
                                os.makedirs(os.path.dirname(target_path), exist_ok=True)
                                candidate_path = staged_path
                            else:
                                partial_path = transfer_staging_path(output_root, target_path, "adb")
                                candidate_path = pull_with_retries(
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
                                    partial_path=partial_path,
                                    promote=False,
                                )
                        else:
                            partial_path = transfer_staging_path(output_root, target_path, "local")
                            shutil.copy2(f.path, partial_path)
                            candidate_path = partial_path

                        # Verify while the bytes still carry an app-owned staging
                        # name. Publishing first left a crash window in which an
                        # unverified or corrupt copy was visible at its final
                        # name and a replace-policy original had already been
                        # displaced. The staging file is on the destination
                        # volume, so this is a read-back of the bytes the drive
                        # actually accepted.
                        partial_path = candidate_path
                        staged_size = os.path.getsize(candidate_path)
                        staged_hash = (
                            compute_hash(candidate_path, verify_settings, stop_event, None, False, logger)
                            if staged_size == f.size
                            else ""
                        )
                        if staged_size != f.size or staged_hash != expected_digest:
                            reason = "size" if staged_size != f.size else "content hash"
                            failed_path = transfer_staging_path(output_root, target_path, "failed")
                            os.replace(candidate_path, failed_path)
                            partial_path = failed_path
                            raise OSError(
                                f"Pre-publication {reason} verification failed; retained as {failed_path}"
                            )

                        target_path, replaced_backup = promote_transfer_file(
                            candidate_path, target_path, conflict_policy, output_root
                        )
                        partial_path = ""
                        if not target_path:
                            conflict_skipped += 1
                            logger.log(f"SKIPPED target created during transfer due to conflict policy: {f.path}")
                            continue
                        # From here until verification passes, the bytes at
                        # ``target_path`` are this run's unproven work. Saying so
                        # explicitly is what lets a failure below tell that file
                        # apart from a restored original or a pre-existing one,
                        # and quarantine only what it actually owns.
                        published_unverified = True
                        # One verification for every route into the destination:
                        # local copy, staged pull, and direct pull alike. Size
                        # alone cannot detect corruption -- a bad sector or a
                        # flaky enclosure preserves the byte count -- so the
                        # bytes that landed are read back in full and must equal
                        # the source. The cache is deliberately not consulted:
                        # promotion preserves the source timestamp, so a cached
                        # digest keyed on (path, size, mtime) can answer for a
                        # file that was just overwritten. Verification has to
                        # read the disk or it is not verification.
                        copied_size = os.path.getsize(target_path)
                        verified_hash = (
                            compute_hash(target_path, verify_settings, stop_event, None, False, logger)
                            if copied_size == f.size
                            else ""
                        )
                        if copied_size != f.size or verified_hash != expected_digest:
                            reason = "size" if copied_size != f.size else "content hash"
                            failed_path = transfer_staging_path(output_root, target_path, "failed")
                            os.replace(target_path, failed_path)
                            partial_path = failed_path
                            published_unverified = False
                            replaced_backup = restore_replaced_original(
                                target_path, replaced_backup, logger
                            )
                            raise OSError(
                                f"Post-transfer {reason} verification failed; retained as {failed_path}"
                            )
                        # Proven good. The file is the user's now, not this
                        # iteration's work in progress.
                        published_unverified = False
                    except HashCancelled:
                        if published_unverified and target_path and os.path.exists(target_path):
                            partial_path = transfer_staging_path(output_root, target_path, "cancelled")
                            os.replace(target_path, partial_path)
                            published_unverified = False
                        replaced_backup = restore_replaced_original(
                            target_path, replaced_backup, logger
                        )
                        if write_journal:
                            write_journal.fail(
                                f.path,
                                "Verification cancelled",
                                partial_path,
                                recovery=_recovery_record(target_path, partial_path, replaced_backup),
                            )
                        break
                    except (OSError, shutil.Error, subprocess.CalledProcessError, ADBOperationError) as exc:
                        # Cancellation restored the displaced original; an
                        # ordinary failure did not, so a replace-policy import
                        # that failed verification left the user's library file
                        # missing from its own path with nothing recording where
                        # it had gone. Both paths now roll back the same way.
                        #
                        # Unverified bytes are moved aside first. Leaving them in
                        # place kept the destination name occupied, which then
                        # blocked the original from coming back and left the user
                        # with an unproven copy standing in for a file they had.
                        if published_unverified and target_path and os.path.exists(target_path):
                            try:
                                quarantined = transfer_staging_path(output_root, target_path, "failed")
                                os.replace(target_path, quarantined)
                                partial_path = quarantined
                            except OSError as quarantine_exc:
                                logger.log(
                                    f"WARNING: Could not move the unverified copy out of "
                                    f"{target_path} ({quarantine_exc})."
                                )
                            else:
                                published_unverified = False
                        replaced_backup = restore_replaced_original(
                            target_path, replaced_backup, logger
                        )
                        recovery = _recovery_record(target_path, partial_path, replaced_backup)
                        logger.log(f"WARNING: Failed to copy file: {f.path} -> {target_path} ({exc})")
                        transfer_errors += 1
                        failure_record = {"source": f.path, "target": target_path, "error": str(exc)}
                        failure_record.update(recovery)
                        failures.append(failure_record)
                        if write_journal:
                            write_journal.fail(f.path, exc, partial_path, recovery=recovery)
                        if isinstance(exc, ADBOperationError) and exc.device_unavailable:
                            adb_device_failed = True
                            logger.log(
                                "ERROR: ADB device is unavailable or unauthorized. Transfer stopped. "
                                "Unlock the phone, reconnect USB debugging, and accept the authorization prompt."
                            )
                            if progress_callback:
                                progress_callback(i + 1, total_src, "ADB authorization lost; transfer stopped")
                            break
                        if _destination_gone(output_root):
                            destination_disconnected = True
                            logger.log(DESTINATION_DISCONNECTED_MESSAGE)
                            if progress_callback:
                                progress_callback(i + 1, total_src, "Destination disconnected; transfer stopped")
                            break
                        continue
                    if write_journal:
                        write_journal.complete(
                            f.path,
                            target_path,
                            f.size,
                            h,
                            replaced_backup,
                            hash_settings.hash_algo,
                            hash_settings.hash_mode,
                        )
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
                    # Newly copied files join the comparison set, so a later source
                    # matching one of them can be confirmed against a real path too.
                    # A dry run copies nothing, so ``target_path`` does not exist
                    # and confirming against it always failed -- which reported
                    # two identical sources as two transfers where the live run
                    # copies one and skips one. The source file is byte-identical
                    # to what would have been written, so it stands in for it.
                    dest_hash_paths.setdefault(h, f.path if settings.dry_run else target_path)
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

            if adb_device_failed or destination_disconnected:
                break
    except OSError as exc:
        # Destination directory creation, the isolate folder, and the staging
        # tree all raise outside the per-file try. Letting that escape left the
        # phone awake, discarded every hash computed so far, saved neither
        # cache, and wrote no report -- the run vanished with nothing to resume.
        transfer_errors += 1
        failures.append({"path": "", "error": f"Transfer stopped: {exc}"})
        logger.log(f"ERROR: Transfer stopped before all files were processed: {exc}")
        if not settings.dry_run and _destination_gone(output_root):
            destination_disconnected = True
            logger.log(DESTINATION_DISCONNECTED_MESSAGE)
    finally:
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
        if write_journal:
            try:
                write_journal.save(force=True)
                logger.log("Transfer resume journal checkpoint completed.")
            except OSError as exc:
                logger.log(f"WARNING: Failed to finalize transfer journal: {exc}")
        staging_dir = os.path.join(output_root, APP_STAGING_DIRECTORY)
        try:
            os.rmdir(staging_dir)
        except FileNotFoundError:
            pass
        except OSError as exc:
            # Say what was left behind. Swallowing this hid staged copies that
            # earlier exits had leaked, so the directory grew run after run with
            # nothing reporting it.
            try:
                leftovers = os.listdir(staging_dir)
            except OSError:
                leftovers = []
            if leftovers:
                logger.log(
                    f"WARNING: {len(leftovers)} staged file(s) remain in {staging_dir}; "
                    "they can be deleted once no transfer is running."
                )
            else:
                logger.log(f"WARNING: Could not remove staging folder {staging_dir}: {exc}")
        if sleep_inhibited:
            restore_windows_sleep()
            logger.log("Windows sleep prevention released.")


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
    if too_large_skipped:
        logger.log(f"Too Large For Destination Filesystem: {too_large_skipped}")
    if destination_disconnected:
        logger.log("Destination Disconnected: yes - reconnect the drive and run again to resume")
    
    if dup_folder_counts:
        logger.log("\nDuplicate Breakdown by Source Folder:")
        for folder, count in sorted(dup_folder_counts.items(), key=lambda x: x[1], reverse=True):
            logger.log(f"  -> {count} duplicate(s) in: {folder}")
    if transferred_folder_counts:
        logger.log("\nTransferred Breakdown by Source Folder:")
        for folder, count in sorted(transferred_folder_counts.items(), key=lambda x: x[1], reverse=True):
            logger.log(f"  -> {count} transferred file(s) from: {folder}")
    result = {
        "transferred": transferred,
        "duplicates": isolated + skipped,
        "isolated": isolated,
        "skipped": skipped + conflict_skipped,
        # A unique file that was not copied because its target name was taken
        # is not a duplicate. Counting it as one told the user the file was
        # already in their library when it had never been imported.
        "conflict_skipped": conflict_skipped,
        "resumed": resumed,
        "errors": transfer_errors,
        "bytes_transferred": bytes_transferred,
        "adb_device_failed": adb_device_failed,
        # Files the destination filesystem cannot hold (FAT32's 4 GiB limit).
        # They are listed in the report's failures with their reason.
        "too_large_skipped": too_large_skipped,
        # The destination vanished mid-run. Distinct from file errors: nothing
        # failed on the user's side, the journal is intact, and running the
        # transfer again after reconnecting resumes it.
        "destination_disconnected": destination_disconnected,
        "dry_run": settings.dry_run,
        # Carried into the report so a completed import that was working from a
        # truncated source listing is not mistaken for a complete one.
        "source_listing_incomplete": bool(source_discovery.get("incomplete")),
        "source_listing_errors": len(source_discovery.get("errors", [])),
        "source_unreadable_files": len(source_discovery.get("unreadable", [])),
    }
    if not settings.dry_run:
        try:
            result["report_path"] = write_transfer_report(output_root, result, failures)
            logger.log(f"Transfer report written: {result['report_path']}")
        except OSError as exc:
            logger.log(f"WARNING: Failed to write transfer report: {exc}")
    return result
