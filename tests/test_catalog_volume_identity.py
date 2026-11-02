"""A drive that takes over another drive's letter must not inherit its records."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from volume_identity import VolumeIdentity

from duplicate_transfer_manager.services.catalog_service import CatalogService


def _prober(guid: str, *, fallback: bool = False):
    def probe(path):
        return VolumeIdentity(
            guid="" if fallback else guid,
            filesystem="NTFS",
            mount_point=str(path),
            tier="fallback" if fallback else "guid",
            is_fallback=fallback,
        )

    return probe


class VolumeKeyReuseTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.folder = self.root / "E_drive"
        self.folder.mkdir()
        (self.folder / "photo.jpg").write_bytes(b"first drive's photo")
        self.db = self.root / "catalog.sqlite3"

    def _index(self, prober):
        with CatalogService(db_path=self.db, volume_prober=prober, compute_hashes=True) as service:
            return service.index_folder(self.folder)

    def test_a_different_drive_at_the_same_path_gets_its_own_key(self):
        first = self._index(_prober("{11111111-1111-1111-1111-111111111111}"))
        # Unplug, and a different drive arrives at the same letter and path.
        (self.folder / "photo.jpg").write_bytes(b"second drive's photo")
        second = self._index(_prober("{22222222-2222-2222-2222-222222222222}"))
        self.assertNotEqual(first.volume_key, second.volume_key)
        self.assertEqual(second.added, 1, "the second drive's file must be indexed as its own")

    def test_the_same_drive_keeps_its_key(self):
        guid = "{11111111-1111-1111-1111-111111111111}"
        first = self._index(_prober(guid))
        second = self._index(_prober(guid))
        self.assertEqual(first.volume_key, second.volume_key)
        self.assertEqual(second.unchanged, 1)

    def test_without_a_strong_identity_the_path_is_still_used(self):
        first = self._index(_prober("", fallback=True))
        second = self._index(_prober("", fallback=True))
        self.assertEqual(first.volume_key, second.volume_key)


if __name__ == "__main__":
    unittest.main()
