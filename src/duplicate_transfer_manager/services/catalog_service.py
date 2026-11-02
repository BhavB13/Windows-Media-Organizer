"""Framework-neutral catalog indexing, search, and storage analytics service."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date, datetime
import math
import os
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Mapping, Sequence

from utils import DEFAULT_EXCLUDES, is_hidden_or_system, normalize_excludes
from volume_identity import (
    VolumeIdentity,
    is_volume_online,
    probe_volume,
    relative_path_from_mount,
    sanitize_path_fallback,
)

from ..core import (
    CancellationToken,
    ErrorCode,
    OperationPhase,
    OperationReporter,
    OperationResult,
    OperationState,
    ServiceError,
)
from ..index import (
    AnalyticsBucket,
    CatalogFileInput,
    CatalogFileRecord,
    MediaCatalog,
    MediaMetadata,
    SearchFilter,
    StopEventProtocol,
    StorageAnalytics,
    UpsertResult,
    VolumeRecord,
    classify_media_kind,
    compute_file_sha256,
    extract_file_metadata,
    normalize_relative_path,
)
from ..runtime_paths import RuntimePaths, get_runtime_paths
from ..sorting.metadata import MetadataExtractor

CATALOG_FILENAME = "library_catalog.sqlite3"
FILE_ATTRIBUTE_REPARSE_POINT = 0x400
DEFAULT_PAGE_SIZE = 50


def _is_cancelled(stop_event: StopEventProtocol | None) -> bool:
    return bool(stop_event is not None and stop_event.is_set())


def default_catalog_path(paths: RuntimePaths | str | os.PathLike[str] | None = None) -> Path:
    """Return the default SQLite catalog database path under application runtime paths."""
    if isinstance(paths, RuntimePaths):
        runtime = paths
    elif paths is not None:
        candidate = Path(paths).expanduser()
        if candidate.suffix.lower() in {".db", ".sqlite", ".sqlite3"} or (
            candidate.exists() and candidate.is_file()
        ):
            candidate.parent.mkdir(parents=True, exist_ok=True)
            return candidate
        runtime = get_runtime_paths(candidate)
    else:
        runtime = get_runtime_paths()
    runtime.cache.mkdir(parents=True, exist_ok=True)
    return runtime.cache / CATALOG_FILENAME


@dataclass(frozen=True)
class CatalogIndexSettings:
    """Configuration for a read-only folder indexing operation."""

    folder_path: str
    volume_key: str = ""
    label: str = ""
    filesystem: str = ""
    compute_hashes: bool = False
    skip_hidden_system: bool = True
    exclude_dirs: tuple[str, ...] = tuple(DEFAULT_EXCLUDES)


@dataclass(frozen=True)
class CatalogIndexResult:
    """Plain summary of an incremental folder indexing run."""

    volume_key: str
    root_path: str
    added: int = 0
    updated: int = 0
    unchanged: int = 0
    removed: int = 0
    volume_status: str = "online"
    cancelled: bool = False
    compute_hashes: bool = False
    writes_performed: int = 0
    warnings: tuple[str, ...] = ()

    @property
    def offline(self) -> bool:
        return self.volume_status == "offline"

    @property
    def is_offline(self) -> bool:
        return self.volume_status == "offline"

    @property
    def total_files(self) -> int:
        return self.added + self.updated + self.unchanged

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["offline"] = self.offline
        payload["total_files"] = self.total_files
        return payload

    def __getitem__(self, key: str) -> Any:
        if hasattr(self, key):
            return getattr(self, key)
        raise KeyError(key)


@dataclass(frozen=True)
class CatalogBucket:
    """Plain grouped aggregate row with count and byte totals."""

    key: str | int | None
    file_count: int
    total_bytes: int
    label: str = ""
    is_online: bool = True
    status: str = "online"

    @property
    def count(self) -> int:
        return self.file_count

    @property
    def bytes(self) -> int:
        return self.total_bytes

    @property
    def offline(self) -> bool:
        return not self.is_online or self.status == "offline"

    @property
    def is_offline(self) -> bool:
        return self.offline

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "label": self.label or (str(self.key) if self.key is not None else "Unknown"),
            "file_count": self.file_count,
            "count": self.file_count,
            "total_bytes": self.total_bytes,
            "bytes": self.total_bytes,
            "is_online": self.is_online,
            "status": self.status,
            "offline": self.offline,
        }

    def __getitem__(self, item: str) -> Any:
        if hasattr(self, item):
            return getattr(self, item)
        raise KeyError(item)


@dataclass(frozen=True)
class CatalogVolumeSummary:
    """Plain storage and online/offline status summary for a cataloged volume."""

    volume_key: str
    label: str = ""
    filesystem: str = ""
    root_path: str = ""
    is_online: bool = True
    status: str = "online"
    last_seen: float = 0.0
    file_count: int = 0
    total_bytes: int = 0

    @property
    def key(self) -> str:
        return self.volume_key

    @property
    def count(self) -> int:
        return self.file_count

    @property
    def bytes(self) -> int:
        return self.total_bytes

    @property
    def offline(self) -> bool:
        return not self.is_online or self.status == "offline"

    @property
    def is_offline(self) -> bool:
        return self.offline

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.volume_key,
            "volume_key": self.volume_key,
            "label": self.label or self.volume_key,
            "filesystem": self.filesystem,
            "root_path": self.root_path,
            "is_online": self.is_online,
            "status": self.status,
            "offline": self.offline,
            "is_offline": self.offline,
            "last_seen": self.last_seen,
            "file_count": self.file_count,
            "count": self.file_count,
            "total_bytes": self.total_bytes,
            "bytes": self.total_bytes,
        }

    def __getitem__(self, item: str) -> Any:
        if hasattr(self, item):
            return getattr(self, item)
        raise KeyError(item)


@dataclass(frozen=True)
class CatalogStorageAnalytics:
    """Plain storage analytics summary across the catalog."""

    total_files: int
    total_bytes: int
    by_media_kind: tuple[CatalogBucket, ...]
    by_volume: tuple[CatalogVolumeSummary, ...]
    by_capture_year: tuple[CatalogBucket, ...]
    by_extension: tuple[CatalogBucket, ...]
    recoverable_duplicate_bytes: int | None
    duplicate_analytics_available: bool = False
    recoverable_duplicate_status: str = "unavailable"
    hashed_files: int = 0
    unhashed_files: int = 0
    volumes: tuple[CatalogVolumeSummary, ...] = ()
    offline_volumes: tuple[CatalogVolumeSummary, ...] = ()

    @property
    def duplicates_available(self) -> bool:
        return self.duplicate_analytics_available

    @property
    def duplicate_status(self) -> str:
        return self.recoverable_duplicate_status

    @property
    def has_offline_volumes(self) -> bool:
        return bool(self.offline_volumes)

    def to_dict(self) -> dict[str, Any]:
        return {
            "total_files": self.total_files,
            "total_bytes": self.total_bytes,
            "by_media_kind": [bucket.to_dict() for bucket in self.by_media_kind],
            "by_volume": [vol.to_dict() for vol in self.by_volume],
            "by_capture_year": [bucket.to_dict() for bucket in self.by_capture_year],
            "by_extension": [bucket.to_dict() for bucket in self.by_extension],
            "recoverable_duplicate_bytes": self.recoverable_duplicate_bytes,
            "duplicate_analytics_available": self.duplicate_analytics_available,
            "duplicates_available": self.duplicate_analytics_available,
            "recoverable_duplicate_status": self.recoverable_duplicate_status,
            "duplicate_status": self.recoverable_duplicate_status,
            "hashed_files": self.hashed_files,
            "unhashed_files": self.unhashed_files,
            "volumes": [vol.to_dict() for vol in self.volumes],
            "offline_volumes": [vol.to_dict() for vol in self.offline_volumes],
            "has_offline_volumes": self.has_offline_volumes,
        }

    def __getitem__(self, item: str) -> Any:
        if hasattr(self, item):
            return getattr(self, item)
        raise KeyError(item)


@dataclass(frozen=True)
class CatalogSearchItem:
    """Plain search result row independent of SQLite."""

    id: int
    volume_key: str
    relative_path: str
    filename: str
    extension: str
    size: int
    mtime_ns: int
    content_hash: str | None
    media_kind: str
    indexed_at: float
    volume_online: bool = True
    volume_status: str = "online"
    volume_label: str = ""
    volume_root: str = ""
    capture_timestamp: float | None = None
    capture_date: str | None = None
    capture_year: int | None = None
    camera_make: str | None = None
    camera_model: str | None = None
    width: int | None = None
    height: int | None = None

    @property
    def is_offline(self) -> bool:
        return not self.volume_online or self.volume_status == "offline"

    @property
    def offline(self) -> bool:
        return self.is_offline

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["is_offline"] = self.is_offline
        payload["offline"] = self.offline
        return payload

    def __getitem__(self, item: str) -> Any:
        if hasattr(self, item):
            return getattr(self, item)
        raise KeyError(item)


@dataclass(frozen=True)
class CatalogSearchPage:
    """Paged search result that also behaves as a sequence of ``CatalogSearchItem``."""

    items: tuple[CatalogSearchItem, ...]
    total_count: int
    limit: int | None
    offset: int
    page: int
    total_pages: int
    has_next: bool
    has_previous: bool

    def __len__(self) -> int:
        return len(self.items)

    def __iter__(self) -> Iterator[CatalogSearchItem]:
        return iter(self.items)

    def __bool__(self) -> bool:
        return bool(self.items)

    def __getitem__(self, index: int | slice | str) -> Any:
        if isinstance(index, str):
            if hasattr(self, index):
                return getattr(self, index)
            raise KeyError(index)
        return self.items[index]

    def __eq__(self, other: object) -> bool:
        if isinstance(other, CatalogSearchPage):
            return (
                self.items == other.items
                and self.total_count == other.total_count
                and self.limit == other.limit
                and self.offset == other.offset
            )
        if isinstance(other, (list, tuple)):
            return list(self.items) == list(other)
        return NotImplemented

    def to_dict(self) -> dict[str, Any]:
        return {
            "items": [item.to_dict() for item in self.items],
            "total_count": self.total_count,
            "limit": self.limit,
            "offset": self.offset,
            "page": self.page,
            "total_pages": self.total_pages,
            "has_next": self.has_next,
            "has_previous": self.has_previous,
        }


@dataclass(frozen=True)
class _DiscoveredFile:
    full_path: Path
    relative_path: str
    size: int
    mtime_ns: int


def _norm_path_key(path: str | Path) -> str:
    raw = str(path).strip()
    if not raw:
        return ""
    try:
        return os.path.normcase(os.path.abspath(raw)).rstrip("\\/")
    except (OSError, ValueError):
        return raw.casefold().rstrip("\\/")


class CatalogService:
    """Framework-neutral service orchestrating incremental catalog indexing, search, and analytics."""

    def __init__(
        self,
        paths: RuntimePaths | MediaCatalog | str | os.PathLike[str] | None = None,
        *,
        db_path: str | os.PathLike[str] | None = None,
        catalog: MediaCatalog | None = None,
        metadata_extractor: MetadataExtractor | None = None,
        volume_prober: Callable[[str | os.PathLike[str]], VolumeIdentity] | None = None,
        online_checker: Callable[[VolumeIdentity | str | os.PathLike[str]], bool] | None = None,
        compute_hashes: bool = False,
    ) -> None:
        if isinstance(paths, MediaCatalog):
            catalog = paths
            paths = None

        self._external_catalog = catalog
        self._catalog_owner_thread: int | None = None
        if catalog is not None:
            import threading

            self._catalog_owner_thread = threading.get_ident()
            resolved_db = catalog.db_path
            self.paths = (
                get_runtime_paths(resolved_db.parent, create=False)
                if str(resolved_db) != ":memory:"
                else get_runtime_paths()
            )
        elif isinstance(paths, RuntimePaths):
            self.paths = paths
            resolved_db = Path(db_path) if db_path is not None else default_catalog_path(paths)
        elif paths is not None:
            candidate = Path(paths).expanduser()
            if db_path is not None:
                self.paths = get_runtime_paths(candidate)
                resolved_db = Path(db_path).expanduser()
            elif candidate.suffix.lower() in {".db", ".sqlite", ".sqlite3"} or (
                candidate.exists() and candidate.is_file()
            ):
                resolved_db = candidate
                self.paths = get_runtime_paths(candidate.parent, create=False)
            else:
                self.paths = get_runtime_paths(candidate)
                resolved_db = default_catalog_path(self.paths)
        else:
            self.paths = get_runtime_paths()
            resolved_db = (
                Path(db_path).expanduser()
                if db_path is not None
                else default_catalog_path(self.paths)
            )

        self.db_path: Path = Path(resolved_db)
        if str(self.db_path) != ":memory:":
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.default_compute_hashes = bool(compute_hashes)
        self.metadata_extractor = metadata_extractor or MetadataExtractor()
        self._volume_prober = volume_prober or probe_volume
        self._online_checker = online_checker or is_volume_online

    @property
    def catalog_path(self) -> Path:
        return self.db_path

    def __enter__(self) -> CatalogService:
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self.close()

    def close(self) -> None:
        """Close any externally attached catalog instance."""
        if self._external_catalog is not None:
            try:
                self._external_catalog.close()
            except Exception:
                pass
            self._external_catalog = None

    def _open_catalog(self) -> tuple[MediaCatalog, bool]:
        """Return a ``(MediaCatalog, should_close)`` pair safe for the current thread."""
        import threading

        if (
            self._external_catalog is not None
            and self._catalog_owner_thread == threading.get_ident()
        ):
            try:
                _ = self._external_catalog.connection
                return self._external_catalog, False
            except Exception:
                pass
        return MediaCatalog(self.db_path), True

    # ------------------------------------------------------------------
    # Volume identity & discovery helpers
    # ------------------------------------------------------------------

    def _resolve_volume_identity(
        self,
        catalog: MediaCatalog,
        folder: Path,
        *,
        explicit_volume_key: str = "",
        explicit_label: str = "",
        explicit_filesystem: str = "",
    ) -> tuple[str, str, str]:
        """Resolve ``(volume_key, label, filesystem)`` for ``folder``."""
        norm_target = _norm_path_key(folder)
        existing_volumes = catalog.list_volumes()

        if explicit_volume_key.strip():
            key = explicit_volume_key.strip()
            existing = catalog.get_volume(key)
        else:
            existing = next(
                (vol for vol in existing_volumes if _norm_path_key(vol.root_path) == norm_target),
                None,
            )
            if existing is None and folder.name:
                # If an existing offline volume has a matching folder basename and its old path is gone,
                # check if it was the same volume key.
                existing = None
            key = existing.volume_key if existing is not None else ""

        try:
            identity = self._volume_prober(folder)
        except Exception:
            identity = VolumeIdentity(mount_point=str(folder))

        if not key:
            mount_norm = _norm_path_key(identity.mount_point) if identity.mount_point else ""
            if mount_norm and mount_norm == norm_target:
                key = identity.stable_key
            else:
                rel_from_mount = relative_path_from_mount(folder, identity.mount_point or None)
                if rel_from_mount:
                    rel_slug = sanitize_path_fallback(rel_from_mount).lower()
                    key = f"{identity.stable_key}:{rel_slug}"
                else:
                    key = identity.stable_key

        label = explicit_label.strip()
        if not label:
            if existing is not None and existing.label:
                label = existing.label
            elif folder.name:
                label = folder.name
            elif identity.label:
                label = identity.label
            else:
                label = str(folder)

        filesystem = explicit_filesystem.strip()
        if not filesystem:
            if existing is not None and existing.filesystem:
                filesystem = existing.filesystem
            elif identity.filesystem:
                filesystem = identity.filesystem

        return key, label, filesystem

    def _is_folder_mounted_and_online(
        self,
        folder: Path,
        *,
        volume_online: bool | None = None,
    ) -> bool:
        if volume_online is not None:
            return bool(volume_online)
        try:
            if not folder.exists() or not folder.is_dir():
                return False
        except OSError:
            return False
        try:
            return bool(self._online_checker(folder))
        except Exception:
            return folder.exists()

    def _discover_folder_files(
        self,
        root: Path,
        *,
        skip_hidden_system: bool = True,
        exclude_dirs: Sequence[str] = DEFAULT_EXCLUDES,
        stop_event: StopEventProtocol | None = None,
        progress_cb: Callable[[int, int, str, str], None] | None = None,
    ) -> tuple[list[_DiscoveredFile], list[str], bool]:
        """Walk ``root`` using the app's read-only local discovery conventions."""
        excluded_names = set(normalize_excludes(exclude_dirs))
        discovered: list[_DiscoveredFile] = []
        warnings: list[str] = []
        stack: list[Path] = [root]
        seen_dirs: set[str] = set()

        while stack:
            if _is_cancelled(stop_event):
                return discovered, warnings, True
            current_dir = stack.pop()
            dir_key = _norm_path_key(current_dir)
            if dir_key in seen_dirs:
                continue
            seen_dirs.add(dir_key)

            subdirs: list[Path] = []
            try:
                with os.scandir(current_dir) as entries:
                    sorted_entries = sorted(entries, key=lambda e: e.name.casefold())
            except OSError as exc:
                warnings.append(f"Could not scan {current_dir}: {exc}")
                continue

            for entry in sorted_entries:
                if _is_cancelled(stop_event):
                    return discovered, warnings, True
                entry_path = Path(entry.path)
                try:
                    entry_stat = entry.stat(follow_symlinks=False)
                except OSError as exc:
                    warnings.append(f"Could not stat {entry_path}: {exc}")
                    continue

                if (
                    getattr(entry_stat, "st_file_attributes", 0) & FILE_ATTRIBUTE_REPARSE_POINT
                    or entry.is_symlink()
                ):
                    continue

                if skip_hidden_system and is_hidden_or_system(entry_path):
                    continue

                try:
                    is_dir = entry.is_dir(follow_symlinks=False)
                    is_file = entry.is_file(follow_symlinks=False)
                except OSError as exc:
                    warnings.append(f"Could not inspect {entry_path}: {exc}")
                    continue

                if is_dir:
                    if entry.name.strip().lower() in excluded_names:
                        continue
                    subdirs.append(entry_path)
                    continue

                if not is_file:
                    continue

                try:
                    rel_path = normalize_relative_path(entry_path.relative_to(root))
                except ValueError:
                    continue

                discovered.append(
                    _DiscoveredFile(
                        full_path=entry_path,
                        relative_path=rel_path,
                        size=max(0, int(entry_stat.st_size)),
                        mtime_ns=int(entry_stat.st_mtime_ns),
                    )
                )
                if progress_cb is not None and len(discovered) % 25 == 0:
                    progress_cb(
                        0,
                        0,
                        f"Discovering files ({len(discovered)} found)…",
                        str(entry_path),
                    )

            for subdir in reversed(subdirs):
                stack.append(subdir)

        discovered.sort(key=lambda item: item.relative_path)
        return discovered, warnings, _is_cancelled(stop_event)

    def _extract_catalog_metadata(
        self,
        file_path: Path,
        stop_event: StopEventProtocol | None = None,
    ) -> MediaMetadata:
        """Extract file metadata through ``sorting.metadata.MetadataExtractor`` with header fallback."""
        if _is_cancelled(stop_event):
            return MediaMetadata()

        captured_str: str | None = None
        width: int | None = None
        height: int | None = None
        camera_make: str | None = None
        camera_model: str | None = None

        try:
            fm = self.metadata_extractor.extract(file_path)
            if fm.width and int(fm.width) > 0:
                width = int(fm.width)
            if fm.height and int(fm.height) > 0:
                height = int(fm.height)
            if fm.captured and float(fm.captured) > 0:
                captured_str = datetime.fromtimestamp(float(fm.captured)).strftime(
                    "%Y:%m:%d %H:%M:%S"
                )
            attrs = fm.attributes or {}
            make_val = attrs.get("camera_make") or attrs.get("make")
            model_val = attrs.get("camera_model") or attrs.get("model")
            if make_val:
                camera_make = str(make_val).strip() or None
            if model_val:
                camera_model = str(model_val).strip() or None
        except Exception:
            pass

        if _is_cancelled(stop_event):
            return MediaMetadata()

        if (
            captured_str is None
            or width is None
            or height is None
            or camera_make is None
            or camera_model is None
        ):
            fallback = extract_file_metadata(file_path, stop_event=stop_event)
            if captured_str is None and fallback.capture_time is not None:
                captured_str = fallback.capture_date or str(fallback.capture_time)
            if width is None and fallback.width is not None:
                width = fallback.width
            if height is None and fallback.height is not None:
                height = fallback.height
            if camera_make is None and fallback.camera_make is not None:
                camera_make = fallback.camera_make
            if camera_model is None and fallback.camera_model is not None:
                camera_model = fallback.camera_model

        return MediaMetadata(
            capture_time=captured_str,
            camera_make=camera_make,
            camera_model=camera_model,
            width=width,
            height=height,
        )

    @staticmethod
    def _emit_progress(
        progress_callback: Callable[..., Any] | OperationReporter | None,
        reporter: OperationReporter | None,
        *,
        current: int,
        total: int,
        message: str,
        current_item: str = "",
        bytes_processed: int = 0,
        total_bytes: int = 0,
        phase: OperationPhase = OperationPhase.DISCOVERY,
    ) -> None:
        if reporter is not None:
            reporter.emit(
                message,
                phase=phase,
                current=current,
                total=total,
                current_item=current_item,
                bytes_processed=bytes_processed,
                total_bytes=total_bytes,
            )
        if progress_callback is None or progress_callback is reporter:
            return
        if isinstance(progress_callback, OperationReporter):
            progress_callback.emit(
                message,
                phase=phase,
                current=current,
                total=total,
                current_item=current_item,
                bytes_processed=bytes_processed,
                total_bytes=total_bytes,
            )
            return
        try:
            progress_callback(current, total, message)
        except TypeError:
            try:
                progress_callback(
                    {
                        "current": current,
                        "total": total,
                        "message": message,
                        "current_item": current_item,
                        "bytes_processed": bytes_processed,
                        "total_bytes": total_bytes,
                    }
                )
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Indexing API
    # ------------------------------------------------------------------

    def index_folder(
        self,
        folder_path: str | os.PathLike[str],
        *,
        volume_key: str = "",
        label: str = "",
        filesystem: str = "",
        volume_online: bool | None = None,
        compute_hashes: bool | None = None,
        skip_hidden_system: bool = True,
        exclude_dirs: Sequence[str] = DEFAULT_EXCLUDES,
        remove_missing: bool = False,
        stop_event: StopEventProtocol | None = None,
        cancellation: CancellationToken | StopEventProtocol | None = None,
        progress_callback: Callable[..., Any] | OperationReporter | None = None,
        reporter: OperationReporter | None = None,
    ) -> CatalogIndexResult:
        """Incrementally index ``folder_path`` into the SQLite catalog.

        * Hashing is optional and OFF by default (``compute_hashes=False``).
        * Metadata is extracted through ``sorting.metadata.MetadataExtractor``.
        * Unchanged files on an already-online volume perform zero SQLite writes.
        * Unmounted / absent volumes are marked offline and all existing rows survive.
        """
        eff_stop = stop_event if stop_event is not None else cancellation
        eff_hashes = self.default_compute_hashes if compute_hashes is None else bool(compute_hashes)
        raw_folder = str(folder_path).strip()
        if not raw_folder:
            raise ServiceError(ErrorCode.VALIDATION, "Choose a folder to index.")
        folder = Path(raw_folder).expanduser()

        catalog, should_close = self._open_catalog()
        try:
            resolved_key, resolved_label, resolved_fs = self._resolve_volume_identity(
                catalog,
                folder,
                explicit_volume_key=volume_key,
                explicit_label=label,
                explicit_filesystem=filesystem,
            )

            if _is_cancelled(eff_stop):
                return CatalogIndexResult(
                    volume_key=resolved_key,
                    root_path=str(folder),
                    volume_status="cancelled",
                    cancelled=True,
                    compute_hashes=eff_hashes,
                )

            online = self._is_folder_mounted_and_online(folder, volume_online=volume_online)
            if not online:
                catalog.mark_volume_offline(resolved_key)
                return CatalogIndexResult(
                    volume_key=resolved_key,
                    root_path=str(folder),
                    added=0,
                    updated=0,
                    unchanged=0,
                    removed=0,
                    volume_status="offline",
                    cancelled=False,
                    compute_hashes=eff_hashes,
                    writes_performed=0,
                )

            if reporter is not None:
                reporter.set_state(
                    OperationState.SCANNING,
                    phase=OperationPhase.DISCOVERY,
                    message=f"Scanning {folder}…",
                )

            discovered, warnings, cancelled = self._discover_folder_files(
                folder,
                skip_hidden_system=skip_hidden_system,
                exclude_dirs=exclude_dirs,
                stop_event=eff_stop,
                progress_cb=lambda cur, tot, msg, item: self._emit_progress(
                    progress_callback,
                    reporter,
                    current=cur,
                    total=tot,
                    message=msg,
                    current_item=item,
                    phase=OperationPhase.DISCOVERY,
                ),
            )
            if cancelled or _is_cancelled(eff_stop):
                return CatalogIndexResult(
                    volume_key=resolved_key,
                    root_path=str(folder),
                    volume_status="cancelled",
                    cancelled=True,
                    compute_hashes=eff_hashes,
                    warnings=tuple(warnings),
                )

            total_files = len(discovered)
            total_bytes = sum(item.size for item in discovered)
            processed_bytes = 0

            # If compute_hashes=True on a volume that was previously indexed without hashes,
            # remove only the unhashed rows so upsert_files computes their hashes now.
            rehashed_paths: set[str] = set()
            if eff_hashes:
                existing_records = catalog.search(volume_key=resolved_key, stop_event=eff_stop)
                discovered_by_rel = {item.relative_path: item for item in discovered}
                unhashed_existing = [
                    rec.relative_path
                    for rec in existing_records
                    if not rec.content_hash
                    and rec.relative_path in discovered_by_rel
                    and rec.size == discovered_by_rel[rec.relative_path].size
                    and rec.mtime_ns == discovered_by_rel[rec.relative_path].mtime_ns
                ]
                if unhashed_existing and not _is_cancelled(eff_stop):
                    catalog.remove_volume_files(resolved_key, unhashed_existing)
                    rehashed_paths = set(unhashed_existing)

            changes_before = catalog.connection.total_changes

            def _iter_inputs() -> Iterator[CatalogFileInput]:
                nonlocal processed_bytes
                for idx, item in enumerate(discovered, 1):
                    if _is_cancelled(eff_stop):
                        return
                    processed_bytes += item.size
                    self._emit_progress(
                        progress_callback,
                        reporter,
                        current=idx,
                        total=max(1, total_files),
                        message=f"Indexing {item.full_path.name} ({idx}/{total_files})…",
                        current_item=str(item.full_path),
                        bytes_processed=processed_bytes,
                        total_bytes=total_bytes,
                        phase=OperationPhase.HASHING if eff_hashes else OperationPhase.DISCOVERY,
                    )
                    yield CatalogFileInput(
                        relative_path=item.relative_path,
                        size=item.size,
                        mtime_ns=item.mtime_ns,
                        file_path=item.full_path,
                        media_kind=classify_media_kind(item.full_path),
                    )

            upsert_res: UpsertResult = catalog.upsert_files(
                resolved_key,
                _iter_inputs(),
                volume_root=folder,
                label=resolved_label,
                filesystem=resolved_fs,
                volume_online=True,
                compute_hashes=eff_hashes,
                extract_metadata=True,
                hash_fn=(
                    (lambda p: compute_file_sha256(p, stop_event=eff_stop))
                    if eff_hashes
                    else None
                ),
                metadata_fn=lambda p: self._extract_catalog_metadata(p, stop_event=eff_stop),
                remove_missing=remove_missing,
                stop_event=eff_stop,
            )

            changes_after = catalog.connection.total_changes
            writes_delta = max(0, int(changes_after - changes_before))

            if upsert_res.cancelled or _is_cancelled(eff_stop):
                return CatalogIndexResult(
                    volume_key=resolved_key,
                    root_path=str(folder),
                    volume_status="cancelled",
                    cancelled=True,
                    compute_hashes=eff_hashes,
                    writes_performed=0,
                    warnings=tuple(warnings),
                )

            if total_files == 0:
                self._emit_progress(
                    progress_callback,
                    reporter,
                    current=1,
                    total=1,
                    message="Index complete (0 files).",
                    current_item=str(folder),
                    phase=OperationPhase.FINALIZATION,
                )

            added_count = upsert_res.added
            updated_count = upsert_res.updated
            if rehashed_paths:
                backfilled = min(added_count, len(rehashed_paths))
                added_count -= backfilled
                updated_count += backfilled

            return CatalogIndexResult(
                volume_key=resolved_key,
                root_path=str(folder),
                added=added_count,
                updated=updated_count,
                unchanged=upsert_res.unchanged,
                removed=upsert_res.removed,
                volume_status=upsert_res.volume_status,
                cancelled=upsert_res.cancelled,
                compute_hashes=eff_hashes,
                writes_performed=writes_delta,
                warnings=tuple(warnings),
            )
        finally:
            if should_close:
                catalog.close()

    index = index_folder
    scan_folder = index_folder

    def scan_volume(
        self,
        volume_key: str,
        volume_root: str | os.PathLike[str],
        *,
        label: str = "",
        filesystem: str = "",
        compute_hashes: bool | None = None,
        remove_missing: bool = False,
        stop_event: StopEventProtocol | None = None,
        progress_callback: Callable[..., Any] | OperationReporter | None = None,
        reporter: OperationReporter | None = None,
    ) -> CatalogIndexResult:
        """Scan ``volume_root`` into ``volume_key`` with hashing off by default."""
        return self.index_folder(
            volume_root,
            volume_key=volume_key,
            label=label,
            filesystem=filesystem,
            compute_hashes=compute_hashes,
            remove_missing=remove_missing,
            stop_event=stop_event,
            progress_callback=progress_callback,
            reporter=reporter,
        )

    def run(
        self,
        settings: CatalogIndexSettings | str | os.PathLike[str],
        cancellation: CancellationToken | StopEventProtocol | None = None,
        reporter: OperationReporter | None = None,
        *,
        compute_hashes: bool | None = None,
    ) -> OperationResult:
        """Execute an indexing operation returning a standard ``OperationResult``."""
        import time

        started = time.monotonic()
        if isinstance(settings, CatalogIndexSettings):
            cfg = settings
        else:
            cfg = CatalogIndexSettings(
                folder_path=str(settings),
                compute_hashes=(
                    self.default_compute_hashes
                    if compute_hashes is None
                    else bool(compute_hashes)
                ),
            )

        eff_hashes = cfg.compute_hashes if compute_hashes is None else bool(compute_hashes)
        index_res = self.index_folder(
            cfg.folder_path,
            volume_key=cfg.volume_key,
            label=cfg.label,
            filesystem=cfg.filesystem,
            compute_hashes=eff_hashes,
            skip_hidden_system=cfg.skip_hidden_system,
            exclude_dirs=cfg.exclude_dirs,
            cancellation=cancellation,
            reporter=reporter,
        )
        duration = max(0.0, time.monotonic() - started)
        if index_res.cancelled or _is_cancelled(cancellation):
            return OperationResult(
                status=OperationState.CANCELLED,
                counts={
                    "added": index_res.added,
                    "updated": index_res.updated,
                    "unchanged": index_res.unchanged,
                    "removed": index_res.removed,
                },
                duration_seconds=duration,
                warnings=index_res.warnings,
                data={"index_result": index_res},
            )

        analytics = self.get_storage_analytics(stop_event=cancellation)
        if reporter is not None:
            reporter.set_state(
                OperationState.COMPLETED,
                phase=OperationPhase.FINALIZATION,
                message="Library catalog index complete.",
            )
        return OperationResult(
            status=OperationState.COMPLETED,
            counts={
                "added": index_res.added,
                "updated": index_res.updated,
                "unchanged": index_res.unchanged,
                "removed": index_res.removed,
            },
            duration_seconds=duration,
            warnings=index_res.warnings,
            data={
                "index_result": index_res,
                "analytics": analytics,
            },
        )

    # ------------------------------------------------------------------
    # Volume status management
    # ------------------------------------------------------------------

    def _sync_missing_volume_roots(self, catalog: MediaCatalog) -> None:
        """Mark any online volume whose recorded ``root_path`` is missing on disk as offline.

        Volumes already marked offline are left offline unless explicitly re-indexed
        or refreshed, and no rows are ever deleted.
        """
        for vol in catalog.list_volumes():
            if vol.is_online and vol.root_path:
                if not os.path.exists(vol.root_path):
                    catalog.mark_volume_offline(vol.volume_key)

    def _find_volume_key(self, catalog: MediaCatalog, volume_key_or_path: str | os.PathLike[str]) -> str:
        raw = str(volume_key_or_path).strip()
        if not raw:
            raise ValueError("volume_key must not be empty")
        if catalog.get_volume(raw) is not None:
            return raw
        target_norm = _norm_path_key(raw)
        for vol in catalog.list_volumes():
            if vol.root_path and _norm_path_key(vol.root_path) == target_norm:
                return vol.volume_key
        return raw

    def set_volume_online(
        self,
        volume_key_or_path: str | os.PathLike[str],
        is_online: bool,
    ) -> CatalogVolumeSummary | None:
        """Mark a cataloged volume online or offline without removing any file rows."""
        catalog, should_close = self._open_catalog()
        try:
            key = self._find_volume_key(catalog, volume_key_or_path)
            updated = catalog.set_volume_online(key, bool(is_online))
            if updated is None:
                return None
            vol_buckets = {
                b.key: b for b in catalog.analytics_by_volume(include_empty=True)
            }
            bucket = vol_buckets.get(key)
            return CatalogVolumeSummary(
                volume_key=updated.volume_key,
                label=updated.label or updated.volume_key,
                filesystem=updated.filesystem,
                root_path=updated.root_path,
                is_online=updated.is_online,
                status=updated.status,
                last_seen=updated.last_seen,
                file_count=bucket.file_count if bucket else 0,
                total_bytes=bucket.total_bytes if bucket else 0,
            )
        finally:
            if should_close:
                catalog.close()

    def mark_volume_offline(
        self,
        volume_key_or_path: str | os.PathLike[str],
    ) -> CatalogVolumeSummary | None:
        """Mark a cataloged volume offline while preserving all of its rows."""
        return self.set_volume_online(volume_key_or_path, False)

    def mark_volume_online(
        self,
        volume_key_or_path: str | os.PathLike[str],
    ) -> CatalogVolumeSummary | None:
        """Mark a cataloged volume online."""
        return self.set_volume_online(volume_key_or_path, True)

    def refresh_volume_statuses(
        self,
        present_volume_keys: Iterable[str] | None = None,
    ) -> dict[str, str]:
        """Refresh and return online/offline status for all cataloged volumes."""
        catalog, should_close = self._open_catalog()
        try:
            return catalog.refresh_volume_statuses(present_volume_keys=present_volume_keys)
        finally:
            if should_close:
                catalog.close()

    def list_volumes(self) -> list[CatalogVolumeSummary]:
        """Return plain volume summaries with file counts, byte totals, and online status."""
        catalog, should_close = self._open_catalog()
        try:
            self._sync_missing_volume_roots(catalog)
            return self._build_volume_summaries(catalog)
        finally:
            if should_close:
                catalog.close()

    def get_volume(self, volume_key_or_path: str | os.PathLike[str]) -> CatalogVolumeSummary | None:
        """Return a single ``CatalogVolumeSummary`` by key or root path."""
        catalog, should_close = self._open_catalog()
        try:
            self._sync_missing_volume_roots(catalog)
            key = self._find_volume_key(catalog, volume_key_or_path)
            for vol in self._build_volume_summaries(catalog):
                if vol.volume_key == key:
                    return vol
            return None
        finally:
            if should_close:
                catalog.close()

    def remove_volume(self, volume_key: str) -> int:
        """Explicitly remove a named volume and its catalog rows."""
        catalog, should_close = self._open_catalog()
        try:
            return catalog.remove_volume(volume_key)
        finally:
            if should_close:
                catalog.close()

    @staticmethod
    def _build_volume_summaries(catalog: MediaCatalog) -> list[CatalogVolumeSummary]:
        records = {vol.volume_key: vol for vol in catalog.list_volumes()}
        buckets = catalog.analytics_by_volume(include_empty=True)
        summaries: list[CatalogVolumeSummary] = []
        seen: set[str] = set()
        for bucket in buckets:
            key = str(bucket.key)
            seen.add(key)
            vol = records.get(key)
            online = vol.is_online if vol is not None else True
            summaries.append(
                CatalogVolumeSummary(
                    volume_key=key,
                    label=(vol.label if vol and vol.label else key),
                    filesystem=vol.filesystem if vol else "",
                    root_path=vol.root_path if vol else "",
                    is_online=online,
                    status="online" if online else "offline",
                    last_seen=vol.last_seen if vol else 0.0,
                    file_count=bucket.file_count,
                    total_bytes=bucket.total_bytes,
                )
            )
        for key, vol in records.items():
            if key not in seen:
                summaries.append(
                    CatalogVolumeSummary(
                        volume_key=key,
                        label=vol.label or key,
                        filesystem=vol.filesystem,
                        root_path=vol.root_path,
                        is_online=vol.is_online,
                        status=vol.status,
                        last_seen=vol.last_seen,
                        file_count=0,
                        total_bytes=0,
                    )
                )
        return summaries

    # ------------------------------------------------------------------
    # Search API (plain data structures, paged)
    # ------------------------------------------------------------------

    @staticmethod
    def _to_search_item(
        record: CatalogFileRecord,
        volumes_by_key: Mapping[str, VolumeRecord],
    ) -> CatalogSearchItem:
        vol = volumes_by_key.get(record.volume_key)
        return CatalogSearchItem(
            id=record.id,
            volume_key=record.volume_key,
            relative_path=record.relative_path,
            filename=record.filename,
            extension=record.extension,
            size=record.size,
            mtime_ns=record.mtime_ns,
            content_hash=record.content_hash,
            media_kind=record.media_kind,
            indexed_at=record.indexed_at,
            volume_online=record.volume_online,
            volume_status=record.volume_status,
            volume_label=(vol.label if vol and vol.label else record.volume_key),
            volume_root=(vol.root_path if vol else ""),
            capture_timestamp=record.capture_timestamp,
            capture_date=record.capture_date,
            capture_year=record.capture_year,
            camera_make=record.camera_make,
            camera_model=record.camera_model,
            width=record.width,
            height=record.height,
        )

    def distinct_cameras(self, *, stop_event: Any = None) -> list[tuple[str | None, str | None]]:
        """Distinct (make, model) pairs in the catalog, ordered case-insensitively."""

        if stop_event is not None and stop_event.is_set():
            return []
        try:
            cat, should_close = self._open_catalog()
            try:
                if stop_event is not None and stop_event.is_set():
                    return []
                cursor = cat.connection.execute(
                    """
                    SELECT DISTINCT camera_make, camera_model
                    FROM metadata
                    WHERE (camera_make IS NOT NULL AND TRIM(camera_make) != '')
                       OR (camera_model IS NOT NULL AND TRIM(camera_model) != '')
                    ORDER BY camera_make COLLATE NOCASE, camera_model COLLATE NOCASE
                    """
                )
                rows = cursor.fetchall()
                cameras: list[tuple[str | None, str | None]] = []
                for r in rows:
                    if stop_event is not None and stop_event.is_set():
                        return []
                    make = str(r["camera_make"]).strip() if r["camera_make"] else None
                    model = str(r["camera_model"]).strip() if r["camera_model"] else None
                    if make or model:
                        cameras.append((make, model))
                return cameras
            finally:
                if should_close:
                    cat.close()
        except Exception:
            return []

    def search(
        self,
        filter_spec: SearchFilter | None = None,
        *,
        media_kind: str | Sequence[str] | None = None,
        filename: str | None = None,
        filename_substring: str | None = None,
        min_size: int | None = None,
        max_size: int | None = None,
        size_range: tuple[int | None, int | None] | None = None,
        min_mtime_ns: int | None = None,
        max_mtime_ns: int | None = None,
        mtime_range: tuple[int | None, int | None] | None = None,
        capture_start: datetime | date | float | int | str | None = None,
        capture_end: datetime | date | float | int | str | None = None,
        capture_date_range: tuple[Any, Any] | None = None,
        date_range: tuple[Any, Any] | None = None,
        camera_make: str | None = None,
        camera_model: str | None = None,
        volume_key: str | Sequence[str] | None = None,
        volume: str | Sequence[str] | None = None,
        has_duplicates: bool | None = None,
        order_by: str | None = None,
        descending: bool | None = None,
        limit: int | None = None,
        offset: int | None = None,
        page: int | None = None,
        page_size: int | None = None,
        stop_event: StopEventProtocol | None = None,
    ) -> CatalogSearchPage:
        """Search the catalog and return a paged ``CatalogSearchPage`` of plain records."""
        if _is_cancelled(stop_event):
            return CatalogSearchPage(
                items=(),
                total_count=0,
                limit=limit,
                offset=offset or 0,
                page=1,
                total_pages=0,
                has_next=False,
                has_previous=False,
            )

        spec = filter_spec or SearchFilter()
        eff_limit = page_size if page_size is not None else (limit if limit is not None else spec.limit)
        if page is not None and page >= 1:
            step = eff_limit if (eff_limit is not None and eff_limit > 0) else DEFAULT_PAGE_SIZE
            eff_limit = step
            eff_offset = (int(page) - 1) * step
        else:
            eff_offset = offset if offset is not None else spec.offset

        eff_date_range = capture_date_range if capture_date_range is not None else date_range

        catalog, should_close = self._open_catalog()
        try:
            self._sync_missing_volume_roots(catalog)
            volumes_by_key = {vol.volume_key: vol for vol in catalog.list_volumes()}

            paged_records = catalog.search(
                spec,
                media_kind=media_kind,
                filename=filename,
                filename_substring=filename_substring,
                min_size=min_size,
                max_size=max_size,
                size_range=size_range,
                min_mtime_ns=min_mtime_ns,
                max_mtime_ns=max_mtime_ns,
                mtime_range=mtime_range,
                capture_start=capture_start,
                capture_end=capture_end,
                capture_date_range=eff_date_range,
                camera_make=camera_make,
                camera_model=camera_model,
                volume_key=volume_key,
                volume=volume,
                has_duplicates=has_duplicates,
                order_by=order_by,
                descending=descending,
                limit=eff_limit,
                offset=eff_offset,
                stop_event=stop_event,
            )

            if eff_limit is None and eff_offset == 0:
                total_count = len(paged_records)
            else:
                unpaged_spec = SearchFilter(
                    media_kind=spec.media_kind,
                    filename=spec.filename,
                    min_size=spec.min_size,
                    max_size=spec.max_size,
                    min_mtime_ns=spec.min_mtime_ns,
                    max_mtime_ns=spec.max_mtime_ns,
                    capture_start=spec.capture_start,
                    capture_end=spec.capture_end,
                    camera_make=spec.camera_make,
                    camera_model=spec.camera_model,
                    volume_key=spec.volume_key,
                    has_duplicates=spec.has_duplicates,
                    order_by=spec.order_by,
                    descending=spec.descending,
                    limit=None,
                    offset=0,
                )
                all_matching = catalog.search(
                    unpaged_spec,
                    media_kind=media_kind,
                    filename=filename,
                    filename_substring=filename_substring,
                    min_size=min_size,
                    max_size=max_size,
                    size_range=size_range,
                    min_mtime_ns=min_mtime_ns,
                    max_mtime_ns=max_mtime_ns,
                    mtime_range=mtime_range,
                    capture_start=capture_start,
                    capture_end=capture_end,
                    capture_date_range=eff_date_range,
                    camera_make=camera_make,
                    camera_model=camera_model,
                    volume_key=volume_key,
                    volume=volume,
                    has_duplicates=has_duplicates,
                    order_by=order_by,
                    descending=descending,
                    limit=None,
                    offset=0,
                    stop_event=stop_event,
                )
                total_count = len(all_matching)

            items = tuple(
                self._to_search_item(rec, volumes_by_key) for rec in paged_records
            )
            if eff_limit and eff_limit > 0:
                current_page = (eff_offset // eff_limit) + 1
                total_pages = max(1, math.ceil(total_count / eff_limit)) if total_count > 0 else 0
                has_next = (eff_offset + len(items)) < total_count
                has_previous = eff_offset > 0
            else:
                current_page = 1 if total_count > 0 else 0
                total_pages = 1 if total_count > 0 else 0
                has_next = False
                has_previous = eff_offset > 0

            return CatalogSearchPage(
                items=items,
                total_count=total_count,
                limit=eff_limit,
                offset=eff_offset,
                page=current_page,
                total_pages=total_pages,
                has_next=has_next,
                has_previous=has_previous,
            )
        finally:
            if should_close:
                catalog.close()

    search_page = search

    # ------------------------------------------------------------------
    # Analytics API (plain data structures)
    # ------------------------------------------------------------------

    def analytics_by_media_kind(
        self,
        *,
        stop_event: StopEventProtocol | None = None,
    ) -> list[CatalogBucket]:
        """Return file counts and total bytes grouped by media kind."""
        catalog, should_close = self._open_catalog()
        try:
            return [
                CatalogBucket(
                    key=b.key,
                    file_count=b.file_count,
                    total_bytes=b.total_bytes,
                    label=str(b.key).title() if b.key is not None else "Other",
                )
                for b in catalog.analytics_by_media_kind(stop_event=stop_event)
            ]
        finally:
            if should_close:
                catalog.close()

    def analytics_by_volume(
        self,
        *,
        include_empty: bool = True,
        stop_event: StopEventProtocol | None = None,
    ) -> list[CatalogVolumeSummary]:
        """Return file counts, byte totals, and online/offline status grouped by volume."""
        catalog, should_close = self._open_catalog()
        try:
            self._sync_missing_volume_roots(catalog)
            summaries = self._build_volume_summaries(catalog)
            if not include_empty:
                return [vol for vol in summaries if vol.file_count > 0]
            return summaries
        finally:
            if should_close:
                catalog.close()

    def analytics_by_capture_year(
        self,
        *,
        include_unknown: bool = False,
        stop_event: StopEventProtocol | None = None,
    ) -> list[CatalogBucket]:
        """Return file counts and total bytes grouped by EXIF capture year."""
        catalog, should_close = self._open_catalog()
        try:
            return [
                CatalogBucket(
                    key=b.key,
                    file_count=b.file_count,
                    total_bytes=b.total_bytes,
                    label=str(b.key) if b.key is not None else "Unknown year",
                )
                for b in catalog.analytics_by_capture_year(
                    include_unknown=include_unknown,
                    stop_event=stop_event,
                )
            ]
        finally:
            if should_close:
                catalog.close()

    def analytics_by_extension(
        self,
        *,
        stop_event: StopEventProtocol | None = None,
    ) -> list[CatalogBucket]:
        """Return file counts and total bytes grouped by lowercase file extension."""
        catalog, should_close = self._open_catalog()
        try:
            return [
                CatalogBucket(
                    key=b.key,
                    file_count=b.file_count,
                    total_bytes=b.total_bytes,
                    label=str(b.key) if b.key else "(no extension)",
                )
                for b in catalog.analytics_by_extension(stop_event=stop_event)
            ]
        finally:
            if should_close:
                catalog.close()

    def _hash_coverage(
        self,
        catalog: MediaCatalog,
        *,
        stop_event: StopEventProtocol | None = None,
    ) -> tuple[int, int]:
        """Return ``(hashed_files, unhashed_files)`` using ``MediaCatalog.search``."""
        all_files = catalog.search(stop_event=stop_event)
        hashed = sum(1 for rec in all_files if rec.content_hash)
        unhashed = len(all_files) - hashed
        return hashed, unhashed

    def recoverable_duplicate_bytes(
        self,
        *,
        stop_event: StopEventProtocol | None = None,
    ) -> int | None:
        """Return recoverable duplicate bytes when hashes were computed, or ``None`` when unavailable."""
        catalog, should_close = self._open_catalog()
        try:
            hashed, _unhashed = self._hash_coverage(catalog, stop_event=stop_event)
            if hashed == 0:
                return None
            return catalog.recoverable_duplicate_bytes(stop_event=stop_event)
        finally:
            if should_close:
                catalog.close()

    def get_storage_analytics(
        self,
        *,
        stop_event: StopEventProtocol | None = None,
    ) -> CatalogStorageAnalytics:
        """Return the complete storage breakdown as plain data structures.

        When hashing was not enabled (`hashed_files == 0`), duplicate analytics
        report ``recoverable_duplicate_bytes=None``,
        ``duplicate_analytics_available=False``, and
        ``recoverable_duplicate_status="unavailable"`` rather than reporting 0.
        """
        catalog, should_close = self._open_catalog()
        try:
            self._sync_missing_volume_roots(catalog)
            raw: StorageAnalytics = catalog.get_storage_analytics(stop_event=stop_event)
            hashed, unhashed = self._hash_coverage(catalog, stop_event=stop_event)
            duplicates_available = hashed > 0
            recoverable: int | None = (
                raw.recoverable_duplicate_bytes if duplicates_available else None
            )
            dup_status = "available" if duplicates_available else "unavailable"

            volume_summaries = tuple(self._build_volume_summaries(catalog))
            non_empty_volumes = tuple(
                vol for vol in volume_summaries if vol.file_count > 0 or not vol.is_online
            )
            offline_volumes = tuple(vol for vol in volume_summaries if not vol.is_online)

            return CatalogStorageAnalytics(
                total_files=raw.total_files,
                total_bytes=raw.total_bytes,
                by_media_kind=tuple(
                    CatalogBucket(
                        key=b.key,
                        file_count=b.file_count,
                        total_bytes=b.total_bytes,
                        label=str(b.key).title() if b.key is not None else "Other",
                    )
                    for b in raw.by_media_kind
                ),
                by_volume=non_empty_volumes,
                by_capture_year=tuple(
                    CatalogBucket(
                        key=b.key,
                        file_count=b.file_count,
                        total_bytes=b.total_bytes,
                        label=str(b.key) if b.key is not None else "Unknown year",
                    )
                    for b in raw.by_capture_year
                ),
                by_extension=tuple(
                    CatalogBucket(
                        key=b.key,
                        file_count=b.file_count,
                        total_bytes=b.total_bytes,
                        label=str(b.key) if b.key else "(no extension)",
                    )
                    for b in raw.by_extension
                ),
                recoverable_duplicate_bytes=recoverable,
                duplicate_analytics_available=duplicates_available,
                recoverable_duplicate_status=dup_status,
                hashed_files=hashed,
                unhashed_files=unhashed,
                volumes=volume_summaries,
                offline_volumes=offline_volumes,
            )
        finally:
            if should_close:
                catalog.close()

    storage_analytics = get_storage_analytics
    analytics = get_storage_analytics


__all__ = [
    "CATALOG_FILENAME",
    "CatalogBucket",
    "CatalogIndexResult",
    "CatalogIndexSettings",
    "CatalogSearchItem",
    "CatalogSearchPage",
    "CatalogService",
    "CatalogStorageAnalytics",
    "CatalogVolumeSummary",
    "DEFAULT_PAGE_SIZE",
    "default_catalog_path",
]
