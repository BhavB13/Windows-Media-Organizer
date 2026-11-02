"""Turning on hashing or similarity for an already indexed library.

Rows indexed earlier are filled in place. Nothing is removed first, so a
cancelled pass leaves the catalog exactly as it was.
"""

from __future__ import annotations

import math
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from volume_identity import VolumeIdentity

from duplicate_transfer_manager.index import catalog as catalog_module
from duplicate_transfer_manager.services.catalog_service import CatalogService

try:
    from PIL import Image, ImageFilter
except Exception:  # pragma: no cover
    Image = None


def _probe(path):
    return VolumeIdentity(guid="{33333333-3333-3333-3333-333333333333}", filesystem="NTFS",
                          mount_point=str(path), tier="guid", is_fallback=False)


def _photo(path: Path, phase: float = 0.0) -> None:
    image = Image.new("RGB", (64, 48))
    pixels = image.load()
    for x in range(64):
        for y in range(48):
            v = int(128 + 100 * math.sin(x / 9 + phase) * math.cos(y / 7))
            pixels[x, y] = (v, (v * 2) % 256, 255 - v)
    image.filter(ImageFilter.GaussianBlur(1)).save(path, "JPEG", quality=90)


@unittest.skipIf(Image is None, "Pillow is required")
class BackfillTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.folder = self.root / "photos"
        self.folder.mkdir()
        for index in range(3):
            _photo(self.folder / f"p{index}.jpg", phase=index)
        self.db = self.root / "catalog.sqlite3"

    def _service(self):
        return CatalogService(db_path=self.db, volume_prober=_probe)

    def _rows(self):
        with self._service() as service:
            catalog, should_close = service._open_catalog()
            try:
                return catalog.connection.execute(
                    "SELECT relative_path, content_hash, perceptual_hash FROM files ORDER BY relative_path"
                ).fetchall()
            finally:
                if should_close:
                    catalog.close()

    def test_a_cancelled_hashing_pass_keeps_every_existing_row(self):
        with self._service() as service:
            service.index_folder(self.folder, compute_hashes=False)
        self.assertEqual(len(self._rows()), 3)

        stop = threading.Event()
        real_hash = catalog_module.compute_file_sha256

        def hash_then_cancel(path, stop_event=None):
            value = real_hash(path, stop_event=stop_event)
            stop.set()
            return value

        with patch("duplicate_transfer_manager.services.catalog_service.compute_file_sha256", hash_then_cancel), self._service() as service:
            result = service.index_folder(self.folder, compute_hashes=True, stop_event=stop)
        self.assertTrue(result.cancelled)
        rows = self._rows()
        self.assertEqual(len(rows), 3, "cancelling a backfill must not lose catalog rows")
        self.assertTrue(all(row["content_hash"] is None for row in rows))

    def test_a_completed_hashing_pass_fills_hashes_as_updates(self):
        with self._service() as service:
            service.index_folder(self.folder, compute_hashes=False)
            result = service.index_folder(self.folder, compute_hashes=True)
        self.assertEqual((result.added, result.updated), (0, 3))
        self.assertTrue(all(row["content_hash"] for row in self._rows()))

        with self._service() as service:
            again = service.index_folder(self.folder, compute_hashes=True)
        self.assertEqual(again.unchanged, 3, "a filled row is not revisited")

    def test_turning_on_similarity_later_fills_perceptual_hashes_and_keeps_content_hashes(self):
        with self._service() as service:
            service.index_folder(self.folder, compute_hashes=True, compute_perceptual=False)
        before = {row["relative_path"]: row["content_hash"] for row in self._rows()}
        with self._service() as service:
            result = service.index_folder(self.folder, compute_hashes=True, compute_perceptual=True)
        self.assertEqual(result.updated, 3)
        rows = self._rows()
        self.assertTrue(all(row["perceptual_hash"] for row in rows))
        self.assertEqual({row["relative_path"]: row["content_hash"] for row in rows}, before)

    def test_turning_on_hashing_later_keeps_perceptual_hashes(self):
        with self._service() as service:
            service.index_folder(self.folder, compute_hashes=False, compute_perceptual=True)
        before = {row["relative_path"]: row["perceptual_hash"] for row in self._rows()}
        with self._service() as service:
            service.index_folder(self.folder, compute_hashes=True, compute_perceptual=False)
        self.assertEqual({row["relative_path"]: row["perceptual_hash"] for row in self._rows()}, before)


if __name__ == "__main__":
    unittest.main()
