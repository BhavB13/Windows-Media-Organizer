"""Framework-neutral persistent thumbnail cache and batch generation service."""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import tempfile
import threading
import time
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Protocol, Sequence, Iterable

from ..media.container_exif import (
    RAW_EXTENSIONS,
    extract_embedded_jpeg,
    read_raw_orientation,
)
from ..runtime_paths import RuntimePaths, get_runtime_paths

try:
    from PIL import Image
except ImportError:  # pragma: no cover - supported fallback for engine-only installs
    Image = None  # type: ignore[assignment]


DEFAULT_THUMBNAIL_SIZE: tuple[int, int] = (256, 256)
DEFAULT_MAX_CACHE_BYTES: int = 256 * 1024 * 1024  # 256 MiB
DEFAULT_MAX_WORKERS: int = 4
DEFAULT_JPEG_QUALITY: int = 85
SHARD_PREFIX_LENGTH: int = 2

_VALID_KEY_RE = re.compile(r"^[0-9a-f]{16,128}$")
_TRANSPARENT_MODES = frozenset({"RGBA", "LA", "PA", "RGBa", "La"})
_JPEG_SOI = b"\xff\xd8"
_JPEG_EOI = b"\xff\xd9"
_PNG_SIG = b"\x89PNG\r\n\x1a\n"
_PNG_IEND_TRAILER = b"IEND\xaeB`\x82"
_EXIF_ORIENTATION_TAG = 0x0112  # 274


class StopEventLike(Protocol):
    """Cooperative cancellation protocol matching engine.py's stop_event."""

    def is_set(self) -> bool:
        ...


def is_pillow_available() -> bool:
    """Return True when Pillow is importable and available for decoding."""

    return Image is not None


def _is_cancelled(stop_event: StopEventLike | None) -> bool:
    return bool(stop_event is not None and stop_event.is_set())


def default_thumbnail_cache_dir(paths: RuntimePaths | None = None) -> Path:
    """Return the persistent thumbnail cache directory under application runtime paths."""

    resolved_paths = paths or get_runtime_paths()
    cache_dir = resolved_paths.cache / "thumbnails"
    cache_dir.mkdir(parents=True, exist_ok=True)
    return cache_dir


def normalize_thumbnail_size(size: int | tuple[int, int] | Sequence[int]) -> tuple[int, int]:
    """Normalize a requested thumbnail pixel size into a validated (width, height) pair."""

    if isinstance(size, bool):
        raise ValueError("Thumbnail size must be a positive integer or (width, height) tuple.")
    if isinstance(size, int):
        width = height = int(size)
    elif isinstance(size, (tuple, list)) and len(size) == 2:
        if isinstance(size[0], bool) or isinstance(size[1], bool):
            raise ValueError("Thumbnail dimensions must be integers, not booleans.")
        width, height = int(size[0]), int(size[1])
    else:
        raise ValueError(f"Invalid thumbnail size: {size!r}. Expected int or (width, height).")
    if width <= 0 or height <= 0:
        raise ValueError(f"Thumbnail dimensions must be positive integers, got {(width, height)!r}.")
    return (width, height)


def _normalize_source_path(source_path: str | os.PathLike[str]) -> str:
    raw = os.fspath(source_path)
    try:
        resolved = str(Path(raw).expanduser().resolve())
    except OSError:
        resolved = os.path.abspath(os.path.expanduser(raw))
    return os.path.normcase(resolved)


def build_cache_key(
    source_path: str | os.PathLike[str],
    *,
    size_bytes: int,
    mtime_ns: int,
    requested_size: int | tuple[int, int] | Sequence[int],
    exif_orientation: int = 1,
) -> str:
    """Build a privacy-preserving SHA-256 cache key for a source file and target parameters.

    The key incorporates the normalized source path, file size in bytes,
    nanosecond modification timestamp, requested pixel dimensions, and the
    EXIF orientation applied. Only the SHA-256 hex digest is returned so the
    on-disk filename is safe on NTFS, exFAT, and FAT32 and never leaks the
    original path or filename.
    """

    width, height = normalize_thumbnail_size(requested_size)
    normalized_path = _normalize_source_path(source_path)
    payload = json.dumps(
        {
            "height": height,
            "mtime_ns": int(mtime_ns),
            "orientation": int(exif_orientation),
            "path": normalized_path,
            "size": int(size_bytes),
            "width": width,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8", errors="surrogatepass")).hexdigest()


def validate_cache_key(key: str) -> str:
    """Validate that a cache key is a safe lowercase hex digest with no traversal tokens."""

    if not isinstance(key, str):
        raise ValueError("Cache key must be a string.")
    normalized = key.strip().lower()
    if (
        not normalized
        or ".." in normalized
        or "/" in normalized
        or "\\" in normalized
        or ":" in normalized
        or "\x00" in normalized
        or not _VALID_KEY_RE.fullmatch(normalized)
    ):
        raise ValueError("Invalid cache key: must be a hexadecimal digest without path separators.")
    return normalized


def _ensure_within_cache(candidate: Path, cache_root: Path) -> Path:
    """Verify that ``candidate`` resolves strictly inside ``cache_root``."""

    resolved_root = cache_root.resolve()
    resolved_candidate = candidate.resolve()
    if resolved_candidate == resolved_root or not resolved_candidate.is_relative_to(resolved_root):
        raise ValueError(f"Path escapes thumbnail cache directory: {candidate}")
    return resolved_candidate


def _extract_exif_orientation(image: Any) -> int:
    """Read the EXIF Orientation tag (0x0112), defaulting to 1 (normal upright)."""

    try:
        exif = image.getexif()
        if exif:
            raw_value = exif.get(_EXIF_ORIENTATION_TAG, 1)
            orientation = int(raw_value)
            if 1 <= orientation <= 8:
                return orientation
    except Exception:
        pass
    return 1


def _apply_exif_orientation(image: Any, orientation: int) -> Any:
    """Rotate or mirror ``image`` according to its EXIF orientation tag (1..8)."""

    if Image is None or orientation <= 1 or orientation > 8:
        return image
    transpose_enum = getattr(Image, "Transpose", Image)
    operations = {
        2: transpose_enum.FLIP_LEFT_RIGHT,
        3: transpose_enum.ROTATE_180,
        4: transpose_enum.FLIP_TOP_BOTTOM,
        5: transpose_enum.TRANSPOSE,
        6: transpose_enum.ROTATE_270,
        7: transpose_enum.TRANSVERSE,
        8: transpose_enum.ROTATE_90,
    }
    op = operations.get(orientation)
    if op is None:
        return image
    return image.transpose(op)


def _has_transparency(image: Any) -> bool:
    """Return True if the image has an alpha channel or palette transparency that must be kept."""

    if getattr(image, "mode", "") in _TRANSPARENT_MODES:
        return True
    info = getattr(image, "info", None)
    if isinstance(info, dict) and info.get("transparency") is not None:
        return True
    return False


def _is_valid_cache_file(path: Path) -> bool:
    """Check that a cache file is complete, non-truncated, and readable by Pillow."""

    if Image is None:
        return False
    try:
        stat = path.stat()
        if stat.st_size < 16:
            return False
        suffix = path.suffix.lower()
        with path.open("rb") as handle:
            header = handle.read(8)
            if suffix == ".jpg":
                if not header.startswith(_JPEG_SOI):
                    return False
                handle.seek(-2, os.SEEK_END)
                if handle.read(2) != _JPEG_EOI:
                    return False
            elif suffix == ".png":
                if header != _PNG_SIG:
                    return False
                handle.seek(-8, os.SEEK_END)
                if handle.read(8) != _PNG_IEND_TRAILER:
                    return False
            else:
                return False
            handle.seek(0)
            with Image.open(handle) as cached_image:
                cached_image.verify()
        return True
    except Exception:
        return False


def _read_cached_dimensions(path: Path) -> tuple[tuple[int, int], str]:
    """Read (width, height) and format from an existing valid thumbnail header."""

    if Image is None:
        return (0, 0), ""
    try:
        with path.open("rb") as handle, Image.open(handle) as cached_image:
            return (int(cached_image.width), int(cached_image.height)), str(cached_image.format or "")
    except Exception:
        return (0, 0), ""


@dataclass(frozen=True)
class ThumbnailRequest:
    """Parameters for a single thumbnail request."""

    source_path: str | Path
    size: tuple[int, int] = DEFAULT_THUMBNAIL_SIZE
    apply_exif_orientation: bool = True


@dataclass(frozen=True)
class ThumbnailResult:
    """Outcome of a single thumbnail lookup or generation request."""

    source_path: str
    thumbnail_path: Path | None
    cache_key: str = ""
    requested_size: tuple[int, int] = DEFAULT_THUMBNAIL_SIZE
    size: tuple[int, int] = (0, 0)
    source_dimensions: tuple[int, int] = (0, 0)
    exif_orientation: int = 1
    format: str = ""
    cache_hit: bool = False
    generated: bool = False
    error: str | None = None
    cancelled: bool = False

    @property
    def ok(self) -> bool:
        return self.error is None and not self.cancelled and self.thumbnail_path is not None

    @property
    def success(self) -> bool:
        return self.ok

    @property
    def from_cache(self) -> bool:
        return self.cache_hit

    @property
    def status(self) -> str:
        if self.cancelled:
            return "cancelled"
        if self.error is not None:
            return "error"
        if self.cache_hit:
            return "cache_hit"
        if self.generated:
            return "generated"
        return "unavailable"


@dataclass(frozen=True)
class EvictionResult:
    """Summary of an LRU cache eviction pass."""

    evicted_count: int
    evicted_bytes: int
    remaining_count: int
    remaining_bytes: int
    evicted_paths: tuple[Path, ...] = ()


@dataclass(frozen=True)
class ThumbnailCacheStats:
    """Snapshot of on-disk usage and request hit-rate metrics."""

    entry_count: int
    total_bytes: int
    max_bytes: int
    hits: int
    misses: int
    errors: int
    evicted_entries: int
    evicted_bytes: int

    @property
    def hit_rate(self) -> float:
        total = self.hits + self.misses
        return (self.hits / total) if total > 0 else 0.0


class ThumbnailCache:
    """Framework-neutral persistent thumbnail cache with sharding and LRU eviction.

    Large JPEGs use Pillow's ``Image.draft()`` before decoding so libjpeg scales
    the image down during DCT decompression rather than decoding the full-size
    raster into memory first — this is the primary performance win when building
    previews across thousands of multi-megapixel camera photos.
    """

    def __init__(
        self,
        cache_dir: str | os.PathLike[str] | None = None,
        *,
        paths: RuntimePaths | None = None,
        max_bytes: int = DEFAULT_MAX_CACHE_BYTES,
        default_size: int | tuple[int, int] = DEFAULT_THUMBNAIL_SIZE,
        max_workers: int = DEFAULT_MAX_WORKERS,
        jpeg_quality: int = DEFAULT_JPEG_QUALITY,
        auto_evict: bool = True,
    ) -> None:
        if max_bytes < 0:
            raise ValueError("max_bytes must be non-negative.")
        if max_workers < 1:
            raise ValueError("max_workers must be at least 1.")
        if cache_dir is not None:
            resolved_dir = Path(cache_dir).expanduser().resolve()
            resolved_dir.mkdir(parents=True, exist_ok=True)
            self._cache_dir = resolved_dir
        else:
            self._cache_dir = default_thumbnail_cache_dir(paths).resolve()
        self.max_bytes: int = int(max_bytes)
        self.default_size: tuple[int, int] = normalize_thumbnail_size(default_size)
        self.max_workers: int = int(max_workers)
        self.jpeg_quality: int = max(1, min(100, int(jpeg_quality)))
        self.auto_evict: bool = bool(auto_evict)

        self._lock = threading.RLock()
        self._evict_lock = threading.Lock()
        self._last_touch_ns: int = time.time_ns()
        self._orientation_memo: dict[tuple[str, int, int], int] = {}
        self._hits: int = 0
        self._misses: int = 0
        self._errors: int = 0
        self._evicted_entries: int = 0
        self._evicted_bytes: int = 0

    @property
    def cache_dir(self) -> Path:
        return self._cache_dir

    @property
    def available(self) -> bool:
        return is_pillow_available()

    def is_available(self) -> bool:
        return is_pillow_available()

    def entry_path(self, key: str, extension: str = "jpg") -> Path:
        """Return the sharded cache path for a validated cache key and extension."""

        validated_key = validate_cache_key(key)
        clean_ext = extension.lstrip(".").lower()
        if clean_ext not in {"jpg", "png"}:
            raise ValueError(f"Unsupported thumbnail cache extension: {extension!r}")
        shard = validated_key[:SHARD_PREFIX_LENGTH]
        candidate = self._cache_dir / shard / f"{validated_key}.{clean_ext}"
        _ensure_within_cache(candidate, self._cache_dir)
        return candidate

    def _touch_entry(self, path: Path) -> None:
        """Advance an entry's modification timestamp monotonically to record LRU access."""

        with self._lock:
            now_ns = time.time_ns()
            # Step by at least 1 microsecond (10 NTFS 100ns ticks) so rapid accesses
            # in the same millisecond still produce strictly ordered timestamps on disk.
            if now_ns <= self._last_touch_ns:
                now_ns = self._last_touch_ns + 1_000
            self._last_touch_ns = now_ns
        try:
            resolved = _ensure_within_cache(path, self._cache_dir)
            os.utime(resolved, ns=(now_ns, now_ns))
        except (OSError, ValueError):
            pass

    def _lookup_existing_entry(self, key: str) -> Path | None:
        """Return the existing valid cached thumbnail path for ``key``, or None."""

        validated_key = validate_cache_key(key)
        for ext in ("jpg", "png"):
            candidate = self.entry_path(validated_key, ext)
            if not candidate.exists():
                continue
            try:
                if candidate.is_symlink():
                    _ensure_within_cache(candidate, self._cache_dir)
                if not candidate.is_file():
                    continue
            except (OSError, ValueError):
                continue
            if _is_valid_cache_file(candidate):
                self._touch_entry(candidate)
                return candidate
            # Remove corrupted or truncated cache entries so they can be regenerated cleanly.
            self._safe_unlink(candidate)
        return None

    def _safe_unlink(self, candidate: str | os.PathLike[str]) -> int:
        """Safely unlink a file only after verifying it lies strictly inside ``cache_dir``.

        Returns the file size in bytes if unlinked, or 0 if already missing.
        Raises ``ValueError`` if ``candidate`` is outside ``cache_dir``.
        """

        raw_path = Path(candidate)
        if not raw_path.is_absolute():
            raw_path = self._cache_dir / raw_path
        # Validate lexical containment as well as resolved containment so symlinks
        # or relative segments (`..`) can never escape the cache directory.
        resolved_root = self._cache_dir.resolve()
        lexical_parent = Path(os.path.abspath(str(raw_path)))
        if lexical_parent == resolved_root or not lexical_parent.is_relative_to(resolved_root):
            raise ValueError(f"Refusing to delete file outside thumbnail cache directory: {candidate}")
        resolved_target = _ensure_within_cache(raw_path, self._cache_dir)
        try:
            size = resolved_target.stat().st_size
        except FileNotFoundError:
            return 0
        except OSError:
            size = 0
        try:
            resolved_target.unlink()
            return size
        except FileNotFoundError:
            return 0
        except OSError:
            return 0

    def delete_entry(self, key: str) -> bool:
        """Delete any cached thumbnail matching ``key`` after validating containment."""

        validated_key = validate_cache_key(key)
        removed = False
        for ext in ("jpg", "png"):
            target = self.entry_path(validated_key, ext)
            if target.exists():
                self._safe_unlink(target)
                removed = True
        return removed

    def get_or_create(
        self,
        source_path: str | os.PathLike[str],
        size: int | tuple[int, int] | Sequence[int] | None = None,
        *,
        apply_exif_orientation: bool = True,
        stop_event: StopEventLike | None = None,
    ) -> ThumbnailResult:
        """Return a cached thumbnail or generate one atomically for ``source_path``.

        Uses Pillow's ``Image.draft()`` mode when decoding JPEGs so full-resolution
        photos are downsampled during DCT decode rather than decoded at full size
        first (the main performance win for large photo libraries).
        """

        source_str = str(source_path)
        try:
            requested_size = normalize_thumbnail_size(size if size is not None else self.default_size)
        except ValueError as exc:
            with self._lock:
                self._errors += 1
            return ThumbnailResult(
                source_path=source_str,
                thumbnail_path=None,
                requested_size=self.default_size,
                error=str(exc),
            )

        if _is_cancelled(stop_event):
            return ThumbnailResult(
                source_path=source_str,
                thumbnail_path=None,
                requested_size=requested_size,
                cancelled=True,
            )

        if Image is None:
            with self._lock:
                self._errors += 1
            return ThumbnailResult(
                source_path=source_str,
                thumbnail_path=None,
                requested_size=requested_size,
                error="Thumbnail generation is unavailable because Pillow is not installed.",
            )

        try:
            candidate = Path(source_path).expanduser()
            stat_result = candidate.stat()
            if not candidate.is_file():
                with self._lock:
                    self._errors += 1
                return ThumbnailResult(
                    source_path=source_str,
                    thumbnail_path=None,
                    requested_size=requested_size,
                    error=f"Source path is not a regular file: {source_str}",
                )
        except OSError as exc:
            with self._lock:
                self._errors += 1
            return ThumbnailResult(
                source_path=source_str,
                thumbnail_path=None,
                requested_size=requested_size,
                error=f"Source file is unavailable: {exc}",
            )

        norm_path = _normalize_source_path(candidate)
        size_bytes = int(stat_result.st_size)
        mtime_ns = int(stat_result.st_mtime_ns)
        memo_key = (norm_path, size_bytes, mtime_ns)

        # Fast-path cache check if orientation was already memoized for this exact file state.
        with self._lock:
            memoized_orientation = self._orientation_memo.get(memo_key) if apply_exif_orientation else 0

        if memoized_orientation is not None:
            key = build_cache_key(
                candidate,
                size_bytes=size_bytes,
                mtime_ns=mtime_ns,
                requested_size=requested_size,
                exif_orientation=memoized_orientation,
            )
            existing = self._lookup_existing_entry(key)
            if existing is not None:
                thumb_dims, thumb_fmt = _read_cached_dimensions(existing)
                with self._lock:
                    self._hits += 1
                return ThumbnailResult(
                    source_path=source_str,
                    thumbnail_path=existing,
                    cache_key=key,
                    requested_size=requested_size,
                    size=thumb_dims,
                    exif_orientation=memoized_orientation,
                    format=thumb_fmt,
                    cache_hit=True,
                    generated=False,
                )

        if _is_cancelled(stop_event):
            return ThumbnailResult(
                source_path=source_str,
                thumbnail_path=None,
                requested_size=requested_size,
                cancelled=True,
            )

        # Open source strictly read-only ("rb") so user media is never modified.
        try:
            is_raw = candidate.suffix.lower() in RAW_EXTENSIONS
            if is_raw:
                raw_orientation = read_raw_orientation(candidate) if apply_exif_orientation else 0
                with self._lock:
                    if apply_exif_orientation:
                        self._orientation_memo[memo_key] = raw_orientation

                key = build_cache_key(
                    candidate,
                    size_bytes=size_bytes,
                    mtime_ns=mtime_ns,
                    requested_size=requested_size,
                    exif_orientation=raw_orientation,
                )
                existing = self._lookup_existing_entry(key)
                if existing is not None:
                    thumb_dims, thumb_fmt = _read_cached_dimensions(existing)
                    with self._lock:
                        self._hits += 1
                    return ThumbnailResult(
                        source_path=source_str,
                        thumbnail_path=existing,
                        cache_key=key,
                        requested_size=requested_size,
                        size=thumb_dims,
                        source_dimensions=(0, 0),
                        exif_orientation=raw_orientation,
                        format=thumb_fmt,
                        cache_hit=True,
                        generated=False,
                    )

                if _is_cancelled(stop_event):
                    return ThumbnailResult(
                        source_path=source_str,
                        thumbnail_path=None,
                        cache_key=key,
                        requested_size=requested_size,
                        cancelled=True,
                    )

                # Try opening with Pillow first; if that fails or yields a tiny IFD0
                # thumbnail smaller than the requested size, decode the embedded JPEG.
                pillow_img = None
                try:
                    with candidate.open("rb") as source_stream:
                        candidate_img = Image.open(source_stream)
                        if candidate_img.width >= requested_size[0] or candidate_img.height >= requested_size[1]:
                            pillow_img = candidate_img
                            pillow_img.load()
                except Exception:
                    pillow_img = None

                if pillow_img is not None:
                    work_image = pillow_img
                    source_format = (pillow_img.format or "").upper()
                else:
                    jpeg_bytes = extract_embedded_jpeg(candidate)
                    if not jpeg_bytes:
                        raise ValueError("No supported image or embedded JPEG preview found in RAW file")
                    work_image = Image.open(io.BytesIO(jpeg_bytes))
                    source_format = "JPEG"

                raw_width, raw_height = int(work_image.width), int(work_image.height)
                upright_dims = (
                    (raw_height, raw_width)
                    if raw_orientation in {5, 6, 7, 8}
                    else (raw_width, raw_height)
                )

                if _is_cancelled(stop_event):
                    return ThumbnailResult(
                        source_path=source_str,
                        thumbnail_path=None,
                        cache_key=key,
                        requested_size=requested_size,
                        cancelled=True,
                    )

                if source_format == "JPEG":
                    draft_size = (
                        (requested_size[1], requested_size[0])
                        if raw_orientation in {5, 6, 7, 8}
                        else requested_size
                    )
                    try:
                        work_image.draft("RGB", draft_size)
                    except Exception:
                        pass

                if apply_exif_orientation and raw_orientation > 1:
                    work_image = _apply_exif_orientation(work_image, raw_orientation)
                else:
                    work_image.load()

                if _is_cancelled(stop_event):
                    return ThumbnailResult(
                        source_path=source_str,
                        thumbnail_path=None,
                        cache_key=key,
                        requested_size=requested_size,
                        cancelled=True,
                    )

                keep_alpha = _has_transparency(work_image)
                target_mode = "RGBA" if keep_alpha else "RGB"
                if work_image.mode != target_mode:
                    work_image = work_image.convert(target_mode)

                resample_ns = getattr(Image, "Resampling", Image)
                resample_filter = getattr(resample_ns, "LANCZOS", getattr(Image, "LANCZOS", 1))
                work_image.thumbnail(requested_size, resample=resample_filter)
                final_size = (int(work_image.width), int(work_image.height))
                target_ext = "png" if keep_alpha else "jpg"
                target_format = "PNG" if keep_alpha else "JPEG"
            else:
                with candidate.open("rb") as source_stream:
                    with Image.open(source_stream) as image:
                        raw_width, raw_height = int(image.width), int(image.height)
                        raw_orientation = _extract_exif_orientation(image) if apply_exif_orientation else 0
                        with self._lock:
                            if apply_exif_orientation:
                                self._orientation_memo[memo_key] = raw_orientation

                        upright_dims = (
                            (raw_height, raw_width)
                            if raw_orientation in {5, 6, 7, 8}
                            else (raw_width, raw_height)
                        )
                        key = build_cache_key(
                            candidate,
                            size_bytes=size_bytes,
                            mtime_ns=mtime_ns,
                            requested_size=requested_size,
                            exif_orientation=raw_orientation,
                        )

                        # Check on-disk cache before decoding pixel data (handles warm disk cache across runs).
                        existing = self._lookup_existing_entry(key)
                        if existing is not None:
                            thumb_dims, thumb_fmt = _read_cached_dimensions(existing)
                            with self._lock:
                                self._hits += 1
                            return ThumbnailResult(
                                source_path=source_str,
                                thumbnail_path=existing,
                                cache_key=key,
                                requested_size=requested_size,
                                size=thumb_dims,
                                source_dimensions=upright_dims,
                                exif_orientation=raw_orientation,
                                format=thumb_fmt,
                                cache_hit=True,
                                generated=False,
                            )

                        if _is_cancelled(stop_event):
                            return ThumbnailResult(
                                source_path=source_str,
                                thumbnail_path=None,
                                cache_key=key,
                                requested_size=requested_size,
                                cancelled=True,
                            )

                        # Main performance win: for JPEGs, configure draft mode before any pixel
                        # access so libjpeg decodes at a reduced DCT scale factor instead of
                        # decoding the full-resolution photo first.
                        source_format = (image.format or "").upper()
                        if source_format == "JPEG":
                            draft_size = (
                                (requested_size[1], requested_size[0])
                                if raw_orientation in {5, 6, 7, 8}
                                else requested_size
                            )
                            try:
                                image.draft("RGB", draft_size)
                            except Exception:
                                pass

                        work_image: Any = image
                        if apply_exif_orientation and raw_orientation > 1:
                            work_image = _apply_exif_orientation(work_image, raw_orientation)
                        else:
                            work_image.load()

                        if _is_cancelled(stop_event):
                            return ThumbnailResult(
                                source_path=source_str,
                                thumbnail_path=None,
                                cache_key=key,
                                requested_size=requested_size,
                                cancelled=True,
                            )

                        keep_alpha = _has_transparency(work_image)
                        target_mode = "RGBA" if keep_alpha else "RGB"
                        if work_image.mode != target_mode:
                            work_image = work_image.convert(target_mode)
                        elif work_image is image:
                            work_image = work_image.copy()

                        resample_ns = getattr(Image, "Resampling", Image)
                        resample_filter = getattr(resample_ns, "LANCZOS", getattr(Image, "LANCZOS", 1))
                        work_image.thumbnail(requested_size, resample=resample_filter)
                        final_size = (int(work_image.width), int(work_image.height))
                        target_ext = "png" if keep_alpha else "jpg"
                        target_format = "PNG" if keep_alpha else "JPEG"
        except Exception as exc:
            with self._lock:
                self._errors += 1
            return ThumbnailResult(
                source_path=source_str,
                thumbnail_path=None,
                requested_size=requested_size,
                error=f"Failed to decode image: {exc}",
            )

        if _is_cancelled(stop_event):
            return ThumbnailResult(
                source_path=source_str,
                thumbnail_path=None,
                cache_key=key,
                requested_size=requested_size,
                cancelled=True,
            )

        try:
            target_path = self.entry_path(key, target_ext)
            self._atomic_save_thumbnail(work_image, target_path, target_format)
        except Exception as exc:
            with self._lock:
                self._errors += 1
            return ThumbnailResult(
                source_path=source_str,
                thumbnail_path=None,
                cache_key=key,
                requested_size=requested_size,
                error=f"Failed to write thumbnail cache entry: {exc}",
            )

        with self._lock:
            self._misses += 1

        if self.auto_evict:
            self.evict()

        return ThumbnailResult(
            source_path=source_str,
            thumbnail_path=target_path,
            cache_key=key,
            requested_size=requested_size,
            size=final_size,
            source_dimensions=upright_dims,
            exif_orientation=raw_orientation,
            format=target_format,
            cache_hit=False,
            generated=True,
        )

    def _atomic_save_thumbnail(self, image: Any, target_path: Path, target_format: str) -> None:
        """Write a thumbnail image to a temporary file inside the cache and rename into place."""

        _ensure_within_cache(target_path, self._cache_dir)
        target_path.parent.mkdir(parents=True, exist_ok=True)
        _ensure_within_cache(target_path.parent, self._cache_dir)

        temp_fd, temp_name = tempfile.mkstemp(
            prefix=f".tmp_{target_path.stem[:16]}_",
            suffix=f"{target_path.suffix}.part",
            dir=str(target_path.parent),
        )
        temp_path = Path(temp_name)
        try:
            with os.fdopen(temp_fd, "wb") as temp_file:
                save_kwargs: dict[str, Any] = {}
                if target_format == "JPEG":
                    save_kwargs["quality"] = self.jpeg_quality
                    save_kwargs["optimize"] = True
                image.save(temp_file, format=target_format, **save_kwargs)
                temp_file.flush()
                os.fsync(temp_file.fileno())
            os.replace(temp_path, target_path)
            self._touch_entry(target_path)
        except BaseException:
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                pass
            raise

    @staticmethod
    def _is_temp_entry(path: Path) -> bool:
        name = path.name
        return name.startswith(".tmp_") or name.endswith(".part") or name.endswith(".tmp")

    def _collect_cache_entries(self) -> list[tuple[int, int, Path]]:
        """Collect (mtime_ns, size_bytes, path) for all completed cache entries."""

        entries: list[tuple[int, int, Path]] = []
        if not self._cache_dir.exists():
            return entries
        resolved_root = self._cache_dir.resolve()
        try:
            shard_dirs = list(self._cache_dir.iterdir())
        except OSError:
            return entries

        for shard in shard_dirs:
            try:
                if shard.is_symlink():
                    continue
                if shard.is_file():
                    if self._is_temp_entry(shard):
                        continue
                    resolved_file = shard.resolve()
                    if resolved_file != resolved_root and resolved_file.is_relative_to(resolved_root):
                        st = shard.stat()
                        entries.append((int(st.st_mtime_ns), int(st.st_size), resolved_file))
                    continue
                if not shard.is_dir():
                    continue
                resolved_shard = shard.resolve()
                if resolved_shard == resolved_root or not resolved_shard.is_relative_to(resolved_root):
                    continue
                for child in shard.iterdir():
                    try:
                        if self._is_temp_entry(child) or child.is_symlink() or not child.is_file():
                            continue
                        resolved_child = child.resolve()
                        if resolved_child == resolved_root or not resolved_child.is_relative_to(resolved_root):
                            continue
                        st = child.stat()
                        entries.append((int(st.st_mtime_ns), int(st.st_size), resolved_child))
                    except OSError:
                        continue
            except OSError:
                continue
        return entries

    def evict(self, max_bytes: int | None = None) -> EvictionResult:
        """Run a thread-safe least-recently-used (LRU) eviction pass to enforce the byte budget.

        In-flight temporary files (``.tmp_*`` / ``*.part``) are skipped so eviction
        is safe to run concurrently while other worker threads are writing entries.
        Every candidate path is validated to lie strictly within ``cache_dir`` before
        unlinking.
        """

        budget = self.max_bytes if max_bytes is None else int(max_bytes)
        if budget < 0:
            raise ValueError("Eviction byte budget must be non-negative.")

        with self._evict_lock:
            entries = self._collect_cache_entries()
            total_bytes = sum(size for _mtime, size, _path in entries)
            if total_bytes <= budget:
                return EvictionResult(
                    evicted_count=0,
                    evicted_bytes=0,
                    remaining_count=len(entries),
                    remaining_bytes=total_bytes,
                )

            # Sort oldest (least recently used) first by mtime_ns, then path for determinism.
            entries.sort(key=lambda item: (item[0], str(item[2])))
            evicted_paths: list[Path] = []
            evicted_bytes = 0
            remaining_count = len(entries)

            for _mtime_ns, entry_size, entry_path in entries:
                if total_bytes <= budget:
                    break
                try:
                    removed_size = self._safe_unlink(entry_path)
                except ValueError:
                    continue
                if removed_size > 0 or not entry_path.exists():
                    actual_removed = removed_size or entry_size
                    total_bytes = max(0, total_bytes - entry_size)
                    evicted_bytes += actual_removed
                    remaining_count = max(0, remaining_count - 1)
                    evicted_paths.append(entry_path)

            with self._lock:
                self._evicted_entries += len(evicted_paths)
                self._evicted_bytes += evicted_bytes

            return EvictionResult(
                evicted_count=len(evicted_paths),
                evicted_bytes=evicted_bytes,
                remaining_count=remaining_count,
                remaining_bytes=total_bytes,
                evicted_paths=tuple(evicted_paths),
            )

    def clear(self) -> EvictionResult:
        """Evict all cached thumbnails inside the cache directory."""

        return self.evict(max_bytes=0)

    def stats(self) -> ThumbnailCacheStats:
        """Return current cache size and hit/miss counters."""

        entries = self._collect_cache_entries()
        total_bytes = sum(size for _mtime, size, _path in entries)
        with self._lock:
            return ThumbnailCacheStats(
                entry_count=len(entries),
                total_bytes=total_bytes,
                max_bytes=self.max_bytes,
                hits=self._hits,
                misses=self._misses,
                errors=self._errors,
                evicted_entries=self._evicted_entries,
                evicted_bytes=self._evicted_bytes,
            )

    def generate_batch(
        self,
        paths: Iterable[str | os.PathLike[str] | ThumbnailRequest],
        size: int | tuple[int, int] | Sequence[int] | None = None,
        *,
        stop_event: StopEventLike | None = None,
        max_workers: int | None = None,
        apply_exif_orientation: bool = True,
    ) -> Iterator[ThumbnailResult]:
        """Yield ``ThumbnailResult`` items for ``paths`` using a bounded worker pool.

        At most ``max_workers`` worker threads are spawned, and in-flight tasks
        are bounded so thousands of paths do not queue unbounded work. Checks
        ``stop_event.is_set()`` before submitting tasks and between results so
        cancellation returns promptly. Corrupt or unreadable files are reported
        via ``ThumbnailResult.error`` and never raise out of the batch.
        """

        if _is_cancelled(stop_event):
            return

        worker_limit = self.max_workers if max_workers is None else int(max_workers)
        if worker_limit < 1:
            raise ValueError("max_workers must be at least 1.")

        default_req_size = size if size is not None else self.default_size
        iterator = iter(paths)
        max_in_flight = worker_limit

        executor = ThreadPoolExecutor(
            max_workers=worker_limit,
            thread_name_prefix="dtm-thumb",
        )
        in_flight: dict[Future[ThumbnailResult], tuple[int, str]] = {}
        ready_by_index: dict[int, ThumbnailResult] = {}
        next_submit_index = 0
        next_yield_index = 0
        exhausted = False

        def _submit_next() -> bool:
            nonlocal next_submit_index, exhausted
            if exhausted or _is_cancelled(stop_event):
                return False
            try:
                item = next(iterator)
            except StopIteration:
                exhausted = True
                return False

            if isinstance(item, ThumbnailRequest):
                req_path = item.source_path
                req_size: Any = item.size
                req_orient = item.apply_exif_orientation
            else:
                req_path = Path(item) if isinstance(item, os.PathLike) else str(item)
                req_size = default_req_size
                req_orient = apply_exif_orientation

            idx = next_submit_index
            next_submit_index += 1
            future = executor.submit(
                self._safe_worker_call,
                req_path,
                req_size,
                req_orient,
                stop_event,
            )
            in_flight[future] = (idx, str(req_path))
            return True

        try:
            while len(in_flight) < max_in_flight and _submit_next():
                pass

            while in_flight or ready_by_index:
                if _is_cancelled(stop_event):
                    break

                while next_yield_index in ready_by_index:
                    if _is_cancelled(stop_event):
                        return
                    result = ready_by_index.pop(next_yield_index)
                    next_yield_index += 1
                    if result.cancelled and _is_cancelled(stop_event):
                        return
                    yield result
                    while len(in_flight) < max_in_flight and _submit_next():
                        pass

                if not in_flight:
                    break

                if _is_cancelled(stop_event):
                    break

                done, _not_done = wait(
                    tuple(in_flight.keys()),
                    timeout=0.02,
                    return_when=FIRST_COMPLETED,
                )
                if not done:
                    continue

                for completed in done:
                    idx, raw_path = in_flight.pop(completed)
                    try:
                        res = completed.result()
                    except Exception as exc:  # pragma: no cover - defensive guard
                        res = ThumbnailResult(
                            source_path=raw_path,
                            thumbnail_path=None,
                            error=f"Unexpected worker error: {exc}",
                        )
                    ready_by_index[idx] = res

                while len(in_flight) < max_in_flight and _submit_next():
                    pass
        finally:
            for pending in list(in_flight.keys()):
                pending.cancel()
            executor.shutdown(wait=True, cancel_futures=True)

    def iter_thumbnails(
        self,
        paths: Iterable[str | os.PathLike[str] | ThumbnailRequest],
        size: int | tuple[int, int] | Sequence[int] | None = None,
        *,
        stop_event: StopEventLike | None = None,
        max_workers: int | None = None,
        apply_exif_orientation: bool = True,
    ) -> Iterator[ThumbnailResult]:
        """Alias for ``generate_batch``."""

        return self.generate_batch(
            paths,
            size=size,
            stop_event=stop_event,
            max_workers=max_workers,
            apply_exif_orientation=apply_exif_orientation,
        )

    def _safe_worker_call(
        self,
        source_path: str | os.PathLike[str],
        size: int | tuple[int, int] | Sequence[int] | None,
        apply_exif_orientation: bool,
        stop_event: StopEventLike | None,
    ) -> ThumbnailResult:
        try:
            return self.get_or_create(
                source_path,
                size=size,
                apply_exif_orientation=apply_exif_orientation,
                stop_event=stop_event,
            )
        except Exception as exc:  # pragma: no cover - defensive fallback
            with self._lock:
                self._errors += 1
            return ThumbnailResult(
                source_path=str(source_path),
                thumbnail_path=None,
                error=f"Thumbnail generation failed: {exc}",
            )


ThumbnailService = ThumbnailCache


def generate_thumbnails_batch(
    paths: Iterable[str | os.PathLike[str] | ThumbnailRequest],
    *,
    cache: ThumbnailCache | None = None,
    cache_dir: str | os.PathLike[str] | None = None,
    size: int | tuple[int, int] = DEFAULT_THUMBNAIL_SIZE,
    stop_event: StopEventLike | None = None,
    max_workers: int = DEFAULT_MAX_WORKERS,
    apply_exif_orientation: bool = True,
) -> Iterator[ThumbnailResult]:
    """Convenience generator that yields thumbnails for ``paths`` using ``ThumbnailCache``."""

    active_cache = cache or ThumbnailCache(
        cache_dir=cache_dir,
        default_size=size,
        max_workers=max_workers,
    )
    yield from active_cache.generate_batch(
        paths,
        size=size,
        stop_event=stop_event,
        max_workers=max_workers,
        apply_exif_orientation=apply_exif_orientation,
    )


__all__ = [
    "DEFAULT_JPEG_QUALITY",
    "DEFAULT_MAX_CACHE_BYTES",
    "DEFAULT_MAX_WORKERS",
    "DEFAULT_THUMBNAIL_SIZE",
    "SHARD_PREFIX_LENGTH",
    "EvictionResult",
    "StopEventLike",
    "ThumbnailCache",
    "ThumbnailCacheStats",
    "ThumbnailRequest",
    "ThumbnailResult",
    "ThumbnailService",
    "build_cache_key",
    "default_thumbnail_cache_dir",
    "generate_thumbnails_batch",
    "is_pillow_available",
    "normalize_thumbnail_size",
    "validate_cache_key",
]
