"""Unit tests for CatalogService (catalog_service.py) and LibraryPage (library_page.py)."""

from __future__ import annotations

import ast
from contextlib import closing
import os
from pathlib import Path
import sqlite3
import struct
import sys
import tempfile
import threading
import time
import unittest
import zlib

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from PySide6.QtWidgets import QApplication

from duplicate_transfer_manager.runtime_paths import get_runtime_paths
from duplicate_transfer_manager.services.catalog_service import (
    CATALOG_FILENAME,
    CatalogBucket,
    CatalogIndexResult,
    CatalogSearchItem,
    CatalogSearchPage,
    CatalogService,
    CatalogStorageAnalytics,
    CatalogVolumeSummary,
    default_catalog_path,
)
from duplicate_transfer_manager.sorting.metadata import MetadataExtractor
from duplicate_transfer_manager.ui.app import create_application
from duplicate_transfer_manager.ui.library_page import LibraryPage


def _build_minimal_png(width: int = 32, height: int = 24) -> bytes:
    """Construct a valid minimal PNG image of ``width`` x ``height``."""
    signature = b"\x89PNG\r\n\x1a\n"
    ihdr_data = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    ihdr_crc = zlib.crc32(b"IHDR" + ihdr_data) & 0xFFFFFFFF
    ihdr_chunk = struct.pack(">I", len(ihdr_data)) + b"IHDR" + ihdr_data + struct.pack(">I", ihdr_crc)
    raw_scanlines = b"\x00" + (b"\x00\x00\x00" * max(1, min(width, 4)))
    compressed = zlib.compress(raw_scanlines)
    idat_crc = zlib.crc32(b"IDAT" + compressed) & 0xFFFFFFFF
    idat_chunk = struct.pack(">I", len(compressed)) + b"IDAT" + compressed + struct.pack(">I", idat_crc)
    iend_crc = zlib.crc32(b"IEND") & 0xFFFFFFFF
    iend_chunk = struct.pack(">I", 0) + b"IEND" + struct.pack(">I", iend_crc)
    return signature + ihdr_chunk + idat_chunk + iend_chunk


def _build_exif_jpeg(
    *,
    make: str = "Canon",
    model: str = "EOS R5",
    date_time_original: str = "2023:07:15 14:30:00",
    width: int = 1920,
    height: int = 1080,
) -> bytes:
    """Build a synthetic JPEG containing APP1 EXIF IFD0 + ExifIFD + SOF0 dimensions."""
    endian = "<"
    make_bytes = make.encode("ascii") + b"\x00"
    model_bytes = model.encode("ascii") + b"\x00"
    dt_bytes = date_time_original.encode("ascii") + b"\x00"

    ifd0_count = 3
    ifd0_size = 2 + (ifd0_count * 12) + 4
    ifd0_offset = 8

    make_offset = ifd0_offset + ifd0_size
    model_offset = make_offset + len(make_bytes)
    exif_ifd_offset = model_offset + len(model_bytes)

    exif_count = 3
    exif_ifd_size = 2 + (exif_count * 12) + 4
    dt_offset = exif_ifd_offset + exif_ifd_size

    ifd0 = bytearray()
    ifd0 += struct.pack(endian + "H", ifd0_count)
    ifd0 += struct.pack(endian + "HHII", 0x010F, 2, len(make_bytes), make_offset)
    ifd0 += struct.pack(endian + "HHII", 0x0110, 2, len(model_bytes), model_offset)
    ifd0 += struct.pack(endian + "HHII", 0x8769, 4, 1, exif_ifd_offset)
    ifd0 += struct.pack(endian + "I", 0)

    exif_ifd = bytearray()
    exif_ifd += struct.pack(endian + "H", exif_count)
    exif_ifd += struct.pack(endian + "HHII", 0x9003, 2, len(dt_bytes), dt_offset)
    exif_ifd += struct.pack(endian + "HHII", 0xA002, 4, 1, width)
    exif_ifd += struct.pack(endian + "HHII", 0xA003, 4, 1, height)
    exif_ifd += struct.pack(endian + "I", 0)

    tiff = (
        b"II"
        + struct.pack(endian + "HI", 42, ifd0_offset)
        + bytes(ifd0)
        + make_bytes
        + model_bytes
        + bytes(exif_ifd)
        + dt_bytes
    )
    app1_payload = b"Exif\x00\x00" + tiff
    app1_segment = b"\xFF\xE1" + struct.pack(">H", len(app1_payload) + 2) + app1_payload

    sof0_payload = struct.pack(">BHHB", 8, height, width, 1) + b"\x01\x11\x00"
    sof0_segment = b"\xFF\xC0" + struct.pack(">H", len(sof0_payload) + 2) + sof0_payload

    return b"\xFF\xD8" + app1_segment + sof0_segment + b"\xFF\xD9"


def _write_test_file(path: Path, data: bytes, *, mtime_ns: int | None = None) -> tuple[bytes, int]:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    if mtime_ns is not None:
        os.utime(path, ns=(mtime_ns, mtime_ns))
    st = path.stat()
    return data, int(st.st_mtime_ns)


class _ProgressStopEvent:
    """Stop event that sets itself once ``trigger_after`` checks or callbacks occur."""

    def __init__(self, trigger_after: int = 2) -> None:
        self.trigger_after = trigger_after
        self.calls = 0
        self._flag = False

    def notify(self) -> None:
        self.calls += 1
        if self.calls >= self.trigger_after:
            self._flag = True

    def is_set(self) -> bool:
        return self._flag


class CatalogServiceTests(unittest.TestCase):
    """Tests for the framework-neutral CatalogService."""

    def test_service_module_is_framework_neutral_and_uses_runtime_paths(self) -> None:
        service_file = (
            ROOT / "src" / "duplicate_transfer_manager" / "services" / "catalog_service.py"
        )
        tree = ast.parse(service_file.read_text(encoding="utf-8"))
        imported_modules: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported_modules.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported_modules.append(node.module)

        for mod in imported_modules:
            self.assertNotIn("PySide6", mod)
            self.assertNotIn("PyQt", mod)

        with tempfile.TemporaryDirectory() as tmp:
            runtime = get_runtime_paths(Path(tmp) / "appdata")
            expected_path = runtime.cache / CATALOG_FILENAME
            self.assertEqual(default_catalog_path(runtime), expected_path)
            service = CatalogService(runtime)
            self.assertEqual(service.db_path, expected_path)
            self.assertIsInstance(service.metadata_extractor, MetadataExtractor)
            service.close()

    def test_first_index_adds_rows_and_reindex_does_zero_writes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            runtime = get_runtime_paths(base / "runtime")
            media_dir = base / "library"

            _write_test_file(
                media_dir / "photos" / "trip.jpg",
                _build_exif_jpeg(date_time_original="2023:07:15 14:30:00", width=1920, height=1080),
                mtime_ns=1_700_000_000_000_000_000,
            )
            _write_test_file(
                media_dir / "photos" / "icon.png",
                _build_minimal_png(64, 48),
                mtime_ns=1_700_000_100_000_000_000,
            )
            _write_test_file(
                media_dir / "clips" / "movie.mp4",
                b"\x00\x00\x00\x18ftypmp42" + (b"v" * 200),
                mtime_ns=1_700_000_200_000_000_000,
            )

            # Spy on MetadataExtractor.extract to verify metadata extraction goes through sorting/metadata.py
            extract_calls: list[Path] = []
            real_extractor = MetadataExtractor()
            orig_extract = real_extractor.extract

            def _tracked_extract(path: Path):
                extract_calls.append(Path(path))
                return orig_extract(path)

            real_extractor.extract = _tracked_extract  # type: ignore[method-assign]

            service = CatalogService(runtime, metadata_extractor=real_extractor)
            first = service.index_folder(media_dir, volume_key="vol-main", label="Main Library")

            self.assertEqual(first.added, 3)
            self.assertEqual(first.updated, 0)
            self.assertEqual(first.unchanged, 0)
            self.assertEqual(first.removed, 0)
            self.assertEqual(first.volume_status, "online")
            self.assertFalse(first.cancelled)
            self.assertGreater(first.writes_performed, 0)
            self.assertEqual(len(extract_calls), 3)

            # Check SQLite data_version before and after second index of unchanged folder
            with closing(sqlite3.connect(service.db_path)) as conn:
                data_version_before = conn.execute("PRAGMA data_version").fetchone()[0]

            extract_calls.clear()
            second = service.index_folder(media_dir, volume_key="vol-main", label="Main Library")

            with closing(sqlite3.connect(service.db_path)) as conn:
                data_version_after = conn.execute("PRAGMA data_version").fetchone()[0]

            self.assertEqual(second.added, 0)
            self.assertEqual(second.updated, 0)
            self.assertEqual(second.unchanged, 3)
            self.assertEqual(second.removed, 0)
            self.assertEqual(second.writes_performed, 0)
            self.assertEqual(data_version_before, data_version_after)
            self.assertEqual(len(extract_calls), 0)
            service.close()

    def test_changed_file_is_updated_incrementally(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            runtime = get_runtime_paths(base / "runtime")
            media_dir = base / "library"

            file_a = media_dir / "a.jpg"
            file_b = media_dir / "b.png"
            _write_test_file(file_a, _build_exif_jpeg(width=800, height=600), mtime_ns=1_700_000_000_000_000_000)
            _write_test_file(file_b, _build_minimal_png(32, 32), mtime_ns=1_700_000_000_000_000_000)

            service = CatalogService(runtime)
            res1 = service.index_folder(media_dir, volume_key="vol-inc")
            self.assertEqual((res1.added, res1.updated, res1.unchanged), (2, 0, 0))

            # Modify only file_a (new size and mtime_ns)
            _write_test_file(
                file_a,
                _build_exif_jpeg(width=1280, height=720) + b"\x00" * 64,
                mtime_ns=1_700_000_999_000_000_000,
            )

            res2 = service.index_folder(media_dir, volume_key="vol-inc")
            self.assertEqual(res2.added, 0)
            self.assertEqual(res2.updated, 1)
            self.assertEqual(res2.unchanged, 1)
            self.assertEqual(res2.removed, 0)

            found = service.search(filename="a.jpg")
            self.assertEqual(len(found), 1)
            self.assertEqual(found[0].width, 1280)
            self.assertEqual(found[0].height, 720)
            service.close()

    def test_hashing_off_by_default_reports_unavailable_and_hashing_on_computes_recoverable_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            runtime = get_runtime_paths(base / "runtime")
            media_dir = base / "library"

            dup_payload = _build_minimal_png(40, 40) + (b"DUPLICATE_PAYLOAD" * 20)
            unique_payload = _build_minimal_png(20, 20) + b"UNIQUE_PAYLOAD"

            _write_test_file(media_dir / "dup1.png", dup_payload, mtime_ns=1_700_100_000_000_000_000)
            _write_test_file(media_dir / "dup2.png", dup_payload, mtime_ns=1_700_100_000_000_000_000)
            _write_test_file(media_dir / "sub" / "dup3.png", dup_payload, mtime_ns=1_700_100_000_000_000_000)
            _write_test_file(media_dir / "unique.png", unique_payload, mtime_ns=1_700_100_000_000_000_000)

            service = CatalogService(runtime)
            # Default: compute_hashes is False
            res_no_hash = service.index_folder(media_dir, volume_key="vol-dup")
            self.assertFalse(res_no_hash.compute_hashes)

            page = service.search()
            self.assertEqual(page.total_count, 4)
            for item in page:
                self.assertIsNone(item.content_hash)

            # Duplicate analytics must report unavailable (None), NOT 0
            self.assertIsNone(service.recoverable_duplicate_bytes())
            analytics_unhashed = service.get_storage_analytics()
            self.assertIsNone(analytics_unhashed.recoverable_duplicate_bytes)
            self.assertFalse(analytics_unhashed.duplicate_analytics_available)
            self.assertEqual(analytics_unhashed.recoverable_duplicate_status, "unavailable")
            self.assertEqual(analytics_unhashed.hashed_files, 0)
            self.assertEqual(analytics_unhashed.unhashed_files, 4)

            # Now index with compute_hashes=True; hashes are populated and recoverable bytes are exact
            res_hashed = service.index_folder(
                media_dir,
                volume_key="vol-dup",
                compute_hashes=True,
            )
            self.assertTrue(res_hashed.compute_hashes)
            self.assertEqual(res_hashed.updated, 4)

            expected_recoverable = 2 * len(dup_payload)
            self.assertEqual(service.recoverable_duplicate_bytes(), expected_recoverable)

            analytics_hashed = service.get_storage_analytics()
            self.assertTrue(analytics_hashed.duplicate_analytics_available)
            self.assertEqual(analytics_hashed.recoverable_duplicate_status, "available")
            self.assertEqual(analytics_hashed.recoverable_duplicate_bytes, expected_recoverable)
            self.assertEqual(analytics_hashed.hashed_files, 4)
            self.assertEqual(analytics_hashed.unhashed_files, 0)
            service.close()

    def test_offline_volume_rows_survive_and_are_reported_offline(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            runtime = get_runtime_paths(base / "runtime")
            online_dir = base / "internal_drive"
            external_dir = base / "removable_drive"

            _write_test_file(online_dir / "local.jpg", _build_exif_jpeg(), mtime_ns=1_700_200_000_000_000_000)
            _write_test_file(external_dir / "ext1.jpg", _build_exif_jpeg(), mtime_ns=1_700_200_000_000_000_000)
            _write_test_file(external_dir / "ext2.png", _build_minimal_png(), mtime_ns=1_700_200_000_000_000_000)

            service = CatalogService(runtime)
            service.index_folder(online_dir, volume_key="vol-internal", label="Internal SSD")
            service.index_folder(external_dir, volume_key="vol-external", label="Backup Drive")

            # Simulate unmounting / disconnecting external_dir by removing its directory
            for child in external_dir.iterdir():
                child.unlink()
            external_dir.rmdir()

            # Re-indexing the absent volume must NOT delete its rows and must report it offline
            offline_res = service.index_folder(
                external_dir,
                volume_key="vol-external",
                label="Backup Drive",
            )
            self.assertEqual(offline_res.volume_status, "offline")
            self.assertTrue(offline_res.offline)
            self.assertEqual(offline_res.removed, 0)
            self.assertEqual(offline_res.added, 0)

            # Search still returns all 3 files, with the 2 external files marked volume_online=False
            all_items = service.search()
            self.assertEqual(all_items.total_count, 3)
            ext_items = service.search(volume_key="vol-external")
            self.assertEqual(ext_items.total_count, 2)
            for item in ext_items:
                self.assertFalse(item.volume_online)
                self.assertEqual(item.volume_status, "offline")
                self.assertTrue(item.is_offline)

            analytics = service.get_storage_analytics()
            self.assertEqual(analytics.total_files, 3)
            self.assertTrue(analytics.has_offline_volumes)
            self.assertEqual(len(analytics.offline_volumes), 1)
            self.assertEqual(analytics.offline_volumes[0].volume_key, "vol-external")
            self.assertTrue(analytics.offline_volumes[0].offline)
            self.assertEqual(analytics.offline_volumes[0].file_count, 2)
            service.close()

    def test_cancellation_returns_early_and_leaves_reopenable_database(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            runtime = get_runtime_paths(base / "runtime")
            media_dir = base / "library"

            for idx in range(6):
                _write_test_file(
                    media_dir / f"img_{idx}.png",
                    _build_minimal_png(16 + idx, 16 + idx),
                    mtime_ns=1_700_300_000_000_000_000 + idx,
                )

            service = CatalogService(runtime)
            # First index 2 files into another volume to verify existing rows remain intact on cancel
            seed_dir = base / "seed"
            _write_test_file(seed_dir / "seed.png", _build_minimal_png(10, 10), mtime_ns=1_700_300_000_000_000_000)
            service.index_folder(seed_dir, volume_key="vol-seed")

            stopper = _ProgressStopEvent(trigger_after=2)

            def _progress_cb(current: int, total: int, message: str) -> None:
                stopper.notify()

            cancelled_res = service.index_folder(
                media_dir,
                volume_key="vol-cancel",
                stop_event=stopper,
                progress_callback=_progress_cb,
            )
            self.assertTrue(cancelled_res.cancelled)
            self.assertEqual(cancelled_res.volume_status, "cancelled")
            service.close()

            # Reopen fresh service on the same SQLite database and verify integrity + seed survival
            reopened = CatalogService(runtime)
            seed_results = reopened.search(volume_key="vol-seed")
            self.assertEqual(seed_results.total_count, 1)

            # Now run full index without cancellation on the reopened DB
            full_res = reopened.index_folder(media_dir, volume_key="vol-cancel")
            self.assertFalse(full_res.cancelled)
            self.assertEqual(full_res.added, 6)
            reopened.close()

    def test_source_files_are_unmodified_after_indexing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            runtime = get_runtime_paths(base / "runtime")
            media_dir = base / "library"

            files_spec = {
                media_dir / "photo.jpg": (
                    _build_exif_jpeg(make="Nikon", model="Z8", date_time_original="2022:11:05 09:15:00"),
                    1_680_000_123_456_789_000,
                ),
                media_dir / "graphic.png": (
                    _build_minimal_png(100, 80),
                    1_680_000_223_456_789_000,
                ),
                media_dir / "notes.pdf": (
                    b"%PDF-1.4\n%Test document\n",
                    1_680_000_323_456_789_000,
                ),
            }
            before_snapshot: dict[Path, tuple[bytes, int, int]] = {}
            for path, (payload, mtime_ns) in files_spec.items():
                raw, recorded_mtime = _write_test_file(path, payload, mtime_ns=mtime_ns)
                before_snapshot[path] = (raw, recorded_mtime, len(raw))

            service = CatalogService(runtime)
            service.index_folder(media_dir, volume_key="vol-ro", compute_hashes=True)
            _ = service.get_storage_analytics()
            _ = service.search()
            service.close()

            for path, (expected_bytes, expected_mtime_ns, expected_size) in before_snapshot.items():
                self.assertTrue(path.exists())
                st = path.stat()
                self.assertEqual(path.read_bytes(), expected_bytes)
                self.assertEqual(int(st.st_mtime_ns), expected_mtime_ns)
                self.assertEqual(int(st.st_size), expected_size)

    def test_search_filters_paging_and_all_analytics_aggregates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            runtime = get_runtime_paths(base / "runtime")
            vol1 = base / "vol1"
            vol2 = base / "vol2"

            jpg_2022 = _build_exif_jpeg(
                make="Sony",
                model="A7IV",
                date_time_original="2022:05:10 08:00:00",
                width=4000,
                height=3000,
            )
            jpg_2024 = _build_exif_jpeg(
                make="Canon",
                model="R6",
                date_time_original="2024:03:20 19:45:00",
                width=6000,
                height=4000,
            ) + (b"\x00" * 500)
            png_bytes = _build_minimal_png(256, 128)
            mp4_bytes = b"\x00\x00\x00\x18ftypmp42" + (b"M" * 1200)
            mp3_bytes = b"ID3" + (b"A" * 300)

            _write_test_file(vol1 / "alpha_2022.jpg", jpg_2022, mtime_ns=1_700_400_000_000_000_000)
            _write_test_file(vol1 / "beta_2024.jpg", jpg_2024, mtime_ns=1_700_400_100_000_000_000)
            _write_test_file(vol1 / "gamma.png", png_bytes, mtime_ns=1_700_400_200_000_000_000)
            _write_test_file(vol2 / "delta_clip.mp4", mp4_bytes, mtime_ns=1_700_400_300_000_000_000)
            _write_test_file(vol2 / "epsilon_song.mp3", mp3_bytes, mtime_ns=1_700_400_400_000_000_000)
            # Duplicate of gamma.png on vol2
            _write_test_file(vol2 / "gamma_copy.png", png_bytes, mtime_ns=1_700_400_500_000_000_000)

            service = CatalogService(runtime)
            service.index_folder(vol1, volume_key="vol-1", label="Volume One", compute_hashes=True)
            service.index_folder(vol2, volume_key="vol-2", label="Volume Two", compute_hashes=True)

            # 1. analytics_by_media_kind
            by_kind = {b.key: b for b in service.analytics_by_media_kind()}
            self.assertEqual(by_kind["image"].file_count, 4)
            self.assertEqual(
                by_kind["image"].total_bytes,
                len(jpg_2022) + len(jpg_2024) + 2 * len(png_bytes),
            )
            self.assertEqual(by_kind["video"].file_count, 1)
            self.assertEqual(by_kind["video"].total_bytes, len(mp4_bytes))
            self.assertEqual(by_kind["audio"].file_count, 1)
            self.assertEqual(by_kind["audio"].total_bytes, len(mp3_bytes))

            # 2. analytics_by_volume
            by_vol = {v.volume_key: v for v in service.analytics_by_volume()}
            self.assertEqual(by_vol["vol-1"].file_count, 3)
            self.assertTrue(by_vol["vol-1"].is_online)
            self.assertEqual(by_vol["vol-2"].file_count, 3)
            self.assertTrue(by_vol["vol-2"].is_online)

            # 3. analytics_by_capture_year
            by_year = {b.key: b for b in service.analytics_by_capture_year()}
            self.assertEqual(by_year[2022].file_count, 1)
            self.assertEqual(by_year[2022].total_bytes, len(jpg_2022))
            self.assertEqual(by_year[2024].file_count, 1)
            self.assertEqual(by_year[2024].total_bytes, len(jpg_2024))

            # 4. analytics_by_extension
            by_ext = {b.key: b for b in service.analytics_by_extension()}
            self.assertEqual(by_ext[".jpg"].file_count, 2)
            self.assertEqual(by_ext[".png"].file_count, 2)
            self.assertEqual(by_ext[".mp4"].file_count, 1)
            self.assertEqual(by_ext[".mp3"].file_count, 1)

            # 5. recoverable_duplicate_bytes & get_storage_analytics
            self.assertEqual(service.recoverable_duplicate_bytes(), len(png_bytes))
            full = service.get_storage_analytics()
            self.assertIsInstance(full, CatalogStorageAnalytics)
            self.assertEqual(full.total_files, 6)
            self.assertEqual(full.recoverable_duplicate_bytes, len(png_bytes))
            self.assertTrue(full.duplicate_analytics_available)

            # 6. Search filters: media_kind, filename, size range, date range, paging
            only_videos = service.search(media_kind="video")
            self.assertEqual(only_videos.total_count, 1)
            self.assertEqual(only_videos[0].filename, "delta_clip.mp4")

            by_name = service.search(filename="gamma")
            self.assertEqual(by_name.total_count, 2)

            by_size = service.search(min_size=1000, max_size=2000)
            self.assertEqual(by_size.total_count, 1)
            self.assertEqual(by_size[0].filename, "delta_clip.mp4")

            by_date = service.search(capture_start="2023-01-01", capture_end="2024-12-31")
            self.assertEqual(by_date.total_count, 1)
            self.assertEqual(by_date[0].filename, "beta_2024.jpg")

            # Paging
            p1 = service.search(page=1, page_size=2, order_by="filename", descending=False)
            p2 = service.search(page=2, page_size=2, order_by="filename", descending=False)
            p3 = service.search(page=3, page_size=2, order_by="filename", descending=False)
            self.assertEqual(p1.total_count, 6)
            self.assertEqual(p1.total_pages, 3)
            self.assertEqual(len(p1.items), 2)
            self.assertTrue(p1.has_next)
            self.assertFalse(p1.has_previous)
            self.assertTrue(p2.has_next)
            self.assertTrue(p2.has_previous)
            self.assertFalse(p3.has_next)
            self.assertTrue(p3.has_previous)
            combined_names = [item.filename for item in (*p1.items, *p2.items, *p3.items)]
            self.assertEqual(len(combined_names), 6)
            self.assertEqual(combined_names, sorted(combined_names))
            service.close()


class LibraryPageTests(unittest.TestCase):
    """Tests for the self-contained PySide6 LibraryPage widget."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.app = create_application()

    def test_page_constructs_and_renders_empty_summary_with_no_index_present(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runtime = get_runtime_paths(Path(tmp) / "runtime")
            page = LibraryPage(runtime)
            self.assertTrue(page.wait_for_done(2000))

            # Accessible names are set
            self.assertTrue(page.index_button.accessibleName())
            self.assertTrue(page.search_button.accessibleName())
            self.assertTrue(page.results_table.accessibleName())
            self.assertTrue(page.volumes_table.accessibleName())

            # Summary renders cleanly with no index present
            self.assertFalse(page.empty_summary_label.isHidden())
            self.assertEqual(page.total_files_card.value_label.text(), "0 files")
            self.assertEqual(page.total_bytes_card.value_label.text(), "0 B")
            self.assertIn("Unavailable", page.recoverable_bytes_card.value_label.text())
            self.assertEqual(page.volumes_table.rowCount(), 0)
            self.assertEqual(page.results_table.rowCount(), 0)
            self.assertIn("Page 0 of 0", page.page_label.text())
            page.service.close()

    def test_page_indexing_runs_off_main_thread_and_search_results_page(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            runtime = get_runtime_paths(base / "runtime")
            media_dir = base / "library"

            for idx in range(5):
                _write_test_file(
                    media_dir / f"photo_{idx:02d}.png",
                    _build_minimal_png(20 + idx, 20 + idx),
                    mtime_ns=1_700_500_000_000_000_000 + idx,
                )

            service = CatalogService(runtime)
            worker_threads: list[int] = []
            orig_index_folder = service.index_folder
            orig_search = service.search

            def _wrapped_index_folder(*args, **kwargs):
                worker_threads.append(threading.get_ident())
                return orig_index_folder(*args, **kwargs)

            def _wrapped_search(*args, **kwargs):
                worker_threads.append(threading.get_ident())
                return orig_search(*args, **kwargs)

            service.index_folder = _wrapped_index_folder  # type: ignore[method-assign]
            service.search = _wrapped_search  # type: ignore[method-assign]

            main_thread_id = threading.get_ident()
            page = LibraryPage(runtime, service=service, page_size=2)
            self.assertTrue(page.wait_for_done(3000))

            started = page.start_index(media_dir)
            self.assertTrue(started)
            self.assertTrue(page.wait_for_done(5000))

            # Every service call must have executed on a background worker thread
            self.assertTrue(worker_threads)
            for tid in worker_threads:
                self.assertNotEqual(tid, main_thread_id)

            # Summary updated
            self.assertTrue(page.empty_summary_label.isHidden())
            self.assertEqual(page.total_files_card.value_label.text(), "5 files")
            self.assertIn("Unavailable", page.recoverable_bytes_card.value_label.text())

            # Page 1 of 3 with page_size=2
            self.assertEqual(page.results_table.rowCount(), 2)
            self.assertFalse(page.prev_page_button.isEnabled())
            self.assertTrue(page.next_page_button.isEnabled())
            self.assertIn("Page 1 of 3", page.page_label.text())

            # Go to Page 2
            self.assertTrue(page.next_page())
            self.assertTrue(page.wait_for_done(3000))
            self.assertEqual(page.results_table.rowCount(), 2)
            self.assertTrue(page.prev_page_button.isEnabled())
            self.assertTrue(page.next_page_button.isEnabled())
            self.assertIn("Page 2 of 3", page.page_label.text())

            # Go to Page 3 (1 remaining item)
            self.assertTrue(page.next_page())
            self.assertTrue(page.wait_for_done(3000))
            self.assertEqual(page.results_table.rowCount(), 1)
            self.assertTrue(page.prev_page_button.isEnabled())
            self.assertFalse(page.next_page_button.isEnabled())
            self.assertIn("Page 3 of 3", page.page_label.text())

            # Go back to Page 2
            self.assertTrue(page.previous_page())
            self.assertTrue(page.wait_for_done(3000))
            self.assertEqual(page.results_table.rowCount(), 2)
            self.assertIn("Page 2 of 3", page.page_label.text())
            service.close()

    def test_page_renders_offline_volume_visibly_in_summary_and_results(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            runtime = get_runtime_paths(base / "runtime")
            online_dir = base / "online_vol"
            offline_dir = base / "offline_vol"

            _write_test_file(online_dir / "keep.png", _build_minimal_png(16, 16), mtime_ns=1_700_600_000_000_000_000)
            _write_test_file(offline_dir / "archived.png", _build_minimal_png(32, 32), mtime_ns=1_700_600_000_000_000_000)

            service = CatalogService(runtime)
            service.index_folder(online_dir, volume_key="vol-on", label="OnlineSSD")
            service.index_folder(offline_dir, volume_key="vol-off", label="ExternalHDD")
            service.mark_volume_offline("vol-off")

            page = LibraryPage(runtime, service=service)
            self.assertTrue(page.wait_for_done(3000))

            # Offline volume banner is visible and explicit
            self.assertFalse(page.offline_banner.isHidden())
            self.assertIn("ExternalHDD", page.offline_banner.label.text())
            self.assertIn("1 offline", page.volumes_count_card.value_label.text())

            # Volumes summary table shows explicit text status (not colour alone)
            self.assertEqual(page.volumes_table.rowCount(), 2)
            vol_rows = {}
            for r in range(page.volumes_table.rowCount()):
                name_cell = page.volumes_table.item(r, 0).text()
                status_cell = page.volumes_table.item(r, 1).text()
                vol_rows[name_cell] = status_cell

            self.assertIn("ExternalHDD [Offline]", vol_rows)
            self.assertEqual(vol_rows["ExternalHDD [Offline]"], "Offline (not mounted)")
            self.assertIn("OnlineSSD", vol_rows)
            self.assertEqual(vol_rows["OnlineSSD"], "Online")
            service.close()

    def test_page_cancellation_leaves_page_usable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            runtime = get_runtime_paths(base / "runtime")
            media_dir = base / "library"

            for idx in range(4):
                _write_test_file(
                    media_dir / f"item_{idx}.png",
                    _build_minimal_png(24, 24),
                    mtime_ns=1_700_700_000_000_000_000 + idx,
                )

            service = CatalogService(runtime)
            real_index = service.index_folder
            entered_worker = threading.Event()
            allow_continue = threading.Event()

            def _slow_index_folder(*args, **kwargs):
                entered_worker.set()
                cancellation = kwargs.get("cancellation") or kwargs.get("stop_event")
                deadline = time.monotonic() + 2.0
                while time.monotonic() < deadline and not allow_continue.is_set():
                    if cancellation is not None and cancellation.is_set():
                        break
                    time.sleep(0.02)
                return real_index(*args, **kwargs)

            service.index_folder = _slow_index_folder  # type: ignore[method-assign]
            page = LibraryPage(runtime, service=service)
            self.assertTrue(page.wait_for_done(2000))

            self.assertTrue(page.start_index(media_dir))
            self.assertTrue(entered_worker.wait(timeout=2.0))
            self.assertTrue(page.cancel_indexing())
            allow_continue.set()
            self.assertTrue(page.wait_for_done(5000))

            # After cancellation, controls are re-enabled and a normal index succeeds
            self.assertTrue(page.index_button.isEnabled())
            service.index_folder = real_index  # type: ignore[method-assign]

            self.assertTrue(page.start_index(media_dir))
            self.assertTrue(page.wait_for_done(5000))
            self.assertEqual(page.total_files_card.value_label.text(), "4 files")
            self.assertEqual(page.results_table.rowCount(), 4)
            service.close()

    def test_page_search_filters_and_hashed_duplicate_summary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            runtime = get_runtime_paths(base / "runtime")
            media_dir = base / "library"

            dup_jpg = _build_exif_jpeg(date_time_original="2023:06:15 12:00:00", width=1024, height=768)
            clip_mp4 = b"\x00\x00\x00\x18ftypmp42" + (b"V" * 1500)

            _write_test_file(media_dir / "holiday_1.jpg", dup_jpg, mtime_ns=1_700_800_000_000_000_000)
            _write_test_file(media_dir / "holiday_2.jpg", dup_jpg, mtime_ns=1_700_800_100_000_000_000)
            _write_test_file(media_dir / "reel.mp4", clip_mp4, mtime_ns=1_700_800_200_000_000_000)

            service = CatalogService(runtime)
            page = LibraryPage(runtime, service=service)
            self.assertTrue(page.wait_for_done(2000))

            # Index with compute_hashes=True
            self.assertTrue(page.start_index(media_dir, compute_hashes=True))
            self.assertTrue(page.wait_for_done(5000))

            # Recoverable duplicate bytes card shows the exact duplicate byte size
            self.assertIn(f"{len(dup_jpg):,} B", page.recoverable_bytes_card.value_label.text())
            self.assertEqual(page.media_kind_table.rowCount(), 2)
            self.assertEqual(page.capture_year_table.rowCount(), 1)
            self.assertEqual(page.extension_table.rowCount(), 2)

            # Filter by media kind = video
            vid_idx = page.media_kind_filter.findData("video")
            page.media_kind_filter.setCurrentIndex(vid_idx)
            self.assertTrue(page.start_search(page=1))
            self.assertTrue(page.wait_for_done(3000))
            self.assertEqual(page.results_table.rowCount(), 1)
            self.assertEqual(page.results_table.item(0, 0).text(), "reel.mp4")

            # Clear filters and filter by date range + filename
            page.clear_filters()
            self.assertTrue(page.wait_for_done(3000))
            self.assertEqual(page.results_table.rowCount(), 3)

            page.filename_filter.setText("holiday_1")
            page.date_start_filter.setText("2023-01-01")
            page.date_end_filter.setText("2023-12-31")
            self.assertTrue(page.start_search(page=1))
            self.assertTrue(page.wait_for_done(3000))
            self.assertEqual(page.results_table.rowCount(), 1)
            self.assertEqual(page.results_table.item(0, 0).text(), "holiday_1.jpg")
            service.close()


if __name__ == "__main__":
    unittest.main()
