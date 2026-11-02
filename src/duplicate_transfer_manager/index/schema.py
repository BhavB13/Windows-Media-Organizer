"""SQLite schema definition and forward-only migration runner for the media catalog.

Index rationale
---------------
Every secondary index in this schema exists to back a specific catalog query
or analytics aggregation:

- ``ux_files_volume_relpath`` (UNIQUE on ``files(volume_key, relative_path)``):
  Enforces the ``(volume_key, relative_path)`` file identity used by
  incremental indexing (``upsert_files``), volume-scoped queries, and
  deterministic path ordering.
- ``idx_files_media_kind`` on ``files(media_kind)``:
  Accelerates ``search(media_kind=...)`` filtering and
  ``analytics_by_media_kind()`` (``GROUP BY media_kind``).
- ``idx_files_content_hash`` on ``files(content_hash) WHERE content_hash IS NOT NULL``:
  Accelerates duplicate detection in ``search(has_duplicates=...)`` and
  ``recoverable_duplicate_bytes()`` (``GROUP BY content_hash HAVING COUNT(*) > 1``).
- ``idx_files_size`` on ``files(size)``:
  Accelerates ``search(min_size=..., max_size=...)`` range predicates and
  size-based ordering.
- ``idx_files_mtime_ns`` on ``files(mtime_ns)``:
  Accelerates ``search(min_mtime_ns=..., max_mtime_ns=...)`` range predicates
  and modification-time ordering.
- ``idx_files_filename_nocase`` on ``files(filename COLLATE NOCASE)``:
  Supports case-insensitive filename ordering and prefix/equality lookups.
- ``idx_files_extension`` on ``files(extension)``:
  Accelerates ``analytics_by_extension()`` (``GROUP BY extension``).
- ``idx_metadata_capture_timestamp`` on ``metadata(capture_timestamp)``:
  Accelerates ``search(capture_start=..., capture_end=...)`` capture-date
  range queries and capture-time ordering.
- ``idx_metadata_capture_year`` on ``metadata(capture_year)``:
  Accelerates ``analytics_by_capture_year()`` (``GROUP BY capture_year``).
- ``idx_metadata_camera`` on ``metadata(camera_make COLLATE NOCASE, camera_model COLLATE NOCASE)``:
  Accelerates ``search(camera_make=..., camera_model=...)`` filtering.
- ``idx_files_perceptual_hash`` on ``files(perceptual_hash) WHERE perceptual_hash IS NOT NULL``:
  Accelerates candidate similarity lookup and similar photo queries.
"""

from __future__ import annotations

from datetime import UTC, datetime
import posixpath
import sqlite3
from typing import Callable, Mapping, Sequence

INITIAL_SCHEMA_VERSION: int = 1
SCHEMA_VERSION: int = 3
DEFAULT_BUSY_TIMEOUT_MS: int = 5000

MigrationFn = Callable[[sqlite3.Connection], None]
MigrationStep = MigrationFn | Sequence[str]


class SchemaMigrationError(ValueError):
    """Raised when a database schema version cannot be migrated forward."""


INDEX_DOCUMENTATION: dict[str, str] = {
    "ux_files_volume_relpath": (
        "Enforces (volume_key, relative_path) identity for incremental upsert_files() "
        "lookups, volume filtering, and deterministic path ordering."
    ),
    "idx_files_media_kind": (
        "Backs search(media_kind=...) predicates and analytics_by_media_kind() GROUP BY."
    ),
    "idx_files_content_hash": (
        "Partial index on non-null content_hash backing search(has_duplicates=...) "
        "and recoverable_duplicate_bytes() duplicate grouping."
    ),
    "idx_files_size": (
        "Backs search(min_size=..., max_size=...) range filters and size ordering."
    ),
    "idx_files_mtime_ns": (
        "Backs search(min_mtime_ns=..., max_mtime_ns=...) range filters and mtime ordering."
    ),
    "idx_files_filename_nocase": (
        "Backs case-insensitive filename ordering and lookups."
    ),
    "idx_files_extension": (
        "Backs analytics_by_extension() GROUP BY aggregation."
    ),
    "idx_metadata_capture_timestamp": (
        "Backs search(capture_start=..., capture_end=...) range filters and capture-date ordering."
    ),
    "idx_metadata_capture_year": (
        "Backs analytics_by_capture_year() GROUP BY aggregation."
    ),
    "idx_metadata_camera": (
        "Backs case-insensitive search(camera_make=..., camera_model=...) filtering."
    ),
    "idx_files_perceptual_hash": (
        "Partial index on non-null perceptual_hash backing candidate similarity lookup "
        "and similar photo grouping."
    ),
}


MIGRATION_V1_SQL: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS volumes (
        volume_key TEXT PRIMARY KEY NOT NULL,
        label TEXT NOT NULL DEFAULT '',
        filesystem TEXT NOT NULL DEFAULT '',
        last_seen REAL NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS files (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        volume_key TEXT NOT NULL REFERENCES volumes(volume_key) ON DELETE CASCADE,
        relative_path TEXT NOT NULL,
        size INTEGER NOT NULL CHECK (size >= 0),
        mtime_ns INTEGER NOT NULL,
        content_hash TEXT,
        media_kind TEXT NOT NULL,
        indexed_at REAL NOT NULL,
        UNIQUE (volume_key, relative_path)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS metadata (
        file_id INTEGER PRIMARY KEY REFERENCES files(id) ON DELETE CASCADE,
        capture_timestamp REAL,
        capture_date TEXT,
        camera_make TEXT,
        camera_model TEXT,
        width INTEGER,
        height INTEGER
    )
    """,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS ux_files_volume_relpath
        ON files(volume_key, relative_path)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_files_media_kind
        ON files(media_kind)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_files_content_hash
        ON files(content_hash)
        WHERE content_hash IS NOT NULL
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_files_size
        ON files(size)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_files_mtime_ns
        ON files(mtime_ns)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_metadata_capture_timestamp
        ON metadata(capture_timestamp)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_metadata_camera
        ON metadata(camera_make COLLATE NOCASE, camera_model COLLATE NOCASE)
    """,
)


def _table_columns(conn: sqlite3.Connection, table_name: str) -> set[str]:
    rows = conn.execute(f"PRAGMA table_info({table_name})").fetchall()
    return {str(row[1]) for row in rows}


def _migrate_to_v1(conn: sqlite3.Connection) -> None:
    """Create the initial v1 catalog tables and core indexes."""
    for statement in MIGRATION_V1_SQL:
        conn.execute(statement)


def _migrate_to_v2(conn: sqlite3.Connection) -> None:
    """Upgrade v1 schema to v2 with online volume tracking and analytics columns.

    Adds ``root_path`` and ``is_online`` to ``volumes``, ``filename`` and
    ``extension`` to ``files``, and ``capture_year`` to ``metadata`` while
    preserving and backfilling all existing v1 rows.
    """
    _migrate_to_v1(conn)

    volume_cols = _table_columns(conn, "volumes")
    if "root_path" not in volume_cols:
        conn.execute("ALTER TABLE volumes ADD COLUMN root_path TEXT NOT NULL DEFAULT ''")
    if "is_online" not in volume_cols:
        conn.execute("ALTER TABLE volumes ADD COLUMN is_online INTEGER NOT NULL DEFAULT 1")

    file_cols = _table_columns(conn, "files")
    added_filename = False
    added_extension = False
    if "filename" not in file_cols:
        conn.execute("ALTER TABLE files ADD COLUMN filename TEXT NOT NULL DEFAULT ''")
        added_filename = True
    if "extension" not in file_cols:
        conn.execute("ALTER TABLE files ADD COLUMN extension TEXT NOT NULL DEFAULT ''")
        added_extension = True

    if added_filename or added_extension:
        rows = conn.execute("SELECT id, relative_path, filename, extension FROM files").fetchall()
        for file_id, rel_path, current_name, current_ext in rows:
            normalized = str(rel_path).replace("\\", "/")
            basename = current_name or posixpath.basename(normalized)
            _, ext = posixpath.splitext(basename)
            ext_norm = current_ext or ext.lower()
            conn.execute(
                "UPDATE files SET filename = ?, extension = ? WHERE id = ?",
                (basename, ext_norm, file_id),
            )

    metadata_cols = _table_columns(conn, "metadata")
    if "capture_year" not in metadata_cols:
        conn.execute("ALTER TABLE metadata ADD COLUMN capture_year INTEGER")
        meta_rows = conn.execute(
            "SELECT file_id, capture_timestamp, capture_date FROM metadata"
        ).fetchall()
        for file_id, capture_ts, capture_date in meta_rows:
            year: int | None = None
            if isinstance(capture_date, str) and len(capture_date) >= 4 and capture_date[:4].isdigit():
                year = int(capture_date[:4])
            elif capture_ts is not None:
                try:
                    year = datetime.fromtimestamp(float(capture_ts), UTC).year
                except (OSError, OverflowError, TypeError, ValueError):
                    year = None
            if year is not None:
                conn.execute(
                    "UPDATE metadata SET capture_year = ? WHERE file_id = ?",
                    (year, file_id),
                )

    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_files_filename_nocase
            ON files(filename COLLATE NOCASE)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_files_extension
            ON files(extension)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_metadata_capture_year
            ON metadata(capture_year)
        """
    )


def _migrate_to_v3(conn: sqlite3.Connection) -> None:
    """Upgrade v2 schema to v3 with perceptual hash columns and candidate index.

    Adds nullable ``perceptual_hash``, ``perceptual_algo``, and ``perceptual_rotations``
    columns to ``files`` and creates ``idx_files_perceptual_hash`` while preserving
    all existing v1 and v2 rows intact.
    """
    _migrate_to_v2(conn)

    file_cols = _table_columns(conn, "files")
    if "perceptual_hash" not in file_cols:
        conn.execute("ALTER TABLE files ADD COLUMN perceptual_hash TEXT")
    if "perceptual_algo" not in file_cols:
        conn.execute("ALTER TABLE files ADD COLUMN perceptual_algo TEXT")
    if "perceptual_rotations" not in file_cols:
        conn.execute("ALTER TABLE files ADD COLUMN perceptual_rotations TEXT")

    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_files_perceptual_hash
            ON files(perceptual_hash)
            WHERE perceptual_hash IS NOT NULL
        """
    )


BUILTIN_MIGRATIONS: Mapping[int, MigrationStep] = {
    1: _migrate_to_v1,
    2: _migrate_to_v2,
    3: _migrate_to_v3,
}


def configure_connection(
    conn: sqlite3.Connection,
    *,
    busy_timeout_ms: int = DEFAULT_BUSY_TIMEOUT_MS,
) -> None:
    """Apply durability and concurrency PRAGMAs required by the catalog."""
    timeout = max(0, int(busy_timeout_ms))
    conn.execute(f"PRAGMA busy_timeout = {timeout}")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA foreign_keys = ON")


def get_user_version(conn: sqlite3.Connection) -> int:
    """Return the current integer ``PRAGMA user_version`` of the database."""
    row = conn.execute("PRAGMA user_version").fetchone()
    return int(row[0]) if row is not None else 0


def set_user_version(conn: sqlite3.Connection, version: int) -> None:
    """Set the integer ``PRAGMA user_version`` of the database."""
    version_int = int(version)
    if version_int < 0:
        raise ValueError("user_version must be a non-negative integer")
    conn.execute(f"PRAGMA user_version = {version_int}")


def apply_migrations(
    conn: sqlite3.Connection,
    *,
    target_version: int = SCHEMA_VERSION,
    migrations: Mapping[int, MigrationStep] | None = None,
) -> int:
    """Run forward-only schema migrations up to ``target_version``.

    Each version step executes atomically inside a transaction together with its
    ``PRAGMA user_version`` update so an interrupted migration never leaves a
    partially upgraded schema.
    """
    if target_version < 0:
        raise SchemaMigrationError(f"Invalid target schema version: {target_version}")

    current = get_user_version(conn)
    if current > target_version:
        raise SchemaMigrationError(
            f"Database user_version {current} is newer than supported target version "
            f"{target_version}; downgrades are not supported."
        )
    if current == target_version:
        return current

    step_map: dict[int, MigrationStep] = dict(BUILTIN_MIGRATIONS)
    if migrations is not None:
        step_map.update(migrations)

    for next_version in range(current + 1, target_version + 1):
        step = step_map.get(next_version)
        if step is None:
            raise SchemaMigrationError(
                f"Missing migration step for version {next_version} "
                f"(migrating from {current} to {target_version})."
            )
        conn.execute("BEGIN IMMEDIATE")
        try:
            if callable(step):
                step(conn)
            else:
                for sql in step:
                    conn.execute(sql)
            set_user_version(conn, next_version)
            conn.execute("COMMIT")
        except Exception:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise

    return get_user_version(conn)


def initialize_schema(
    conn: sqlite3.Connection,
    *,
    busy_timeout_ms: int = DEFAULT_BUSY_TIMEOUT_MS,
    target_version: int = SCHEMA_VERSION,
    migrations: Mapping[int, MigrationStep] | None = None,
) -> int:
    """Configure connection PRAGMAs and migrate the schema to ``target_version``."""
    configure_connection(conn, busy_timeout_ms=busy_timeout_ms)
    return apply_migrations(conn, target_version=target_version, migrations=migrations)
