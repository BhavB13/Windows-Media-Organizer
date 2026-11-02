"""Unit tests for LibraryPage similarity queries, perceptual indexing, and catalog integration."""

from __future__ import annotations

import math
import os
from pathlib import Path
import tempfile
import unittest

from PIL import Image, ImageDraw
from PySide6.QtWidgets import QApplication

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from duplicate_transfer_manager.runtime_paths import get_runtime_paths
from duplicate_transfer_manager.services.catalog_service import (
    CatalogIndexSettings,
    CatalogSearchItem,
    CatalogService,
)
from duplicate_transfer_manager.services.similar_service import SENSITIVITY
from duplicate_transfer_manager.ui.library_page import LibraryPage


def create_application() -> QApplication:
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


def _create_smooth_image(size: tuple[int, int] = (256, 256), phase: float = 0.0) -> Image.Image:
    """Generate a smooth, photograph-like test image with low spatial frequencies."""
    width, height = size
    img = Image.new("RGB", (width, height))
    pixels = img.load()
    assert pixels is not None
    for y in range(height):
        for x in range(width):
            u = x / max(1, width - 1)
            v = y / max(1, height - 1)
            r = int(40 + 180 * u * (1.0 - 0.3 * v))
            g = int(30 + 190 * v + 25 * math.cos(2.0 * math.pi * u + phase))
            b = int(50 + 150 * (1.0 - u) * v)
            pixels[x, y] = (
                max(0, min(255, r)),
                max(0, min(255, g)),
                max(0, min(255, b)),
            )
    draw = ImageDraw.Draw(img)
    draw.ellipse(
        (int(width * 0.12), int(height * 0.15), int(width * 0.42), int(height * 0.45)),
        fill=(245, 240, 220),
    )
    draw.rectangle(
        (int(width * 0.58), int(height * 0.55), int(width * 0.88), int(height * 0.85)),
        fill=(15, 25, 35),
    )
    return img


def _create_unrelated_image(size: tuple[int, int] = (256, 256)) -> Image.Image:
    """Generate an unrelated image with distinct spatial frequency structure."""
    width, height = size
    img = Image.new("RGB", (width, height), (240, 240, 240))
    draw = ImageDraw.Draw(img)
    draw.rectangle((0, 0, width // 2, height), fill=(235, 235, 235))
    draw.rectangle((width // 2, 0, width, height), fill=(20, 20, 20))
    draw.rectangle((0, height // 2, width, height), fill=(250, 250, 250))
    draw.ellipse(
        (int(width * 0.60), int(height * 0.08), int(width * 0.92), int(height * 0.38)),
        fill=(10, 10, 10),
    )
    draw.rectangle(
        (int(width * 0.05), int(height * 0.60), int(width * 0.35), int(height * 0.92)),
        fill=(30, 30, 30),
    )
    return img


def _create_variant_image(size: tuple[int, int] = (256, 256), rect_size: int = 25) -> Image.Image:
    """Generate a controlled variation of the smooth test image."""
    img = _create_smooth_image(size)
    draw = ImageDraw.Draw(img)
    draw.rectangle((20, 20, 20 + rect_size, 20 + rect_size), fill=(200, 50, 100))
    return img


class TestLibrarySimilar(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = create_application()

    def test_indexing_option_reaches_compute_perceptual(self) -> None:
        """The 'Compare photos for similarity' option passes through CatalogService to compute_perceptual=True."""
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            runtime = get_runtime_paths(base / "runtime")
            media_dir = base / "photos"
            media_dir.mkdir(parents=True)

            img = _create_smooth_image()
            img.save(media_dir / "sample.jpg", "JPEG", quality=90)

            # 1. By default, similarity indexing is OFF
            service = CatalogService(runtime)
            res_default = service.index_folder(media_dir)
            self.assertFalse(res_default.compute_perceptual)

            items_default = service.search()
            self.assertEqual(len(items_default.items), 1)
            self.assertIsNone(items_default.items[0].perceptual_hash)

            # 2. When compute_perceptual=True, perceptual hash is stored
            media_dir2 = base / "photos_perceptual"
            media_dir2.mkdir(parents=True)
            img.save(media_dir2 / "sample_p.jpg", "JPEG", quality=90)

            res_perceptual = service.index_folder(media_dir2, compute_perceptual=True)
            self.assertTrue(res_perceptual.compute_perceptual)

            items_p = service.search(filename="sample_p.jpg")
            self.assertEqual(len(items_p.items), 1)
            self.assertIsNotNone(items_p.items[0].perceptual_hash)
            self.assertEqual(len(items_p.items[0].perceptual_hash), 16)

            # 3. Via CatalogIndexSettings
            settings = CatalogIndexSettings(folder_path=str(media_dir2), compute_perceptual=True)
            self.assertTrue(settings.compute_perceptual)

            service.close()

    def test_find_similar_returns_resized_and_excludes_unrelated(self) -> None:
        """find_similar returns resized copy nearest first, and excludes unrelated image."""
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            runtime = get_runtime_paths(base / "runtime")
            media_dir = base / "library"
            media_dir.mkdir(parents=True)

            orig = _create_smooth_image((256, 256))
            resized = orig.resize((128, 128))
            unrelated = _create_unrelated_image((256, 256))

            orig_path = media_dir / "orig.jpg"
            resized_path = media_dir / "resized.jpg"
            unrelated_path = media_dir / "unrelated.jpg"

            orig.save(orig_path, "JPEG", quality=95)
            resized.save(resized_path, "JPEG", quality=90)
            unrelated.save(unrelated_path, "JPEG", quality=90)

            service = CatalogService(runtime)
            idx_res = service.index_folder(media_dir, compute_perceptual=True)
            self.assertEqual(idx_res.added, 3)

            orig_items = service.search(filename="orig.jpg")
            self.assertEqual(len(orig_items.items), 1)
            orig_rec = orig_items.items[0]

            # Query similar
            matches = service.find_similar(orig_rec, sensitivity="balanced")
            match_names = [m.filename for m in matches]

            self.assertIn("resized.jpg", match_names)
            self.assertNotIn("unrelated.jpg", match_names)
            self.assertNotIn("orig.jpg", match_names)  # target itself excluded

            # Check properties on returned plain item
            resized_match = next(m for m in matches if m.filename == "resized.jpg")
            self.assertIsInstance(resized_match, CatalogSearchItem)
            self.assertIsNotNone(resized_match.distance)
            self.assertLessEqual(resized_match.distance, 4)
            self.assertTrue(resized_match.volume_online)
            self.assertFalse(resized_match.is_offline)

            service.close()

    def test_sensitivity_mapping(self) -> None:
        """Sensitivity levels strict (6), balanced (10), loose (14) map correctly and filter thresholds."""
        self.assertEqual(SENSITIVITY["strict"], 6)
        self.assertEqual(SENSITIVITY["balanced"], 10)
        self.assertEqual(SENSITIVITY["loose"], 14)

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            runtime = get_runtime_paths(base / "runtime")
            media_dir = base / "library"
            media_dir.mkdir(parents=True)

            orig = _create_smooth_image((256, 256))
            res = orig.resize((128, 128))          # dist ~2
            v8 = _create_variant_image((256, 256), rect_size=25)   # dist ~8
            v12 = _create_variant_image((256, 256), rect_size=45)  # dist ~12
            unrel = _create_unrelated_image((256, 256))            # dist ~30

            orig.save(media_dir / "orig.jpg", "JPEG", quality=95)
            res.save(media_dir / "res.jpg", "JPEG", quality=90)
            v8.save(media_dir / "v8.jpg", "JPEG", quality=90)
            v12.save(media_dir / "v12.jpg", "JPEG", quality=90)
            unrel.save(media_dir / "unrel.jpg", "JPEG", quality=90)

            service = CatalogService(runtime)
            service.index_folder(media_dir, compute_perceptual=True)

            orig_rec = service.search(filename="orig.jpg").items[0]

            # Strict (threshold 6): includes res (dist ~2), excludes v8 (8), v12 (12), unrel (30)
            matches_strict = service.find_similar(orig_rec, sensitivity="strict")
            strict_names = {m.filename for m in matches_strict}
            self.assertIn("res.jpg", strict_names)
            self.assertNotIn("v8.jpg", strict_names)
            self.assertNotIn("v12.jpg", strict_names)
            self.assertNotIn("unrel.jpg", strict_names)

            # Balanced (threshold 10): includes res and v8, excludes v12 and unrel
            matches_balanced = service.find_similar(orig_rec, sensitivity="balanced")
            balanced_names = {m.filename for m in matches_balanced}
            self.assertIn("res.jpg", balanced_names)
            self.assertIn("v8.jpg", balanced_names)
            self.assertNotIn("v12.jpg", balanced_names)
            self.assertNotIn("unrel.jpg", balanced_names)

            # Loose (threshold 14): includes res, v8, v12, excludes unrel
            matches_loose = service.find_similar(orig_rec, sensitivity="loose")
            loose_names = {m.filename for m in matches_loose}
            self.assertIn("res.jpg", loose_names)
            self.assertIn("v8.jpg", loose_names)
            self.assertIn("v12.jpg", loose_names)
            self.assertNotIn("unrel.jpg", loose_names)

            # Invalid sensitivity raises ValueError
            with self.assertRaises(ValueError):
                service.find_similar(orig_rec, sensitivity="hyper-strict")

            service.close()

    def test_photo_without_hash_gives_reindex_message(self) -> None:
        """A photo indexed without similarity hash gives the 're-index' message and is not decoded on main thread."""
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            runtime = get_runtime_paths(base / "runtime")
            media_dir = base / "library"
            media_dir.mkdir(parents=True)

            orig = _create_smooth_image((256, 256))
            orig.save(media_dir / "unhashed.jpg", "JPEG", quality=90)

            service = CatalogService(runtime)
            # Index WITHOUT similarity
            service.index_folder(media_dir, compute_perceptual=False)

            unhashed_item = service.search(filename="unhashed.jpg").items[0]
            self.assertIsNone(unhashed_item.perceptual_hash)

            # 1. Calling CatalogService.find_similar directly raises ValueError suggesting re-indexing
            with self.assertRaises(ValueError) as ctx:
                service.find_similar(unhashed_item)
            self.assertIn("re-index", str(ctx.exception).lower())

            # 2. On LibraryPage, selecting the unhashed photo and triggering find_similar shows warning
            page = LibraryPage(runtime, service=service, view_mode="table")
            self.assertTrue(page.wait_for_done(3000))

            # Select row 0 (unhashed.jpg)
            page.results_table.selectRow(0)
            started = page.find_similar()
            # find_similar returns False immediately without starting background decoding
            self.assertFalse(started)
            self.assertFalse(page.banner.isHidden())
            self.assertIn("re-index", page.banner.label.text().lower())

            page.close()
            service.close()

    def test_offline_matches_marked_offline_and_never_opened(self) -> None:
        """Matches on offline volumes are marked offline and never opened."""
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            runtime = get_runtime_paths(base / "runtime")
            online_dir = base / "online_photos"
            online_dir.mkdir(parents=True)

            orig = _create_smooth_image((256, 256))
            res = orig.resize((128, 128))
            orig.save(online_dir / "orig.jpg", "JPEG", quality=95)

            # Create an offline volume in catalog with a similar image
            service = CatalogService(runtime)
            service.index_folder(online_dir, volume_key="vol_online", compute_perceptual=True)

            offline_dir = base / "offline_photos"
            offline_dir.mkdir(parents=True)
            res.save(offline_dir / "res_offline.jpg", "JPEG", quality=90)
            service.index_folder(offline_dir, volume_key="vol_offline", compute_perceptual=True)

            # Mark vol_offline as offline
            cat, should_close = service._open_catalog()
            try:
                cat.mark_volume_offline("vol_offline")
            finally:
                if should_close:
                    cat.close()

            orig_rec = service.search(filename="orig.jpg").items[0]
            matches = service.find_similar(orig_rec, sensitivity="balanced")
            self.assertEqual(len(matches), 1)

            offline_match = matches[0]
            self.assertEqual(offline_match.filename, "res_offline.jpg")
            self.assertTrue(offline_match.is_offline)
            self.assertFalse(offline_match.volume_online)
            self.assertEqual(offline_match.volume_status, "offline")

            # Verify on LibraryPage
            opened_calls: list[Path] = []
            page = LibraryPage(
                runtime,
                service=service,
                opener=lambda p: opened_calls.append(Path(p)),
                view_mode="table",
            )
            self.assertTrue(page.wait_for_done(3000))

            # Select orig.jpg and run find_similar
            orig_row = next(
                r for r in range(page.results_table.rowCount())
                if page.results_table.item(r, 0).text() == "orig.jpg"
            )
            page.results_table.selectRow(orig_row)
            self.assertTrue(page.find_similar())
            self.assertTrue(page.wait_for_done(5000))

            # Results table shows offline match
            self.assertEqual(page.results_table.rowCount(), 1)
            status_cell = page.results_table.item(0, 5).text()
            self.assertIn("Offline", status_cell)

            # Gallery view also displays offline
            page.set_view_mode("gallery")
            self.assertEqual(page.gallery_widget.count(), 1)
            item_text = page.gallery_widget.item(0).text()
            self.assertIn("Offline", item_text)

            # Attempting to open the offline item is refused and returns False
            opened = page.open_item(offline_match)
            self.assertFalse(opened)
            self.assertEqual(len(opened_calls), 0)

            page.close()
            service.close()

    def test_page_action_shows_heading_results_and_back_restores_previous(self) -> None:
        """Find similar on LibraryPage updates heading and results; back button restores previous search."""
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            runtime = get_runtime_paths(base / "runtime")
            media_dir = base / "library"
            media_dir.mkdir(parents=True)

            orig = _create_smooth_image((256, 256))
            res = orig.resize((128, 128))
            unrel = _create_unrelated_image((256, 256))

            orig.save(media_dir / "alpha_orig.jpg", "JPEG", quality=95)
            res.save(media_dir / "beta_res.jpg", "JPEG", quality=90)
            unrel.save(media_dir / "gamma_unrel.jpg", "JPEG", quality=90)

            service = CatalogService(runtime)
            page = LibraryPage(runtime, service=service, view_mode="table")
            self.assertTrue(page.wait_for_done(3000))

            # Index with similarity enabled
            page.compute_perceptual_check.setChecked(True)
            self.assertTrue(page.start_index(media_dir))
            self.assertTrue(page.wait_for_done(5000))

            # Initial search state has 3 rows
            self.assertEqual(page.results_table.rowCount(), 3)
            self.assertEqual(page.heading_label.text(), "Search Catalog")
            self.assertTrue(page.back_button.isHidden())

            # Select alpha_orig.jpg (row 0)
            page.results_table.selectRow(0)
            selected = page.get_selected_item()
            self.assertIsNotNone(selected)
            assert selected is not None
            self.assertEqual(selected.filename, "alpha_orig.jpg")

            # Click Find similar
            self.assertTrue(page.find_similar())
            self.assertTrue(page.wait_for_done(5000))

            # Heading updated to "Photos similar to alpha_orig.jpg"
            self.assertEqual(page.heading_label.text(), "Photos similar to alpha_orig.jpg")
            self.assertFalse(page.back_button.isHidden())

            # Results table now shows only the similar photo (beta_res.jpg)
            self.assertEqual(page.results_table.rowCount(), 1)
            self.assertEqual(page.results_table.item(0, 0).text(), "beta_res.jpg")

            # Click Back to search
            page.back_button.click()

            # Heading and results restored to previous 3 search rows
            self.assertEqual(page.heading_label.text(), "Search Catalog")
            self.assertTrue(page.back_button.isHidden())
            self.assertEqual(page.results_table.rowCount(), 3)

            page.close()
            service.close()

    def test_nothing_in_folder_changes_bytes_and_mtime(self) -> None:
        """User media is sacred: bytes, size, and mtime remain completely unchanged throughout."""
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            runtime = get_runtime_paths(base / "runtime")
            media_dir = base / "sacred_media"
            media_dir.mkdir(parents=True)

            orig = _create_smooth_image((256, 256))
            res = orig.resize((128, 128))
            unrel = _create_unrelated_image((256, 256))

            orig_path = media_dir / "photo_orig.jpg"
            res_path = media_dir / "photo_res.jpg"
            unrel_path = media_dir / "photo_unrel.jpg"

            orig.save(orig_path, "JPEG", quality=95)
            res.save(res_path, "JPEG", quality=90)
            unrel.save(unrel_path, "JPEG", quality=90)

            # Record pristine bytes and timestamps
            initial_state: dict[Path, tuple[bytes, int, int]] = {}
            for p in [orig_path, res_path, unrel_path]:
                stat = p.stat()
                initial_state[p] = (p.read_bytes(), stat.st_size, stat.st_mtime_ns)

            # Perform indexing with similarity enabled
            service = CatalogService(runtime)
            page = LibraryPage(runtime, service=service, view_mode="gallery")
            self.assertTrue(page.wait_for_done(3000))

            page.compute_perceptual_check.setChecked(True)
            self.assertTrue(page.start_index(media_dir))
            self.assertTrue(page.wait_for_done(5000))

            # Perform search and find_similar
            orig_item = service.search(filename="photo_orig.jpg").items[0]
            matches = service.find_similar(orig_item, sensitivity="balanced")
            self.assertEqual(len(matches), 1)

            page.find_similar(orig_item)
            self.assertTrue(page.wait_for_done(5000))
            page.back_to_search()

            page.close()
            service.close()

            # Verify that not a single byte, size, or mtime changed
            for p, (expected_bytes, expected_size, expected_mtime) in initial_state.items():
                self.assertEqual(p.read_bytes(), expected_bytes, f"Bytes changed for {p.name}")
                curr_stat = p.stat()
                self.assertEqual(curr_stat.st_size, expected_size, f"Size changed for {p.name}")
                self.assertEqual(curr_stat.st_mtime_ns, expected_mtime, f"mtime changed for {p.name}")


if __name__ == "__main__":
    unittest.main()
