"""Duplicate review previews come from the persistent thumbnail cache.

The review used to decode every photo at full size on every build, ignore the
EXIF orientation tag so portrait photos appeared on their side, and leave its
thumbnail folder to grow without limit. These tests pin the behaviour that
replaced it, using generated images only.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from models import FileInfo

from duplicate_transfer_manager.previews.thumbnails import ThumbnailCache
from duplicate_transfer_manager.services.duplicate_workflow import build_duplicate_review

try:
    from PIL import Image
except Exception:  # pragma: no cover - skipped below
    Image = None


ORIENTATION_TAG = 0x0112


def _landscape_jpeg(path: Path, orientation: int | None = None) -> None:
    image = Image.new("RGB", (400, 200), (40, 120, 200))
    for x in range(0, 400, 20):
        for y in range(200):
            image.putpixel((x, y), (250, 250, 250))
    if orientation is None:
        image.save(path, "JPEG", quality=90)
        return
    exif = Image.Exif()
    exif[ORIENTATION_TAG] = orientation
    image.save(path, "JPEG", quality=90, exif=exif.tobytes())


def _state(path: Path) -> tuple[bytes, int]:
    return path.read_bytes(), path.stat().st_mtime_ns


@unittest.skipIf(Image is None, "Pillow is required for preview tests")
class DuplicateReviewThumbnailTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.thumbs = self.root / "thumbs"

    def _review(self, *paths: Path, is_adb: bool = False):
        group = [FileInfo(str(p), p.stat().st_size if p.exists() else 1, 100 + i, is_adb) for i, p in enumerate(paths)]
        return build_duplicate_review([group], thumbnail_root=self.thumbs)

    def test_rotated_photo_is_previewed_upright(self) -> None:
        # Orientation 6: stored landscape, meant to be shown rotated 90 degrees.
        upright = self.root / "portrait.jpg"
        copy = self.root / "portrait-copy.jpg"
        _landscape_jpeg(upright, orientation=6)
        _landscape_jpeg(copy, orientation=6)

        item = self._review(upright, copy).groups[0].items[0]

        self.assertTrue(item.thumbnail_path, item.preview_status)
        with Image.open(item.thumbnail_path) as thumb:
            width, height = thumb.size
        self.assertGreater(height, width, "a portrait photo must not be previewed on its side")
        self.assertLessEqual(max(width, height), 96)

    def test_thumbnails_live_in_the_requested_root(self) -> None:
        a, b = self.root / "a.jpg", self.root / "b.jpg"
        _landscape_jpeg(a)
        _landscape_jpeg(b)
        for item in self._review(a, b).groups[0].items:
            self.assertTrue(Path(item.thumbnail_path).is_relative_to(self.thumbs))

    def test_rebuilding_a_review_reuses_thumbnails_without_rewriting_them(self) -> None:
        a, b = self.root / "a.jpg", self.root / "b.jpg"
        _landscape_jpeg(a)
        _landscape_jpeg(b)
        first = self._review(a, b).groups[0].items[0]

        # The cache refreshes an entry's timestamp on every hit for its LRU
        # budget, so a timestamp cannot show whether it re-decoded. Ask it.
        results = []
        original = ThumbnailCache.get_or_create

        def recording(cache, *args, **kwargs):
            result = original(cache, *args, **kwargs)
            results.append(result)
            return result

        with mock.patch.object(ThumbnailCache, "get_or_create", recording):
            second = self._review(a, b).groups[0].items[0]

        self.assertEqual(second.thumbnail_path, first.thumbnail_path)
        self.assertEqual(len(results), 2)
        self.assertTrue(all(r.cache_hit and not r.generated for r in results), results)

    def test_an_edited_photo_gets_a_fresh_thumbnail(self) -> None:
        a, b = self.root / "a.jpg", self.root / "b.jpg"
        _landscape_jpeg(a)
        _landscape_jpeg(b)
        before = self._review(a, b).groups[0].items[0].thumbnail_path

        Image.new("RGB", (300, 300), (200, 30, 30)).save(a, "JPEG")
        os.utime(a, ns=(a.stat().st_atime_ns, a.stat().st_mtime_ns + 5_000_000_000))
        after = self._review(a, b).groups[0].items[0].thumbnail_path

        self.assertNotEqual(after, before, "a changed photo must not be shown its old thumbnail")

    def test_existing_statuses_for_items_without_a_preview(self) -> None:
        notes, notes_copy = self.root / "notes.txt", self.root / "notes-copy.txt"
        notes.write_text("same")
        notes_copy.write_text("same")
        item = self._review(notes, notes_copy).groups[0].items[0]
        self.assertEqual(item.thumbnail_path, "")
        self.assertEqual(item.preview_status, "No thumbnail for this file type.")

        phone = self._review(Path("/sdcard/DCIM/a.jpg"), Path("/sdcard/DCIM/b.jpg"), is_adb=True)
        android = phone.groups[0].items[0]
        self.assertEqual(android.thumbnail_path, "")
        self.assertEqual(android.preview_status, "Android preview unavailable before quarantine.")

    def test_an_undecodable_image_is_reported_not_raised(self) -> None:
        broken, broken_copy = self.root / "broken.jpg", self.root / "broken-copy.jpg"
        broken.write_bytes(b"not really a jpeg")
        broken_copy.write_bytes(b"not really a jpeg")
        item = self._review(broken, broken_copy).groups[0].items[0]
        self.assertEqual(item.thumbnail_path, "")
        self.assertTrue(item.preview_status)

    def test_building_previews_never_touches_the_photos(self) -> None:
        a, b = self.root / "a.jpg", self.root / "b.jpg"
        _landscape_jpeg(a, orientation=6)
        _landscape_jpeg(b)
        before = (_state(a), _state(b))
        self._review(a, b)
        self._review(a, b)
        self.assertEqual((_state(a), _state(b)), before)


if __name__ == "__main__":
    unittest.main()
