"""Framework-neutral preview and thumbnail cache services."""

from __future__ import annotations

from .thumbnails import (
    DEFAULT_JPEG_QUALITY,
    DEFAULT_MAX_CACHE_BYTES,
    DEFAULT_MAX_WORKERS,
    DEFAULT_THUMBNAIL_SIZE,
    SHARD_PREFIX_LENGTH,
    EvictionResult,
    StopEventLike,
    ThumbnailCache,
    ThumbnailCacheStats,
    ThumbnailRequest,
    ThumbnailResult,
    ThumbnailService,
    build_cache_key,
    default_thumbnail_cache_dir,
    generate_thumbnails_batch,
    is_pillow_available,
    normalize_thumbnail_size,
    validate_cache_key,
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
