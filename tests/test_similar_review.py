"""Similar-photo review: grouping, keep recommendations and safe quarantine.

Fixtures are smooth, photograph-like images. A pattern built with modulo
arithmetic has almost no low-frequency content and defeats pHash, which makes a
correct hash look broken; see tests/test_analysis_integration.py.
"""

from __future__ import annotations

import math
import os
import shutil
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

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


try:
    import os as _os

    _os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication, QDialog

    from duplicate_transfer_manager.ui.similar_review_page import SimilarReviewPage
    from duplicate_transfer_manager.ui.widgets import ConfirmationDialog
except Exception:  # pragma: no cover - page tests skip without PySide6
    QApplication = None


@unittest.skipIf(Image is None or QApplication is None, "Pillow and PySide6 are required")
class SimilarReviewPageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.application = QApplication.instance() or QApplication([])

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        self.photos = root / "photos"
        self.photos.mkdir()
        self.original = self.photos / "IMG_0001.jpg"
        self.resized = self.photos / "small.jpg"
        _photo(self.original)
        with Image.open(self.original) as handle:
            handle.resize((240, 180)).save(self.resized, "JPEG", quality=80)
        self.page = SimilarReviewPage(get_runtime_paths(root / "data"))
        self.page.path.set_path(str(self.photos))

    def tearDown(self):
        self.page.controller.cancel()
        self.page.controller.wait_for_done(5000)
        self._drain(lambda: not getattr(self.page, "_background_workers", []), 5)
        self.page.deleteLater()
        self.application.processEvents()

    def _drain(self, until, timeout=30.0):
        import time

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline and not until():
            self.application.processEvents()
            time.sleep(0.02)
        return until()

    def _scan(self):
        self.assertTrue(self.page.start_scan())
        self.assertTrue(self._drain(lambda: self.page.review is not None), "scan did not finish")

    def test_scan_shows_groups_with_nothing_selected(self):
        self._scan()
        self.assertEqual(len(self.page.review.groups), 1)
        self.assertEqual(len(self.page.checks), 2)
        self.assertEqual(self.page.selected_ids(), [])
        self.assertFalse(self.page.quarantine_button.isEnabled())

    def test_a_missing_folder_is_reported_without_scanning(self):
        self.page.path.set_path(str(self.photos / "missing"))
        self.assertFalse(self.page.start_scan())
        self.assertIsNone(self.page.review)

    def test_quarantining_a_chosen_copy_after_confirmation(self):
        self._scan()
        group = self.page.review.groups[0]
        victim = next(item for item in group.items if item.id != group.keeper_id)
        self.page.checks[victim.id].setChecked(True)
        self.assertTrue(self.page.quarantine_button.isEnabled())
        with patch.object(ConfirmationDialog, "exec", return_value=QDialog.DialogCode.Accepted):
            self.page.quarantine_selected()
            self.assertTrue(self._drain(lambda: "Moved" in self.page.banner.label.text()))
        self.assertFalse(Path(victim.path).exists())
        self.assertTrue(self.original.exists())
        self.assertFalse(self.page.checks[victim.id].isEnabled())

    def test_declining_the_confirmation_moves_nothing(self):
        self._scan()
        group = self.page.review.groups[0]
        victim = next(item for item in group.items if item.id != group.keeper_id)
        self.page.checks[victim.id].setChecked(True)
        with patch.object(ConfirmationDialog, "exec", return_value=QDialog.DialogCode.Rejected):
            self.page.quarantine_selected()
        self.application.processEvents()
        self.assertTrue(Path(victim.path).exists())


@unittest.skipIf(Image is None, "Pillow is required to generate fixtures")
class PerceptualHashStoreTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.photos = self.root / "photos"
        self.photos.mkdir()
        self.original = self.photos / "a.jpg"
        _photo(self.original)
        with Image.open(self.original) as handle:
            handle.resize((240, 180)).save(self.photos / "b.jpg", "JPEG", quality=80)
        self.service = SimilarPhotoService(get_runtime_paths(self.root / "data"))

    def _scan_counting(self):
        from duplicate_transfer_manager.media import perceptual

        hashed = []
        real = perceptual.hash_images

        def counting(paths, *args, **kwargs):
            paths = list(paths)
            hashed.extend(paths)
            return real(paths, *args, **kwargs)

        with patch.object(perceptual, "hash_images", counting):
            review = self.service.scan([str(self.photos)], thumbnail_root=self.root / "thumbs")
        return review, hashed

    def test_a_repeat_scan_decodes_nothing_and_finds_the_same_groups(self):
        first, hashed_first = self._scan_counting()
        second, hashed_second = self._scan_counting()
        self.assertEqual(len(hashed_first), 2)
        self.assertEqual(hashed_second, [], "unchanged photos must come from the store")
        self.assertEqual(
            [sorted(i.path for i in g.items) for g in first.groups],
            [sorted(i.path for i in g.items) for g in second.groups],
        )

    def test_an_edited_photo_is_hashed_again(self):
        self._scan_counting()
        _photo(self.original, phase=1.0)
        os.utime(self.original, ns=(self.original.stat().st_atime_ns, self.original.stat().st_mtime_ns + 10**9))
        _review, hashed = self._scan_counting()
        self.assertEqual([Path(p).name for p in hashed], ["a.jpg"])

    def test_hashes_with_the_top_bit_set_round_trip(self):
        from duplicate_transfer_manager.media.hash_store import PerceptualHashStore
        from duplicate_transfer_manager.media.perceptual import HashedImageEntry, ImageHash

        top = (1 << 63) | 0x1234
        entry = HashedImageEntry(str(self.original), ImageHash("phash", top), (ImageHash("phash", top), ImageHash("phash", 7)))
        with PerceptualHashStore(self.root / "h.sqlite3") as store:
            store.store([entry])
            hits, misses = store.lookup([str(self.original)])
        self.assertEqual(misses, [])
        got = hits[str(self.original)]
        self.assertEqual(got.image_hash.bits, top)
        self.assertEqual([h.bits for h in got.rotation_hashes], [top, 7])

    def test_a_corrupt_store_does_not_stop_the_scan(self):
        store_path = self.service.paths.cache / "perceptual_hashes.sqlite3"
        store_path.parent.mkdir(parents=True, exist_ok=True)
        store_path.write_bytes(b"this is not a database")
        review, hashed = self._scan_counting()
        self.assertEqual(len(review.groups), 1)
        self.assertEqual(len(hashed), 2)
