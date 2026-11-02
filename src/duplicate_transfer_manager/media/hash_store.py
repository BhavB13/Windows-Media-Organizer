"""Remember perceptual hashes between similar-photo scans.

Computing a perceptual hash decodes the whole image, which is the slow part of
finding similar photos. A photo whose path, size and modification time are
unchanged has the same hash, so a second scan of the same library only needs to
decode what is new or edited.

The store is a cache in the app's own data folder. It never touches the photos,
and every failure - a locked or corrupt database, a full disk - degrades to
hashing without it rather than stopping the scan.
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from typing import Iterable

from .perceptual import HashedImageEntry, ImageHash

_SCHEMA = """
CREATE TABLE IF NOT EXISTS hashes (
    path TEXT NOT NULL,
    algorithm TEXT NOT NULL,
    rotation INTEGER NOT NULL,
    size INTEGER NOT NULL,
    mtime_ns INTEGER NOT NULL,
    bit_count INTEGER NOT NULL,
    bits TEXT NOT NULL,
    rotations TEXT NOT NULL,
    PRIMARY KEY (path, algorithm, rotation)
)
"""


def _key(path: str) -> str:
    return os.path.normcase(os.path.abspath(path))


class PerceptualHashStore:
    def __init__(self, db_path: str | os.PathLike[str]) -> None:
        self.db_path = Path(db_path)
        self._connection: sqlite3.Connection | None = None
        connection = None
        try:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            connection = sqlite3.connect(str(self.db_path), timeout=5)
            connection.execute(_SCHEMA)
            connection.commit()
            self._connection = connection
        except (OSError, sqlite3.Error):
            # A corrupt file fails here, after connect succeeded; left open, the
            # connection would keep the file locked for the rest of the run.
            if connection is not None:
                connection.close()
            self._connection = None

    @property
    def available(self) -> bool:
        return self._connection is not None

    def close(self) -> None:
        if self._connection is not None:
            try:
                self._connection.close()
            finally:
                self._connection = None

    def __enter__(self) -> "PerceptualHashStore":
        return self

    def __exit__(self, *_exc) -> None:
        self.close()

    def lookup(
        self, paths: Iterable[str], *, algorithm: str = "phash", rotation: bool = True
    ) -> tuple[dict[str, HashedImageEntry], list[str]]:
        """Split ``paths`` into cached entries and the paths still to hash."""

        hits: dict[str, HashedImageEntry] = {}
        misses: list[str] = []
        for path in paths:
            entry = self._get(path, algorithm, rotation) if self.available else None
            if entry is None:
                misses.append(path)
            else:
                hits[path] = entry
        return hits, misses

    def _get(self, path: str, algorithm: str, rotation: bool) -> HashedImageEntry | None:
        try:
            stat = os.stat(path)
            row = self._connection.execute(
                "SELECT size, mtime_ns, bit_count, bits, rotations FROM hashes "
                "WHERE path = ? AND algorithm = ? AND rotation = ?",
                (_key(path), algorithm, int(rotation)),
            ).fetchone()
        except (OSError, sqlite3.Error):
            return None
        if row is None or row[0] != stat.st_size or row[1] != stat.st_mtime_ns:
            return None
        try:
            bit_count = int(row[2])
            image_hash = ImageHash(algorithm=algorithm, bits=int(row[3], 16), bit_count=bit_count)
            rotations = tuple(
                ImageHash(algorithm=algorithm, bits=int(value, 16), bit_count=bit_count)
                for value in row[4].split(",")
                if value
            )
        except (ValueError, TypeError):
            return None
        return HashedImageEntry(path=path, image_hash=image_hash, rotation_hashes=rotations)

    def store(self, entries: Iterable[HashedImageEntry], *, rotation: bool = True) -> None:
        if not self.available:
            return
        rows = []
        for entry in entries:
            try:
                stat = os.stat(entry.path)
            except OSError:
                continue
            image_hash = entry.image_hash
            rows.append(
                (
                    _key(entry.path),
                    image_hash.algorithm,
                    int(rotation),
                    stat.st_size,
                    stat.st_mtime_ns,
                    image_hash.bit_count,
                    # Hex text, not INTEGER: SQLite integers are signed 64-bit,
                    # so a hash with its top bit set would not round-trip.
                    format(image_hash.bits, "x"),
                    ",".join(format(h.bits, "x") for h in entry.rotation_hashes),
                )
            )
        if not rows:
            return
        try:
            self._connection.executemany(
                "INSERT OR REPLACE INTO hashes "
                "(path, algorithm, rotation, size, mtime_ns, bit_count, bits, rotations) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                rows,
            )
            self._connection.commit()
        except sqlite3.Error:
            pass
