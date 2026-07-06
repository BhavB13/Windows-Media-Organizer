"""Compatibility import for runtime paths during the Phase 0 transition."""

from pathlib import Path
import sys


_SOURCE_ROOT = Path(__file__).resolve().parent / "src"
if str(_SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SOURCE_ROOT))

from duplicate_transfer_manager.runtime_paths import (  # noqa: E402,F401
    APP_DIRECTORY_NAME,
    MIGRATION_MARKER_NAME,
    MigrationResult,
    RuntimePaths,
    get_runtime_paths,
    initialize_runtime_data,
    migrate_legacy_data,
)

__all__ = [
    "APP_DIRECTORY_NAME",
    "MIGRATION_MARKER_NAME",
    "MigrationResult",
    "RuntimePaths",
    "get_runtime_paths",
    "initialize_runtime_data",
    "migrate_legacy_data",
]
