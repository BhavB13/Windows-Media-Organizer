"""Framework-neutral Windows volume identity, filesystem constraints, and online checks.

External drives on Windows routinely change drive letters (for example from ``E:``
to ``F:``) across reconnects or when another USB storage device is attached first.
Keying hash caches or media catalogs on the drive letter causes two defects:

1. **Orphaned caches**: a reassigned letter invalidates the entire hash cache and
   forces the library to be re-hashed from scratch.
2. **Cross-drive collisions**: a different physical drive later assigned the old
   letter can inherit a stale cache that belongs to another volume.

This module obtains a stable volume identity via ``kernel32`` (`GetVolumeInformationW`
and `GetVolumeNameForVolumeMountPointW`), preferring the volume GUID, then the
volume serial number, and finally degrading gracefully to a sanitised path
fallback when running on non-Windows platforms or when OS queries fail.

Filesystem timestamp granularity and size limits
------------------------------------------------
* **FAT32 / FAT16 / FAT**: modification timestamps (``mtime``) have a coarse
  **2-second** resolution, and individual files cannot exceed **4 GiB minus 1 byte**
  (``4,294,967,295`` bytes).
* **exFAT**: removes the 4 GiB file-size limit, but the base directory-entry
  modification timestamp is still stored in 2-second increments (with an optional
  10 ms field that is not consistently populated or preserved across devices,
  cameras, and drivers). Because of this coarse ``mtime`` granularity on FAT32
  and exFAT, a file modified or overwritten within the same timestamp tick can
  retain an identical ``(size, mtime)`` pair — which is why a cache keyed solely
  on ``mtime`` can miss an in-place change unless callers account for filesystem
  precision.
* **NTFS / ReFS**: preserve sub-second ``mtime`` precision (100-nanosecond ticks)
  and do not have the FAT 4 GiB per-file limit.
"""

from __future__ import annotations

import ctypes
import os
import re
import threading
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any


IDENTITY_TIER_GUID = "guid"
IDENTITY_TIER_SERIAL = "serial"
IDENTITY_TIER_PATH = "path"

FAT_MAX_FILE_BYTES = (1 << 32) - 1  # 4 GiB - 1 byte (4,294,967,295 bytes)
FAT32_MTIME_GRANULARITY_SECONDS = 2.0
EXFAT_MTIME_GRANULARITY_SECONDS = 2.0
NTFS_MTIME_GRANULARITY_SECONDS = 1e-7

_FAT_4GIB_LIMIT_FILESYSTEMS = frozenset({"fat", "fat12", "fat16", "fat32", "vfat", "msdos"})
_COARSE_MTIME_FILESYSTEMS = frozenset({"fat", "fat12", "fat16", "fat32", "vfat", "msdos", "exfat"})
_SUBSECOND_MTIME_FILESYSTEMS = frozenset({"ntfs", "refs", "apfs", "ext4", "xfs", "btrfs", "zfs"})

_CACHE_LOCK = threading.RLock()
_PROBE_CACHE: dict[str, "VolumeIdentity"] = {}


def _clean_guid(guid: str) -> str:
    """Normalise a Windows volume GUID path into a filename-safe identifier."""
    text = (guid or "").strip()
    if not text:
        return ""
    match = re.search(r"\{([0-9a-fA-F-]+)\}", text)
    if match:
        cleaned = match.group(1).strip("-_.").lower()
    else:
        cleaned = re.sub(r"^\\\\\?\\volume", "", text, flags=re.IGNORECASE)
        cleaned = cleaned.strip("\\/{} ")
        cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", cleaned).strip("._-").lower()
    return f"vol_{cleaned}" if cleaned else ""


def _clean_serial(serial: str) -> str:
    """Normalise a volume serial string into a filename-safe identifier."""
    text = (serial or "").strip()
    if not text:
        return ""
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", text).strip("._-").lower()
    return f"ser_{cleaned}" if cleaned else ""


def sanitize_path_fallback(path: str | os.PathLike[str]) -> str:
    """Sanitise a mount point or path the same way legacy drive cache names did."""
    raw = os.fspath(path).strip() if path is not None else ""
    if not raw:
        return "root"
    if os.name == "nt" or (not re.match(r"^[A-Za-z]:", raw) and not raw.startswith("\\\\")):
        if re.fullmatch(r"[A-Za-z]:", raw):
            raw = f"{raw}\\"
        raw = os.path.abspath(raw)
    safe = raw.replace("\\", "_").replace("/", "_").replace(":", "").strip("_")
    return safe or "root"


def resolve_mount_point(path: str | os.PathLike[str]) -> str:
    """Resolve the volume root / mount point for ``path`` (including nested paths).

    A folder deep inside a drive (e.g. ``E:\\Photos\\2024\\Vacation``) resolves
    to the same volume mount point (``E:\\``) as the drive root itself.
    """
    raw = os.fspath(path).strip() if path is not None else ""
    if not raw:
        raw = os.getcwd()
    drive_match = re.match(r"^([A-Za-z]):(?:[\\/](.*))?$", raw)
    if drive_match:
        return f"{drive_match.group(1).upper()}:\\"
    drive, _tail = os.path.splitdrive(raw)
    if drive:
        if len(drive) == 2 and drive[1] == ":":
            return f"{drive[0].upper()}:\\"
        return drive.rstrip("\\/") + "\\"
    abs_path = os.path.abspath(raw)
    drive, _tail = os.path.splitdrive(abs_path)
    if drive:
        if len(drive) == 2 and drive[1] == ":":
            return f"{drive[0].upper()}:\\"
        return drive.rstrip("\\/") + "\\"
    return "/"


def relative_path_from_mount(
    path: str | os.PathLike[str],
    mount_point: str | None = None,
) -> str:
    """Return the path of ``path`` relative to its volume mount point.

    Returns ``""`` when ``path`` refers to the volume root itself.
    """
    raw = os.fspath(path).strip() if path is not None else ""
    if not raw:
        return ""
    resolved_mount = mount_point or resolve_mount_point(raw)
    drive_match = re.match(r"^([A-Za-z]):(?:[\\/](.*))?$", raw)
    if drive_match:
        tail = drive_match.group(2) or ""
        normalized_tail = os.path.normpath(tail.replace("/", "\\")) if tail else ""
        if normalized_tail in {"", ".", "\\", "/"}:
            return ""
        parts = [
            part
            for part in normalized_tail.replace("/", "\\").split("\\")
            if part and part not in {".", ".."}
        ]
        return "\\".join(parts)
    if raw.startswith("\\\\"):
        drive, tail = os.path.splitdrive(raw)
        if drive:
            normalized_tail = os.path.normpath(tail.replace("/", "\\")) if tail else ""
            if normalized_tail in {"", ".", "\\", "/"}:
                return ""
            parts = [
                part
                for part in normalized_tail.replace("/", "\\").split("\\")
                if part and part not in {".", ".."}
            ]
            return "\\".join(parts)
    try:
        abs_path = os.path.abspath(raw)
        abs_mount = os.path.abspath(resolved_mount)
        rel = os.path.relpath(abs_path, abs_mount)
    except ValueError:
        return ""
    if rel in {"", "."} or rel.startswith(".."):
        return ""
    return rel


def _extract_filesystem_name(filesystem_or_identity: str | "VolumeIdentity" | None) -> str:
    if isinstance(filesystem_or_identity, VolumeIdentity):
        return (filesystem_or_identity.filesystem or "").strip().lower()
    if isinstance(filesystem_or_identity, str):
        return filesystem_or_identity.strip().lower()
    return ""


def preserves_subsecond_mtime(filesystem_or_identity: str | "VolumeIdentity" | None) -> bool:
    """Return ``True`` only when the filesystem reliably preserves sub-second ``mtime``.

    FAT32 has a 2-second modification timestamp granularity and exFAT uses a
    2-second base timestamp (whose optional 10 ms increment is not reliably
    preserved across drivers and devices). As a result, a cache keyed on
    ``mtime`` can miss an in-place modification that happens within the same
    coarse timestamp window on FAT32 or exFAT. NTFS and ReFS preserve 100 ns
    sub-second precision.
    """
    fs = _extract_filesystem_name(filesystem_or_identity)
    if not fs or fs in _COARSE_MTIME_FILESYSTEMS:
        return False
    return fs in _SUBSECOND_MTIME_FILESYSTEMS


supports_subsecond_mtime = preserves_subsecond_mtime


def has_fat_4gib_limit(filesystem_or_identity: str | "VolumeIdentity" | None) -> bool:
    """Return ``True`` when the filesystem enforces the FAT 4 GiB per-file limit.

    FAT32 (and FAT12/FAT16) stores file sizes in an unsigned 32-bit field and
    therefore rejects any file larger than ``4,294,967,295`` bytes (4 GiB - 1).
    exFAT and NTFS use 64-bit file sizes and do not have this limit.
    """
    fs = _extract_filesystem_name(filesystem_or_identity)
    return fs in _FAT_4GIB_LIMIT_FILESYSTEMS


has_4gib_file_limit = has_fat_4gib_limit


def mtime_granularity_seconds(filesystem_or_identity: str | "VolumeIdentity" | None) -> float:
    """Return the conservative ``mtime`` resolution in seconds for a filesystem."""
    fs = _extract_filesystem_name(filesystem_or_identity)
    if fs in _SUBSECOND_MTIME_FILESYSTEMS:
        return NTFS_MTIME_GRANULARITY_SECONDS
    return FAT32_MTIME_GRANULARITY_SECONDS


@dataclass(frozen=True)
class VolumeIdentity:
    """Stable identity and filesystem metadata for a local storage volume."""

    guid: str = ""
    serial: str = ""
    filesystem: str = ""
    label: str = ""
    mount_point: str = ""
    tier: str = ""
    is_fallback: bool = False

    def __post_init__(self) -> None:
        cleaned_guid = _clean_guid(self.guid)
        cleaned_serial = _clean_serial(self.serial)
        if cleaned_guid:
            resolved_tier = IDENTITY_TIER_GUID
        elif cleaned_serial:
            resolved_tier = IDENTITY_TIER_SERIAL
        else:
            resolved_tier = IDENTITY_TIER_PATH
        object.__setattr__(self, "tier", resolved_tier)
        object.__setattr__(self, "is_fallback", resolved_tier == IDENTITY_TIER_PATH)

    @property
    def stable_key(self) -> str:
        """Return a filename-safe key preferring GUID, then serial, then path fallback."""
        cleaned_guid = _clean_guid(self.guid)
        if cleaned_guid:
            return cleaned_guid
        cleaned_serial = _clean_serial(self.serial)
        if cleaned_serial:
            return cleaned_serial
        return sanitize_path_fallback(self.mount_point)

    @property
    def is_path_fallback(self) -> bool:
        """Return ``True`` when neither GUID nor serial was available."""
        return self.tier == IDENTITY_TIER_PATH

    @property
    def used_fallback(self) -> bool:
        """Alias for :attr:`is_path_fallback`."""
        return self.tier == IDENTITY_TIER_PATH

    @property
    def is_strong(self) -> bool:
        """Return ``True`` when backed by a hardware/volume GUID or serial number."""
        return self.tier in {IDENTITY_TIER_GUID, IDENTITY_TIER_SERIAL}

    @property
    def identity_tier(self) -> str:
        """Return the tier used for :attr:`stable_key` (``guid``, ``serial``, or ``path``)."""
        return self.tier

    @property
    def preserves_subsecond_mtime(self) -> bool:
        """Return ``True`` if this volume's filesystem preserves sub-second ``mtime``."""
        return preserves_subsecond_mtime(self.filesystem)

    @property
    def supports_subsecond_mtime(self) -> bool:
        """Alias for :attr:`preserves_subsecond_mtime`."""
        return preserves_subsecond_mtime(self.filesystem)

    @property
    def has_fat_4gib_limit(self) -> bool:
        """Return ``True`` if this volume's filesystem has the FAT 4 GiB file-size limit."""
        return has_fat_4gib_limit(self.filesystem)

    @property
    def has_4gib_file_limit(self) -> bool:
        """Alias for :attr:`has_fat_4gib_limit`."""
        return has_fat_4gib_limit(self.filesystem)

    def to_dict(self) -> dict[str, Any]:
        return {
            "guid": self.guid,
            "serial": self.serial,
            "filesystem": self.filesystem,
            "label": self.label,
            "mount_point": self.mount_point,
            "tier": self.tier,
            "is_fallback": self.is_fallback,
            "stable_key": self.stable_key,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "VolumeIdentity":
        if not isinstance(payload, Mapping):
            return cls()
        return cls(
            guid=str(payload.get("guid", "") or ""),
            serial=str(payload.get("serial", "") or ""),
            filesystem=str(payload.get("filesystem", "") or ""),
            label=str(payload.get("label", "") or ""),
            mount_point=str(payload.get("mount_point", "") or ""),
        )


def _mount_cache_key(mount_point: str) -> str:
    return os.path.normcase(mount_point.strip()) if mount_point else ""


def clear_probe_cache() -> None:
    """Clear the in-process volume identity probe cache."""
    with _CACHE_LOCK:
        _PROBE_CACHE.clear()


clear_volume_probe_cache = clear_probe_cache


def probe_volume(
    path: str | os.PathLike[str],
    *,
    kernel32: Any = None,
    use_cache: bool = True,
) -> VolumeIdentity:
    """Probe the OS for the volume identity backing ``path``.

    Uses ``ctypes`` against ``kernel32`` (`GetVolumeInformationW` and
    `GetVolumeNameForVolumeMountPointW`) on Windows, resolving nested folder or
    file paths to their volume mount point first so any path inside a drive
    yields the same :class:`VolumeIdentity` as the drive root.

    Never raises: degrades gracefully to the path fallback on non-Windows
    platforms and on any ``OSError``, ``AttributeError``, or ``ctypes`` failure.
    """
    try:
        mount_point = resolve_mount_point(path)
    except Exception:
        mount_point = str(path or "")

    cache_key = _mount_cache_key(mount_point)
    if use_cache and kernel32 is None and cache_key:
        with _CACHE_LOCK:
            cached = _PROBE_CACHE.get(cache_key)
            if cached is not None:
                return cached

    guid = ""
    serial = ""
    filesystem = ""
    label = ""

    if os.name == "nt" or kernel32 is not None:
        try:
            k32 = (
                kernel32
                if kernel32 is not None
                else getattr(getattr(ctypes, "windll", None), "kernel32", None)
            )
        except Exception:
            k32 = None

        if k32 is not None:
            try:
                name_buffer = ctypes.create_unicode_buffer(261)
                serial_number = ctypes.c_uint(0)
                maximum_component = ctypes.c_uint(0)
                flags = ctypes.c_uint(0)
                fs_buffer = ctypes.create_unicode_buffer(261)
                ok = k32.GetVolumeInformationW(
                    mount_point,
                    name_buffer,
                    len(name_buffer),
                    ctypes.byref(serial_number),
                    ctypes.byref(maximum_component),
                    ctypes.byref(flags),
                    fs_buffer,
                    len(fs_buffer),
                )
                if ok:
                    if serial_number.value:
                        serial = f"{serial_number.value:08X}"
                    label = (name_buffer.value or "").strip()
                    filesystem = (fs_buffer.value or "").strip()
            except Exception:
                pass

            try:
                guid_buffer = ctypes.create_unicode_buffer(261)
                if k32.GetVolumeNameForVolumeMountPointW(
                    mount_point,
                    guid_buffer,
                    len(guid_buffer),
                ):
                    raw_guid = (guid_buffer.value or "").strip()
                    if raw_guid:
                        guid = raw_guid.rstrip("\\").casefold()
            except Exception:
                pass

    identity = VolumeIdentity(
        guid=guid,
        serial=serial,
        filesystem=filesystem,
        label=label,
        mount_point=mount_point,
    )
    if use_cache and kernel32 is None and cache_key:
        with _CACHE_LOCK:
            _PROBE_CACHE[cache_key] = identity
    return identity


probe_volume.cache_clear = clear_probe_cache  # type: ignore[attr-defined]


def is_volume_online(
    identity_or_path: VolumeIdentity | str | os.PathLike[str],
    *,
    kernel32: Any = None,
) -> bool:
    """Return ``True`` if the storage volume is currently mounted and accessible.

    Data-safety contract (offline vs. deleted):
    Callers MUST distinguish "the volume is not currently mounted" from "the
    file was deleted". When an external USB/SSD/HDD drive is unplugged, every
    path on that volume ceases to exist in the filesystem namespace, which looks
    superficially identical to an empty directory or deleted files. Treating a
    disconnected volume as deleted media would invalidate cached hashes, mark
    entire libraries stale, or cause downstream sync/cleanup logic to treat user
    media as removed.

    * When passed a :class:`VolumeIdentity` with a strong identity (GUID or
      serial), this helper probes the volume's mount point fresh (bypassing the
      in-process cache) and confirms both that the mount point responds and that
      the mounted volume still matches the expected GUID/serial rather than a
      different drive reassigned to the same letter.
    * When passed a path (including the path of a deleted file on an online
      drive), it checks the **volume mount point** rather than the individual
      file path so callers can tell a deleted file on an online volume
      (``True``) apart from an unmounted volume (``False``).
    """
    if isinstance(identity_or_path, VolumeIdentity):
        identity = identity_or_path
        mount_point = identity.mount_point
        if not mount_point:
            return False
        current = probe_volume(mount_point, kernel32=kernel32, use_cache=False)
        if identity.is_strong:
            if current.is_fallback:
                return False
            if identity.guid and current.guid:
                return _clean_guid(identity.guid) == _clean_guid(current.guid)
            if identity.serial and current.serial:
                return _clean_serial(identity.serial) == _clean_serial(current.serial)
            return False
        return os.path.exists(mount_point)

    try:
        raw_path = os.fspath(identity_or_path).strip() if identity_or_path is not None else ""
    except TypeError:
        return False
    if not raw_path:
        return False

    try:
        mount_point = resolve_mount_point(raw_path)
    except Exception:
        return False
    if not mount_point:
        return False

    current = probe_volume(mount_point, kernel32=kernel32, use_cache=False)
    if current.is_strong:
        return True
    if os.name == "nt" or kernel32 is not None:
        return False
    return os.path.exists(mount_point)


__all__ = [
    "EXFAT_MTIME_GRANULARITY_SECONDS",
    "FAT32_MTIME_GRANULARITY_SECONDS",
    "FAT_MAX_FILE_BYTES",
    "IDENTITY_TIER_GUID",
    "IDENTITY_TIER_PATH",
    "IDENTITY_TIER_SERIAL",
    "NTFS_MTIME_GRANULARITY_SECONDS",
    "VolumeIdentity",
    "clear_probe_cache",
    "clear_volume_probe_cache",
    "has_4gib_file_limit",
    "has_fat_4gib_limit",
    "is_volume_online",
    "mtime_granularity_seconds",
    "preserves_subsecond_mtime",
    "probe_volume",
    "relative_path_from_mount",
    "resolve_mount_point",
    "sanitize_path_fallback",
    "supports_subsecond_mtime",
]
