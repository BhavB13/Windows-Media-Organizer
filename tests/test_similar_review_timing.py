"""Timing and performance regression tests for Similar Photos review."""

from __future__ import annotations

import math
import os
import tempfile
import time
import unittest
from pathlib import Path

try:
    from PIL import Image, ImageFilter
except Exception:  # pragma: no cover
    Image = None

try:
    import os as _os

    _os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    from duplicate_transfer_manager.runtime_paths import get_runtime_paths
    from duplicate_transfer_manager.services.similar_service import SimilarPhotoService
    from duplicate_transfer_manager.ui.similar_review_page import SimilarReviewPage
except Exception:  # pragma: no cover
    QApplication = None


def _make_photo(path: Path, size: tuple[int, int] = (480, 360), phase: float = 0.0) -> None:
    width, height = size
    image = Image.new("RGB", size)
    pixels = image.load()
    for x in range(width):
        for y in range(height):
            a = 128 + 100 * math.sin(2 * math.pi * (x / width) * 1.5 + phase) * math.cos(
                2 * math.pi * (y / height) * 1.1
            )
            b = 128 + 90 * math.sin(2 * math.pi * ((x + y) / (width + height)) * 2.0 + phase)
            pixels[x, y] = (int(max(0, min(255, a))), int(max(0, min(255, b))), int(max(0, min(255, (a + b) / 2))))
    image.filter(ImageFilter.GaussianBlur(1.2)).save(path, "JPEG", quality=95)


@unittest.skipIf(Image is None or QApplication is None, "Pillow and PySide6 are required")
class SimilarReviewTimingTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.application = QApplication.instance() or QApplication([])

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.photos = self.root / "photos"
        self.photos.mkdir()
        self.original = self.photos / "IMG_0001.jpg"
        self.resized = self.photos / "small.jpg"
        _make_photo(self.original)
        with Image.open(self.original) as handle:
            handle.resize((240, 180)).save(self.resized, "JPEG", quality=80)
        self.paths = get_runtime_paths(self.root / "data")
        self.page = SimilarReviewPage(self.paths)
        self.page.path.set_path(str(self.photos))

    def tearDown(self):
        self.page.controller.cancel()
        self.page.controller.wait_for_done(5000)
        self._drain(lambda: not getattr(self.page, "_background_workers", []), 5.0)
        self.page.deleteLater()
        self.application.processEvents()

    def _drain(self, until, timeout=30.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline and not until():
            self.application.processEvents()
            time.sleep(0.005)
        return until()

    def test_two_image_page_scan_completes_within_five_seconds(self):
        """Assert that a two-image page scan completes within 5 s (generous ceiling)."""
        t0 = time.perf_counter()
        started = self.page.start_scan()
        self.assertTrue(started, "page scan must start")
        finished = self._drain(lambda: self.page.review is not None, timeout=5.0)
        elapsed = time.perf_counter() - t0

        self.assertTrue(finished, f"scan must finish within 5 s (took {elapsed:.2f} s)")
        self.assertLess(elapsed, 5.0)
        self.assertIsNotNone(self.page.review)
        self.assertEqual(len(self.page.review.groups), 1)

    def test_scan_uses_isolated_thumbnail_cache_instead_of_global_appdata(self):
        """Service and page scans must use the configured runtime paths cache."""
        service = SimilarPhotoService(self.paths)
        review = service.scan([str(self.photos)])
        self.assertEqual(len(review.groups), 1)
        expected_root = (self.paths.cache / "thumbnails").resolve()
        for item in review.groups[0].items:
            self.assertTrue(item.thumbnail_path, "item must have a thumbnail path")
            item_path = Path(item.thumbnail_path).resolve()
            self.assertTrue(
                item_path.is_relative_to(expected_root),
                f"thumbnail {item_path} must be inside isolated cache {expected_root}",
            )

    def test_immediate_cancellation_during_cloud_check_prevents_scan_queueing(self):
        """Cancelling immediately after start_scan must stop before queueing the scan."""
        started = self.page.start_scan()
        self.assertTrue(started)
        # Controller must be marked busy immediately so cancel() succeeds
        self.assertTrue(self.page.controller.busy)
        cancelled = self.page.controller.cancel()
        self.assertTrue(cancelled, "cancel() must return True when scan is starting")
        # Drain events so cloud check finishes and verifies that stop_event was respected
        self._drain(lambda: not self.page.controller.busy, timeout=5.0)
        self.assertFalse(self.page.controller.busy)
        self.assertIsNone(self.page.review, "scan must not have run or populated review")
