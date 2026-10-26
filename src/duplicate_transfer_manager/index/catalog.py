"""Framework-neutral SQLite media catalog with incremental indexing, search, and analytics."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime
import hashlib
import os
from pathlib import Path
import posixpath
import sqlite3
import struct
import time
from typing import Any, Callable, Iterable, Iterator, Mapping, Protocol, Sequence

from .schema import (
    DEFAULT_BUSY_TIMEOUT_MS,
    SCHEMA_VERSION,
    MigrationStep,
    configure_connection,
    get_user_version,
    initialize_schema,
)

IMAGE_EXTENSIONS: frozenset[str] = frozenset(
    {
        ".jpg",
        ".jpeg",
        ".png",
        ".gif",
        ".bmp",
        ".tif",
        ".tiff",
        ".heic",
        ".heif",
        ".webp",
        ".avif",
        ".dng",
        ".cr2",
        ".cr3",
        ".nef",
        ".arw",
        ".raf",
        ".orf",
        ".rw2",
        ".pef",
        ".srw",
        ".svg",
        ".ico",
    }
)

VIDEO_EXTENSIONS: frozenset[str] = frozenset(
    {
        ".mp4",
        ".mov",
        ".avi",
        ".mkv",
        ".wmv",
        ".flv",
        ".mpeg",
        ".mpg",
        ".3gp",
        ".mts",
        ".m2ts",
        ".hevc",
        ".webm",
        ".m4v",
        ".vob",
        ".ogv",
    }
)

AUDIO_EXTENSIONS: frozenset[str] = frozenset(
    {
        ".mp3",
        ".aac",
        ".m4a",
        ".wav",
        ".flac",
        ".ogg",
        ".opus",
        ".wma",
        ".aiff",
        ".alac",
    }
)

DOCUMENT_EXTENSIONS: frozenset[str] = frozenset(
    {
        ".pdf",
        ".txt",
        ".doc",
        ".docx",
        ".xls",
        ".xlsx",
        ".ppt",
        ".pptx",
        ".csv",
        ".md",
        ".rtf",
        ".odt",
    }
)


class StopEventProtocol(Protocol):
    """Cooperative cancellation protocol matching ``threading.Event`` and ``CancellationToken``."""

    def is_set(self) -> bool: ...


def _is_cancelled(stop_event: StopEventProtocol | None) -> bool:
    return bool(stop_event is not None and stop_event.is_set())


def classify_media_kind(path_or_ext: str | Path) -> str:
    """Classify a file path or extension into a canonical media kind string."""
    raw = str(path_or_ext).strip().lower()
    if not raw:
        return "other"
    if "/" in raw or "\\" in raw or ("." in raw and not raw.startswith(".")):
        _, ext = posixpath.splitext(raw.replace("\\", "/"))
    else:
        ext = raw if raw.startswith(".") else f".{raw}"
    if ext in IMAGE_EXTENSIONS:
        return "image"
    if ext in VIDEO_EXTENSIONS:
        return "video"
    if ext in AUDIO_EXTENSIONS:
        return "audio"
    if ext in DOCUMENT_EXTENSIONS:
        return "document"
    return "other"


def normalize_relative_path(relative_path: str | Path) -> str:
    """Normalize a volume-relative path to a forward-slash relative path."""
    raw = str(relative_path).replace("\\", "/").strip()
    while raw.startswith("./"):
        raw = raw[2:]
    raw = raw.lstrip("/")
    if not raw:
        raise ValueError("relative_path must not be empty")
    normalized = posixpath.normpath(raw)
    if normalized in {"", "."} or normalized == ".." or normalized.startswith("../"):
        raise ValueError(f"Invalid relative_path outside volume root: {relative_path!r}")
    return normalized


def _extract_filename_and_extension(relative_path: str) -> tuple[str, str]:
    filename = posixpath.basename(relative_path)
    _, ext = posixpath.splitext(filename)
    return filename, ext.lower()


def _normalize_capture_time(
    value: datetime | date | float | int | str | None,
    *,
    is_upper_bound: bool = False,
) -> tuple[float | None, str | None, int | None]:
    """Normalize an EXIF/ISO/datetime/epoch capture time into (timestamp, iso_str, year)."""
    if value is None:
        return None, None, None

    if isinstance(value, datetime):
        dt = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
        dt_utc = dt.astimezone(UTC)
        return dt_utc.timestamp(), dt_utc.isoformat(), dt_utc.year

    if isinstance(value, date):
        if is_upper_bound:
            dt_utc = datetime(value.year, value.month, value.day, 23, 59, 59, 999999, tzinfo=UTC)
        else:
            dt_utc = datetime(value.year, value.month, value.day, 0, 0, 0, tzinfo=UTC)
        return dt_utc.timestamp(), dt_utc.isoformat(), dt_utc.year

    if isinstance(value, (int, float)):
        ts = float(value)
        dt_utc = datetime.fromtimestamp(ts, UTC)
        return ts, dt_utc.isoformat(), dt_utc.year

    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None, None, None
        # EXIF format: "YYYY:MM:DD HH:MM:SS"
        for fmt in ("%Y:%m:%d %H:%M:%S", "%Y-%m-%d %H:%M:%S"):
            try:
                parsed = datetime.strptime(text, fmt).replace(tzinfo=UTC)
                return parsed.timestamp(), parsed.isoformat(), parsed.year
            except ValueError:
                pass
        # Date-only ISO string: "YYYY-MM-DD" or "YYYY:MM:DD"
        for fmt in ("%Y-%m-%d", "%Y:%m:%d"):
            try:
                d = datetime.strptime(text, fmt).date()
                if is_upper_bound:
                    parsed = datetime(d.year, d.month, d.day, 23, 59, 59, 999999, tzinfo=UTC)
                else:
                    parsed = datetime(d.year, d.month, d.day, 0, 0, 0, tzinfo=UTC)
                return parsed.timestamp(), parsed.isoformat(), parsed.year
            except ValueError:
                pass
        # Full ISO-8601 string
        try:
            iso_candidate = text[:-1] + "+00:00" if text.endswith("Z") else text
            parsed = datetime.fromisoformat(iso_candidate)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=UTC)
            else:
                parsed = parsed.astimezone(UTC)
            return parsed.timestamp(), parsed.isoformat(), parsed.year
        except ValueError as exc:
            raise ValueError(f"Unsupported capture_time format: {value!r}") from exc

    raise TypeError(f"Unsupported capture_time type: {type(value).__name__}")


@dataclass(frozen=True)
class VolumeRecord:
    """Catalog representation of an indexed storage volume."""

    volume_key: str
    label: str = ""
    filesystem: str = ""
    last_seen: float = 0.0
    root_path: str = ""
    is_online: bool = True

    @property
    def status(self) -> str:
        return "online" if self.is_online else "offline"


@dataclass(frozen=True)
class MediaMetadata:
    """Per-file EXIF/container metadata stored in the catalog."""

    capture_time: datetime | date | float | int | str | None = None
    camera_make: str | None = None
    camera_model: str | None = None
    width: int | None = None
    height: int | None = None
    capture_timestamp: float | None = None
    capture_date: str | None = None
    capture_year: int | None = None

    def __post_init__(self) -> None:
        source_time = self.capture_time if self.capture_time is not None else self.capture_timestamp
        if source_time is None and self.capture_date is not None:
            source_time = self.capture_date
        ts, iso_str, year = _normalize_capture_time(source_time)
        if self.capture_timestamp is None and ts is not None:
            object.__setattr__(self, "capture_timestamp", ts)
        if self.capture_date is None and iso_str is not None:
            object.__setattr__(self, "capture_date", iso_str)
        if self.capture_year is None and year is not None:
            object.__setattr__(self, "capture_year", year)
        if self.capture_time is None and ts is not None:
            object.__setattr__(self, "capture_time", ts)


@dataclass(frozen=True)
class CatalogFileInput:
    """Input descriptor for incremental ``upsert_files()`` indexing."""

    relative_path: str
    size: int | None = None
    mtime_ns: int | None = None
    content_hash: str | None = None
    media_kind: str | None = None
    file_path: str | Path | None = None
    metadata: MediaMetadata | None = None
    capture_time: datetime | date | float | int | str | None = None
    camera_make: str | None = None
    camera_model: str | None = None
    width: int | None = None
    height: int | None = None

    def resolved_metadata(self) -> MediaMetadata | None:
        if self.metadata is not None:
            return self.metadata
        if any(
            value is not None
            for value in (
                self.capture_time,
                self.camera_make,
                self.camera_model,
                self.width,
                self.height,
            )
        ):
            return MediaMetadata(
                capture_time=self.capture_time,
                camera_make=self.camera_make,
                camera_model=self.camera_model,
                width=self.width,
                height=self.height,
            )
        return None


@dataclass(frozen=True)
class CatalogFileRecord:
    """Full catalog row joining ``files``, ``volumes``, and ``metadata``."""

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
    capture_timestamp: float | None = None
    capture_date: str | None = None
    capture_year: int | None = None
    camera_make: str | None = None
    camera_model: str | None = None
    width: int | None = None
    height: int | None = None

    @property
    def is_offline(self) -> bool:
        return not self.volume_online

    @property
    def metadata(self) -> MediaMetadata:
        return MediaMetadata(
            capture_time=self.capture_timestamp,
            camera_make=self.camera_make,
            camera_model=self.camera_model,
            width=self.width,
            height=self.height,
            capture_timestamp=self.capture_timestamp,
            capture_date=self.capture_date,
            capture_year=self.capture_year,
        )


@dataclass(frozen=True)
class UpsertResult:
    """Summary counts returned by ``upsert_files()`` and ``scan_volume()``."""

    added: int = 0
    updated: int = 0
    unchanged: int = 0
    removed: int = 0
    volume_status: str = "online"
    cancelled: bool = False

    @property
    def offline(self) -> bool:
        return self.volume_status == "offline"

    def __getitem__(self, key: str) -> Any:
        if hasattr(self, key):
            return getattr(self, key)
        raise KeyError(key)


@dataclass(frozen=True)
class SearchFilter:
    """Structured query filter for ``MediaCatalog.search()``."""

    media_kind: str | Sequence[str] | None = None
    filename: str | None = None
    min_size: int | None = None
    max_size: int | None = None
    min_mtime_ns: int | None = None
    max_mtime_ns: int | None = None
    capture_start: datetime | date | float | int | str | None = None
    capture_end: datetime | date | float | int | str | None = None
    camera_make: str | None = None
    camera_model: str | None = None
    volume_key: str | Sequence[str] | None = None
    has_duplicates: bool | None = None
    order_by: str = "relative_path"
    descending: bool = False
    limit: int | None = None
    offset: int = 0


@dataclass(frozen=True)
class AnalyticsBucket:
    """Single grouped aggregate row containing file count and total bytes."""

    key: str | int | None
    file_count: int
    total_bytes: int

    @property
    def count(self) -> int:
        return self.file_count

    @property
    def bytes(self) -> int:
        return self.total_bytes

    def __getitem__(self, item: str) -> Any:
        if item in {"key", "file_count", "total_bytes", "count", "bytes"}:
            return getattr(self, item)
        raise KeyError(item)


@dataclass(frozen=True)
class StorageAnalytics:
    """Complete storage breakdown across media kinds, volumes, capture years, and extensions."""

    total_files: int
    total_bytes: int
    by_media_kind: tuple[AnalyticsBucket, ...]
    by_volume: tuple[AnalyticsBucket, ...]
    by_capture_year: tuple[AnalyticsBucket, ...]
    by_extension: tuple[AnalyticsBucket, ...]
    recoverable_duplicate_bytes: int


def compute_file_sha256(
    path: str | Path,
    stop_event: StopEventProtocol | None = None,
) -> str | None:
    """Compute a full SHA-256 hex digest of ``path``, honoring ``stop_event``."""
    if _is_cancelled(stop_event):
        return None
    digest = hashlib.sha256()
    try:
        with open(path, "rb") as handle:
            while True:
                if _is_cancelled(stop_event):
                    return None
                chunk = handle.read(1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
    except OSError:
        return None
    if _is_cancelled(stop_event):
        return None
    return digest.hexdigest()


def _parse_tiff_exif(data: bytes, tiff_start: int) -> dict[str, Any]:
    if len(data) < tiff_start + 8:
        return {}
    endian_marker = data[tiff_start : tiff_start + 2]
    if endian_marker == b"II":
        order = "<"
    elif endian_marker == b"MM":
        order = ">"
    else:
        return {}

    try:
        magic, first_ifd_offset = struct.unpack_from(f"{order}HI", data, tiff_start + 2)
        if magic != 42:
            return {}
    except struct.error:
        return {}

    def read_ifd(ifd_offset: int) -> dict[int, tuple[int, int, bytes]]:
        pos = tiff_start + ifd_offset
        if pos < 0 or pos + 2 > len(data):
            return {}
        try:
            entry_count = struct.unpack_from(f"{order}H", data, pos)[0]
        except struct.error:
            return {}
        entries: dict[int, tuple[int, int, bytes]] = {}
        for idx in range(entry_count):
            entry_pos = pos + 2 + idx * 12
            if entry_pos + 12 > len(data):
                break
            tag, field_type, count, value_or_offset = struct.unpack_from(
                f"{order}HHII", data, entry_pos
            )
            type_size = {1: 1, 2: 1, 3: 2, 4: 4, 5: 8, 7: 1, 9: 4, 10: 8}.get(field_type, 1)
            total_bytes = count * type_size
            if total_bytes <= 4:
                raw_bytes = data[entry_pos + 8 : entry_pos + 8 + total_bytes]
            else:
                val_pos = tiff_start + value_or_offset
                if val_pos < 0 or val_pos + total_bytes > len(data):
                    continue
                raw_bytes = data[val_pos : val_pos + total_bytes]
            entries[tag] = (field_type, count, raw_bytes)
        return entries

    def decode_ascii(entry: tuple[int, int, bytes] | None) -> str | None:
        if entry is None or entry[0] != 2:
            return None
        text = entry[2].split(b"\x00", 1)[0].decode("ascii", errors="replace").strip()
        return text or None

    def decode_int(entry: tuple[int, int, bytes] | None) -> int | None:
        if entry is None:
            return None
        field_type, _count, raw = entry
        try:
            if field_type == 3 and len(raw) >= 2:
                return int(struct.unpack_from(f"{order}H", raw, 0)[0])
            if field_type == 4 and len(raw) >= 4:
                return int(struct.unpack_from(f"{order}I", raw, 0)[0])
        except struct.error:
            return None
        return None

    ifd0 = read_ifd(first_ifd_offset)
    exif_ifd: dict[int, tuple[int, int, bytes]] = {}
    exif_ptr = decode_int(ifd0.get(0x8769))
    if exif_ptr is not None:
        exif_ifd = read_ifd(exif_ptr)

    capture_str = (
        decode_ascii(exif_ifd.get(0x9003))
        or decode_ascii(exif_ifd.get(0x9004))
        or decode_ascii(ifd0.get(0x0132))
    )
    make = decode_ascii(ifd0.get(0x010F))
    model = decode_ascii(ifd0.get(0x0110))
    width = decode_int(exif_ifd.get(0xA002)) or decode_int(ifd0.get(0x0100))
    height = decode_int(exif_ifd.get(0xA003)) or decode_int(ifd0.get(0x0101))

    return {
        "capture_time": capture_str,
        "camera_make": make,
        "camera_model": model,
        "width": width,
        "height": height,
    }


def _parse_jpeg_dimensions(data: bytes) -> tuple[int | None, int | None]:
    if len(data) < 4 or data[:2] != b"\xff\xd8":
        return None, None
    idx = 2
    sof_markers = {
        0xC0,
        0xC1,
        0xC2,
        0xC3,
        0xC5,
        0xC6,
        0xC7,
        0xC9,
        0xCA,
        0xCB,
        0xCD,
        0xCE,
        0xCF,
    }
    while idx + 4 <= len(data):
        if data[idx] != 0xFF:
            idx += 1
            continue
        while idx < len(data) and data[idx] == 0xFF:
            idx += 1
        if idx >= len(data):
            break
        marker = data[idx]
        idx += 1
        if marker in {0xD8, 0xD9} or 0xD0 <= marker <= 0xD7:
            continue
        if idx + 2 > len(data):
            break
        seg_len = struct.unpack_from(">H", data, idx)[0]
        if seg_len < 2 or idx + seg_len > len(data):
            break
        if marker in sof_markers and seg_len >= 7:
            height, width = struct.unpack_from(">HH", data, idx + 3)
            return int(width), int(height)
        idx += seg_len
    return None, None


def extract_file_metadata(
    path: str | Path,
    stop_event: StopEventProtocol | None = None,
) -> MediaMetadata:
    """Extract EXIF capture time, camera make/model, and dimensions using stdlib only."""
    if _is_cancelled(stop_event):
        return MediaMetadata()
    try:
        with open(path, "rb") as handle:
            header = handle.read(512 * 1024)
    except OSError:
        return MediaMetadata()

    if not header or _is_cancelled(stop_event):
        return MediaMetadata()

    # PNG IHDR
    if header.startswith(b"\x89PNG\r\n\x1a\n") and len(header) >= 24 and header[12:16] == b"IHDR":
        width, height = struct.unpack_from(">II", header, 16)
        return MediaMetadata(width=int(width), height=int(height))

    # GIF87a / GIF89a
    if header[:6] in {b"GIF87a", b"GIF89a"} and len(header) >= 10:
        width, height = struct.unpack_from("<HH", header, 6)
        return MediaMetadata(width=int(width), height=int(height))

    parsed: dict[str, Any] = {}
    exif_idx = header.find(b"Exif\x00\x00")
    if exif_idx >= 0:
        parsed = _parse_tiff_exif(header, exif_idx + 6)
    elif header[:2] in {b"II", b"MM"}:
        parsed = _parse_tiff_exif(header, 0)

    width = parsed.get("width")
    height = parsed.get("height")
    if (width is None or height is None) and header.startswith(b"\xff\xd8"):
        sof_w, sof_h = _parse_jpeg_dimensions(header)
        width = width or sof_w
        height = height or sof_h

    try:
        return MediaMetadata(
            capture_time=parsed.get("capture_time"),
            camera_make=parsed.get("camera_make"),
            camera_model=parsed.get("camera_model"),
            width=width,
            height=height,
        )
    except ValueError:
        return MediaMetadata(
            camera_make=parsed.get("camera_make"),
            camera_model=parsed.get("camera_model"),
            width=width,
            height=height,
        )


_ORDER_BY_CLAUSES: dict[str, tuple[str, str]] = {
    "relative_path": (
        "f.volume_key ASC, f.relative_path ASC, f.id ASC",
        "f.volume_key DESC, f.relative_path DESC, f.id DESC",
    ),
    "path": (
        "f.volume_key ASC, f.relative_path ASC, f.id ASC",
        "f.volume_key DESC, f.relative_path DESC, f.id DESC",
    ),
    "filename": (
        "f.filename COLLATE NOCASE ASC, f.volume_key ASC, f.relative_path ASC, f.id ASC",
        "f.filename COLLATE NOCASE DESC, f.volume_key ASC, f.relative_path ASC, f.id ASC",
    ),
    "name": (
        "f.filename COLLATE NOCASE ASC, f.volume_key ASC, f.relative_path ASC, f.id ASC",
        "f.filename COLLATE NOCASE DESC, f.volume_key ASC, f.relative_path ASC, f.id ASC",
    ),
    "size": (
        "f.size ASC, f.volume_key ASC, f.relative_path ASC, f.id ASC",
        "f.size DESC, f.volume_key ASC, f.relative_path ASC, f.id ASC",
    ),
    "mtime_ns": (
        "f.mtime_ns ASC, f.volume_key ASC, f.relative_path ASC, f.id ASC",
        "f.mtime_ns DESC, f.volume_key ASC, f.relative_path ASC, f.id ASC",
    ),
    "mtime": (
        "f.mtime_ns ASC, f.volume_key ASC, f.relative_path ASC, f.id ASC",
        "f.mtime_ns DESC, f.volume_key ASC, f.relative_path ASC, f.id ASC",
    ),
    "capture_time": (
        "CASE WHEN m.capture_timestamp IS NULL THEN 1 ELSE 0 END ASC, "
        "m.capture_timestamp ASC, f.volume_key ASC, f.relative_path ASC, f.id ASC",
        "CASE WHEN m.capture_timestamp IS NULL THEN 1 ELSE 0 END ASC, "
        "m.capture_timestamp DESC, f.volume_key ASC, f.relative_path ASC, f.id ASC",
    ),
    "capture_date": (
        "CASE WHEN m.capture_timestamp IS NULL THEN 1 ELSE 0 END ASC, "
        "m.capture_timestamp ASC, f.volume_key ASC, f.relative_path ASC, f.id ASC",
        "CASE WHEN m.capture_timestamp IS NULL THEN 1 ELSE 0 END ASC, "
        "m.capture_timestamp DESC, f.volume_key ASC, f.relative_path ASC, f.id ASC",
    ),
    "indexed_at": (
        "f.indexed_at ASC, f.volume_key ASC, f.relative_path ASC, f.id ASC",
        "f.indexed_at DESC, f.volume_key ASC, f.relative_path ASC, f.id ASC",
    ),
    "id": (
        "f.id ASC",
        "f.id DESC",
    ),
}


def _escape_like_pattern(substring: str) -> str:
    """Escape SQLite LIKE metacharacters so user input is treated strictly as literal data."""
    escaped = (
        substring.replace("\\", "\\\\")
        .replace("%", "\\%")
        .replace("_", "\\_")
    )
    return f"%{escaped}%"


class MediaCatalog:
    """SQLite-backed media catalog supporting incremental scans, search, and storage analytics."""

    def __init__(
        self,
        db_path: str | Path,
        *,
        busy_timeout_ms: int = DEFAULT_BUSY_TIMEOUT_MS,
        target_version: int = SCHEMA_VERSION,
        migrations: Mapping[int, MigrationStep] | None = None,
    ) -> None:
        self.db_path = Path(db_path)
        self.busy_timeout_ms = int(busy_timeout_ms)
        if str(self.db_path) != ":memory:":
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn: sqlite3.Connection | None = sqlite3.connect(
            str(self.db_path),
            timeout=max(0.1, self.busy_timeout_ms / 1000.0),
            isolation_level=None,
        )
        self._conn.row_factory = sqlite3.Row
        try:
            initialize_schema(
                self._conn,
                busy_timeout_ms=self.busy_timeout_ms,
                target_version=target_version,
                migrations=migrations,
            )
        except Exception:
            self.close()
            raise

    @property
    def connection(self) -> sqlite3.Connection:
        if self._conn is None:
            raise sqlite3.ProgrammingError("MediaCatalog connection is closed.")
        return self._conn

    @property
    def user_version(self) -> int:
        return get_user_version(self.connection)

    def __enter__(self) -> MediaCatalog:
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self.close()

    def close(self) -> None:
        """Close the underlying SQLite connection safely."""
        if self._conn is not None:
            try:
                if self._conn.in_transaction:
                    self._conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            try:
                self._conn.close()
            finally:
                self._conn = None

    def _begin(self) -> None:
        self.connection.execute("BEGIN IMMEDIATE")

    def _commit(self) -> None:
        self.connection.execute("COMMIT")

    def _rollback(self) -> None:
        if self.connection.in_transaction:
            self.connection.execute("ROLLBACK")

    # ------------------------------------------------------------------
    # Volume management & offline state
    # ------------------------------------------------------------------

    def upsert_volume(
        self,
        volume_key: str,
        *,
        label: str = "",
        filesystem: str = "",
        root_path: str | Path = "",
        is_online: bool = True,
        last_seen: float | None = None,
    ) -> VolumeRecord:
        """Register or update a storage volume."""
        key = str(volume_key).strip()
        if not key:
            raise ValueError("volume_key must not be empty")
        seen_ts = float(last_seen if last_seen is not None else time.time())
        root_str = str(root_path) if root_path else ""
        online_int = 1 if is_online else 0

        self._begin()
        try:
            existing = self.connection.execute(
                "SELECT label, filesystem, root_path, last_seen FROM volumes WHERE volume_key = ?",
                (key,),
            ).fetchone()
            if existing is None:
                self.connection.execute(
                    """
                    INSERT INTO volumes (volume_key, label, filesystem, root_path, is_online, last_seen)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (key, label, filesystem, root_str, online_int, seen_ts),
                )
            else:
                new_label = label if label != "" else str(existing["label"])
                new_fs = filesystem if filesystem != "" else str(existing["filesystem"])
                new_root = root_str if root_str != "" else str(existing["root_path"])
                new_seen = seen_ts if is_online or last_seen is not None else float(existing["last_seen"])
                self.connection.execute(
                    """
                    UPDATE volumes
                    SET label = ?, filesystem = ?, root_path = ?, is_online = ?, last_seen = ?
                    WHERE volume_key = ?
                    """,
                    (new_label, new_fs, new_root, online_int, new_seen, key),
                )
            self._commit()
        except Exception:
            self._rollback()
            raise

        vol = self.get_volume(key)
        assert vol is not None
        return vol

    def set_volume_online(self, volume_key: str, is_online: bool) -> VolumeRecord | None:
        """Mark a known volume online or offline without modifying or deleting its file rows."""
        key = str(volume_key).strip()
        if not key:
            raise ValueError("volume_key must not be empty")
        existing = self.get_volume(key)
        if existing is None:
            return None
        if existing.is_online == bool(is_online):
            return existing
        self._begin()
        try:
            if is_online:
                self.connection.execute(
                    "UPDATE volumes SET is_online = 1, last_seen = ? WHERE volume_key = ?",
                    (time.time(), key),
                )
            else:
                self.connection.execute(
                    "UPDATE volumes SET is_online = 0 WHERE volume_key = ?",
                    (key,),
                )
            self._commit()
        except Exception:
            self._rollback()
            raise
        return self.get_volume(key)

    def mark_volume_offline(self, volume_key: str) -> VolumeRecord | None:
        """Mark ``volume_key`` offline while preserving all of its indexed files."""
        return self.set_volume_online(volume_key, False)

    def mark_volume_online(self, volume_key: str) -> VolumeRecord | None:
        """Mark ``volume_key`` online."""
        return self.set_volume_online(volume_key, True)

    def refresh_volume_statuses(
        self,
        present_volume_keys: Iterable[str] | None = None,
    ) -> dict[str, str]:
        """Update and return online/offline status for all cataloged volumes.

        If ``present_volume_keys`` is provided, only volumes in that collection
        are marked online and all others are marked offline. Otherwise, each
        volume with a recorded ``root_path`` is checked on disk via
        ``Path(root_path).exists()``. Offline volumes are never deleted.
        """
        present_set = (
            {str(k).strip() for k in present_volume_keys}
            if present_volume_keys is not None
            else None
        )
        volumes = self.list_volumes()
        statuses: dict[str, str] = {}
        self._begin()
        try:
            now = time.time()
            for vol in volumes:
                if present_set is not None:
                    online = vol.volume_key in present_set
                elif vol.root_path:
                    online = os.path.exists(vol.root_path)
                else:
                    online = vol.is_online
                if online != vol.is_online:
                    if online:
                        self.connection.execute(
                            "UPDATE volumes SET is_online = 1, last_seen = ? WHERE volume_key = ?",
                            (now, vol.volume_key),
                        )
                    else:
                        self.connection.execute(
                            "UPDATE volumes SET is_online = 0 WHERE volume_key = ?",
                            (vol.volume_key,),
                        )
                statuses[vol.volume_key] = "online" if online else "offline"
            self._commit()
        except Exception:
            self._rollback()
            raise
        return statuses

    def get_volume(self, volume_key: str) -> VolumeRecord | None:
        """Return the ``VolumeRecord`` for ``volume_key``, or ``None`` if not registered."""
        row = self.connection.execute(
            """
            SELECT volume_key, label, filesystem, last_seen, root_path, is_online
            FROM volumes
            WHERE volume_key = ?
            """,
            (str(volume_key).strip(),),
        ).fetchone()
        if row is None:
            return None
        return VolumeRecord(
            volume_key=str(row["volume_key"]),
            label=str(row["label"]),
            filesystem=str(row["filesystem"]),
            last_seen=float(row["last_seen"]),
            root_path=str(row["root_path"]),
            is_online=bool(row["is_online"]),
        )

    def list_volumes(self) -> list[VolumeRecord]:
        """Return all registered volumes ordered by ``volume_key``."""
        rows = self.connection.execute(
            """
            SELECT volume_key, label, filesystem, last_seen, root_path, is_online
            FROM volumes
            ORDER BY volume_key ASC
            """
        ).fetchall()
        return [
            VolumeRecord(
                volume_key=str(row["volume_key"]),
                label=str(row["label"]),
                filesystem=str(row["filesystem"]),
                last_seen=float(row["last_seen"]),
                root_path=str(row["root_path"]),
                is_online=bool(row["is_online"]),
            )
            for row in rows
        ]

    # ------------------------------------------------------------------
    # Explicit removal (must name the volume)
    # ------------------------------------------------------------------

    def remove_volume(self, volume_key: str) -> int:
        """Explicitly remove ``volume_key`` and all of its file and metadata rows.

        Returns the number of file rows removed.
        """
        key = str(volume_key).strip()
        if not key:
            raise ValueError("volume_key must be explicitly named to remove volume rows")
        self._begin()
        try:
            count_row = self.connection.execute(
                "SELECT COUNT(*) FROM files WHERE volume_key = ?",
                (key,),
            ).fetchone()
            removed_files = int(count_row[0]) if count_row is not None else 0
            self.connection.execute("DELETE FROM volumes WHERE volume_key = ?", (key,))
            self._commit()
            return removed_files
        except Exception:
            self._rollback()
            raise

    def remove_volume_files(
        self,
        volume_key: str,
        relative_paths: Iterable[str | Path] | None = None,
    ) -> int:
        """Explicitly remove specific (or all) file rows belonging to ``volume_key``."""
        key = str(volume_key).strip()
        if not key:
            raise ValueError("volume_key must be explicitly named to remove file rows")
        self._begin()
        try:
            if relative_paths is None:
                cursor = self.connection.execute(
                    "DELETE FROM files WHERE volume_key = ?",
                    (key,),
                )
                removed = int(cursor.rowcount)
            else:
                normalized_paths = [normalize_relative_path(p) for p in relative_paths]
                removed = 0
                for rel_path in normalized_paths:
                    cursor = self.connection.execute(
                        "DELETE FROM files WHERE volume_key = ? AND relative_path = ?",
                        (key, rel_path),
                    )
                    removed += int(cursor.rowcount)
            self._commit()
            return removed
        except Exception:
            self._rollback()
            raise

    # ------------------------------------------------------------------
    # Incremental indexing
    # ------------------------------------------------------------------

    def _iter_volume_root_inputs(
        self,
        root: Path,
        stop_event: StopEventProtocol | None = None,
    ) -> Iterator[CatalogFileInput]:
        for dirpath, dirnames, filenames in os.walk(root):
            if _is_cancelled(stop_event):
                return
            dirnames.sort()
            filenames.sort()
            for fname in filenames:
                if _is_cancelled(stop_event):
                    return
                full_path = Path(dirpath) / fname
                try:
                    st = full_path.stat()
                except OSError:
                    continue
                rel = normalize_relative_path(full_path.relative_to(root))
                yield CatalogFileInput(
                    relative_path=rel,
                    size=int(st.st_size),
                    mtime_ns=int(st.st_mtime_ns),
                    file_path=full_path,
                )

    def _coerce_file_input(
        self,
        item: CatalogFileInput | Mapping[str, Any] | str | Path,
        volume_root: Path | None,
    ) -> CatalogFileInput:
        if isinstance(item, CatalogFileInput):
            file_path = Path(item.file_path) if item.file_path is not None else None
            if file_path is None and volume_root is not None:
                candidate = volume_root / normalize_relative_path(item.relative_path)
                if candidate.exists():
                    file_path = candidate
            size = item.size
            mtime_ns = item.mtime_ns
            if (size is None or mtime_ns is None) and file_path is not None:
                st = file_path.stat()
                size = int(st.st_size) if size is None else int(size)
                mtime_ns = int(st.st_mtime_ns) if mtime_ns is None else int(mtime_ns)
            if size is None or mtime_ns is None:
                raise ValueError(
                    f"size and mtime_ns are required when file is not on disk: {item.relative_path!r}"
                )
            return CatalogFileInput(
                relative_path=normalize_relative_path(item.relative_path),
                size=int(size),
                mtime_ns=int(mtime_ns),
                content_hash=item.content_hash,
                media_kind=item.media_kind,
                file_path=file_path,
                metadata=item.resolved_metadata(),
            )

        if isinstance(item, (str, Path)):
            path_obj = Path(item)
            if volume_root is not None:
                if path_obj.is_absolute():
                    rel_path = normalize_relative_path(path_obj.relative_to(volume_root))
                    full_path = path_obj
                else:
                    rel_path = normalize_relative_path(path_obj)
                    full_path = volume_root / rel_path
            else:
                full_path = path_obj
                rel_path = normalize_relative_path(path_obj.name)
            st = full_path.stat()
            return CatalogFileInput(
                relative_path=rel_path,
                size=int(st.st_size),
                mtime_ns=int(st.st_mtime_ns),
                file_path=full_path,
            )

        if isinstance(item, Mapping):
            raw_meta = item.get("metadata")
            meta_obj: MediaMetadata | None
            if isinstance(raw_meta, MediaMetadata):
                meta_obj = raw_meta
            elif isinstance(raw_meta, Mapping):
                meta_obj = MediaMetadata(
                    capture_time=raw_meta.get("capture_time", raw_meta.get("capture_timestamp")),
                    camera_make=raw_meta.get("camera_make"),
                    camera_model=raw_meta.get("camera_model"),
                    width=raw_meta.get("width"),
                    height=raw_meta.get("height"),
                )
            else:
                meta_obj = None

            candidate_input = CatalogFileInput(
                relative_path=str(item["relative_path"]),
                size=item.get("size"),
                mtime_ns=item.get("mtime_ns"),
                content_hash=item.get("content_hash"),
                media_kind=item.get("media_kind"),
                file_path=item.get("file_path") or item.get("path"),
                metadata=meta_obj,
                capture_time=item.get("capture_time"),
                camera_make=item.get("camera_make"),
                camera_model=item.get("camera_model"),
                width=item.get("width"),
                height=item.get("height"),
            )
            return self._coerce_file_input(candidate_input, volume_root)

        raise TypeError(f"Unsupported file input type: {type(item).__name__}")

    def upsert_files(
        self,
        volume_key: str,
        files: Iterable[CatalogFileInput | Mapping[str, Any] | str | Path] | None = None,
        *,
        volume_root: str | Path | None = None,
        label: str = "",
        filesystem: str = "",
        volume_online: bool | None = None,
        compute_hashes: bool = True,
        extract_metadata: bool = True,
        hash_fn: Callable[[Path], str | None] | None = None,
        metadata_fn: Callable[[Path], MediaMetadata | None] | None = None,
        remove_missing: bool = False,
        stop_event: StopEventProtocol | None = None,
    ) -> UpsertResult:
        """Incrementally index files for ``volume_key``.

        Treats ``(volume_key, relative_path)`` as the row identity and computes
        hashes or reads metadata ONLY when a file is newly added or its
        ``size`` or ``mtime_ns`` has changed. When nothing has changed on a
        known online volume, performs zero SQLite writes.

        If ``volume_online is False`` or ``volume_root`` does not exist on disk
        when scanning a directory tree, the volume is marked ``"offline"`` and
        its existing catalog rows are preserved untouched.
        """
        key = str(volume_key).strip()
        if not key:
            raise ValueError("volume_key must not be empty")

        if _is_cancelled(stop_event):
            return UpsertResult(volume_status="cancelled", cancelled=True)

        root_path = Path(volume_root) if volume_root is not None else None
        if volume_online is False or (root_path is not None and not root_path.exists()):
            self.mark_volume_offline(key)
            return UpsertResult(
                added=0,
                updated=0,
                unchanged=0,
                removed=0,
                volume_status="offline",
            )

        if files is None:
            if root_path is None:
                raise ValueError("Either 'files' or 'volume_root' must be provided to upsert_files()")
            source_iter: Iterable[CatalogFileInput | Mapping[str, Any] | str | Path] = (
                self._iter_volume_root_inputs(root_path, stop_event=stop_event)
            )
        else:
            source_iter = files

        existing_vol = self.get_volume(key)
        existing_rows = self.connection.execute(
            """
            SELECT id, relative_path, size, mtime_ns
            FROM files
            WHERE volume_key = ?
            """,
            (key,),
        ).fetchall()
        existing_by_path: dict[str, tuple[int, int, int]] = {
            str(row["relative_path"]): (
                int(row["id"]),
                int(row["size"]),
                int(row["mtime_ns"]),
            )
            for row in existing_rows
        }

        added = 0
        updated = 0
        unchanged = 0
        seen_paths: set[str] = set()

        # Prepared operations:
        # insert_ops: (rel_path, filename, ext, size, mtime_ns, content_hash, media_kind, indexed_at, meta)
        insert_ops: list[
            tuple[str, str, str, int, int, str | None, str, float, MediaMetadata | None]
        ] = []
        # update_ops: (file_id, filename, ext, size, mtime_ns, content_hash, media_kind, indexed_at, meta)
        update_ops: list[
            tuple[int, str, str, int, int, str | None, str, float, MediaMetadata | None]
        ] = []

        now = time.time()

        for raw_item in source_iter:
            if _is_cancelled(stop_event):
                return UpsertResult(volume_status="cancelled", cancelled=True)

            coerced = self._coerce_file_input(raw_item, root_path)
            rel_path = coerced.relative_path
            seen_paths.add(rel_path)
            assert coerced.size is not None and coerced.mtime_ns is not None
            size = int(coerced.size)
            mtime_ns = int(coerced.mtime_ns)

            existing_entry = existing_by_path.get(rel_path)
            if existing_entry is not None:
                file_id, old_size, old_mtime_ns = existing_entry
                if old_size == size and old_mtime_ns == mtime_ns:
                    unchanged += 1
                    continue

            # File is either new or has a changed size/mtime_ns: compute hash and metadata now.
            if _is_cancelled(stop_event):
                return UpsertResult(volume_status="cancelled", cancelled=True)

            file_path_obj = Path(coerced.file_path) if coerced.file_path is not None else None
            content_hash = coerced.content_hash
            if content_hash is None and compute_hashes and file_path_obj is not None:
                if hash_fn is not None:
                    content_hash = hash_fn(file_path_obj)
                else:
                    content_hash = compute_file_sha256(file_path_obj, stop_event=stop_event)
                if _is_cancelled(stop_event):
                    return UpsertResult(volume_status="cancelled", cancelled=True)

            meta = coerced.resolved_metadata()
            if meta is None and extract_metadata and file_path_obj is not None:
                if metadata_fn is not None:
                    meta = metadata_fn(file_path_obj)
                else:
                    meta = extract_file_metadata(file_path_obj, stop_event=stop_event)
                if _is_cancelled(stop_event):
                    return UpsertResult(volume_status="cancelled", cancelled=True)

            filename, ext = _extract_filename_and_extension(rel_path)
            media_kind = (
                coerced.media_kind.strip().lower()
                if coerced.media_kind
                else classify_media_kind(ext or filename)
            )

            if existing_entry is None:
                insert_ops.append(
                    (rel_path, filename, ext, size, mtime_ns, content_hash, media_kind, now, meta)
                )
                added += 1
            else:
                file_id = existing_entry[0]
                update_ops.append(
                    (file_id, filename, ext, size, mtime_ns, content_hash, media_kind, now, meta)
                )
                updated += 1

        if _is_cancelled(stop_event):
            return UpsertResult(volume_status="cancelled", cancelled=True)

        missing_ids: list[int] = []
        if remove_missing:
            for rel_path, (file_id, _old_size, _old_mtime) in existing_by_path.items():
                if rel_path not in seen_paths:
                    missing_ids.append(file_id)

        root_str = str(root_path) if root_path is not None else ""
        volume_needs_write = (
            existing_vol is None
            or not existing_vol.is_online
            or (bool(label) and label != existing_vol.label)
            or (bool(filesystem) and filesystem != existing_vol.filesystem)
            or (bool(root_str) and root_str != existing_vol.root_path)
        )

        # If nothing changed at all on an already-online volume, perform zero writes!
        if not insert_ops and not update_ops and not missing_ids and not volume_needs_write:
            return UpsertResult(
                added=0,
                updated=0,
                unchanged=unchanged,
                removed=0,
                volume_status="online",
            )

        self._begin()
        try:
            if existing_vol is None:
                self.connection.execute(
                    """
                    INSERT INTO volumes (volume_key, label, filesystem, root_path, is_online, last_seen)
                    VALUES (?, ?, ?, ?, 1, ?)
                    """,
                    (key, label, filesystem, root_str, now),
                )
            else:
                new_label = label if label != "" else existing_vol.label
                new_fs = filesystem if filesystem != "" else existing_vol.filesystem
                new_root = root_str if root_str != "" else existing_vol.root_path
                self.connection.execute(
                    """
                    UPDATE volumes
                    SET label = ?, filesystem = ?, root_path = ?, is_online = 1, last_seen = ?
                    WHERE volume_key = ?
                    """,
                    (new_label, new_fs, new_root, now, key),
                )

            for (
                rel_path,
                filename,
                ext,
                size,
                mtime_ns,
                content_hash,
                media_kind,
                indexed_at,
                meta,
            ) in insert_ops:
                if _is_cancelled(stop_event):
                    self._rollback()
                    return UpsertResult(volume_status="cancelled", cancelled=True)
                cursor = self.connection.execute(
                    """
                    INSERT INTO files (
                        volume_key, relative_path, filename, extension,
                        size, mtime_ns, content_hash, media_kind, indexed_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        key,
                        rel_path,
                        filename,
                        ext,
                        size,
                        mtime_ns,
                        content_hash,
                        media_kind,
                        indexed_at,
                    ),
                )
                new_file_id = int(cursor.lastrowid)
                self._upsert_metadata_row(new_file_id, meta)

            for (
                file_id,
                filename,
                ext,
                size,
                mtime_ns,
                content_hash,
                media_kind,
                indexed_at,
                meta,
            ) in update_ops:
                if _is_cancelled(stop_event):
                    self._rollback()
                    return UpsertResult(volume_status="cancelled", cancelled=True)
                self.connection.execute(
                    """
                    UPDATE files
                    SET filename = ?, extension = ?, size = ?, mtime_ns = ?,
                        content_hash = ?, media_kind = ?, indexed_at = ?
                    WHERE id = ?
                    """,
                    (
                        filename,
                        ext,
                        size,
                        mtime_ns,
                        content_hash,
                        media_kind,
                        indexed_at,
                        file_id,
                    ),
                )
                self._upsert_metadata_row(file_id, meta)

            removed_count = 0
            for file_id in missing_ids:
                if _is_cancelled(stop_event):
                    self._rollback()
                    return UpsertResult(volume_status="cancelled", cancelled=True)
                cursor = self.connection.execute(
                    "DELETE FROM files WHERE id = ?",
                    (file_id,),
                )
                removed_count += int(cursor.rowcount)

            self._commit()
            return UpsertResult(
                added=added,
                updated=updated,
                unchanged=unchanged,
                removed=removed_count,
                volume_status="online",
            )
        except Exception:
            self._rollback()
            raise

    def scan_volume(
        self,
        volume_key: str,
        volume_root: str | Path,
        *,
        label: str = "",
        filesystem: str = "",
        compute_hashes: bool = True,
        extract_metadata: bool = True,
        hash_fn: Callable[[Path], str | None] | None = None,
        metadata_fn: Callable[[Path], MediaMetadata | None] | None = None,
        remove_missing: bool = False,
        stop_event: StopEventProtocol | None = None,
    ) -> UpsertResult:
        """Scan ``volume_root`` on disk and incrementally update ``volume_key``.

        If ``volume_root`` does not exist on disk, preserves all existing rows
        for ``volume_key`` and marks the volume as ``"offline"``.
        """
        return self.upsert_files(
            volume_key,
            None,
            volume_root=volume_root,
            label=label,
            filesystem=filesystem,
            compute_hashes=compute_hashes,
            extract_metadata=extract_metadata,
            hash_fn=hash_fn,
            metadata_fn=metadata_fn,
            remove_missing=remove_missing,
            stop_event=stop_event,
        )

    def _upsert_metadata_row(self, file_id: int, meta: MediaMetadata | None) -> None:
        resolved = meta or MediaMetadata()
        self.connection.execute(
            """
            INSERT INTO metadata (
                file_id, capture_timestamp, capture_date, capture_year,
                camera_make, camera_model, width, height
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(file_id) DO UPDATE SET
                capture_timestamp = excluded.capture_timestamp,
                capture_date = excluded.capture_date,
                capture_year = excluded.capture_year,
                camera_make = excluded.camera_make,
                camera_model = excluded.camera_model,
                width = excluded.width,
                height = excluded.height
            """,
            (
                file_id,
                resolved.capture_timestamp,
                resolved.capture_date,
                resolved.capture_year,
                resolved.camera_make,
                resolved.camera_model,
                resolved.width,
                resolved.height,
            ),
        )

    # ------------------------------------------------------------------
    # Search & Lookup API
    # ------------------------------------------------------------------

    def get_file(self, volume_key: str, relative_path: str | Path) -> CatalogFileRecord | None:
        """Fetch a single catalog file record by ``(volume_key, relative_path)``."""
        norm_rel = normalize_relative_path(relative_path)
        rows = self.connection.execute(
            """
            SELECT
                f.id, f.volume_key, f.relative_path, f.filename, f.extension,
                f.size, f.mtime_ns, f.content_hash, f.media_kind, f.indexed_at,
                v.is_online AS volume_online,
                m.capture_timestamp, m.capture_date, m.capture_year,
                m.camera_make, m.camera_model, m.width, m.height
            FROM files f
            JOIN volumes v ON v.volume_key = f.volume_key
            LEFT JOIN metadata m ON m.file_id = f.id
            WHERE f.volume_key = ? AND f.relative_path = ?
            """,
            (str(volume_key).strip(), norm_rel),
        ).fetchall()
        if not rows:
            return None
        return self._row_to_file_record(rows[0])

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
        camera_make: str | None = None,
        camera_model: str | None = None,
        volume_key: str | Sequence[str] | None = None,
        volume: str | Sequence[str] | None = None,
        has_duplicates: bool | None = None,
        order_by: str | None = None,
        descending: bool | None = None,
        limit: int | None = None,
        offset: int | None = None,
        stop_event: StopEventProtocol | None = None,
    ) -> list[CatalogFileRecord]:
        """Search indexed files using parameterized SQL predicates and deterministic paging."""
        if _is_cancelled(stop_event):
            return []

        spec = filter_spec or SearchFilter()
        eff_media_kind = media_kind if media_kind is not None else spec.media_kind
        eff_filename = (
            filename_substring
            if filename_substring is not None
            else (filename if filename is not None else spec.filename)
        )
        eff_min_size = min_size if min_size is not None else spec.min_size
        eff_max_size = max_size if max_size is not None else spec.max_size
        if size_range is not None:
            if size_range[0] is not None:
                eff_min_size = size_range[0]
            if size_range[1] is not None:
                eff_max_size = size_range[1]

        eff_min_mtime = min_mtime_ns if min_mtime_ns is not None else spec.min_mtime_ns
        eff_max_mtime = max_mtime_ns if max_mtime_ns is not None else spec.max_mtime_ns
        if mtime_range is not None:
            if mtime_range[0] is not None:
                eff_min_mtime = mtime_range[0]
            if mtime_range[1] is not None:
                eff_max_mtime = mtime_range[1]

        eff_cap_start = capture_start if capture_start is not None else spec.capture_start
        eff_cap_end = capture_end if capture_end is not None else spec.capture_end
        if capture_date_range is not None:
            if capture_date_range[0] is not None:
                eff_cap_start = capture_date_range[0]
            if capture_date_range[1] is not None:
                eff_cap_end = capture_date_range[1]

        eff_make = camera_make if camera_make is not None else spec.camera_make
        eff_model = camera_model if camera_model is not None else spec.camera_model
        eff_volume = (
            volume_key
            if volume_key is not None
            else (volume if volume is not None else spec.volume_key)
        )
        eff_dups = has_duplicates if has_duplicates is not None else spec.has_duplicates
        eff_order_by = order_by if order_by is not None else spec.order_by
        eff_descending = descending if descending is not None else spec.descending
        eff_limit = limit if limit is not None else spec.limit
        eff_offset = offset if offset is not None else spec.offset

        order_key = eff_order_by.strip().lower()
        if order_key not in _ORDER_BY_CLAUSES:
            raise ValueError(f"Unsupported order_by column: {eff_order_by!r}")
        order_clause = _ORDER_BY_CLAUSES[order_key][1 if eff_descending else 0]

        if eff_offset < 0:
            raise ValueError("offset must be >= 0")
        if eff_limit is not None and eff_limit < 0:
            raise ValueError("limit must be >= 0")

        where_clauses: list[str] = []
        params: list[Any] = []

        if eff_media_kind is not None:
            if isinstance(eff_media_kind, str):
                where_clauses.append("f.media_kind = ?")
                params.append(eff_media_kind.strip().lower())
            else:
                kinds = [str(k).strip().lower() for k in eff_media_kind]
                if not kinds:
                    return []
                placeholders = ", ".join("?" for _ in kinds)
                where_clauses.append(f"f.media_kind IN ({placeholders})")
                params.extend(kinds)

        if eff_filename is not None and eff_filename != "":
            where_clauses.append("f.filename LIKE ? ESCAPE '\\' COLLATE NOCASE")
            params.append(_escape_like_pattern(str(eff_filename)))

        if eff_min_size is not None:
            where_clauses.append("f.size >= ?")
            params.append(int(eff_min_size))

        if eff_max_size is not None:
            where_clauses.append("f.size <= ?")
            params.append(int(eff_max_size))

        if eff_min_mtime is not None:
            where_clauses.append("f.mtime_ns >= ?")
            params.append(int(eff_min_mtime))

        if eff_max_mtime is not None:
            where_clauses.append("f.mtime_ns <= ?")
            params.append(int(eff_max_mtime))

        if eff_cap_start is not None:
            start_ts, _, _ = _normalize_capture_time(eff_cap_start, is_upper_bound=False)
            if start_ts is not None:
                where_clauses.append("m.capture_timestamp >= ?")
                params.append(start_ts)

        if eff_cap_end is not None:
            end_ts, _, _ = _normalize_capture_time(eff_cap_end, is_upper_bound=True)
            if end_ts is not None:
                where_clauses.append("m.capture_timestamp <= ?")
                params.append(end_ts)

        if eff_make is not None:
            where_clauses.append("m.camera_make = ? COLLATE NOCASE")
            params.append(str(eff_make).strip())

        if eff_model is not None:
            where_clauses.append("m.camera_model = ? COLLATE NOCASE")
            params.append(str(eff_model).strip())

        if eff_volume is not None:
            if isinstance(eff_volume, str):
                where_clauses.append("f.volume_key = ?")
                params.append(eff_volume.strip())
            else:
                vols = [str(v).strip() for v in eff_volume]
                if not vols:
                    return []
                placeholders = ", ".join("?" for _ in vols)
                where_clauses.append(f"f.volume_key IN ({placeholders})")
                params.extend(vols)

        if eff_dups is True:
            where_clauses.append(
                "f.content_hash IS NOT NULL AND f.content_hash != '' AND f.content_hash IN ("
                "SELECT content_hash FROM files "
                "WHERE content_hash IS NOT NULL AND content_hash != '' "
                "GROUP BY content_hash HAVING COUNT(*) > 1"
                ")"
            )
        elif eff_dups is False:
            where_clauses.append(
                "(f.content_hash IS NULL OR f.content_hash = '' OR f.content_hash IN ("
                "SELECT content_hash FROM files "
                "WHERE content_hash IS NOT NULL AND content_hash != '' "
                "GROUP BY content_hash HAVING COUNT(*) = 1"
                "))"
            )

        query_parts = [
            """
            SELECT
                f.id, f.volume_key, f.relative_path, f.filename, f.extension,
                f.size, f.mtime_ns, f.content_hash, f.media_kind, f.indexed_at,
                v.is_online AS volume_online,
                m.capture_timestamp, m.capture_date, m.capture_year,
                m.camera_make, m.camera_model, m.width, m.height
            FROM files f
            JOIN volumes v ON v.volume_key = f.volume_key
            LEFT JOIN metadata m ON m.file_id = f.id
            """
        ]
        if where_clauses:
            query_parts.append("WHERE " + " AND ".join(where_clauses))
        query_parts.append(f"ORDER BY {order_clause}")

        if eff_limit is not None:
            query_parts.append("LIMIT ? OFFSET ?")
            params.extend([int(eff_limit), int(eff_offset)])
        elif eff_offset > 0:
            query_parts.append("LIMIT -1 OFFSET ?")
            params.append(int(eff_offset))

        sql = "\n".join(query_parts)
        cursor = self.connection.execute(sql, tuple(params))
        results: list[CatalogFileRecord] = []
        for row in cursor:
            if _is_cancelled(stop_event):
                break
            results.append(self._row_to_file_record(row))
        return results

    @staticmethod
    def _row_to_file_record(row: sqlite3.Row) -> CatalogFileRecord:
        online = bool(row["volume_online"])
        return CatalogFileRecord(
            id=int(row["id"]),
            volume_key=str(row["volume_key"]),
            relative_path=str(row["relative_path"]),
            filename=str(row["filename"]),
            extension=str(row["extension"]),
            size=int(row["size"]),
            mtime_ns=int(row["mtime_ns"]),
            content_hash=str(row["content_hash"]) if row["content_hash"] is not None else None,
            media_kind=str(row["media_kind"]),
            indexed_at=float(row["indexed_at"]),
            volume_online=online,
            volume_status="online" if online else "offline",
            capture_timestamp=(
                float(row["capture_timestamp"])
                if row["capture_timestamp"] is not None
                else None
            ),
            capture_date=str(row["capture_date"]) if row["capture_date"] is not None else None,
            capture_year=int(row["capture_year"]) if row["capture_year"] is not None else None,
            camera_make=str(row["camera_make"]) if row["camera_make"] is not None else None,
            camera_model=str(row["camera_model"]) if row["camera_model"] is not None else None,
            width=int(row["width"]) if row["width"] is not None else None,
            height=int(row["height"]) if row["height"] is not None else None,
        )

    # ------------------------------------------------------------------
    # Storage analytics
    # ------------------------------------------------------------------

    def analytics_by_media_kind(
        self,
        *,
        stop_event: StopEventProtocol | None = None,
    ) -> list[AnalyticsBucket]:
        """Return file counts and total bytes grouped by ``media_kind``."""
        if _is_cancelled(stop_event):
            return []
        rows = self.connection.execute(
            """
            SELECT media_kind, COUNT(*) AS file_count, COALESCE(SUM(size), 0) AS total_bytes
            FROM files
            GROUP BY media_kind
            ORDER BY total_bytes DESC, media_kind ASC
            """
        ).fetchall()
        return [
            AnalyticsBucket(
                key=str(row["media_kind"]),
                file_count=int(row["file_count"]),
                total_bytes=int(row["total_bytes"]),
            )
            for row in rows
        ]

    def analytics_by_volume(
        self,
        *,
        include_empty: bool = False,
        stop_event: StopEventProtocol | None = None,
    ) -> list[AnalyticsBucket]:
        """Return file counts and total bytes grouped by ``volume_key``."""
        if _is_cancelled(stop_event):
            return []
        if include_empty:
            rows = self.connection.execute(
                """
                SELECT
                    v.volume_key AS volume_key,
                    COUNT(f.id) AS file_count,
                    COALESCE(SUM(f.size), 0) AS total_bytes
                FROM volumes v
                LEFT JOIN files f ON f.volume_key = v.volume_key
                GROUP BY v.volume_key
                ORDER BY total_bytes DESC, v.volume_key ASC
                """
            ).fetchall()
        else:
            rows = self.connection.execute(
                """
                SELECT volume_key, COUNT(*) AS file_count, COALESCE(SUM(size), 0) AS total_bytes
                FROM files
                GROUP BY volume_key
                ORDER BY total_bytes DESC, volume_key ASC
                """
            ).fetchall()
        return [
            AnalyticsBucket(
                key=str(row["volume_key"]),
                file_count=int(row["file_count"]),
                total_bytes=int(row["total_bytes"]),
            )
            for row in rows
        ]

    def analytics_by_capture_year(
        self,
        *,
        include_unknown: bool = False,
        stop_event: StopEventProtocol | None = None,
    ) -> list[AnalyticsBucket]:
        """Return file counts and total bytes grouped by integer EXIF capture year."""
        if _is_cancelled(stop_event):
            return []
        if include_unknown:
            rows = self.connection.execute(
                """
                SELECT
                    m.capture_year AS capture_year,
                    COUNT(f.id) AS file_count,
                    COALESCE(SUM(f.size), 0) AS total_bytes
                FROM files f
                LEFT JOIN metadata m ON m.file_id = f.id
                GROUP BY m.capture_year
                ORDER BY
                    CASE WHEN m.capture_year IS NULL THEN 1 ELSE 0 END ASC,
                    m.capture_year ASC
                """
            ).fetchall()
        else:
            rows = self.connection.execute(
                """
                SELECT
                    m.capture_year AS capture_year,
                    COUNT(f.id) AS file_count,
                    COALESCE(SUM(f.size), 0) AS total_bytes
                FROM files f
                JOIN metadata m ON m.file_id = f.id
                WHERE m.capture_year IS NOT NULL
                GROUP BY m.capture_year
                ORDER BY m.capture_year ASC
                """
            ).fetchall()
        return [
            AnalyticsBucket(
                key=int(row["capture_year"]) if row["capture_year"] is not None else None,
                file_count=int(row["file_count"]),
                total_bytes=int(row["total_bytes"]),
            )
            for row in rows
        ]

    def analytics_by_extension(
        self,
        *,
        stop_event: StopEventProtocol | None = None,
    ) -> list[AnalyticsBucket]:
        """Return file counts and total bytes grouped by normalized lowercase extension."""
        if _is_cancelled(stop_event):
            return []
        rows = self.connection.execute(
            """
            SELECT extension, COUNT(*) AS file_count, COALESCE(SUM(size), 0) AS total_bytes
            FROM files
            GROUP BY extension
            ORDER BY total_bytes DESC, extension ASC
            """
        ).fetchall()
        return [
            AnalyticsBucket(
                key=str(row["extension"]),
                file_count=int(row["file_count"]),
                total_bytes=int(row["total_bytes"]),
            )
            for row in rows
        ]

    def recoverable_duplicate_bytes(
        self,
        *,
        stop_event: StopEventProtocol | None = None,
    ) -> int:
        """Return total bytes recoverable by keeping one file per duplicate ``content_hash`` group."""
        if _is_cancelled(stop_event):
            return 0
        row = self.connection.execute(
            """
            SELECT COALESCE(SUM(group_recoverable), 0)
            FROM (
                SELECT SUM(size) - MIN(size) AS group_recoverable
                FROM files
                WHERE content_hash IS NOT NULL AND content_hash != ''
                GROUP BY content_hash
                HAVING COUNT(*) > 1
            )
            """
        ).fetchone()
        return int(row[0]) if row is not None else 0

    def get_storage_analytics(
        self,
        *,
        stop_event: StopEventProtocol | None = None,
    ) -> StorageAnalytics:
        """Compute full storage analytics summary across the catalog."""
        if _is_cancelled(stop_event):
            return StorageAnalytics(
                total_files=0,
                total_bytes=0,
                by_media_kind=(),
                by_volume=(),
                by_capture_year=(),
                by_extension=(),
                recoverable_duplicate_bytes=0,
            )
        totals_row = self.connection.execute(
            "SELECT COUNT(*), COALESCE(SUM(size), 0) FROM files"
        ).fetchone()
        total_files = int(totals_row[0]) if totals_row is not None else 0
        total_bytes = int(totals_row[1]) if totals_row is not None else 0
        return StorageAnalytics(
            total_files=total_files,
            total_bytes=total_bytes,
            by_media_kind=tuple(self.analytics_by_media_kind(stop_event=stop_event)),
            by_volume=tuple(self.analytics_by_volume(stop_event=stop_event)),
            by_capture_year=tuple(self.analytics_by_capture_year(stop_event=stop_event)),
            by_extension=tuple(self.analytics_by_extension(stop_event=stop_event)),
            recoverable_duplicate_bytes=self.recoverable_duplicate_bytes(stop_event=stop_event),
        )
