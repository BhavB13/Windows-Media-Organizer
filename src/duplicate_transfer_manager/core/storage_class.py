"""Classify storage media and cloud-placeholder residency before reading files.

This module inspects path storage types (fixed, removable, network/UNC, CD-ROM,
RAM disk) and per-file residency (local, cloud placeholder that recalls on
access, offline, other reparse point, or unknown) using Win32 metadata APIs
via ``ctypes`` and the Python standard library only.

Safety contract:
- Never opens any file for reading content, so inspecting a cloud placeholder
  cannot trigger the silent hydration/download this module exists to warn about.
- Never follows directory reparse points / junctions during tree walks.
- Total and non-raising: vanished files, permission errors, paths exceeding
  ``MAX_PATH``, non-Windows platforms, and missing ``ctypes`` symbols all return
  ``UNKNOWN`` classifications with a human-readable ``reason``.
"""

from __future__ import annotations

import ctypes
import os
import stat
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from enum import Enum
from typing import Any, Protocol


MAX_PATH: int = 260
INVALID_FILE_ATTRIBUTES: int = 0xFFFFFFFF

# Win32 GetDriveTypeW return codes
DRIVE_UNKNOWN: int = 0
DRIVE_NO_ROOT_DIR: int = 1
DRIVE_REMOVABLE: int = 2
DRIVE_FIXED: int = 3
DRIVE_REMOTE: int = 4
DRIVE_CDROM: int = 5
DRIVE_RAMDISK: int = 6

# Win32 File Attribute flags (winnt.h)
FILE_ATTRIBUTE_READONLY: int = 0x00000001
FILE_ATTRIBUTE_HIDDEN: int = 0x00000002
FILE_ATTRIBUTE_SYSTEM: int = 0x00000004
FILE_ATTRIBUTE_DIRECTORY: int = 0x00000010
FILE_ATTRIBUTE_ARCHIVE: int = 0x00000020
FILE_ATTRIBUTE_DEVICE: int = 0x00000040
FILE_ATTRIBUTE_NORMAL: int = 0x00000080
FILE_ATTRIBUTE_TEMPORARY: int = 0x00000100
FILE_ATTRIBUTE_SPARSE_FILE: int = 0x00000200
FILE_ATTRIBUTE_REPARSE_POINT: int = 0x00000400
FILE_ATTRIBUTE_COMPRESSED: int = 0x00000800
FILE_ATTRIBUTE_OFFLINE: int = 0x00001000
FILE_ATTRIBUTE_NOT_CONTENT_INDEXED: int = 0x00002000
FILE_ATTRIBUTE_ENCRYPTED: int = 0x00004000
FILE_ATTRIBUTE_INTEGRITY_STREAM: int = 0x00008000
FILE_ATTRIBUTE_VIRTUAL: int = 0x00010000
FILE_ATTRIBUTE_NO_SCRUB_DATA: int = 0x00020000
FILE_ATTRIBUTE_RECALL_ON_OPEN: int = 0x00040000
FILE_ATTRIBUTE_PINNED: int = 0x00080000
FILE_ATTRIBUTE_UNPINNED: int = 0x00100000
FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS: int = 0x00400000

# Win32 Reparse Point tags (winnt.h)
IO_REPARSE_TAG_MOUNT_POINT: int = 0xA0000003
IO_REPARSE_TAG_HSM: int = 0xC0000004
IO_REPARSE_TAG_HSM2: int = 0x80000006
IO_REPARSE_TAG_SIS: int = 0x80000007
IO_REPARSE_TAG_WIM: int = 0x80000008
IO_REPARSE_TAG_CSV: int = 0x80000009
IO_REPARSE_TAG_DFS: int = 0x8000000A
IO_REPARSE_TAG_SYMLINK: int = 0xA000000C
IO_REPARSE_TAG_DFSR: int = 0x80000012
IO_REPARSE_TAG_DEDUP: int = 0x80000013
IO_REPARSE_TAG_NFS: int = 0x80000014
IO_REPARSE_TAG_FILE_PLACEHOLDER: int = 0x80000015
IO_REPARSE_TAG_WOF: int = 0x80000017
IO_REPARSE_TAG_WCI: int = 0x80000018
IO_REPARSE_TAG_GLOBAL_REPARSE: int = 0xA0000019
IO_REPARSE_TAG_CLOUD: int = 0x9000001A
IO_REPARSE_TAG_CLOUD_1: int = 0x9000101A
IO_REPARSE_TAG_CLOUD_2: int = 0x9000201A
IO_REPARSE_TAG_CLOUD_3: int = 0x9000301A
IO_REPARSE_TAG_CLOUD_4: int = 0x9000401A
IO_REPARSE_TAG_CLOUD_5: int = 0x9000501A
IO_REPARSE_TAG_CLOUD_6: int = 0x9000601A
IO_REPARSE_TAG_CLOUD_7: int = 0x9000701A
IO_REPARSE_TAG_CLOUD_8: int = 0x9000801A
IO_REPARSE_TAG_CLOUD_9: int = 0x9000901A
IO_REPARSE_TAG_CLOUD_A: int = 0x9000A01A
IO_REPARSE_TAG_CLOUD_B: int = 0x9000B01A
IO_REPARSE_TAG_CLOUD_C: int = 0x9000C01A
IO_REPARSE_TAG_CLOUD_D: int = 0x9000D01A
IO_REPARSE_TAG_CLOUD_E: int = 0x9000E01A
IO_REPARSE_TAG_CLOUD_F: int = 0x9000F01A
IO_REPARSE_TAG_CLOUD_MASK: int = 0x0000F000
IO_REPARSE_TAG_APPEXECLINK: int = 0x8000001B
IO_REPARSE_TAG_PROJFS: int = 0x9000001C
IO_REPARSE_TAG_STORAGE_SYNC: int = 0x8000001E
IO_REPARSE_TAG_ONEDRIVE: int = 0x80000021

PROVIDER_ONEDRIVE: str = "OneDrive"
PROVIDER_DROPBOX: str = "Dropbox"
PROVIDER_GOOGLE_DRIVE: str = "Google Drive"
PROVIDER_ICLOUD: str = "iCloud"
UNKNOWN_PROVIDER: str = "unknown provider"


class StopEventLike(Protocol):
    """Protocol matching ``threading.Event`` and ``CancellationToken``."""

    def is_set(self) -> bool: ...


class DriveType(str, Enum):
    """Storage drive classification from Win32 ``GetDriveTypeW`` and UNC checks."""

    FIXED = "fixed"
    REMOVABLE = "removable"
    REMOTE = "remote"
    CDROM = "cdrom"
    RAMDISK = "ramdisk"
    UNKNOWN = "unknown"

    # Aliases for caller convenience
    NETWORK = "remote"
    CD_ROM = "cdrom"
    RAM_DISK = "ramdisk"


class Residency(str, Enum):
    """File residency state determined strictly from filesystem metadata."""

    LOCAL = "local"
    PLACEHOLDER_RECALL_ON_ACCESS = "placeholder_recall_on_access"
    OFFLINE = "offline"
    REPARSE_OTHER = "reparse_other"
    UNKNOWN = "unknown"


_DRIVE_TYPE_MAP: dict[int, DriveType] = {
    DRIVE_UNKNOWN: DriveType.UNKNOWN,
    DRIVE_NO_ROOT_DIR: DriveType.UNKNOWN,
    DRIVE_REMOVABLE: DriveType.REMOVABLE,
    DRIVE_FIXED: DriveType.FIXED,
    DRIVE_REMOTE: DriveType.REMOTE,
    DRIVE_CDROM: DriveType.CDROM,
    DRIVE_RAMDISK: DriveType.RAMDISK,
}


class _FILETIME(ctypes.Structure):
    _fields_ = [
        ("dwLowDateTime", ctypes.c_uint32),
        ("dwHighDateTime", ctypes.c_uint32),
    ]


class _WIN32_FIND_DATAW(ctypes.Structure):
    _fields_ = [
        ("dwFileAttributes", ctypes.c_uint32),
        ("ftCreationTime", _FILETIME),
        ("ftLastAccessTime", _FILETIME),
        ("ftLastWriteTime", _FILETIME),
        ("nFileSizeHigh", ctypes.c_uint32),
        ("nFileSizeLow", ctypes.c_uint32),
        ("dwReserved0", ctypes.c_uint32),
        ("dwReserved1", ctypes.c_uint32),
        ("cFileName", ctypes.c_wchar * MAX_PATH),
        ("cAlternateFileName", ctypes.c_wchar * 14),
    ]


@dataclass(frozen=True)
class StorageClassification:
    """Storage medium classification for a filesystem path."""

    path: str
    root: str
    drive_type: DriveType
    is_unc: bool = False
    reason: str = ""

    @property
    def is_remote(self) -> bool:
        return self.drive_type is DriveType.REMOTE or self.is_unc

    @property
    def is_removable(self) -> bool:
        return self.drive_type is DriveType.REMOVABLE

    @property
    def is_fixed(self) -> bool:
        return self.drive_type is DriveType.FIXED


@dataclass(frozen=True)
class FileClassification:
    """Metadata-only residency and storage classification for a single path.

    Attributes:
        path: Inspected filesystem path.
        residency: Resolved :class:`Residency` enum value.
        would_download_on_read: Unambiguous boolean gate for callers.
            ``True`` if and only if opening and reading the file's content bytes
            would trigger a recall/download from remote or cloud storage —
            specifically when ``residency`` is
            :attr:`Residency.PLACEHOLDER_RECALL_ON_ACCESS` or
            :attr:`Residency.OFFLINE` (caused by ``FILE_ATTRIBUTE_OFFLINE``,
            ``FILE_ATTRIBUTE_RECALL_ON_OPEN``,
            ``FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS``, or
            ``FILE_ATTRIBUTE_REPARSE_POINT`` paired with a cloud reparse tag).
            ``False`` for local files (:attr:`Residency.LOCAL`), non-cloud
            reparse points (:attr:`Residency.REPARSE_OTHER`), directories, and
            entries whose metadata could not be read (:attr:`Residency.UNKNOWN`).
        provider: Cloud sync provider name (``"OneDrive"``, ``"Dropbox"``,
            ``"Google Drive"``, ``"iCloud"``) when knowable from the path or
            reparse tag, or ``"unknown provider"`` otherwise.
        drive_type: Storage drive type for ``path``.
        raw_attributes: Raw Win32 ``DWORD`` attribute mask, or ``None`` if
            attributes could not be read.
        reparse_tag: Win32 reparse tag (``0`` if not a reparse point or
            unavailable).
        size_bytes: Logical file size in bytes from directory metadata (``0``
            for directories or when unavailable).
        is_directory: ``True`` when the path is a directory.
        is_reparse_point: ``True`` when ``FILE_ATTRIBUTE_REPARSE_POINT`` is set.
        is_reparse_directory: ``True`` when the path is a directory reparse
            point / junction that tree walks must report and not follow.
        reason: Human-readable explanation, always populated when ``residency``
            is :attr:`Residency.UNKNOWN` or when reporting a directory reparse
            point.
    """

    path: str
    residency: Residency
    would_download_on_read: bool
    provider: str = UNKNOWN_PROVIDER
    drive_type: DriveType = DriveType.UNKNOWN
    raw_attributes: int | None = None
    reparse_tag: int = 0
    size_bytes: int = 0
    is_directory: bool = False
    is_reparse_point: bool = False
    is_reparse_directory: bool = False
    reason: str = ""

    @property
    def sync_provider(self) -> str:
        return self.provider

    @property
    def attributes(self) -> int | None:
        return self.raw_attributes

    @property
    def size(self) -> int:
        return self.size_bytes


ResidencyClassification = FileClassification


@dataclass(frozen=True)
class StorageSurveyResult:
    """Summary returned by a metadata-only batch walk of a directory tree.

    Attributes:
        root: Root path surveyed.
        drive_type: Drive classification for ``root``.
        is_unc: ``True`` when ``root`` is a UNC network path.
        local_files: Count of files with :attr:`Residency.LOCAL`.
        recall_files: Count of files with ``would_download_on_read == True``.
        recall_bytes: Total logical bytes across all ``recall_files`` that
            reading them all would pull from remote/cloud storage.
        local_bytes: Total logical bytes across all ``local_files``.
        offline_files: Count of files with :attr:`Residency.OFFLINE`
            (included in ``recall_files``).
        reparse_other_files: Count of files with :attr:`Residency.REPARSE_OTHER`.
        unknown_files: Count of files or unreadable entries with
            :attr:`Residency.UNKNOWN`.
        reparse_directories: Paths of directory reparse points / junctions
            discovered and skipped (not followed) during the walk.
        unknown_entries: Detailed :class:`FileClassification` records for all
            ``UNKNOWN`` items encountered.
        providers: Distinct sync providers identified among ``recall_files``.
        directories_scanned: Count of non-reparse directories traversed.
        cancelled: ``True`` if ``stop_event.is_set()`` ended the walk early.
        reason: Stated reason when the root itself could not be surveyed.
    """

    root: str
    drive_type: DriveType = DriveType.UNKNOWN
    is_unc: bool = False
    local_files: int = 0
    recall_files: int = 0
    recall_bytes: int = 0
    local_bytes: int = 0
    offline_files: int = 0
    reparse_other_files: int = 0
    unknown_files: int = 0
    reparse_directories: tuple[str, ...] = ()
    unknown_entries: tuple[FileClassification, ...] = ()
    providers: tuple[str, ...] = ()
    directories_scanned: int = 0
    cancelled: bool = False
    reason: str = ""

    @property
    def local_count(self) -> int:
        return self.local_files

    @property
    def would_download_files(self) -> int:
        return self.recall_files

    @property
    def would_download_count(self) -> int:
        return self.recall_files

    @property
    def would_download_bytes(self) -> int:
        return self.recall_bytes

    @property
    def total_recall_bytes(self) -> int:
        return self.recall_bytes

    @property
    def download_bytes(self) -> int:
        return self.recall_bytes

    @property
    def unknown_count(self) -> int:
        return self.unknown_files

    @property
    def skipped_reparse_dirs(self) -> tuple[str, ...]:
        return self.reparse_directories

    @property
    def total_files(self) -> int:
        return (
            self.local_files
            + self.recall_files
            + self.reparse_other_files
            + self.unknown_files
        )


BatchSurveyResult = StorageSurveyResult


def _default_is_windows() -> bool:
    return os.name == "nt"


_is_windows: Callable[[], bool] = _default_is_windows


def _get_kernel32() -> Any:
    windll = getattr(ctypes, "windll", None)
    if windll is None:
        raise AttributeError("Missing ctypes.windll on this platform")
    kernel32 = getattr(windll, "kernel32", None)
    if kernel32 is None:
        raise AttributeError("Missing ctypes.windll.kernel32")
    return kernel32


def _win32_get_drive_type(root_path: str) -> int:
    if not _is_windows():
        raise OSError("Non-Windows platform: GetDriveTypeW is unavailable")
    kernel32 = _get_kernel32()
    fn = getattr(kernel32, "GetDriveTypeW", None)
    if fn is None:
        raise AttributeError("Missing ctypes symbol: kernel32.GetDriveTypeW")
    return int(fn(str(root_path)))


def _win32_get_file_attributes(path: str) -> int:
    if not _is_windows():
        raise OSError("Non-Windows platform: GetFileAttributesW is unavailable")
    kernel32 = _get_kernel32()
    fn = getattr(kernel32, "GetFileAttributesW", None)
    if fn is None:
        raise AttributeError("Missing ctypes symbol: kernel32.GetFileAttributesW")
    raw = int(fn(str(path)))
    if raw < 0:
        return INVALID_FILE_ATTRIBUTES
    return raw & 0xFFFFFFFF


def _win32_get_reparse_tag(path: str) -> int:
    if not _is_windows():
        raise OSError("Non-Windows platform: FindFirstFileW is unavailable")
    kernel32 = _get_kernel32()
    find_first = getattr(kernel32, "FindFirstFileW", None)
    find_close = getattr(kernel32, "FindClose", None)
    if find_first is None:
        raise AttributeError("Missing ctypes symbol: kernel32.FindFirstFileW")
    if find_close is None:
        raise AttributeError("Missing ctypes symbol: kernel32.FindClose")

    try:
        find_first.argtypes = [ctypes.c_wchar_p, ctypes.POINTER(_WIN32_FIND_DATAW)]
        find_first.restype = ctypes.c_void_p
        find_close.argtypes = [ctypes.c_void_p]
        find_close.restype = ctypes.c_int
    except (AttributeError, TypeError):
        pass

    query_path = str(path)
    if len(query_path) > 3 and query_path.endswith(("\\", "/")):
        query_path = query_path.rstrip("\\/")

    find_data = _WIN32_FIND_DATAW()
    handle = find_first(query_path, ctypes.byref(find_data))
    invalid_handle = ctypes.c_void_p(-1).value
    if handle is None or handle == invalid_handle or handle == -1:
        # Fallback for drive roots where FindFirstFileW cannot open a parent entry
        try:
            st = os.lstat(path)
            return int(getattr(st, "st_reparse_tag", 0)) & 0xFFFFFFFF
        except OSError as exc:
            raise OSError(f"FindFirstFileW failed for {path!r}: {exc}") from exc

    try:
        if find_data.dwFileAttributes & FILE_ATTRIBUTE_REPARSE_POINT:
            return int(find_data.dwReserved0) & 0xFFFFFFFF
        return 0
    finally:
        try:
            find_close(ctypes.c_void_p(handle))
        except Exception:
            pass


def _win32_get_file_size(path: str) -> int:
    st = os.lstat(path)
    if stat.S_ISDIR(st.st_mode):
        return 0
    return max(0, int(st.st_size))


# Module-level hooks that tests may monkeypatch or override via injection.
_get_drive_type: Callable[[str], int] = _win32_get_drive_type
_get_file_attributes: Callable[[str], int] = _win32_get_file_attributes
_get_reparse_tag: Callable[[str], int] = _win32_get_reparse_tag
_get_file_size: Callable[[str], int] = _win32_get_file_size


def _coerce_path_str(path: str | os.PathLike[str] | Any) -> tuple[str | None, str]:
    if path is None:
        return None, "Path is None"
    try:
        raw = os.fspath(path)
    except TypeError as exc:
        return None, f"Invalid path object: {exc}"
    if isinstance(raw, bytes):
        try:
            raw = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            return None, f"Invalid bytes path encoding: {exc}"
    if not isinstance(raw, str):
        return None, f"Unsupported path type: {type(raw).__name__}"
    stripped = raw.strip()
    if not stripped:
        return None, "Path is empty"
    if "\x00" in raw:
        return None, "Path contains null byte"
    return raw, ""


def _parse_unc_root(path_str: str) -> str | None:
    """Return the ``\\\\server\\share\\`` root if *path_str* is a UNC path."""
    normalized = path_str.replace("/", "\\")
    upper = normalized.upper()
    if upper.startswith("\\\\?\\UNC\\"):
        remainder = normalized[8:]
    elif normalized.startswith("\\\\") and not normalized.startswith("\\\\?\\") and not normalized.startswith("\\\\.\\"):
        remainder = normalized[2:]
    else:
        return None

    parts = [part for part in remainder.split("\\") if part]
    if len(parts) < 2:
        return f"\\\\{parts[0]}\\" if parts else "\\\\"
    return f"\\\\{parts[0]}\\{parts[1]}\\"


def _extract_drive_root(path_str: str) -> tuple[str, bool, str]:
    """Return ``(root, is_unc, error_reason)`` for *path_str*."""
    unc_root = _parse_unc_root(path_str)
    if unc_root is not None:
        parts = [p for p in unc_root.strip("\\").split("\\") if p]
        if len(parts) < 2:
            return unc_root, True, f"Incomplete UNC path (missing share): {path_str}"
        return unc_root, True, ""

    normalized = path_str.replace("/", "\\")
    if normalized.startswith("\\\\?\\") or normalized.startswith("\\\\.\\"):
        normalized = normalized[4:]

    if len(normalized) >= 2 and normalized[0].isalpha() and normalized[1] == ":":
        drive_letter = normalized[0].upper()
        return f"{drive_letter}:\\", False, ""

    try:
        abs_path = os.path.abspath(path_str)
    except (OSError, ValueError) as exc:
        return "", False, f"Cannot resolve path root: {exc}"

    unc_abs = _parse_unc_root(abs_path)
    if unc_abs is not None:
        return unc_abs, True, ""

    abs_norm = abs_path.replace("/", "\\")
    if len(abs_norm) >= 2 and abs_norm[0].isalpha() and abs_norm[1] == ":":
        return f"{abs_norm[0].upper()}:\\", False, ""

    return "", False, f"Path has no recognizable Windows drive or UNC root: {path_str}"


def is_cloud_reparse_tag(reparse_tag: int) -> bool:
    """Return ``True`` when *reparse_tag* identifies a cloud placeholder tag."""
    try:
        tag = int(reparse_tag) & 0xFFFFFFFF
    except (TypeError, ValueError):
        return False
    if tag == 0:
        return False
    if (tag & ~IO_REPARSE_TAG_CLOUD_MASK) == IO_REPARSE_TAG_CLOUD:
        return True
    return tag in {
        IO_REPARSE_TAG_FILE_PLACEHOLDER,
        IO_REPARSE_TAG_ONEDRIVE,
        IO_REPARSE_TAG_STORAGE_SYNC,
    }


def identify_sync_provider(
    path: str | os.PathLike[str] = "",
    reparse_tag: int = 0,
) -> str:
    """Identify the cloud sync provider from *path* or *reparse_tag*.

    Recognises ``"OneDrive"``, ``"Dropbox"``, ``"Google Drive"``, and
    ``"iCloud"`` when the path segments or a provider-specific reparse tag make
    it knowable. Returns ``"unknown provider"`` rather than guessing when
    neither reveals the provider.
    """
    path_str, _ = _coerce_path_str(path)
    if path_str:
        normalized = path_str.replace("/", "\\")
        segments = [seg.strip() for seg in normalized.split("\\") if seg.strip()]
        lowered = [seg.casefold() for seg in segments]

        for idx, seg in enumerate(lowered):
            # OneDrive
            if (
                seg == "onedrive"
                or seg.startswith("onedrive -")
                or seg.startswith("onedrive ")
                or seg.startswith("onedrive_")
                or seg in {"onedrivetemp", "skydrive"}
            ):
                return PROVIDER_ONEDRIVE

            # Dropbox
            if (
                seg == "dropbox"
                or seg.startswith("dropbox (")
                or seg.startswith("dropbox -")
                or seg.startswith("dropbox ")
            ):
                return PROVIDER_DROPBOX

            # Google Drive
            if (
                seg in {"google drive", "googledrive", "google drivefs"}
                or seg.startswith("google drive (")
                or seg.startswith("google drive -")
                or seg.startswith("google drive ")
            ):
                return PROVIDER_GOOGLE_DRIVE
            if seg == "google" and idx + 1 < len(lowered) and lowered[idx + 1] in {"drive", "drivefs"}:
                return PROVIDER_GOOGLE_DRIVE

            # iCloud
            if (
                seg in {
                    "icloud",
                    "icloud drive",
                    "iclouddrive",
                    "icloud photos",
                    "icloudsharedalbums",
                }
                or seg.startswith("icloud drive ")
                or seg.startswith("icloud (")
            ):
                return PROVIDER_ICLOUD

    try:
        tag = int(reparse_tag) & 0xFFFFFFFF
    except (TypeError, ValueError):
        tag = 0

    if tag in {IO_REPARSE_TAG_ONEDRIVE, IO_REPARSE_TAG_FILE_PLACEHOLDER}:
        return PROVIDER_ONEDRIVE

    return UNKNOWN_PROVIDER


def classify_path_storage(
    path: str | os.PathLike[str],
    *,
    get_drive_type: Callable[[str], int] | None = None,
    backend: Any | None = None,
    open_hook: Callable[..., Any] | None = None,
    platform_is_windows: bool | None = None,
) -> StorageClassification:
    """Classify the storage medium of *path* without opening or reading files.

    UNC paths (``\\\\server\\share\\...``) are classified as
    :attr:`DriveType.REMOTE` even though they have no drive letter. Never
    raises; returns :attr:`DriveType.UNKNOWN` with a non-empty ``reason`` on
    any error.
    """
    _ = open_hook  # Intentionally unused: metadata-only inspection never opens files.
    if get_drive_type is None and backend is not None:
        get_drive_type = getattr(backend, "get_drive_type", None)

    path_str, err = _coerce_path_str(path)
    if path_str is None:
        return StorageClassification(
            path="",
            root="",
            drive_type=DriveType.UNKNOWN,
            is_unc=False,
            reason=err,
        )

    using_default_win32 = get_drive_type is None and _get_drive_type is _win32_get_drive_type
    if platform_is_windows is False or (
        (_is_windows is not _default_is_windows or using_default_win32) and not _is_windows()
    ):
        return StorageClassification(
            path=path_str,
            root="",
            drive_type=DriveType.UNKNOWN,
            is_unc=False,
            reason="Non-Windows platform: Win32 storage classification requires Windows",
        )

    if len(path_str) >= MAX_PATH:
        return StorageClassification(
            path=path_str,
            root="",
            drive_type=DriveType.UNKNOWN,
            is_unc=False,
            reason=f"Path length ({len(path_str)}) reaches or exceeds MAX_PATH ({MAX_PATH})",
        )

    root, is_unc, root_err = _extract_drive_root(path_str)
    if root_err:
        return StorageClassification(
            path=path_str,
            root=root,
            drive_type=DriveType.UNKNOWN,
            is_unc=is_unc,
            reason=root_err,
        )

    if is_unc:
        return StorageClassification(
            path=path_str,
            root=root,
            drive_type=DriveType.REMOTE,
            is_unc=True,
            reason="UNC network path",
        )

    drive_type_fn = get_drive_type or _get_drive_type
    try:
        raw_code = int(drive_type_fn(root))
    except Exception as exc:
        return StorageClassification(
            path=path_str,
            root=root,
            drive_type=DriveType.UNKNOWN,
            is_unc=False,
            reason=f"GetDriveTypeW failed for {root!r}: {type(exc).__name__}: {exc}",
        )

    mapped = _DRIVE_TYPE_MAP.get(raw_code, DriveType.UNKNOWN)
    if mapped is DriveType.UNKNOWN:
        if raw_code == DRIVE_UNKNOWN:
            reason = f"GetDriveTypeW returned DRIVE_UNKNOWN (0) for {root!r}"
        elif raw_code == DRIVE_NO_ROOT_DIR:
            reason = f"GetDriveTypeW returned DRIVE_NO_ROOT_DIR (1) for {root!r}"
        else:
            reason = f"GetDriveTypeW returned unrecognized code ({raw_code}) for {root!r}"
        return StorageClassification(
            path=path_str,
            root=root,
            drive_type=DriveType.UNKNOWN,
            is_unc=False,
            reason=reason,
        )

    return StorageClassification(
        path=path_str,
        root=root,
        drive_type=mapped,
        is_unc=False,
        reason="",
    )


classify_storage = classify_path_storage


def classify_drive_type(
    path: str | os.PathLike[str],
    *,
    get_drive_type: Callable[[str], int] | None = None,
    platform_is_windows: bool | None = None,
) -> DriveType:
    """Return the :class:`DriveType` enum for *path*."""
    return classify_path_storage(
        path,
        get_drive_type=get_drive_type,
        platform_is_windows=platform_is_windows,
    ).drive_type


def classify_attributes(
    raw_attributes: int,
    reparse_tag: int = 0,
    *,
    path: str | os.PathLike[str] = "",
    size_bytes: int = 0,
    drive_type: DriveType = DriveType.UNKNOWN,
    is_directory_hint: bool = False,
) -> FileClassification:
    """Resolve a Win32 attribute mask and reparse tag into a :class:`FileClassification`.

    Precedence rules:
    1. Invalid attributes (``0xFFFFFFFF`` or negative) -> :attr:`Residency.UNKNOWN`,
       ``would_download_on_read=False``.
    2. Directory with ``FILE_ATTRIBUTE_REPARSE_POINT`` -> :attr:`Residency.REPARSE_OTHER`,
       ``is_reparse_directory=True``, ``would_download_on_read=False``.
    3. ``FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS`` (``0x400000``) or
       ``FILE_ATTRIBUTE_RECALL_ON_OPEN`` (``0x40000``) ->
       :attr:`Residency.PLACEHOLDER_RECALL_ON_ACCESS`, ``would_download_on_read=True``.
    4. ``FILE_ATTRIBUTE_REPARSE_POINT`` (``0x400``) + cloud reparse tag ->
       :attr:`Residency.PLACEHOLDER_RECALL_ON_ACCESS`, ``would_download_on_read=True``.
    5. ``FILE_ATTRIBUTE_OFFLINE`` (``0x1000``) ->
       :attr:`Residency.OFFLINE`, ``would_download_on_read=True``.
    6. ``FILE_ATTRIBUTE_REPARSE_POINT`` (``0x400``) with a non-cloud tag ->
       :attr:`Residency.REPARSE_OTHER`, ``would_download_on_read=False``.
    7. Otherwise -> :attr:`Residency.LOCAL`, ``would_download_on_read=False``.
    """
    path_str, _ = _coerce_path_str(path)
    resolved_path = path_str or ""
    provider = identify_sync_provider(resolved_path, reparse_tag)

    try:
        attrs = int(raw_attributes)
    except (TypeError, ValueError) as exc:
        return FileClassification(
            path=resolved_path,
            residency=Residency.UNKNOWN,
            would_download_on_read=False,
            provider=provider,
            drive_type=drive_type,
            raw_attributes=None,
            reparse_tag=0,
            size_bytes=0,
            reason=f"Invalid file attribute value: {exc}",
        )

    if attrs < 0 or (attrs & 0xFFFFFFFF) == INVALID_FILE_ATTRIBUTES:
        return FileClassification(
            path=resolved_path,
            residency=Residency.UNKNOWN,
            would_download_on_read=False,
            provider=provider,
            drive_type=drive_type,
            raw_attributes=None,
            reparse_tag=0,
            size_bytes=0,
            reason="Win32 GetFileAttributesW returned INVALID_FILE_ATTRIBUTES (0xFFFFFFFF)",
        )

    attrs &= 0xFFFFFFFF
    tag = int(reparse_tag) & 0xFFFFFFFF
    is_dir = bool(attrs & FILE_ATTRIBUTE_DIRECTORY) or bool(is_directory_hint)
    is_reparse = bool(attrs & FILE_ATTRIBUTE_REPARSE_POINT)
    is_reparse_dir = is_dir and is_reparse
    safe_size = 0 if is_dir else max(0, int(size_bytes))

    if is_reparse_dir:
        return FileClassification(
            path=resolved_path,
            residency=Residency.REPARSE_OTHER,
            would_download_on_read=False,
            provider=provider,
            drive_type=drive_type,
            raw_attributes=attrs,
            reparse_tag=tag,
            size_bytes=0,
            is_directory=True,
            is_reparse_point=True,
            is_reparse_directory=True,
            reason="Directory reparse point / junction; do not follow",
        )

    if is_dir:
        return FileClassification(
            path=resolved_path,
            residency=Residency.LOCAL,
            would_download_on_read=False,
            provider=provider,
            drive_type=drive_type,
            raw_attributes=attrs,
            reparse_tag=tag,
            size_bytes=0,
            is_directory=True,
            is_reparse_point=False,
            is_reparse_directory=False,
            reason="",
        )

    has_recall_flag = bool(
        attrs & (FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS | FILE_ATTRIBUTE_RECALL_ON_OPEN)
    )
    has_cloud_tag = is_reparse and is_cloud_reparse_tag(tag)

    if has_recall_flag or has_cloud_tag:
        return FileClassification(
            path=resolved_path,
            residency=Residency.PLACEHOLDER_RECALL_ON_ACCESS,
            would_download_on_read=True,
            provider=provider,
            drive_type=drive_type,
            raw_attributes=attrs,
            reparse_tag=tag,
            size_bytes=safe_size,
            is_directory=False,
            is_reparse_point=is_reparse,
            is_reparse_directory=False,
            reason="",
        )

    if attrs & FILE_ATTRIBUTE_OFFLINE:
        return FileClassification(
            path=resolved_path,
            residency=Residency.OFFLINE,
            would_download_on_read=True,
            provider=provider,
            drive_type=drive_type,
            raw_attributes=attrs,
            reparse_tag=tag,
            size_bytes=safe_size,
            is_directory=False,
            is_reparse_point=is_reparse,
            is_reparse_directory=False,
            reason="",
        )

    if is_reparse:
        return FileClassification(
            path=resolved_path,
            residency=Residency.REPARSE_OTHER,
            would_download_on_read=False,
            provider=provider,
            drive_type=drive_type,
            raw_attributes=attrs,
            reparse_tag=tag,
            size_bytes=safe_size,
            is_directory=False,
            is_reparse_point=True,
            is_reparse_directory=False,
            reason="",
        )

    return FileClassification(
        path=resolved_path,
        residency=Residency.LOCAL,
        would_download_on_read=False,
        provider=provider,
        drive_type=drive_type,
        raw_attributes=attrs,
        reparse_tag=tag,
        size_bytes=safe_size,
        is_directory=False,
        is_reparse_point=False,
        is_reparse_directory=False,
        reason="",
    )


def _is_dir_no_follow(path_str: str) -> tuple[bool, int | None, str]:
    """Return ``(is_dir, size_bytes_or_none, error_reason)`` via ``os.lstat`` without following links."""
    try:
        st = os.lstat(path_str)
    except OSError as exc:
        return False, None, f"{type(exc).__name__}: {exc}"
    is_dir = stat.S_ISDIR(st.st_mode)
    size = 0 if is_dir else max(0, int(st.st_size))
    return is_dir, size, ""


def classify_file(
    path: str | os.PathLike[str],
    *,
    get_file_attributes: Callable[[str], int] | None = None,
    get_reparse_tag: Callable[[str], int] | None = None,
    get_file_size: Callable[[str], int] | None = None,
    get_drive_type: Callable[[str], int] | None = None,
    backend: Any | None = None,
    open_hook: Callable[..., Any] | None = None,
    platform_is_windows: bool | None = None,
    require_stat: bool = False,
) -> FileClassification:
    """Classify a file or directory's residency using metadata only.

    Never opens the file for reading and never raises. If the file vanished,
    permissions are denied, the path reaches or exceeds ``MAX_PATH``, the
    platform is not Windows, or a ``ctypes`` symbol fails, returns
    :attr:`Residency.UNKNOWN` with ``would_download_on_read=False`` and a
    populated ``reason``.
    """
    _ = open_hook  # Intentionally unused: metadata-only inspection never opens files.
    if backend is not None:
        if get_file_attributes is None:
            get_file_attributes = getattr(backend, "get_file_attributes", None)
        if get_reparse_tag is None:
            get_reparse_tag = getattr(backend, "get_reparse_tag", None)
        if get_file_size is None:
            get_file_size = getattr(backend, "get_file_size", None)
        if get_drive_type is None:
            get_drive_type = getattr(backend, "get_drive_type", None)

    path_str, err = _coerce_path_str(path)
    if path_str is None:
        return FileClassification(
            path="",
            residency=Residency.UNKNOWN,
            would_download_on_read=False,
            provider=UNKNOWN_PROVIDER,
            drive_type=DriveType.UNKNOWN,
            reason=err,
        )

    provider_from_path = identify_sync_provider(path_str, 0)

    using_default_win32 = (
        get_file_attributes is None and _get_file_attributes is _win32_get_file_attributes
    )
    if platform_is_windows is False or (
        (_is_windows is not _default_is_windows or using_default_win32) and not _is_windows()
    ):
        return FileClassification(
            path=path_str,
            residency=Residency.UNKNOWN,
            would_download_on_read=False,
            provider=provider_from_path,
            drive_type=DriveType.UNKNOWN,
            reason="Non-Windows platform: Win32 storage classification requires Windows",
        )

    if len(path_str) >= MAX_PATH:
        return FileClassification(
            path=path_str,
            residency=Residency.UNKNOWN,
            would_download_on_read=False,
            provider=provider_from_path,
            drive_type=DriveType.UNKNOWN,
            reason=f"Path length ({len(path_str)}) reaches or exceeds MAX_PATH ({MAX_PATH})",
        )

    storage_info = classify_path_storage(
        path_str,
        get_drive_type=get_drive_type,
        platform_is_windows=platform_is_windows,
    )
    drive_type = storage_info.drive_type

    attr_fn = get_file_attributes or _get_file_attributes
    try:
        raw_attrs = int(attr_fn(path_str))
    except Exception as exc:
        return FileClassification(
            path=path_str,
            residency=Residency.UNKNOWN,
            would_download_on_read=False,
            provider=provider_from_path,
            drive_type=drive_type,
            reason=f"Could not read file attributes for {path_str!r}: {type(exc).__name__}: {exc}",
        )

    if raw_attrs < 0 or (raw_attrs & 0xFFFFFFFF) == INVALID_FILE_ATTRIBUTES:
        # Probe os.lstat solely to produce a more specific reason (e.g. vanished vs permission)
        _, _, stat_err = _is_dir_no_follow(path_str)
        detail = f" ({stat_err})" if stat_err else ""
        return FileClassification(
            path=path_str,
            residency=Residency.UNKNOWN,
            would_download_on_read=False,
            provider=provider_from_path,
            drive_type=drive_type,
            reason=f"Win32 GetFileAttributesW returned INVALID_FILE_ATTRIBUTES for {path_str!r}{detail}",
        )

    attrs = raw_attrs & 0xFFFFFFFF

    # Check directory status and size from lstat when available on disk
    lstat_is_dir, lstat_size, lstat_err = _is_dir_no_follow(path_str)
    if require_stat and lstat_size is None and get_file_size is None:
        return FileClassification(
            path=path_str,
            residency=Residency.UNKNOWN,
            would_download_on_read=False,
            provider=provider_from_path,
            drive_type=drive_type,
            reason=f"File vanished or metadata became unreadable for {path_str!r}: {lstat_err}",
        )

    is_dir = bool(attrs & FILE_ATTRIBUTE_DIRECTORY) or lstat_is_dir

    reparse_tag = 0
    if attrs & FILE_ATTRIBUTE_REPARSE_POINT:
        tag_fn = get_reparse_tag or _get_reparse_tag
        custom_tag_fn = get_reparse_tag is not None or _get_reparse_tag is not _win32_get_reparse_tag
        try:
            reparse_tag = int(tag_fn(path_str)) & 0xFFFFFFFF
        except Exception as exc:
            if custom_tag_fn or using_default_win32 or isinstance(exc, (AttributeError, ctypes.ArgumentError)):
                return FileClassification(
                    path=path_str,
                    residency=Residency.UNKNOWN,
                    would_download_on_read=False,
                    provider=provider_from_path,
                    drive_type=drive_type,
                    reason=f"Could not read reparse tag for {path_str!r}: {type(exc).__name__}: {exc}",
                )
            reparse_tag = 0

    size_bytes = 0
    if not is_dir:
        if get_file_size is not None or _get_file_size is not _win32_get_file_size:
            size_fn = get_file_size or _get_file_size
            try:
                size_bytes = max(0, int(size_fn(path_str)))
            except Exception as exc:
                return FileClassification(
                    path=path_str,
                    residency=Residency.UNKNOWN,
                    would_download_on_read=False,
                    provider=identify_sync_provider(path_str, reparse_tag),
                    drive_type=drive_type,
                    reason=f"Could not read file size for {path_str!r}: {type(exc).__name__}: {exc}",
                )
        elif lstat_size is not None:
            size_bytes = lstat_size

    return classify_attributes(
        attrs,
        reparse_tag,
        path=path_str,
        size_bytes=size_bytes,
        drive_type=drive_type,
        is_directory_hint=is_dir,
    )


classify_residency = classify_file


def survey_directory(
    root: str | os.PathLike[str],
    stop_event: StopEventLike | None = None,
    *,
    get_file_attributes: Callable[[str], int] | None = None,
    get_reparse_tag: Callable[[str], int] | None = None,
    get_file_size: Callable[[str], int] | None = None,
    get_drive_type: Callable[[str], int] | None = None,
    scandir_fn: Callable[[str], Any] | None = None,
    backend: Any | None = None,
    open_hook: Callable[..., Any] | None = None,
    platform_is_windows: bool | None = None,
) -> StorageSurveyResult:
    """Walk *root* without opening files or following directory reparse points.

    Returns a :class:`StorageSurveyResult` detailing how many files are local,
    how many would trigger a download on read, the total bytes that reading
    them all would pull, any directory junctions/reparse points skipped, and
    any ``UNKNOWN`` entries encountered. Checks ``stop_event.is_set()`` on
    every directory and entry and returns promptly when set.
    """
    _ = open_hook  # Intentionally unused: metadata-only walk never opens files.
    if backend is not None:
        if get_file_attributes is None:
            get_file_attributes = getattr(backend, "get_file_attributes", None)
        if get_reparse_tag is None:
            get_reparse_tag = getattr(backend, "get_reparse_tag", None)
        if get_file_size is None:
            get_file_size = getattr(backend, "get_file_size", None)
        if get_drive_type is None:
            get_drive_type = getattr(backend, "get_drive_type", None)
        if scandir_fn is None:
            scandir_fn = getattr(backend, "scandir", None)

    path_str, err = _coerce_path_str(root)
    if path_str is None:
        unknown_root = FileClassification(
            path="",
            residency=Residency.UNKNOWN,
            would_download_on_read=False,
            reason=err,
        )
        return StorageSurveyResult(
            root="",
            drive_type=DriveType.UNKNOWN,
            unknown_files=1,
            unknown_entries=(unknown_root,),
            reason=err,
        )

    storage_info = classify_path_storage(
        path_str,
        get_drive_type=get_drive_type,
        platform_is_windows=platform_is_windows,
    )

    if stop_event is not None and stop_event.is_set():
        return StorageSurveyResult(
            root=path_str,
            drive_type=storage_info.drive_type,
            is_unc=storage_info.is_unc,
            cancelled=True,
        )

    root_classification = classify_file(
        path_str,
        get_file_attributes=get_file_attributes,
        get_reparse_tag=get_reparse_tag,
        get_file_size=get_file_size,
        get_drive_type=get_drive_type,
        platform_is_windows=platform_is_windows,
        require_stat=True,
    )

    if root_classification.residency is Residency.UNKNOWN:
        return StorageSurveyResult(
            root=path_str,
            drive_type=storage_info.drive_type,
            is_unc=storage_info.is_unc,
            unknown_files=1,
            unknown_entries=(root_classification,),
            reason=root_classification.reason,
        )

    if root_classification.is_reparse_directory:
        return StorageSurveyResult(
            root=path_str,
            drive_type=storage_info.drive_type,
            is_unc=storage_info.is_unc,
            reparse_directories=(path_str,),
            reason=root_classification.reason,
        )

    if not root_classification.is_directory:
        local_files = 1 if root_classification.residency is Residency.LOCAL else 0
        local_bytes = root_classification.size_bytes if local_files else 0
        recall_files = 1 if root_classification.would_download_on_read else 0
        recall_bytes = root_classification.size_bytes if recall_files else 0
        offline_files = 1 if root_classification.residency is Residency.OFFLINE else 0
        reparse_other = 1 if root_classification.residency is Residency.REPARSE_OTHER else 0
        providers = (
            (root_classification.provider,)
            if recall_files and root_classification.provider != UNKNOWN_PROVIDER
            else ()
        )
        return StorageSurveyResult(
            root=path_str,
            drive_type=storage_info.drive_type,
            is_unc=storage_info.is_unc,
            local_files=local_files,
            recall_files=recall_files,
            recall_bytes=recall_bytes,
            local_bytes=local_bytes,
            offline_files=offline_files,
            reparse_other_files=reparse_other,
            providers=providers,
        )

    local_files = 0
    local_bytes = 0
    recall_files = 0
    recall_bytes = 0
    offline_files = 0
    reparse_other_files = 0
    unknown_files = 0
    reparse_directories: list[str] = []
    unknown_entries: list[FileClassification] = []
    providers_seen: list[str] = []
    directories_scanned = 0
    cancelled = False

    scanner = scandir_fn or os.scandir
    stack: list[str] = [path_str]
    visited_dirs: set[str] = set()

    while stack:
        if stop_event is not None and stop_event.is_set():
            cancelled = True
            break

        current_dir = stack.pop()
        norm_dir = os.path.normcase(os.path.abspath(current_dir))
        if norm_dir in visited_dirs:
            continue
        visited_dirs.add(norm_dir)
        directories_scanned += 1

        subdirs: list[str] = []
        try:
            dir_context = scanner(current_dir)
        except Exception as exc:
            unknown_item = FileClassification(
                path=current_dir,
                residency=Residency.UNKNOWN,
                would_download_on_read=False,
                provider=identify_sync_provider(current_dir, 0),
                drive_type=storage_info.drive_type,
                is_directory=True,
                reason=f"Cannot open directory {current_dir!r}: {type(exc).__name__}: {exc}",
            )
            unknown_files += 1
            unknown_entries.append(unknown_item)
            continue

        try:
            with dir_context as entries_iter:
                for entry in entries_iter:
                    if stop_event is not None and stop_event.is_set():
                        cancelled = True
                        break

                    try:
                        entry_path = str(entry.path)
                    except Exception as exc:
                        unknown_item = FileClassification(
                            path=current_dir,
                            residency=Residency.UNKNOWN,
                            would_download_on_read=False,
                            provider=identify_sync_provider(current_dir, 0),
                            drive_type=storage_info.drive_type,
                            reason=f"Unreadable directory entry in {current_dir!r}: {type(exc).__name__}: {exc}",
                        )
                        unknown_files += 1
                        unknown_entries.append(unknown_item)
                        continue

                    item_class = classify_file(
                        entry_path,
                        get_file_attributes=get_file_attributes,
                        get_reparse_tag=get_reparse_tag,
                        get_file_size=get_file_size,
                        get_drive_type=get_drive_type,
                        platform_is_windows=platform_is_windows,
                        require_stat=True,
                    )

                    if item_class.residency is Residency.UNKNOWN:
                        unknown_files += 1
                        unknown_entries.append(item_class)
                        continue

                    if item_class.is_reparse_directory:
                        reparse_directories.append(entry_path)
                        continue

                    if item_class.is_directory:
                        subdirs.append(entry_path)
                        continue

                    if item_class.would_download_on_read:
                        recall_files += 1
                        recall_bytes += item_class.size_bytes
                        if item_class.residency is Residency.OFFLINE:
                            offline_files += 1
                        if item_class.provider not in providers_seen:
                            providers_seen.append(item_class.provider)
                    elif item_class.residency is Residency.LOCAL:
                        local_files += 1
                        local_bytes += item_class.size_bytes
                    elif item_class.residency is Residency.REPARSE_OTHER:
                        reparse_other_files += 1
        except Exception as exc:
            unknown_item = FileClassification(
                path=current_dir,
                residency=Residency.UNKNOWN,
                would_download_on_read=False,
                provider=identify_sync_provider(current_dir, 0),
                drive_type=storage_info.drive_type,
                is_directory=True,
                reason=f"Error while iterating directory {current_dir!r}: {type(exc).__name__}: {exc}",
            )
            unknown_files += 1
            unknown_entries.append(unknown_item)

        if cancelled:
            break

        for subdir in reversed(subdirs):
            stack.append(subdir)

    return StorageSurveyResult(
        root=path_str,
        drive_type=storage_info.drive_type,
        is_unc=storage_info.is_unc,
        local_files=local_files,
        recall_files=recall_files,
        recall_bytes=recall_bytes,
        local_bytes=local_bytes,
        offline_files=offline_files,
        reparse_other_files=reparse_other_files,
        unknown_files=unknown_files,
        reparse_directories=tuple(reparse_directories),
        unknown_entries=tuple(unknown_entries),
        providers=tuple(providers_seen),
        directories_scanned=directories_scanned,
        cancelled=cancelled,
    )


survey_tree = survey_directory


__all__ = [
    "BatchSurveyResult",
    "DRIVE_CDROM",
    "DRIVE_FIXED",
    "DRIVE_NO_ROOT_DIR",
    "DRIVE_RAMDISK",
    "DRIVE_REMOTE",
    "DRIVE_REMOVABLE",
    "DRIVE_UNKNOWN",
    "DriveType",
    "FILE_ATTRIBUTE_ARCHIVE",
    "FILE_ATTRIBUTE_COMPRESSED",
    "FILE_ATTRIBUTE_DEVICE",
    "FILE_ATTRIBUTE_DIRECTORY",
    "FILE_ATTRIBUTE_ENCRYPTED",
    "FILE_ATTRIBUTE_HIDDEN",
    "FILE_ATTRIBUTE_INTEGRITY_STREAM",
    "FILE_ATTRIBUTE_NORMAL",
    "FILE_ATTRIBUTE_NOT_CONTENT_INDEXED",
    "FILE_ATTRIBUTE_NO_SCRUB_DATA",
    "FILE_ATTRIBUTE_OFFLINE",
    "FILE_ATTRIBUTE_PINNED",
    "FILE_ATTRIBUTE_READONLY",
    "FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS",
    "FILE_ATTRIBUTE_RECALL_ON_OPEN",
    "FILE_ATTRIBUTE_REPARSE_POINT",
    "FILE_ATTRIBUTE_SPARSE_FILE",
    "FILE_ATTRIBUTE_SYSTEM",
    "FILE_ATTRIBUTE_TEMPORARY",
    "FILE_ATTRIBUTE_UNPINNED",
    "FILE_ATTRIBUTE_VIRTUAL",
    "FileClassification",
    "INVALID_FILE_ATTRIBUTES",
    "IO_REPARSE_TAG_APPEXECLINK",
    "IO_REPARSE_TAG_CLOUD",
    "IO_REPARSE_TAG_CLOUD_1",
    "IO_REPARSE_TAG_CLOUD_2",
    "IO_REPARSE_TAG_CLOUD_3",
    "IO_REPARSE_TAG_CLOUD_4",
    "IO_REPARSE_TAG_CLOUD_5",
    "IO_REPARSE_TAG_CLOUD_6",
    "IO_REPARSE_TAG_CLOUD_7",
    "IO_REPARSE_TAG_CLOUD_8",
    "IO_REPARSE_TAG_CLOUD_9",
    "IO_REPARSE_TAG_CLOUD_A",
    "IO_REPARSE_TAG_CLOUD_B",
    "IO_REPARSE_TAG_CLOUD_C",
    "IO_REPARSE_TAG_CLOUD_D",
    "IO_REPARSE_TAG_CLOUD_E",
    "IO_REPARSE_TAG_CLOUD_F",
    "IO_REPARSE_TAG_CLOUD_MASK",
    "IO_REPARSE_TAG_CSV",
    "IO_REPARSE_TAG_DEDUP",
    "IO_REPARSE_TAG_DFS",
    "IO_REPARSE_TAG_DFSR",
    "IO_REPARSE_TAG_FILE_PLACEHOLDER",
    "IO_REPARSE_TAG_GLOBAL_REPARSE",
    "IO_REPARSE_TAG_HSM",
    "IO_REPARSE_TAG_HSM2",
    "IO_REPARSE_TAG_MOUNT_POINT",
    "IO_REPARSE_TAG_NFS",
    "IO_REPARSE_TAG_ONEDRIVE",
    "IO_REPARSE_TAG_PROJFS",
    "IO_REPARSE_TAG_SIS",
    "IO_REPARSE_TAG_STORAGE_SYNC",
    "IO_REPARSE_TAG_SYMLINK",
    "IO_REPARSE_TAG_WCI",
    "IO_REPARSE_TAG_WIM",
    "IO_REPARSE_TAG_WOF",
    "MAX_PATH",
    "PROVIDER_DROPBOX",
    "PROVIDER_GOOGLE_DRIVE",
    "PROVIDER_ICLOUD",
    "PROVIDER_ONEDRIVE",
    "Residency",
    "ResidencyClassification",
    "StopEventLike",
    "StorageClassification",
    "StorageSurveyResult",
    "UNKNOWN_PROVIDER",
    "classify_attributes",
    "classify_drive_type",
    "classify_file",
    "classify_path_storage",
    "classify_residency",
    "classify_storage",
    "identify_sync_provider",
    "is_cloud_reparse_tag",
    "survey_directory",
    "survey_tree",
]
