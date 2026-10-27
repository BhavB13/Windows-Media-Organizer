import argparse
from collections.abc import Mapping
import hashlib
import json
import os
from pathlib import Path
import posixpath
import re
import shutil
import threading
import time
from dataclasses import dataclass

from discovery import discover_files
from models import Settings
from runtime_paths import get_runtime_paths
from utils import DEFAULT_EXCLUDES, DEFAULT_MEDIA_EXTS, normalize_extensions

import volume_identity
from volume_identity import (
    VolumeIdentity,
    clear_probe_cache,
    clear_volume_probe_cache,
    has_fat_4gib_limit,
    is_volume_online,
    preserves_subsecond_mtime,
    probe_volume,
    relative_path_from_mount,
    resolve_mount_point,
    sanitize_path_fallback,
)


CACHE_VERSION = 1


@dataclass
class DriveCacheStats:
    files: int = 0
    hashes: int = 0
    stale: int = 0


class DriveHashCache:
    def __init__(self, path):
        self.path = path
        self.lock_path = f"{path}.lock"
        self.data = self._empty_data()

    def _empty_data(self):
        return {
            "version": CACHE_VERSION,
            "roots": {},
            "files": {},
            "hash_index": {},
            "updated_at": 0,
        }

    def load(self):
        if not self.path or not os.path.exists(self.path):
            self.data = self._empty_data()
            return
        try:
            with open(self.path, "r", encoding="utf-8") as handle:
                loaded = json.load(handle)
            if loaded.get("version") != CACHE_VERSION:
                self.data = self._empty_data()
                return
            if not isinstance(loaded.get("files", {}), dict) or not isinstance(loaded.get("hash_index", {}), dict):
                self.data = self._empty_data()
                return
            self.data = loaded
            self.data.setdefault("roots", {})
            self.data.setdefault("files", {})
            self.data.setdefault("hash_index", {})
            self._repair_hash_index()
        except (OSError, json.JSONDecodeError):
            self.data = self._empty_data()

    def save(self):
        if not self.path:
            return
        directory = os.path.dirname(os.path.abspath(self.path))
        os.makedirs(directory, exist_ok=True)
        tmp_path = f"{self.path}.tmp"
        with open(tmp_path, "w", encoding="utf-8") as handle:
            json.dump(self.data, handle)
        os.replace(tmp_path, self.path)

    def acquire_lock(self):
        if not self.lock_path:
            return True
        try:
            directory = os.path.dirname(os.path.abspath(self.lock_path))
            if directory:
                os.makedirs(directory, exist_ok=True)
            fd = os.open(self.lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(str(os.getpid()))
            return True
        except FileExistsError:
            return False

    def release_lock(self):
        try:
            os.remove(self.lock_path)
        except OSError:
            pass

    def normalize_path(self, path):
        return os.path.normcase(os.path.abspath(path))

    def get_file_entry(self, path):
        return self.data.get("files", {}).get(self.normalize_path(path))

    def get_valid_hash(self, path, algo, mode, size, mtime):
        entry = self.get_file_entry(path)
        if not entry:
            return None
        if not isinstance(entry, dict):
            return None
        if entry.get("algo") != algo or entry.get("mode") != mode:
            return None
        if entry.get("size") != size or entry.get("mtime") != mtime:
            return None
        if entry.get("stale"):
            return None
        digest = entry.get("hash")
        if not isinstance(digest, str) or not re.fullmatch(r"[a-fA-F0-9]{32,128}", digest):
            entry["stale"] = True
            return None
        return digest

    def set_file_hash(self, path, size, mtime, digest, algo, mode, root=None):
        normalized = self.normalize_path(path)
        old_entry = self.data["files"].get(normalized)
        if old_entry:
            self._remove_from_hash_index(normalized, old_entry.get("hash"))

        entry = {
            "path": path,
            "size": size,
            "mtime": mtime,
            "hash": digest,
            "algo": algo,
            "mode": mode,
            "root": root or "",
            "stale": False,
            "updated_at": time.time(),
        }
        self.data["files"][normalized] = entry
        self.data["hash_index"].setdefault(digest, [])
        if normalized not in self.data["hash_index"][digest]:
            self.data["hash_index"][digest].append(normalized)
        self.data["updated_at"] = time.time()

    def _is_root_online(self, root):
        if not root or not volume_identity.is_volume_online(root):
            return False
        mount_point = resolve_mount_point(root)
        if mount_point and os.path.exists(mount_point) and not os.path.exists(root):
            return False
        stored_identity = self.data.get("volume_identity")
        if isinstance(stored_identity, Mapping):
            identity = VolumeIdentity.from_dict(stored_identity)
            if identity.is_strong and mount_point == resolve_mount_point(identity.mount_point):
                if not volume_identity.is_volume_online(identity):
                    return False
        return True

    def mark_missing_under_root(self, root, seen_paths):
        """Mark cached entries under ``root`` not present in ``seen_paths`` as stale.

        Data-safety guarantee (offline vs. deleted):
        If the volume backing ``root`` is currently offline or unmounted, this
        method refuses to mark any cached entry stale and returns ``0``. A
        disconnected external drive must never be interpreted as deleted media,
        otherwise an unplugged drive would invalidate every cached hash under
        ``root`` and cause callers to treat absent files as removed.
        """
        if not self._is_root_online(root):
            return 0
        normalized_root = self.normalize_path(root)
        seen = {self.normalize_path(path) for path in seen_paths}
        stale_count = 0
        for normalized, entry in self.data.get("files", {}).items():
            if self._is_under_root(normalized_root, normalized) and normalized not in seen:
                entry["stale"] = True
                stale_count += 1
        return stale_count

    def entries_under_root(self, root, algo=None, mode=None, include_stale=False):
        normalized_root = self.normalize_path(root)
        entries = []
        for normalized, entry in self.data.get("files", {}).items():
            if not self._is_under_root(normalized_root, normalized):
                continue
            if not include_stale and entry.get("stale"):
                continue
            if algo and entry.get("algo") != algo:
                continue
            if mode and entry.get("mode") != mode:
                continue
            entries.append(entry)
        return entries

    def _is_under_root(self, normalized_root, normalized_path):
        try:
            return os.path.commonpath([normalized_root, normalized_path]) == normalized_root
        except ValueError:
            return False

    def active_file_count_under_root(self, root, algo=None, mode=None):
        return len(self.entries_under_root(root, algo=algo, mode=mode))

    def hashes_under_root(self, root, algo=None, mode=None):
        return {
            entry["hash"]
            for entry in self.entries_under_root(root, algo=algo, mode=mode)
            if entry.get("hash")
        }

    def has_hash(self, digest):
        paths = self.data.get("hash_index", {}).get(digest, [])
        return any(not self.data["files"].get(path, {}).get("stale") for path in paths)

    def hashes(self):
        return {
            digest
            for digest, paths in self.data.get("hash_index", {}).items()
            if any(not self.data["files"].get(path, {}).get("stale") for path in paths)
        }

    def stats(self):
        files = self.data.get("files", {})
        return DriveCacheStats(
            files=len(files),
            hashes=len(self.data.get("hash_index", {})),
            stale=sum(1 for entry in files.values() if entry.get("stale")),
        )

    def note_root(self, root, settings, files_found, errors):
        self.data["roots"][self.normalize_path(root)] = {
            "path": root,
            "updated_at": time.time(),
            "algo": settings.hash_algo,
            "mode": settings.hash_mode,
            "only_media": settings.only_media,
            "extensions": settings.extensions,
            "files_found": files_found,
            "errors": errors,
        }
        self.data["updated_at"] = time.time()

    def _remove_from_hash_index(self, normalized_path, digest):
        if not digest:
            return
        paths = self.data.get("hash_index", {}).get(digest)
        if not paths:
            return
        self.data["hash_index"][digest] = [path for path in paths if path != normalized_path]
        if not self.data["hash_index"][digest]:
            del self.data["hash_index"][digest]

    def _repair_hash_index(self):
        repaired = {}
        for normalized, entry in list(self.data.get("files", {}).items()):
            if not isinstance(entry, dict):
                self.data["files"].pop(normalized, None)
                continue
            digest = entry.get("hash")
            if not isinstance(digest, str) or not re.fullmatch(r"[a-fA-F0-9]{32,128}", digest):
                entry["stale"] = True
                continue
            repaired.setdefault(digest, [])
            if normalized not in repaired[digest]:
                repaired[digest].append(normalized)
        self.data["hash_index"] = repaired


def _legacy_cache_stem(root):
    safe = os.path.abspath(root).replace("\\", "_").replace("/", "_").replace(":", "")
    return safe.strip("_") or "root"


def _migrate_legacy_cache_file(legacy_path: Path, new_path: Path, root: str, identity: VolumeIdentity) -> None:
    if new_path.exists() or not legacy_path.is_file():
        return
    try:
        new_path.parent.mkdir(parents=True, exist_ok=True)
        raw_text = legacy_path.read_text(encoding="utf-8")
        try:
            payload = json.loads(raw_text)
        except json.JSONDecodeError:
            payload = None
        if isinstance(payload, dict):
            migration_record = {
                "migrated_from": str(legacy_path),
                "legacy_path": str(legacy_path),
                "migrated_to": str(new_path),
                "migrated_at": time.time(),
                "root": str(root),
                "stable_key": identity.stable_key,
                "tier": identity.tier,
                "volume_identity": identity.to_dict(),
            }
            payload["migrated_from"] = str(legacy_path)
            payload["migration"] = migration_record
            payload["volume_identity"] = identity.to_dict()
            tmp_path = new_path.with_suffix(new_path.suffix + ".tmp")
            tmp_path.write_text(json.dumps(payload), encoding="utf-8")
            os.replace(tmp_path, new_path)
        else:
            shutil.copy2(legacy_path, new_path)
    except OSError:
        pass


def default_cache_path(root):
    caches_dir = get_runtime_paths().drive_caches
    legacy_path = caches_dir / f"{_legacy_cache_stem(root)}.json"
    identity = volume_identity.probe_volume(root)
    if identity.is_fallback:
        return str(legacy_path)

    rel = relative_path_from_mount(root, identity.mount_point)
    rel_safe = rel.replace("\\", "_").replace("/", "_").replace(":", "").strip("_")
    stem = f"{identity.stable_key}_{rel_safe}" if rel_safe else identity.stable_key
    new_path = caches_dir / f"{stem}.json"
    if new_path != legacy_path and not new_path.exists() and legacy_path.is_file():
        _migrate_legacy_cache_file(legacy_path, new_path, str(root), identity)
    return str(new_path)


class ADBHashCache(DriveHashCache):
    def __init__(self, path, serial):
        self.serial = serial.strip()
        super().__init__(path)

    def _is_root_online(self, root):
        return bool(root)

    def normalize_path(self, path):
        normalized = posixpath.normpath("/" + str(path).replace("\\", "/").lstrip("/"))
        if normalized == "/storage/emulated/0":
            normalized = "/sdcard"
        elif normalized.startswith("/storage/emulated/0/"):
            normalized = f"/sdcard/{normalized[len('/storage/emulated/0/'):]}"
        return f"adb://{self.serial}/{normalized.lstrip('/')}"

    def _is_under_root(self, normalized_root, normalized_path):
        root = normalized_root.rstrip("/")
        return normalized_path == root or normalized_path.startswith(f"{root}/")


def default_adb_cache_path(serial):
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", serial.strip()).strip("._") or "device"
    serial_tag = hashlib.sha256(serial.strip().encode("utf-8")).hexdigest()[:10]
    return str(get_runtime_paths().drive_caches / f"adb_{cleaned}_{serial_tag}.json")


def build_drive_cache(root, cache_path, settings, stop_event, logger=None, progress_callback=None, hash_func=None):
    from engine import compute_hash

    cache = (
        ADBHashCache(cache_path, getattr(settings, "adb_serial", ""))
        if getattr(settings, "use_adb", False)
        else DriveHashCache(cache_path)
    )
    cache.load()
    if not cache.acquire_lock():
        raise RuntimeError(f"Cache is locked: {cache_path}")
    try:
        # discover_files reports with a single ScanProgress argument, while the
        # hashing loop below reports (current, total, text). Handing the same
        # three-argument callback straight to discovery raised TypeError on the
        # first folder, so the documented dtm-build-cache CLI crashed on every
        # run before hashing anything.
        discovery_progress = None
        if progress_callback:
            def discovery_progress(scan):
                message = scan.message or f"Scanning {scan.source}"
                progress_callback(
                    scan.files_found,
                    scan.total,
                    f"{message} ({scan.files_found} file(s) found)",
                )

        discovery = discover_files(root, settings, stop_event, progress_callback=discovery_progress, logger=logger)
        seen_paths = []
        total = len(discovery.files)
        hash_func = hash_func or compute_hash
        for index, info in enumerate(discovery.files, 1):
            if stop_event.is_set():
                break
            seen_paths.append(info.path)
            size = info.size
            mtime = info.created
            cached = cache.get_valid_hash(info.path, settings.hash_algo, settings.hash_mode, size, mtime)
            digest = cached or hash_func(info.path, settings, stop_event, None, info.is_adb, logger)
            if digest:
                cache.set_file_hash(info.path, size, mtime, digest, settings.hash_algo, settings.hash_mode, root=root)
            if progress_callback:
                progress_callback(index, total, f"Hashing cache entries... {index}/{total}")
        stale_count = cache.mark_missing_under_root(root, seen_paths)
        cache.note_root(root, settings, len(discovery.files), len(discovery.errors) + stale_count)
        cache.save()
        return cache
    finally:
        cache.release_lock()


def _parse_extensions(value):
    if not value:
        return []
    return normalize_extensions([item.strip() for item in value.split(",") if item.strip()])


def main():
    parser = argparse.ArgumentParser(description="Build or update a Duplicate & Transfer Manager drive hash cache.")
    parser.add_argument("--root", required=True, help="Folder or drive root to cache, for example N:\\ or C:\\Users\\Name\\Pictures.")
    parser.add_argument(
        "--cache",
        default="",
        help="Cache JSON path. Defaults to the application cache folder under LocalAppData.",
    )
    parser.add_argument("--algo", default="sha256", choices=["sha256", "md5"])
    parser.add_argument("--mode", default="full", choices=["full", "fast"])
    parser.add_argument("--adb", action="store_true", help="Treat --root as an ADB device path.")
    parser.add_argument("--serial", default="", help="ADB serial. Required with --adb.")
    parser.add_argument("--media-only", action="store_true", help="Cache only supported media files.")
    parser.add_argument("--extensions", default="", help="Comma-separated extension list. Defaults to built-in media types.")
    parser.add_argument("--min-size-kb", type=int, default=0)
    args = parser.parse_args()
    if args.adb and not args.serial:
        parser.error("--serial is required with --adb")

    extensions = _parse_extensions(args.extensions) or DEFAULT_MEDIA_EXTS
    settings = Settings(
        scan_root=args.root,
        output_root="",
        criteria="hash",
        hash_algo=args.algo,
        hash_mode=args.mode,
        only_media=args.media_only,
        extensions=extensions,
        min_size_kb=args.min_size_kb,
        exclude_dirs=DEFAULT_EXCLUDES,
        skip_hidden_system=True,
        dry_run=True,
        preserve_structure=True,
        max_hash_workers=1,
        use_adb=args.adb,
        adb_serial=args.serial,
    )

    cache_path = args.cache or (
        default_adb_cache_path(args.serial) if args.adb else default_cache_path(args.root)
    )
    stop_event = threading.Event()

    def progress(current, total, text):
        if total > 0:
            print(f"{text} ({current}/{total})", flush=True)
        else:
            print(text, flush=True)

    cache = build_drive_cache(args.root, cache_path, settings, stop_event, progress_callback=progress)
    stats = cache.stats()
    print(f"Cache updated: {cache_path}")
    print(f"Files: {stats.files} | Hashes: {stats.hashes} | Stale: {stats.stale}")


if __name__ == "__main__":
    main()
