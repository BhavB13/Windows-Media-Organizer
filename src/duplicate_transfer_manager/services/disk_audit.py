"""Read-only local-volume inventory, tiering, history, and review metadata."""

from __future__ import annotations

import ctypes
import hashlib
import json
import os
import shutil
import string
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..core import (
    AuditTier,
    BackupStatus,
    CancellationToken,
    DiskAuditItem,
    DiskAuditSettings,
    DiskAuditSnapshot,
    OperationPhase,
    OperationReporter,
    OperationResult,
    OperationState,
)
from ..runtime_paths import RuntimePaths, get_runtime_paths


SCHEMA_VERSION = 1
MIB = 1 << 20
FILE_ATTRIBUTE_REPARSE_POINT = 0x400


@dataclass(frozen=True)
class VolumeDescriptor:
    root: str
    volume_id: str
    label: str
    drive_type: str
    capacity_bytes: int
    used_bytes: int
    free_bytes: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "root": self.root,
            "volume_id": self.volume_id,
            "label": self.label,
            "drive_type": self.drive_type,
            "capacity_bytes": self.capacity_bytes,
            "used_bytes": self.used_bytes,
            "free_bytes": self.free_bytes,
        }


@dataclass(frozen=True)
class CatalogEntry:
    key: str
    label: str
    path: str
    tier: AuditTier
    kind: str
    verdict: str
    rationale: str
    recommendation: str
    command: str = ""
    always_show: bool = False


def _atomic_write(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    temporary.replace(path)


def _safe_path_key(value: str) -> str:
    normalized = os.path.normcase(os.path.abspath(value))
    drive, tail = os.path.splitdrive(normalized)
    if drive and tail in {"\\", "/"}:
        return drive + os.sep
    return normalized.rstrip("\\/")


def _is_within(path: str, root: str) -> bool:
    try:
        return os.path.commonpath([_safe_path_key(path), _safe_path_key(root)]) == _safe_path_key(root)
    except ValueError:
        return False


def _relative_key(path: str, root: str) -> str:
    try:
        relative = os.path.relpath(path, root)
    except ValueError:
        relative = os.path.abspath(path)
    return os.path.normcase(relative).replace("\\", "/")


def _stable_id(volume_id: str, kind: str, key: str) -> str:
    digest = hashlib.sha256(f"{volume_id}|{kind}|{key}".encode("utf-8", "surrogatepass")).hexdigest()
    return f"{kind}_{digest[:20]}"


class AuditSnapshotStore:
    """Own immutable successful snapshots and mutable, path-free review state."""

    # A saved snapshot is immutable -- save() refuses to overwrite one -- so a
    # parsed snapshot can be cached against its (mtime, size) stamp. Without
    # this, latest() re-read and re-parsed every snapshot on the drive, and the
    # Overview page calls latest() on every refresh.
    # Shared across instances on purpose: callers such as DashboardService
    # construct a store per call, so a per-instance cache would never survive
    # to be used. Entries are keyed by absolute path and only trusted while the
    # file's (mtime, size) is unchanged, so two stores over different data
    # directories cannot see each other's entries.
    _CACHE_LIMIT = 64
    _cache_lock = threading.RLock()
    _snapshot_cache: dict[str, tuple[tuple[int, int], "DiskAuditSnapshot"]] = {}
    _review_cache: dict[str, tuple[tuple[int, int], dict[str, Any]]] = {}

    def __init__(self, paths: RuntimePaths | None = None, *, retention: int = 10) -> None:
        self.paths = paths or get_runtime_paths()
        self.retention = max(1, retention)
        self.snapshot_dir = self.paths.audits / "snapshots"
        self.review_path = self.paths.audits / "review_state.json"
        self._lock = threading.RLock()

    @staticmethod
    def _stamp(path: Path) -> tuple[int, int] | None:
        """Identity of a file's current contents, cheaply."""

        try:
            stat = path.stat()
        except OSError:
            return None
        return (stat.st_mtime_ns, stat.st_size)

    def _parse_snapshot(self, path: Path) -> DiskAuditSnapshot | None:
        """Parse a snapshot file, reusing the previous parse when unchanged."""

        key = str(path)
        stamp = self._stamp(path)
        if stamp is None:
            with self._cache_lock:
                type(self)._snapshot_cache.pop(key, None)
            return None
        with self._cache_lock:
            cached = type(self)._snapshot_cache.get(key)
            if cached is not None and cached[0] == stamp:
                return cached[1]
        try:
            snapshot = DiskAuditSnapshot.from_dict(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return None
        if snapshot.schema_version != SCHEMA_VERSION:
            return None
        with self._cache_lock:
            cache = type(self)._snapshot_cache
            if len(cache) >= self._CACHE_LIMIT:
                cache.pop(next(iter(cache)), None)
            cache[key] = (stamp, snapshot)
        return snapshot

    def save(self, snapshot: DiskAuditSnapshot) -> str:
        target = self.snapshot_dir / f"{snapshot.audit_id}.json"
        with self._lock:
            if target.exists():
                raise FileExistsError(f"Audit snapshot already exists and is immutable: {snapshot.audit_id}")
            _atomic_write(target, snapshot.to_dict())
            self._prune(snapshot.volume_id)
        return str(target)

    def load(self, identifier: str | os.PathLike[str]) -> DiskAuditSnapshot:
        candidate = Path(identifier)
        if not candidate.is_absolute():
            candidate = self.snapshot_dir / f"{candidate.stem}.json"
        resolved = candidate.resolve()
        root = self.snapshot_dir.resolve()
        if resolved.parent != root:
            raise ValueError("Audit snapshot is outside the application audit folder.")
        snapshot = DiskAuditSnapshot.from_dict(json.loads(resolved.read_text(encoding="utf-8")))
        if snapshot.schema_version != SCHEMA_VERSION:
            raise ValueError(f"Unsupported Disk Audit snapshot schema: {snapshot.schema_version}")
        return snapshot

    def list_snapshots(
        self,
        *,
        volume_id: str = "",
        mode: str = "",
    ) -> list[DiskAuditSnapshot]:
        snapshots: list[DiskAuditSnapshot] = []
        for path in self.snapshot_dir.glob("*.json"):
            snapshot = self._parse_snapshot(path)
            if snapshot is None:
                continue
            if volume_id and snapshot.volume_id != volume_id:
                continue
            if mode and snapshot.mode != mode:
                continue
            snapshots.append(snapshot)
        return sorted(snapshots, key=lambda item: item.completed_at, reverse=True)

    def latest(self, *, volume_id: str = "", mode: str = "") -> DiskAuditSnapshot | None:
        snapshots = self.list_snapshots(volume_id=volume_id, mode=mode)
        return snapshots[0] if snapshots else None

    def _prune(self, volume_id: str) -> None:
        for snapshot in self.list_snapshots(volume_id=volume_id)[self.retention :]:
            (self.snapshot_dir / f"{snapshot.audit_id}.json").unlink(missing_ok=True)

    def review_state(self, volume_id: str, item_id: str) -> dict[str, Any]:
        value = self._read_review().get(volume_id, {}).get(item_id, {})
        return dict(value) if isinstance(value, dict) else {}

    def set_review_state(
        self,
        volume_id: str,
        item_id: str,
        *,
        reviewed: bool | None = None,
        hidden: bool | None = None,
        backup_status: BackupStatus | str | None = None,
    ) -> dict[str, Any]:
        with self._lock:
            # Deep-copied: the cache hands out the object it stores, and this
            # method edits its argument before writing.
            payload = json.loads(json.dumps(self._read_review()))
            state = payload.setdefault(volume_id, {}).setdefault(item_id, {})
            if reviewed is not None:
                state["reviewed"] = bool(reviewed)
            if hidden is not None:
                state["hidden"] = bool(hidden)
            if backup_status is not None:
                state["backup_status"] = BackupStatus(str(getattr(backup_status, "value", backup_status))).value
            state["updated_at"] = datetime.now(timezone.utc).isoformat()
            _atomic_write(self.review_path, payload)
            with self._cache_lock:
                type(self)._review_cache.pop(str(self.review_path), None)
            return dict(state)

    def _read_review(self) -> dict[str, Any]:
        """Review state, re-read only when the file on disk has changed.

        The dashboard asks for the state of every finding, and this used to open
        and parse the whole file per question.
        """

        key = str(self.review_path)
        stamp = self._stamp(self.review_path)
        if stamp is None:
            with self._cache_lock:
                type(self)._review_cache.pop(key, None)
            return {}
        with self._cache_lock:
            cached = type(self)._review_cache.get(key)
            if cached is not None and cached[0] == stamp:
                return cached[1]
        try:
            payload = json.loads(self.review_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        payload = payload if isinstance(payload, dict) else {}
        with self._cache_lock:
            type(self)._review_cache[key] = (stamp, payload)
        return payload


class WindowsDiskInventory:
    """Small Windows API/registry boundary kept injectable for deterministic tests."""

    def list_volumes(self) -> list[VolumeDescriptor]:
        if os.name != "nt":
            return [self.describe_volume(str(Path.cwd().anchor or Path.cwd()))]
        mask = ctypes.windll.kernel32.GetLogicalDrives()
        volumes: list[VolumeDescriptor] = []
        for index, letter in enumerate(string.ascii_uppercase):
            if not mask & (1 << index):
                continue
            root = f"{letter}:\\"
            drive_type = int(ctypes.windll.kernel32.GetDriveTypeW(root))
            if drive_type not in {2, 3}:  # removable or fixed
                continue
            try:
                volumes.append(self.describe_volume(root))
            except OSError:
                continue
        return volumes

    def describe_volume(self, root: str) -> VolumeDescriptor:
        root = os.path.abspath(root)
        usage = shutil.disk_usage(root)
        label = Path(root).drive or root
        serial = ""
        volume_guid = ""
        drive_type = "fixed"
        if os.name == "nt":
            name_buffer = ctypes.create_unicode_buffer(261)
            serial_number = ctypes.c_uint(0)
            maximum_component = ctypes.c_uint(0)
            flags = ctypes.c_uint(0)
            fs_buffer = ctypes.create_unicode_buffer(261)
            ok = ctypes.windll.kernel32.GetVolumeInformationW(
                root,
                name_buffer,
                len(name_buffer),
                ctypes.byref(serial_number),
                ctypes.byref(maximum_component),
                ctypes.byref(flags),
                fs_buffer,
                len(fs_buffer),
            )
            if ok:
                serial = f"{serial_number.value:08X}"
                label = name_buffer.value or label
            guid_buffer = ctypes.create_unicode_buffer(261)
            if ctypes.windll.kernel32.GetVolumeNameForVolumeMountPointW(
                root,
                guid_buffer,
                len(guid_buffer),
            ):
                volume_guid = guid_buffer.value.rstrip("\\").casefold()
            drive_type = "removable" if int(ctypes.windll.kernel32.GetDriveTypeW(root)) == 2 else "fixed"
        identity = volume_guid or serial or _safe_path_key(root)
        volume_id = "volume-" + hashlib.sha256(identity.encode("utf-8", "surrogatepass")).hexdigest()[:20]
        return VolumeDescriptor(
            root=root,
            volume_id=volume_id,
            label=label,
            drive_type=drive_type,
            capacity_bytes=usage.total,
            used_bytes=usage.used,
            free_bytes=usage.free,
        )

    def catalog(self, volume_root: str) -> list[CatalogEntry]:
        entries: list[CatalogEntry] = []
        home = Path.home()
        protect_paths = {
            "downloads": ("Downloads", home / "Downloads"),
            "desktop": ("Desktop", home / "Desktop"),
            "documents": ("Documents", home / "Documents"),
            "pictures": ("Pictures", home / "Pictures"),
            "videos": ("Videos", home / "Videos"),
            "music": ("Music", home / "Music"),
        }
        if os.name == "nt":
            try:
                import winreg

                shell_folders = r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders"
                registry_names = {
                    "downloads": "{374DE290-123F-4565-9164-39C4925E467B}",
                    "desktop": "Desktop",
                    "documents": "Personal",
                    "pictures": "My Pictures",
                    "videos": "My Video",
                    "music": "My Music",
                }
                with winreg.OpenKey(winreg.HKEY_CURRENT_USER, shell_folders) as key:
                    for name, registry_name in registry_names.items():
                        try:
                            raw_path = winreg.QueryValueEx(key, registry_name)[0]
                        except OSError:
                            continue
                        label, _fallback = protect_paths[name]
                        protect_paths[name] = (
                            label,
                            Path(os.path.expandvars(str(raw_path))),
                        )
            except OSError:
                pass
        protect = tuple(
            (key, label, path)
            for key, (label, path) in protect_paths.items()
        )
        one_drive = os.environ.get("OneDrive", "").strip()
        for key, label, path in protect + (("onedrive", "OneDrive", Path(one_drive)),) if one_drive else protect:
            if _is_within(str(path), volume_root):
                entries.append(
                    CatalogEntry(
                        key,
                        label,
                        str(path),
                        AuditTier.PROTECT_RECLAIM,
                        "personal_data",
                        "protect first",
                        "Large user-controlled data can be valuable even when it is also a reclaim opportunity.",
                        "Review the contents and confirm a second copy before removing anything.",
                    )
                )

        local = Path(os.environ.get("LOCALAPPDATA", home / "AppData" / "Local"))
        caches = [
            ("temp", "User temporary files", local / "Temp", "Temporary files regenerate, but active installers may still be using some entries.", "Review and clear through Windows Storage settings."),
            ("npm", "npm package cache", local / "npm-cache", "Downloaded npm packages can be fetched again.", "Use npm cache verification or cleanup when no install is running."),
            ("pip", "pip download cache", local / "pip" / "Cache", "Downloaded Python packages can be fetched again.", "Clear with pip's supported cache command if desired."),
            ("uv", "uv package cache", local / "uv" / "cache", "Downloaded and built Python artifacts can be recreated.", "Use uv's supported cache cleanup command if desired."),
            ("pnpm", "pnpm package store", local / "pnpm" / "store", "Package content can be downloaded again.", "Prune the store with pnpm when projects are closed."),
            ("gradle", "Gradle build cache", home / ".gradle" / "caches", "Build artifacts and dependency caches regenerate.", "Close builds before using Gradle's supported cache cleanup."),
            ("generic", "User tool cache", home / ".cache", "This allowlisted cache directory contains regenerable tool data.", "Review large children and clear only caches for tools that are not running."),
        ]
        browser_roots = (
            ("chrome", "Chrome", local / "Google" / "Chrome" / "User Data"),
            ("edge", "Microsoft Edge", local / "Microsoft" / "Edge" / "User Data"),
        )
        for browser_key, browser_label, profiles_root in browser_roots:
            for profile in profiles_root.glob("*") if profiles_root.is_dir() else ():
                if not profile.is_dir():
                    continue
                for cache_name in ("Cache", "Code Cache", "GPUCache"):
                    cache_path = profile / cache_name
                    if cache_path.is_dir():
                        caches.append(
                            (
                                f"{browser_key}-{profile.name}-{cache_name}",
                                f"{browser_label} {profile.name} {cache_name}",
                                cache_path,
                                "This browser cache is profile-specific and can be downloaded or generated again.",
                                f"Close {browser_label}, then use its built-in browsing-data controls if cleanup is desired.",
                            )
                        )
        firefox_profiles = Path(os.environ.get("APPDATA", home / "AppData" / "Roaming")) / "Mozilla" / "Firefox" / "Profiles"
        for profile in firefox_profiles.glob("*") if firefox_profiles.is_dir() else ():
            cache_path = profile / "cache2"
            if cache_path.is_dir():
                caches.append(
                    (
                        f"firefox-{profile.name}-cache2",
                        f"Firefox {profile.name} cache",
                        cache_path,
                        "This Firefox profile cache can be downloaded or generated again.",
                        "Close Firefox, then use its built-in browsing-data controls if cleanup is desired.",
                    )
                )
        avd_root = home / ".android" / "avd"
        for snapshot_path in avd_root.glob("*.avd/snapshots") if avd_root.is_dir() else ():
            if snapshot_path.is_dir():
                caches.append(
                    (
                        f"avd-{snapshot_path.parent.name}-snapshots",
                        f"Android emulator snapshots ({snapshot_path.parent.stem})",
                        snapshot_path,
                        "Emulator snapshots can be recreated, but saved emulator state will be lost.",
                        "Delete unused snapshots through Android Studio's Device Manager.",
                    )
                )
        commands = {
            "npm": "npm cache verify",
            "pip": "python -m pip cache purge",
            "uv": "uv cache clean",
            "pnpm": "pnpm store prune",
        }
        for key, label, path, rationale, recommendation in caches:
            if _is_within(str(path), volume_root):
                entries.append(
                    CatalogEntry(
                        f"cache-{key}",
                        label,
                        str(path),
                        AuditTier.REGENERABLE_CACHE,
                        "cache",
                        "regenerable",
                        rationale,
                        recommendation,
                        commands.get(key, ""),
                    )
                )

        system_root = Path(os.environ.get("SystemRoot", Path(volume_root) / "Windows"))
        if _is_within(str(system_root), volume_root):
            windows_entries = (
                ("hiberfil", "Hibernation file", Path(volume_root) / "hiberfil.sys", "feature storage", "Windows owns this file; disabling hibernation changes system behavior.", "Review hibernation in an elevated terminal only if you understand the tradeoff.", "powercfg /h off"),
                ("pagefile", "Virtual memory", Path(volume_root) / "pagefile.sys", "leave alone", "Windows manages virtual memory for stability.", "Leave system-managed virtual memory enabled.", ""),
                ("winsxs", "Windows component store", system_root / "WinSxS", "leave alone", "The component store supports servicing and rollback.", "Use Windows servicing tools only; never delete files here directly.", "DISM /Online /Cleanup-Image /AnalyzeComponentStore"),
                ("installer", "Windows Installer rollback data", system_root / "Installer", "leave alone", "Installer data is required to repair or uninstall applications.", "Never delete this folder directly.", ""),
                ("updates", "Windows Update download cache", system_root / "SoftwareDistribution" / "Download", "system cleanup", "Windows manages downloaded update packages.", "Use Windows Storage or Update troubleshooting rather than deleting files directly.", ""),
                ("restore", "System restore and protected storage", Path(volume_root) / "System Volume Information", "leave alone", "Windows protects restore points and other volume metadata here.", "Review restore-point usage through System Protection; never change this folder directly.", ""),
            )
            for key, label, path, verdict, rationale, recommendation, command in windows_entries:
                entries.append(
                    CatalogEntry(
                        f"windows-{key}",
                        label,
                        str(path),
                        AuditTier.WINDOWS_MANAGED,
                        "windows",
                        verdict,
                        rationale,
                        recommendation,
                        command,
                        always_show=True,
                    )
                )
        return entries

    def installed_apps(self, volume_root: str) -> list[dict[str, Any]]:
        if os.name != "nt":
            return []
        try:
            import winreg
        except ImportError:
            return []
        uninstall = r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"
        locations = (
            (winreg.HKEY_LOCAL_MACHINE, uninstall, winreg.KEY_WOW64_64KEY),
            (winreg.HKEY_LOCAL_MACHINE, uninstall, winreg.KEY_WOW64_32KEY),
            (winreg.HKEY_CURRENT_USER, uninstall, 0),
        )
        apps: dict[tuple[str, str], dict[str, Any]] = {}
        for hive, key_path, view in locations:
            try:
                root_key = winreg.OpenKey(hive, key_path, 0, winreg.KEY_READ | view)
            except OSError:
                continue
            with root_key:
                index = 0
                while True:
                    try:
                        child_name = winreg.EnumKey(root_key, index)
                    except OSError:
                        break
                    index += 1
                    try:
                        child = winreg.OpenKey(root_key, child_name)
                    except OSError:
                        continue
                    with child:
                        def value(name: str, default: Any = "") -> Any:
                            try:
                                return winreg.QueryValueEx(child, name)[0]
                            except OSError:
                                return default
                        name = str(value("DisplayName", "")).strip()
                        if not name or int(value("SystemComponent", 0) or 0):
                            continue
                        location = os.path.expandvars(str(value("InstallLocation", "")).strip().strip('"'))
                        if location and not _is_within(location, volume_root):
                            continue
                        system_drive = os.environ.get("SystemDrive", "C:") + os.sep
                        if not location and not _is_within(system_drive, volume_root):
                            continue
                        estimated = max(0, int(value("EstimatedSize", 0) or 0)) * 1024
                        if not location and estimated < 100 * MIB:
                            continue
                        apps[(name.casefold(), _safe_path_key(location) if location else "")] = {
                            "name": name,
                            "location": location,
                            "estimated_size": estimated,
                        }
        return list(apps.values())


class DiskAuditService:
    """Build safety-oriented read-only findings for a local Windows volume."""

    def __init__(
        self,
        paths: RuntimePaths | None = None,
        *,
        inventory: WindowsDiskInventory | None = None,
        store: AuditSnapshotStore | None = None,
    ) -> None:
        self.paths = paths or get_runtime_paths()
        self.inventory = inventory or WindowsDiskInventory()
        self.store = store or AuditSnapshotStore(self.paths)

    def list_volumes(self) -> list[dict[str, Any]]:
        return [volume.to_dict() for volume in self.inventory.list_volumes()]

    def run(
        self,
        settings: DiskAuditSettings,
        cancellation: CancellationToken,
        reporter: OperationReporter,
    ) -> OperationResult:
        started = datetime.now(timezone.utc)
        mode = settings.mode.lower().strip()
        if mode not in {"quick", "deep"}:
            raise ValueError("Disk Audit mode must be quick or deep.")
        volume = self.inventory.describe_volume(settings.volume_root)
        previous = self.store.latest(volume_id=volume.volume_id, mode=mode)
        reporter.set_state(OperationState.SCANNING, phase=OperationPhase.DISCOVERY, message="Inventorying known storage locations…")
        warnings: list[str] = []
        inaccessible = 0
        catalog_items: list[DiskAuditItem] = []
        measurement_cache: dict[str, tuple[int, int, list[str]]] = {}
        entries = self.inventory.catalog(volume.root)
        for index, entry in enumerate(entries, 1):
            cancellation.raise_if_cancelled()
            reporter.emit(
                f"Measuring {entry.label}…",
                phase=OperationPhase.DISCOVERY,
                current=index,
                total=max(1, len(entries)),
                current_item=entry.path,
            )
            cache_key = _safe_path_key(entry.path)
            if cache_key not in measurement_cache:
                measurement_cache[cache_key] = self._measure_path(entry.path, cancellation)
            size, count, entry_warnings = measurement_cache[cache_key]
            inaccessible += len(entry_warnings)
            warnings.extend(entry_warnings)
            if size < 100 * MIB and not entry.always_show:
                continue
            if size == 0 and not os.path.exists(entry.path) and not entry.always_show:
                continue
            catalog_items.append(
                self._catalog_item(volume, entry, size, count, entry_warnings)
            )

        reporter.set_state(
            OperationState.SCANNING,
            phase=OperationPhase.DISCOVERY,
            message="Inventorying installed applications…",
        )
        app_items = self._application_items(volume, cancellation, reporter, measurement_cache)
        reporter.set_state(
            OperationState.COMPARING,
            phase=OperationPhase.COMPARISON,
            message="Analyzing findings and resolving overlaps…",
        )
        known_items = self._deduplicate_known(catalog_items + app_items)

        deep_items: list[DiskAuditItem] = []
        if mode == "deep":
            reporter.set_state(OperationState.SCANNING, phase=OperationPhase.DISCOVERY, message="Walking the selected drive for large files and folders…")
            deep_items, deep_warnings = self._deep_findings(volume, settings, cancellation, reporter, known_items)
            warnings.extend(deep_warnings)
            inaccessible += len(deep_warnings)

        items = tuple(sorted(known_items + deep_items, key=lambda item: (item.tier.value, -item.size_bytes, item.label.casefold())))
        cancellation.raise_if_cancelled()
        completed = datetime.now(timezone.utc)
        audit_id = completed.strftime("disk_audit_%Y%m%d_%H%M%S_%f")
        final_volume = self.inventory.describe_volume(volume.root)
        snapshot = DiskAuditSnapshot(
            schema_version=SCHEMA_VERSION,
            audit_id=audit_id,
            volume_id=volume.volume_id,
            volume_label=volume.label,
            scanned_root=volume.root,
            mode=mode,
            started_at=started.isoformat(),
            completed_at=completed.isoformat(),
            capacity_bytes=final_volume.capacity_bytes,
            used_bytes=final_volume.used_bytes,
            free_bytes=final_volume.free_bytes,
            items=items,
            warnings=tuple(warnings[:200]),
            inaccessible_count=inaccessible,
            previous_snapshot_id=previous.audit_id if previous else "",
        )
        reporter.set_state(
            OperationState.COMPARING,
            phase=OperationPhase.COMPARISON,
            message="Comparing with the latest successful audit of this drive and mode…",
        )
        comparison = compare_snapshots(snapshot, previous)
        cancellation.raise_if_cancelled()
        reporter.set_state(OperationState.SCANNING, phase=OperationPhase.FINALIZATION, message="Saving the local audit snapshot…")
        snapshot_path = self.store.save(snapshot)
        reporter.set_state(OperationState.COMPLETED, phase=OperationPhase.FINALIZATION, message="Disk audit complete.")
        return OperationResult(
            status=OperationState.COMPLETED,
            counts={
                "findings": len(items),
                "warnings": len(warnings),
                "inaccessible": inaccessible,
            },
            duration_seconds=max(0.0, (completed - started).total_seconds()),
            warnings=tuple(warnings[:50]),
            report_path=snapshot_path,
            data={"snapshot": snapshot, "comparison": comparison},
        )

    def _catalog_item(
        self,
        volume: VolumeDescriptor,
        entry: CatalogEntry,
        size: int,
        count: int,
        warnings: list[str],
    ) -> DiskAuditItem:
        key = entry.key + ":" + _relative_key(entry.path, volume.root)
        exists = os.path.lexists(entry.path)
        item_warnings = list(warnings)
        if not exists and entry.always_show:
            item_warnings.append(f"Not present or not visible: {entry.path}")
        return DiskAuditItem(
            stable_id=_stable_id(volume.volume_id, entry.kind, key),
            label=entry.label,
            kind=entry.kind,
            paths=(entry.path,),
            size_bytes=size,
            file_count=count,
            tier=entry.tier,
            verdict=entry.verdict,
            rationale=entry.rationale,
            recommendation=entry.recommendation,
            command=entry.command,
            measurement_confidence="unavailable" if size == 0 and (warnings or not exists) else "partial" if warnings else "measured",
            warnings=tuple(item_warnings[:10]),
        )

    def _application_items(
        self,
        volume: VolumeDescriptor,
        cancellation: CancellationToken,
        reporter: OperationReporter,
        measurement_cache: dict[str, tuple[int, int, list[str]]],
    ) -> list[DiskAuditItem]:
        applications = self.inventory.installed_apps(volume.root)
        findings: list[DiskAuditItem] = []
        broad_roots = {
            _safe_path_key(value)
            for value in (
                volume.root,
                os.environ.get("ProgramFiles", ""),
                os.environ.get("ProgramFiles(x86)", ""),
                os.environ.get("SystemRoot", ""),
            )
            if value
        }
        for index, app in enumerate(applications, 1):
            cancellation.raise_if_cancelled()
            location = str(app.get("location", ""))
            estimated = max(0, int(app.get("estimated_size", 0)))
            warnings: list[str] = []
            size = estimated
            count = 0
            confidence = "estimated"
            location_key = _safe_path_key(location) if location else ""
            if location and os.path.exists(location) and location_key not in broad_roots:
                reporter.emit(
                    f"Measuring application {app.get('name', 'Application')}…",
                    phase=OperationPhase.COMPARISON,
                    current=index,
                    total=max(1, len(applications)),
                    current_item=location,
                )
                if location_key not in measurement_cache:
                    measurement_cache[location_key] = self._measure_path(location, cancellation)
                measured, count, warnings = measurement_cache[location_key]
                if measured:
                    size = measured
                    confidence = "partial" if warnings else "measured"
            if size < 100 * MIB:
                continue
            key = _relative_key(location, volume.root) if location else str(app.get("name", "")).casefold()
            findings.append(
                DiskAuditItem(
                    stable_id=_stable_id(volume.volume_id, "application", key),
                    label=str(app.get("name", "Installed application")),
                    kind="application",
                    paths=(location,) if location else (),
                    size_bytes=size,
                    file_count=count,
                    tier=AuditTier.APPS_REVIEW,
                    verdict="review",
                    rationale="Windows reports this application as installed; timestamps do not prove whether it is unused.",
                    recommendation="Review whether you still need this app. Uninstall through Windows Apps settings if appropriate.",
                    measurement_confidence=confidence,
                    warnings=tuple(warnings[:10]),
                )
            )
        return findings

    def _measure_path(self, path: str, cancellation: CancellationToken) -> tuple[int, int, list[str]]:
        cancellation.raise_if_cancelled()
        warnings: list[str] = []
        try:
            stat = os.lstat(path)
        except OSError as exc:
            return 0, 0, [f"Could not measure {path}: {exc}"] if os.path.lexists(path) else []
        if getattr(stat, "st_file_attributes", 0) & FILE_ATTRIBUTE_REPARSE_POINT or os.path.islink(path):
            return 0, 0, [f"Skipped reparse point: {path}"]
        if not os.path.isdir(path):
            return max(0, int(stat.st_size)), 1, warnings

        def visit(directory: str) -> tuple[int, int]:
            cancellation.raise_if_cancelled()
            size = 0
            count = 0
            try:
                with os.scandir(directory) as entries:
                    for entry in entries:
                        cancellation.raise_if_cancelled()
                        try:
                            entry_stat = entry.stat(follow_symlinks=False)
                            if getattr(entry_stat, "st_file_attributes", 0) & FILE_ATTRIBUTE_REPARSE_POINT or entry.is_symlink():
                                continue
                            if entry.is_dir(follow_symlinks=False):
                                child_size, child_count = visit(entry.path)
                                size += child_size
                                count += child_count
                            elif entry.is_file(follow_symlinks=False):
                                size += max(0, int(entry_stat.st_size))
                                count += 1
                        except OSError as exc:
                            if len(warnings) < 200:
                                warnings.append(f"Could not inspect {entry.path}: {exc}")
            except OSError as exc:
                if len(warnings) < 200:
                    warnings.append(f"Could not open {directory}: {exc}")
            return size, count

        size, count = visit(path)
        return size, count, warnings

    def _deep_findings(
        self,
        volume: VolumeDescriptor,
        settings: DiskAuditSettings,
        cancellation: CancellationToken,
        reporter: OperationReporter,
        known_items: list[DiskAuditItem],
    ) -> tuple[list[DiskAuditItem], list[str]]:
        directories: list[tuple[str, int, int]] = []
        files: list[tuple[str, int]] = []
        warnings: list[str] = []
        processed = 0

        def visit(directory: str) -> tuple[int, int]:
            nonlocal processed
            cancellation.raise_if_cancelled()
            size = 0
            count = 0
            try:
                with os.scandir(directory) as entries:
                    for entry in entries:
                        cancellation.raise_if_cancelled()
                        processed += 1
                        if processed % 500 == 0:
                            reporter.emit(
                                f"Deep scan: {processed:,} entries inspected",
                                phase=OperationPhase.DISCOVERY,
                                current=processed,
                                current_item=entry.path,
                            )
                        try:
                            stat = entry.stat(follow_symlinks=False)
                            if getattr(stat, "st_file_attributes", 0) & FILE_ATTRIBUTE_REPARSE_POINT or entry.is_symlink():
                                continue
                            if entry.is_dir(follow_symlinks=False):
                                child_size, child_count = visit(entry.path)
                                size += child_size
                                count += child_count
                            elif entry.is_file(follow_symlinks=False):
                                file_size = max(0, int(stat.st_size))
                                size += file_size
                                count += 1
                                if file_size >= settings.large_file_threshold:
                                    files.append((entry.path, file_size))
                        except OSError as exc:
                            if len(warnings) < 200:
                                warnings.append(f"Could not inspect {entry.path}: {exc}")
            except OSError as exc:
                if len(warnings) < 200:
                    warnings.append(f"Could not open {directory}: {exc}")
            if _safe_path_key(directory) != _safe_path_key(volume.root) and size >= settings.large_folder_threshold:
                directories.append((directory, size, count))
            return size, count

        visit(volume.root)
        claimed = [path for item in known_items for path in item.paths if path]
        accepted_paths = list(claimed)
        findings: list[DiskAuditItem] = []
        for path, size, count in sorted(directories, key=lambda value: value[1], reverse=True):
            if len([item for item in findings if item.kind == "large_folder"]) >= settings.max_large_items:
                break
            if any(_is_within(path, existing) or _is_within(existing, path) for existing in accepted_paths):
                continue
            accepted_paths.append(path)
            findings.append(self._deep_item(volume, path, size, count, "large_folder"))
        for path, size in sorted(files, key=lambda value: value[1], reverse=True):
            if len([item for item in findings if item.kind == "large_file"]) >= settings.max_large_items:
                break
            if any(_is_within(path, existing) for existing in accepted_paths):
                continue
            accepted_paths.append(path)
            findings.append(self._deep_item(volume, path, size, 1, "large_file"))
        return findings, warnings

    def _deep_item(
        self,
        volume: VolumeDescriptor,
        path: str,
        size: int,
        count: int,
        kind: str,
    ) -> DiskAuditItem:
        relative = _relative_key(path, volume.root)
        return DiskAuditItem(
            stable_id=_stable_id(volume.volume_id, kind, relative),
            label=Path(path).name or path,
            kind=kind,
            paths=(path,),
            size_bytes=size,
            file_count=count,
            tier=AuditTier.PROTECT_RECLAIM,
            verdict="review",
            rationale="A Deep scan found this as one of the largest user-controlled locations on the drive.",
            recommendation="Open and review it. Confirm a second copy before removing valuable or irreplaceable data.",
        )

    def _deduplicate_known(self, items: list[DiskAuditItem]) -> list[DiskAuditItem]:
        # Specific catalog/application locations win over their broad ancestors.
        ranked = sorted(
            items,
            key=lambda item: (
                max((_safe_path_key(path).count(os.sep) for path in item.paths), default=0),
                item.size_bytes,
            ),
            reverse=True,
        )
        accepted: list[DiskAuditItem] = []
        claimed: list[str] = []
        for item in ranked:
            paths = [path for path in item.paths if path]
            if paths and any(
                _is_within(path, existing) or _is_within(existing, path)
                for path in paths
                for existing in claimed
            ):
                continue
            accepted.append(item)
            claimed.extend(paths)
        return accepted


def compare_snapshots(
    current: DiskAuditSnapshot,
    previous: DiskAuditSnapshot | None,
) -> dict[str, Any]:
    """Return conservative deltas; absent/partial findings are never called cleared."""

    if previous is None or previous.volume_id != current.volume_id or previous.mode != current.mode:
        return {"baseline_available": False, "free_delta": 0, "changes": []}
    previous_items = {item.stable_id: item for item in previous.items}
    changes: list[dict[str, Any]] = []
    for item in current.items:
        old = previous_items.get(item.stable_id)
        if old is None or old.measurement_confidence == "unavailable" or item.measurement_confidence == "unavailable":
            continue
        delta = item.size_bytes - old.size_bytes
        if delta:
            changes.append(
                {
                    "stable_id": item.stable_id,
                    "label": item.label,
                    "before_bytes": old.size_bytes,
                    "after_bytes": item.size_bytes,
                    "delta_bytes": delta,
                }
            )
    changes.sort(key=lambda value: abs(int(value["delta_bytes"])), reverse=True)
    return {
        "baseline_available": True,
        "previous_snapshot_id": previous.audit_id,
        "free_delta": current.free_bytes - previous.free_bytes,
        "changes": changes,
    }
