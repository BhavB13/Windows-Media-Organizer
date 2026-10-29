"""Similar-photo review: grouping, keep recommendations and safe quarantine.

Fixtures are smooth, photograph-like images. A pattern built with modulo
arithmetic has almost no low-frequency content and defeats pHash, which makes a
correct hash look broken; see tests/test_analysis_integration.py.
"""

from __future__ import annotations

import math
import shutil
import tempfile
import threading
import unittest
from pathlib import Path

from duplicate_transfer_manager.core import ServiceError
from duplicate_transfer_manager.runtime_paths import get_runtime_paths
from duplicate_transfer_manager.services.similar_service import SimilarPhotoService, find_images

try:
    from PIL import Image, ImageFilter
except Exception:  # pragma: no cover
    Image = None


def _photo(path: Path, size=(480, 360), phase=0.0) -> None:
    width, height = size
    image = Image.new("RGB", size)
    pixels = image.load()
    for x in range(width):
        for y in range(height):
            a = 128 + 100 * math.sin(2 * math.pi * (x / width) * 1.5 + phase) * math.cos(2 * math.pi * (y / height) * 1.1)
            b = 128 + 90 * math.sin(2 * math.pi * ((x + y) / (width + height)) * 2.0 + phase)
            pixels[x, y] = (int(max(0, min(255, a))), int(max(0, min(255, b))), int(max(0, min(255, (a + b) / 2))))
    image.filter(ImageFilter.GaussianBlur(1.2)).save(path, "JPEG", quality=95)


def _fingerprint(root: Path) -> dict[str, tuple[bytes, int]]:
    out = {}
    for path in sorted(root.rglob("*")):
        if path.is_file():
            out[str(path)] = (path.read_bytes(), path.stat().st_mtime_ns)
    return out


@unittest.skipIf(Image is None, "Pillow is required to generate fixtures")
class SimilarPhotoServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.photos = self.root / "photos"
        self.photos.mkdir()
        self.original = self.photos / "IMG_0001.jpg"
        self.resized = self.photos / "shared-small.jpg"
        self.unrelated = self.photos / "IMG_0002.jpg"
        _photo(self.original)
        with Image.open(self.original) as handle:
            handle.resize((240, 180)).save(self.resized, "JPEG", quality=80)
        _photo(self.unrelated, phase=2.3)
        self.paths = get_runtime_paths(self.root / "data")
        self.service = SimilarPhotoService(self.paths)

    def _scan(self, **kwargs):
        return self.service.scan([str(self.photos)], thumbnail_root=self.root / "thumbs", **kwargs)

    def test_a_resized_copy_is_grouped_and_an_unrelated_photo_is_not(self):
        review = self._scan()
        self.assertEqual(len(review.groups), 1)
        names = {Path(item.path).name for item in review.groups[0].items}
        self.assertEqual(names, {"IMG_0001.jpg", "shared-small.jpg"})
        self.assertEqual(review.groups[0].kind, "similar")
        self.assertEqual(review.scanned_images, 3)

    def test_nothing_is_selected_for_removal_by_default(self):
        review = self._scan()
        self.assertEqual(review.groups[0].group.selected_item_ids, ())
        self.assertEqual(review.duplicate_review.selected_count, 0)

    def test_the_larger_original_is_recommended_with_a_reason(self):
        group = self._scan().groups[0]
        keeper = next(item for item in group.items if item.id == group.keeper_id)
        self.assertEqual(Path(keeper.path).name, "IMG_0001.jpg")
        self.assertTrue(group.reasons, "a recommendation must say why")
        self.assertTrue(all(item.thumbnail_path for item in group.items))

    def test_byte_identical_copies_are_labelled_identical(self):
        shutil.copy2(self.unrelated, self.photos / "IMG_0002 (copy).jpg")
        review = self._scan()
        kinds = {frozenset(Path(i.path).name for i in g.items): g.kind for g in review.groups}
        self.assertEqual(kinds[frozenset({"IMG_0002.jpg", "IMG_0002 (copy).jpg"})], "identical")

    def test_scanning_reads_without_changing_anything(self):
        before = _fingerprint(self.photos)
        self._scan()
        self._scan(sensitivity="loose")
        self.assertEqual(_fingerprint(self.photos), before)

    def test_strict_sensitivity_still_groups_a_plain_resize(self):
        self.assertEqual(len(self._scan(sensitivity="strict").groups), 1)

    def test_cancellation_returns_promptly_and_reports_it(self):
        stop = threading.Event()
        stop.set()
        review = self._scan(stop_event=stop)
        self.assertTrue(review.cancelled)
        self.assertEqual(review.groups, ())

    def test_system_and_hidden_folders_are_skipped(self):
        bin_dir = self.photos / "$RECYCLE.BIN"
        bin_dir.mkdir()
        shutil.copy2(self.original, bin_dir / "deleted.jpg")
        hidden = self.photos / ".thumbnails"
        hidden.mkdir()
        shutil.copy2(self.original, hidden / "cache.jpg")
        found = {Path(p).name for p in find_images([str(self.photos)])}
        self.assertNotIn("deleted.jpg", found)
        self.assertNotIn("cache.jpg", found)

    def test_quarantine_moves_only_the_chosen_photo_and_can_be_inspected(self):
        review = self._scan()
        group = review.groups[0]
        victim = next(item for item in group.items if item.id != group.keeper_id)
        result = self.service.quarantine(review, [victim.id])
        self.assertEqual(len(result.records), 1)
        self.assertFalse(Path(victim.path).exists(), "the chosen photo moves into quarantine")
        self.assertTrue(Path(result.records[0].stored_path).exists())
        self.assertTrue(self.original.exists(), "the keeper is untouched")
        self.assertTrue(self.unrelated.exists())

    def test_removing_every_copy_of_a_photo_is_refused(self):
        review = self._scan()
        every = [item.id for item in review.groups[0].items]
        before = _fingerprint(self.photos)
        with self.assertRaises(ServiceError):
            self.service.quarantine(review, every)
        self.assertEqual(_fingerprint(self.photos), before)

    def test_empty_or_foreign_selections_are_refused(self):
        review = self._scan()
        with self.assertRaises(ServiceError):
            self.service.quarantine(review, [])
        with self.assertRaises(ServiceError):
            self.service.quarantine(review, ["not-from-this-review"])

    def test_a_dry_run_moves_nothing(self):
        review = self._scan()
        group = review.groups[0]
        victim = next(item for item in group.items if item.id != group.keeper_id)
        before = _fingerprint(self.photos)
        self.service.quarantine(review, [victim.id], dry_run=True)
        self.assertEqual(_fingerprint(self.photos), before)


if __name__ == "__main__":
    unittest.main()
