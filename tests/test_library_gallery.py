"""Tests for thumbnail gallery and camera filter on the Library page."""

from __future__ import annotations

import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import MagicMock, patch

from PIL import Image
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from duplicate_transfer_manager.previews.thumbnails import ThumbnailCache
from duplicate_transfer_manager.runtime_paths import get_runtime_paths
from duplicate_transfer_manager.services.catalog_service import CatalogService
from duplicate_transfer_manager.ui.library_page import LibraryPage


def create_application() -> QApplication:
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


def _create_photo(
    path: Path,
    *,
    make: str | None = None,
    model: str | None = None,
    date_str: str = "2024:06:15 10:30:00",
    color: tuple[int, int, int] = (120, 160, 200),
    size: tuple[int, int] = (64, 48),
) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    img = Image.new("RGB", size, color)
    exif = Image.Exif()
    if make:
        exif[271] = make
    if model:
        exif[272] = model
    if date_str:
        exif[306] = date_str
    img.save(path, "JPEG", quality=90, exif=exif.tobytes())
    return path


def _wait_for_event(event: threading.Event, timeout: float = 3.0) -> bool:
    app = QApplication.instance()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if event.is_set():
            return True
        if app is not None:
            app.processEvents()
        time.sleep(0.01)
    return event.is_set()


class LibraryGalleryTests(unittest.TestCase):
    """Unit tests for thumbnail gallery grid and camera filter in LibraryPage."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.app = create_application()

    def test_gallery_shows_one_item_per_image_on_the_page(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            runtime = get_runtime_paths(base / "runtime")
            media_dir = base / "library"

            for i in range(3):
                _create_photo(
                    media_dir / f"vacation_{i:02d}.jpg",
                    make="Nikon",
                    model="D850",
                    date_str=f"2024:06:1{5+i} 10:30:00",
                )

            service = CatalogService(runtime)
            page = LibraryPage(runtime, service=service, view_mode="gallery")
            self.assertTrue(page.wait_for_done(3000))

            started = page.start_index(media_dir)
            self.assertTrue(started)
            self.assertTrue(page.wait_for_done(5000))

            # View mode is gallery
            self.assertEqual(page.view_mode, "gallery")
            self.assertTrue(page.gallery_button.isChecked())
            self.assertFalse(page.table_button.isChecked())
            self.assertFalse(page.gallery_widget.isHidden())
            self.assertTrue(page.results_table.isHidden())

            # Shows one item per image on the current page
            self.assertEqual(page.gallery_widget.count(), 3)
            for idx in range(3):
                item = page.gallery_widget.item(idx)
                self.assertIsNotNone(item)
                assert item is not None
                self.assertIn(f"vacation_{idx:02d}.jpg", item.text())
                self.assertIn(f"2024-06-1{5+idx}", item.text())

            # Table mode toggle still functions
            page.set_view_mode("table")
            self.assertEqual(page.view_mode, "table")
            self.assertFalse(page.results_table.isHidden())
            self.assertTrue(page.gallery_widget.isHidden())
            self.assertEqual(page.results_table.rowCount(), 3)

            page.close()
            service.close()

    def test_thumbnails_produced_asynchronously_and_land_in_cache_dir(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            runtime = get_runtime_paths(base / "runtime")
            media_dir = base / "library"

            for i in range(3):
                _create_photo(
                    media_dir / f"image_{i:02d}.jpg",
                    make="Sony",
                    model="A7IV",
                    date_str="2024:07:01 12:00:00",
                )

            service = CatalogService(runtime)
            page = LibraryPage(runtime, service=service, view_mode="gallery")
            self.assertTrue(page.wait_for_done(3000))

            self.assertTrue(page.start_index(media_dir))
            self.assertTrue(page.wait_for_done(5000))

            # Thumbnails land in the cache dir
            cache_dir = page.thumbnail_cache.cache_dir
            self.assertTrue(cache_dir.is_dir())
            cached_files = list(cache_dir.rglob("*.jpg"))
            self.assertEqual(len(cached_files), 3)
            for thumb in cached_files:
                self.assertTrue(thumb.is_file())
                self.assertGreater(thumb.stat().st_size, 0)

            # Gallery items receive non-null icons
            self.assertEqual(page.gallery_widget.count(), 3)
            for idx in range(3):
                item = page.gallery_widget.item(idx)
                assert item is not None
                self.assertFalse(item.icon().isNull())

            page.close()
            service.close()

    def test_switching_page_cancels_pending_thumbnail_work(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            runtime = get_runtime_paths(base / "runtime")
            media_dir = base / "library"

            for i in range(4):
                _create_photo(
                    media_dir / f"shot_{i:02d}.jpg",
                    make="Fujifilm",
                    model="X-T5",
                    date_str="2024:08:10 09:00:00",
                )

            service = CatalogService(runtime)
            # Index first so files are cataloged
            service.index_folder(media_dir)

            page = LibraryPage(runtime, service=service, page_size=2, view_mode="gallery")
            self.assertTrue(page.wait_for_done(3000))

            # Hook get_or_create to observe stop_event cancellation on the first item
            orig_get_or_create = page.thumbnail_cache.get_or_create
            entered_event = threading.Event()
            cancelled_event = threading.Event()
            should_slow = True

            def _slow_get_or_create(source_path, *args, **kwargs):
                nonlocal should_slow
                if should_slow:
                    should_slow = False
                    entered_event.set()
                    stop_event = kwargs.get("stop_event")
                    deadline = time.monotonic() + 3.0
                    while time.monotonic() < deadline:
                        if stop_event is not None and stop_event.is_set():
                            cancelled_event.set()
                            break
                        time.sleep(0.01)
                return orig_get_or_create(source_path, *args, **kwargs)

            page.thumbnail_cache.get_or_create = _slow_get_or_create  # type: ignore[method-assign]

            # Start search on page 1
            self.assertTrue(page.start_search(page=1))
            # Wait for thumbnail work to enter worker while pumping Qt events
            self.assertTrue(_wait_for_event(entered_event, timeout=3.0))

            # Switch page while thumbnail worker is generating
            self.assertTrue(page.next_page())

            # Cancellation must be triggered promptly on the pending worker
            self.assertTrue(_wait_for_event(cancelled_event, timeout=3.0))
            self.assertTrue(cancelled_event.is_set())

            # Finish remaining work
            self.assertTrue(page.wait_for_done(5000))
            page.close()
            service.close()

    def test_offline_volume_item_shows_offline_and_no_file_read_attempted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            runtime = get_runtime_paths(base / "runtime")
            online_dir = base / "online_vol"
            offline_dir = base / "offline_vol"

            _create_photo(online_dir / "active.jpg", make="Canon", model="R5")
            _create_photo(offline_dir / "archived.jpg", make="Canon", model="R5")

            service = CatalogService(runtime)
            service.index_folder(online_dir, volume_key="vol-on", label="InternalSSD")
            service.index_folder(offline_dir, volume_key="vol-off", label="BackupDrive")
            service.mark_volume_offline("vol-off")

            page = LibraryPage(runtime, service=service, view_mode="gallery")
            self.assertTrue(page.wait_for_done(3000))

            # Patch get_or_create and assert it is never called for offline volume files
            with patch.object(
                page.thumbnail_cache,
                "get_or_create",
                wraps=page.thumbnail_cache.get_or_create,
            ) as mock_thumb:
                self.assertTrue(page.refresh())
                self.assertTrue(page.wait_for_done(5000))

                self.assertEqual(page.gallery_widget.count(), 2)
                offline_items = [
                    page.gallery_widget.item(i)
                    for i in range(page.gallery_widget.count())
                    if "archived.jpg" in page.gallery_widget.item(i).text()  # type: ignore[union-attr]
                ]
                self.assertEqual(len(offline_items), 1)
                offline_item = offline_items[0]
                assert offline_item is not None
                # Assert offline item shows text "Offline"
                self.assertIn("Offline", offline_item.text())

                # Assert ThumbnailCache.get_or_create was never called for archived.jpg
                called_paths = [
                    str(call.args[0]) for call in mock_thumb.call_args_list if call.args
                ]
                for path_str in called_paths:
                    self.assertNotIn("archived.jpg", path_str)

            page.close()
            service.close()

    def test_camera_filter_narrows_results(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            runtime = get_runtime_paths(base / "runtime")
            media_dir = base / "library"

            _create_photo(media_dir / "canon_photo.jpg", make="Canon", model="EOS 5D")
            _create_photo(media_dir / "nikon_photo.jpg", make="Nikon", model="D850")

            service = CatalogService(runtime)
            page = LibraryPage(runtime, service=service, view_mode="gallery")
            self.assertTrue(page.wait_for_done(3000))

            self.assertTrue(page.start_index(media_dir))
            self.assertTrue(page.wait_for_done(5000))

            # Camera filter is populated with distinct makes/models
            combo_labels = [page.camera_filter.itemText(i) for i in range(page.camera_filter.count())]
            self.assertIn("All cameras", combo_labels)
            canon_matches = [i for i, lbl in enumerate(combo_labels) if "Canon" in lbl]
            nikon_matches = [i for i, lbl in enumerate(combo_labels) if "Nikon" in lbl]
            self.assertTrue(canon_matches)
            self.assertTrue(nikon_matches)

            # Filtering by Canon narrows results to 1 item
            page.camera_filter.setCurrentIndex(canon_matches[0])
            self.assertTrue(page.wait_for_done(3000))
            self.assertEqual(page.search_page_data.total_count, 1)
            self.assertEqual(page.search_page_data.items[0].filename, "canon_photo.jpg")
            self.assertEqual(page.gallery_widget.count(), 1)
            item = page.gallery_widget.item(0)
            assert item is not None
            self.assertIn("canon_photo.jpg", item.text())

            # Filtering by Nikon narrows results to 1 item
            page.camera_filter.setCurrentIndex(nikon_matches[0])
            self.assertTrue(page.wait_for_done(3000))
            self.assertEqual(page.search_page_data.total_count, 1)
            self.assertEqual(page.search_page_data.items[0].filename, "nikon_photo.jpg")
            self.assertEqual(page.gallery_widget.count(), 1)

            # Resetting to All cameras shows both
            page.camera_filter.setCurrentIndex(0)
            self.assertTrue(page.wait_for_done(3000))
            self.assertEqual(page.search_page_data.total_count, 2)
            self.assertEqual(page.gallery_widget.count(), 2)

            page.close()
            service.close()

    def test_double_click_opener_receives_right_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            runtime = get_runtime_paths(base / "runtime")
            media_dir = base / "library"

            target_file = _create_photo(media_dir / "target_photo.jpg", make="Apple", model="iPhone 13")

            service = CatalogService(runtime)
            mock_opener = MagicMock()
            page = LibraryPage(
                runtime,
                service=service,
                opener=mock_opener,
                view_mode="gallery",
            )
            self.assertTrue(page.wait_for_done(3000))

            self.assertTrue(page.start_index(media_dir))
            self.assertTrue(page.wait_for_done(5000))

            self.assertEqual(page.gallery_widget.count(), 1)
            gallery_item = page.gallery_widget.item(0)
            assert gallery_item is not None

            # Simulate double-click
            page.gallery_widget.itemDoubleClicked.emit(gallery_item)

            mock_opener.assert_called_once()
            called_path = Path(mock_opener.call_args[0][0])
            self.assertEqual(called_path.resolve(), target_file.resolve())

            page.close()
            service.close()

    def test_accessible_names_and_tab_order(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            runtime = get_runtime_paths(base / "runtime")
            service = CatalogService(runtime)
            page = LibraryPage(runtime, service=service)
            self.assertTrue(page.wait_for_done(2000))

            # Accessible names are set on new controls
            self.assertTrue(page.table_button.accessibleName())
            self.assertTrue(page.gallery_button.accessibleName())
            self.assertTrue(page.camera_filter.accessibleName())
            self.assertTrue(page.gallery_widget.accessibleName())

            # Tab order verification
            tab_chain = [
                page.media_kind_filter,
                page.filename_filter,
                page.min_size_filter,
                page.max_size_filter,
                page.date_start_filter,
                page.date_end_filter,
                page.camera_filter,
                page.page_size_combo,
                page.table_button,
                page.gallery_button,
                page.clear_filters_button,
                page.search_button,
                page.results_table,
                page.gallery_widget,
                page.prev_page_button,
                page.next_page_button,
            ]
            for first, second in zip(tab_chain, tab_chain[1:]):
                self.assertIsNotNone(first)
                self.assertIsNotNone(second)

            page.close()
            service.close()


if __name__ == "__main__":
    unittest.main()
