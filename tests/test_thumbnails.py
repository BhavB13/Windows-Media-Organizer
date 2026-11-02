"""Unit tests for the framework-neutral thumbnail cache service."""

from __future__ import annotations

import builtins
import hashlib
import importlib
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from duplicate_transfer_manager import previews as previews_module
from duplicate_transfer_manager.previews import (
    ThumbnailCache,
    ThumbnailRequest,
    build_cache_key,
    default_thumbnail_cache_dir,
    generate_thumbnails_batch,
    is_pillow_available,
    normalize_thumbnail_size,
    validate_cache_key,
)
from duplicate_transfer_manager.previews import thumbnails as thumbnails_module
from duplicate_transfer_manager.runtime_paths import get_runtime_paths


class ThumbnailCacheTests(unittest.TestCase):
    """Comprehensive unit tests for ThumbnailCache and batch thumbnail generation."""

    def setUp(self) -> None:
        self._temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self._temp_dir.name)
        self.sources_dir = self.root / "sources"
        self.cache_dir = self.root / "cache" / "thumbnails"
        self.sources_dir.mkdir(parents=True, exist_ok=True)
        self.cache = ThumbnailCache(cache_dir=self.cache_dir, default_size=(128, 128))

    def tearDown(self) -> None:
        self._temp_dir.cleanup()

    def _create_jpeg(
        self,
        name: str = "photo.jpg",
        size: tuple[int, int] = (640, 480),
        color: tuple[int, int, int] = (40, 120, 200),
        orientation: int | None = None,
    ) -> Path:
        path = self.sources_dir / name
        path.parent.mkdir(parents=True, exist_ok=True)
        image = Image.new("RGB", size, color)
        if orientation is not None:
            exif = image.getexif()
            exif[0x0112] = orientation
            image.save(path, format="JPEG", quality=92, exif=exif)
        else:
            image.save(path, format="JPEG", quality=92)
        return path

    def _create_transparent_png(
        self,
        name: str = "badge.png",
        size: tuple[int, int] = (400, 200),
    ) -> Path:
        path = self.sources_dir / name
        path.parent.mkdir(parents=True, exist_ok=True)
        image = Image.new("RGBA", size, (255, 64, 32, 128))
        image.save(path, format="PNG")
        return path

    def test_runtime_paths_integration_uses_temp_root(self) -> None:
        runtime_root = self.root / "runtime_appdata"
        paths = get_runtime_paths(runtime_root)
        resolved_cache_dir = default_thumbnail_cache_dir(paths)
        self.assertEqual(resolved_cache_dir, (runtime_root / "cache" / "thumbnails").resolve())
        service = ThumbnailCache(paths=paths)
        self.assertEqual(service.cache_dir, (runtime_root / "cache" / "thumbnails").resolve())

    def test_generate_then_second_request_is_cache_hit_and_sharded_privately(self) -> None:
        source = self._create_jpeg("secret_vacation_photo_2026.jpg", size=(800, 600))

        first = self.cache.get_or_create(source, size=(128, 128))
        self.assertTrue(first.ok)
        self.assertFalse(first.cache_hit)
        self.assertTrue(first.generated)
        self.assertIsNotNone(first.thumbnail_path)
        assert first.thumbnail_path is not None
        self.assertTrue(first.thumbnail_path.is_file())

        # Verify sharding by hash prefix and privacy (source filename never in cache path).
        self.assertEqual(first.thumbnail_path.parent.name, first.cache_key[:2])
        self.assertEqual(first.thumbnail_path.parent.parent, self.cache.cache_dir)
        self.assertNotIn("secret_vacation", str(first.thumbnail_path).lower())
        self.assertNotIn("photo_2026", str(first.thumbnail_path).lower())

        second = self.cache.get_or_create(source, size=(128, 128))
        self.assertTrue(second.ok)
        self.assertTrue(second.cache_hit)
        self.assertTrue(second.from_cache)
        self.assertFalse(second.generated)
        self.assertEqual(second.thumbnail_path, first.thumbnail_path)
        self.assertEqual(second.cache_key, first.cache_key)

        # Also verify warm on-disk cache hit across a fresh ThumbnailCache instance.
        fresh_cache = ThumbnailCache(cache_dir=self.cache_dir, default_size=(128, 128))
        third = fresh_cache.get_or_create(source, size=(128, 128))
        self.assertTrue(third.ok)
        self.assertTrue(third.cache_hit)
        self.assertFalse(third.generated)
        self.assertEqual(third.thumbnail_path, first.thumbnail_path)

    def test_changing_source_mtime_or_size_invalidates_cache_entry(self) -> None:
        source = self._create_jpeg("editable.jpg", size=(500, 400))
        initial = self.cache.get_or_create(source, size=(128, 128))
        self.assertTrue(initial.ok)
        self.assertFalse(initial.cache_hit)

        # Advance mtime_ns without changing size -> must invalidate and regenerate.
        stat_before = source.stat()
        os.utime(source, ns=(stat_before.st_atime_ns, stat_before.st_mtime_ns + 5_000_000_000))
        after_mtime = self.cache.get_or_create(source, size=(128, 128))
        self.assertTrue(after_mtime.ok)
        self.assertFalse(after_mtime.cache_hit)
        self.assertTrue(after_mtime.generated)
        self.assertNotEqual(after_mtime.cache_key, initial.cache_key)

        # Replace file with a different byte size -> must invalidate and regenerate.
        fixed_mtime_ns = source.stat().st_mtime_ns
        bigger_img = Image.new("RGB", (1200, 900), (10, 200, 50))
        bigger_img.save(source, format="JPEG", quality=95)
        os.utime(source, ns=(fixed_mtime_ns, fixed_mtime_ns))
        self.assertNotEqual(source.stat().st_size, stat_before.st_size)

        after_size = self.cache.get_or_create(source, size=(128, 128))
        self.assertTrue(after_size.ok)
        self.assertFalse(after_size.cache_hit)
        self.assertTrue(after_size.generated)
        self.assertNotEqual(after_size.cache_key, after_mtime.cache_key)

    def test_different_requested_size_produces_different_cache_entry(self) -> None:
        source = self._create_jpeg("multisize.jpg", size=(1000, 500))

        small = self.cache.get_or_create(source, size=(64, 64))
        large = self.cache.get_or_create(source, size=(256, 256))

        self.assertTrue(small.ok)
        self.assertTrue(large.ok)
        self.assertNotEqual(small.cache_key, large.cache_key)
        self.assertNotEqual(small.thumbnail_path, large.thumbnail_path)
        self.assertEqual(small.size, (64, 32))
        self.assertEqual(large.size, (256, 128))

    def test_exif_orientation_applied_so_rotated_source_is_upright(self) -> None:
        # Build an upright portrait image (100x200): top half RED, bottom half BLUE.
        upright = Image.new("RGB", (100, 200), (0, 0, 255))
        upright.paste((255, 0, 0), (0, 0, 100, 100))

        # Store raw pixels sideways (200x100, rotated 90 CCW) with EXIF Orientation=6 (Rotate 90 CW).
        sideways_raw = upright.transpose(Image.Transpose.ROTATE_90)
        self.assertEqual(sideways_raw.size, (200, 100))

        source = self.sources_dir / "portrait_exif6.jpg"
        exif = sideways_raw.getexif()
        exif[0x0112] = 6
        sideways_raw.save(source, format="JPEG", quality=95, exif=exif)

        result = self.cache.get_or_create(source, size=(100, 100))
        self.assertTrue(result.ok)
        self.assertEqual(result.exif_orientation, 6)
        self.assertEqual(result.size, (50, 100))
        assert result.thumbnail_path is not None

        with Image.open(result.thumbnail_path) as thumb:
            self.assertEqual(thumb.size, (50, 100))
            top_pixel = thumb.getpixel((25, 10))
            bottom_pixel = thumb.getpixel((25, 90))
            # Top of upright thumbnail must be predominantly red; bottom must be predominantly blue.
            self.assertGreater(top_pixel[0], 200)
            self.assertLess(top_pixel[2], 50)
            self.assertGreater(bottom_pixel[2], 200)
            self.assertLess(bottom_pixel[0], 50)

        # Requesting without EXIF orientation produces a distinct cache key and unrotated aspect.
        unrotated = self.cache.get_or_create(source, size=(100, 100), apply_exif_orientation=False)
        self.assertTrue(unrotated.ok)
        self.assertNotEqual(unrotated.cache_key, result.cache_key)
        self.assertEqual(unrotated.size, (100, 50))

    def test_aspect_ratio_preserved_and_transparent_png_kept_as_png(self) -> None:
        wide_jpeg = self._create_jpeg("panorama.jpg", size=(1200, 300))
        wide_res = self.cache.get_or_create(wide_jpeg, size=(200, 200))
        self.assertTrue(wide_res.ok)
        self.assertEqual(wide_res.size, (200, 50))
        self.assertEqual(wide_res.format, "JPEG")
        assert wide_res.thumbnail_path is not None
        self.assertEqual(wide_res.thumbnail_path.suffix, ".jpg")

        tall_png = self._create_transparent_png("overlay.png", size=(150, 600))
        png_res = self.cache.get_or_create(tall_png, size=(200, 200))
        self.assertTrue(png_res.ok)
        self.assertEqual(png_res.size, (50, 200))
        self.assertEqual(png_res.format, "PNG")
        assert png_res.thumbnail_path is not None
        self.assertEqual(png_res.thumbnail_path.suffix, ".png")
        with Image.open(png_res.thumbnail_path) as saved_png:
            self.assertEqual(saved_png.mode, "RGBA")
            self.assertEqual(saved_png.getpixel((25, 100))[3], 128)

        # CMYK source photo is converted to a consistent RGB mode before saving as JPEG.
        cmyk_path = self.sources_dir / "cmyk_photo.tiff"
        Image.new("CMYK", (300, 150), (10, 20, 30, 40)).save(cmyk_path, format="TIFF")
        cmyk_res = self.cache.get_or_create(cmyk_path, size=(120, 120))
        self.assertTrue(cmyk_res.ok)
        self.assertEqual(cmyk_res.format, "JPEG")
        self.assertEqual(cmyk_res.size, (120, 60))
        assert cmyk_res.thumbnail_path is not None
        with Image.open(cmyk_res.thumbnail_path) as saved_cmyk:
            self.assertEqual(saved_cmyk.mode, "RGB")

    def test_jpeg_uses_pillow_draft_mode_before_decode(self) -> None:
        source = self._create_jpeg("large_camera.jpg", size=(1600, 1200))
        draft_calls: list[tuple[str, tuple[int, int]]] = []
        original_open = Image.open

        def tracking_open(*args, **kwargs):
            img = original_open(*args, **kwargs)
            orig_draft = img.draft

            def wrapped_draft(mode, size):
                draft_calls.append((mode, size))
                return orig_draft(mode, size)

            img.draft = wrapped_draft  # type: ignore[method-assign]
            return img

        with patch.object(thumbnails_module.Image, "open", side_effect=tracking_open):
            result = self.cache.get_or_create(source, size=(160, 160))

        self.assertTrue(result.ok)
        self.assertEqual(draft_calls, [("RGB", (160, 160))])

    def test_source_file_is_byte_for_byte_unchanged_after_generation(self) -> None:
        source = self._create_jpeg("immutable_source.jpg", size=(640, 480), orientation=3)
        before_bytes = source.read_bytes()
        before_digest = hashlib.sha256(before_bytes).hexdigest()
        before_stat = source.stat()

        result = self.cache.get_or_create(source, size=(120, 120))
        self.assertTrue(result.ok)

        after_bytes = source.read_bytes()
        after_digest = hashlib.sha256(after_bytes).hexdigest()
        after_stat = source.stat()

        self.assertEqual(before_bytes, after_bytes)
        self.assertEqual(before_digest, after_digest)
        self.assertEqual(before_stat.st_size, after_stat.st_size)
        self.assertEqual(before_stat.st_mtime_ns, after_stat.st_mtime_ns)

    def test_interrupted_or_partial_cache_write_is_not_served_as_valid(self) -> None:
        source = self._create_jpeg("atomic_target.jpg", size=(400, 300))

        # 1. Simulate an exception midway through writing the temp file in _atomic_save_thumbnail.
        original_replace = os.replace

        def failing_replace(src, dst):
            # Leave partial data in the temp file and fail before renaming into place.
            Path(src).write_bytes(b"\xff\xd8\xff\xe0partial_interrupted_data")
            raise OSError("Simulated power loss during atomic promotion")

        with patch("duplicate_transfer_manager.previews.thumbnails.os.replace", side_effect=failing_replace):
            failed = self.cache.get_or_create(source, size=(128, 128))
        self.assertFalse(failed.ok)
        self.assertIsNotNone(failed.error)

        # Ensure no leftover .part temp file remained and the next request generates a fresh thumbnail.
        part_files = list(self.cache_dir.rglob("*.part"))
        self.assertEqual(part_files, [])

        recovered = self.cache.get_or_create(source, size=(128, 128))
        self.assertTrue(recovered.ok)
        self.assertFalse(recovered.cache_hit)
        self.assertTrue(recovered.generated)
        assert recovered.thumbnail_path is not None

        # 2. Simulate a truncated/incomplete JPEG sitting at the final cache path (missing EOI).
        valid_bytes = recovered.thumbnail_path.read_bytes()
        recovered.thumbnail_path.write_bytes(valid_bytes[:-12])

        regenerated = self.cache.get_or_create(source, size=(128, 128))
        self.assertTrue(regenerated.ok)
        self.assertFalse(regenerated.cache_hit)
        self.assertTrue(regenerated.generated)
        self.assertEqual(regenerated.thumbnail_path.read_bytes(), valid_bytes)
        self.assertIsNotNone(original_replace)

    def test_lru_eviction_respects_byte_budget_and_evicts_least_recently_used_first(self) -> None:
        manual_cache = ThumbnailCache(
            cache_dir=self.cache_dir,
            default_size=(160, 160),
            auto_evict=False,
        )
        sources = [
            self._create_jpeg(f"img_{idx}.jpg", size=(600, 450), color=(idx * 40, 100, 200 - idx * 30))
            for idx in range(4)
        ]
        results = [manual_cache.get_or_create(src) for src in sources]
        for res in results:
            self.assertTrue(res.ok)
            assert res.thumbnail_path is not None
            self.assertTrue(res.thumbnail_path.exists())

        # Touch source[0] via a cache hit so source[1] becomes the least-recently-used entry,
        # followed by source[2], source[3], and finally source[0] (most recently used).
        hit_zero = manual_cache.get_or_create(sources[0])
        self.assertTrue(hit_zero.cache_hit)

        sizes = [res.thumbnail_path.stat().st_size for res in results if res.thumbnail_path is not None]
        # Set budget large enough to keep only the 2 most recently used entries (sources[3] and sources[0]).
        budget = sizes[0] + sizes[3]
        eviction = manual_cache.evict(max_bytes=budget)

        self.assertEqual(eviction.evicted_count, 2)
        self.assertLessEqual(eviction.remaining_bytes, budget)
        assert results[0].thumbnail_path is not None
        assert results[1].thumbnail_path is not None
        assert results[2].thumbnail_path is not None
        assert results[3].thumbnail_path is not None

        # Least-recently-used entries (1 and 2) were evicted first; 3 and 0 remain.
        self.assertFalse(results[1].thumbnail_path.exists())
        self.assertFalse(results[2].thumbnail_path.exists())
        self.assertTrue(results[3].thumbnail_path.exists())
        self.assertTrue(results[0].thumbnail_path.exists())
        self.assertEqual(
            list(eviction.evicted_paths),
            [results[1].thumbnail_path.resolve(), results[2].thumbnail_path.resolve()],
        )

    def test_eviction_is_safe_while_other_entries_are_being_written(self) -> None:
        shared_cache = ThumbnailCache(
            cache_dir=self.cache_dir,
            max_bytes=4096,
            default_size=(128, 128),
            auto_evict=True,
        )
        sources = [
            self._create_jpeg(f"concurrent_{i}.jpg", size=(500, 500), color=((i * 23) % 255, 90, 150))
            for i in range(12)
        ]
        stop_evictor = threading.Event()
        evictor_errors: list[Exception] = []

        def concurrent_evictor() -> None:
            while not stop_evictor.is_set():
                try:
                    shared_cache.evict(max_bytes=2048)
                except Exception as exc:  # pragma: no cover
                    evictor_errors.append(exc)

        thread = threading.Thread(target=concurrent_evictor, daemon=True)
        thread.start()
        try:
            batch_results = list(shared_cache.generate_batch(sources, max_workers=3))
        finally:
            stop_evictor.set()
            thread.join(timeout=5)

        self.assertEqual(evictor_errors, [])
        self.assertEqual(len(batch_results), len(sources))
        self.assertTrue(all(r.ok for r in batch_results))
        final_evict = shared_cache.evict(max_bytes=2048)
        self.assertLessEqual(final_evict.remaining_bytes, 2048)

    def test_crafted_key_cannot_write_or_delete_outside_cache_directory(self) -> None:
        sentinel_file = self.root / "outside_sentinel.jpg"
        sentinel_bytes = b"DO_NOT_TOUCH_USER_FILE"
        sentinel_file.write_bytes(sentinel_bytes)

        crafted_keys = [
            "../../outside_sentinel",
            "..\\..\\outside_sentinel",
            "../outside_sentinel.jpg",
            "/outside_sentinel",
            str(sentinel_file),
            "00/../../../outside_sentinel",
            "abcd\x00ef0123456789",
            "not-a-hex-digest-at-all!",
        ]
        for bad_key in crafted_keys:
            with self.assertRaises(ValueError):
                validate_cache_key(bad_key)
            with self.assertRaises(ValueError):
                self.cache.entry_path(bad_key)
            with self.assertRaises(ValueError):
                self.cache.delete_entry(bad_key)

        # Direct unlink helper and atomic save helper must also reject any path outside cache_dir.
        with self.assertRaises(ValueError):
            self.cache._safe_unlink(sentinel_file)
        with self.assertRaises(ValueError):
            self.cache._safe_unlink(self.cache_dir / ".." / ".." / "outside_sentinel.jpg")
        with self.assertRaises(ValueError):
            self.cache._atomic_save_thumbnail(
                Image.new("RGB", (10, 10)),
                sentinel_file,
                "JPEG",
            )

        # Eviction must also never touch files outside cache_dir.
        self.cache.evict(max_bytes=0)
        self.assertTrue(sentinel_file.exists())
        self.assertEqual(sentinel_file.read_bytes(), sentinel_bytes)

    def test_corrupt_or_missing_source_reported_as_error_without_raising(self) -> None:
        valid_one = self._create_jpeg("valid_1.jpg", size=(300, 200))
        corrupt = self.sources_dir / "corrupt.jpg"
        corrupt.write_bytes(b"not a valid jpeg or image file at all")
        missing = self.sources_dir / "does_not_exist.jpg"
        valid_two = self._create_jpeg("valid_2.jpg", size=(300, 200))

        single_corrupt = self.cache.get_or_create(corrupt)
        self.assertFalse(single_corrupt.ok)
        self.assertIsNotNone(single_corrupt.error)
        self.assertIsNone(single_corrupt.thumbnail_path)

        batch = list(self.cache.generate_batch([valid_one, corrupt, missing, valid_two], max_workers=2))
        self.assertEqual(len(batch), 4)
        self.assertTrue(batch[0].ok)
        self.assertFalse(batch[1].ok)
        self.assertIsNotNone(batch[1].error)
        self.assertFalse(batch[2].ok)
        self.assertIsNotNone(batch[2].error)
        self.assertTrue(batch[3].ok)

    def test_cancellation_returns_early(self) -> None:
        sources = [self._create_jpeg(f"cancel_{i}.jpg", size=(600, 400)) for i in range(25)]
        stop_event = threading.Event()

        # Pre-set cancellation returns zero items immediately.
        stop_event.set()
        immediate = list(self.cache.generate_batch(sources, stop_event=stop_event, max_workers=2))
        self.assertEqual(immediate, [])

        # Setting stop_event after the first yielded item stops the batch early.
        stop_midway = threading.Event()
        collected = []
        for item in self.cache.generate_batch(sources, stop_event=stop_midway, max_workers=2):
            collected.append(item)
            if len(collected) == 2:
                stop_midway.set()

        self.assertLess(len(collected), len(sources))
        self.assertEqual(len(collected), 2)

    def test_batch_path_does_not_exceed_worker_bound(self) -> None:
        sources = [self._create_jpeg(f"pool_{i}.jpg", size=(320, 240)) for i in range(16)]
        worker_bound = 3
        active_workers = 0
        peak_workers = 0
        counter_lock = threading.Lock()
        original_get_or_create = self.cache.get_or_create

        def instrumented_get_or_create(*args, **kwargs):
            nonlocal active_workers, peak_workers
            with counter_lock:
                active_workers += 1
                if active_workers > peak_workers:
                    peak_workers = active_workers
            try:
                time.sleep(0.015)
                return original_get_or_create(*args, **kwargs)
            finally:
                with counter_lock:
                    active_workers -= 1

        with patch.object(self.cache, "get_or_create", side_effect=instrumented_get_or_create):
            results = list(
                generate_thumbnails_batch(
                    sources,
                    cache=self.cache,
                    size=(96, 96),
                    max_workers=worker_bound,
                )
            )

        self.assertEqual(len(results), len(sources))
        self.assertTrue(all(r.ok for r in results))
        self.assertGreaterEqual(peak_workers, 1)
        self.assertLessEqual(peak_workers, worker_bound)

    def test_cache_hit_reporting_and_stats_accuracy(self) -> None:
        s1 = self._create_jpeg("stat_1.jpg", size=(400, 300))
        s2 = self._create_jpeg("stat_2.jpg", size=(400, 300))
        corrupt = self.sources_dir / "stat_bad.jpg"
        corrupt.write_bytes(b"corrupt")

        r1 = self.cache.get_or_create(s1)
        r2 = self.cache.get_or_create(s2)
        r1_hit = self.cache.get_or_create(s1)
        r2_hit = self.cache.get_or_create(s2)
        r1_hit_again = self.cache.get_or_create(s1)
        r_bad = self.cache.get_or_create(corrupt)

        self.assertFalse(r1.cache_hit)
        self.assertTrue(r1.generated)
        self.assertFalse(r2.cache_hit)
        self.assertTrue(r2.generated)
        self.assertTrue(r1_hit.cache_hit)
        self.assertFalse(r1_hit.generated)
        self.assertTrue(r2_hit.cache_hit)
        self.assertTrue(r1_hit_again.cache_hit)
        self.assertFalse(r_bad.ok)

        stats = self.cache.stats()
        self.assertEqual(stats.entry_count, 2)
        self.assertGreater(stats.total_bytes, 0)
        self.assertEqual(stats.misses, 2)
        self.assertEqual(stats.hits, 3)
        self.assertEqual(stats.errors, 1)
        self.assertAlmostEqual(stats.hit_rate, 3 / 5)

    def test_module_imports_and_degrades_gracefully_when_pillow_is_absent(self) -> None:
        source = self._create_jpeg("degrade.jpg", size=(300, 200))

        # 1. Verify runtime degradation when Image is None.
        with patch.object(thumbnails_module, "Image", None):
            self.assertFalse(is_pillow_available())
            self.assertFalse(self.cache.available)
            single = self.cache.get_or_create(source)
            self.assertFalse(single.ok)
            self.assertFalse(single.cache_hit)
            self.assertFalse(single.generated)
            self.assertIsNotNone(single.error)
            self.assertIn("Pillow", single.error or "")

            batch = list(self.cache.generate_batch([ ThumbnailRequest(source_path=source, size=(64, 64)) ]))
            self.assertEqual(len(batch), 1)
            self.assertFalse(batch[0].ok)
            self.assertIn("Pillow", batch[0].error or "")

        # 2. Verify top-level module import succeeds when PIL raises ImportError.
        real_import = builtins.__import__

        def block_pil(name, globals=None, locals=None, fromlist=(), level=0):
            if name == "PIL" or name.startswith("PIL."):
                raise ImportError("Simulated missing Pillow installation")
            return real_import(name, globals, locals, fromlist, level)

        with patch("builtins.__import__", side_effect=block_pil):
            importlib.reload(thumbnails_module)
            self.assertIsNone(thumbnails_module.Image)
            self.assertFalse(thumbnails_module.is_pillow_available())

        # Reload with real Pillow restored so subsequent tests see normal state.
        importlib.reload(thumbnails_module)
        importlib.reload(previews_module)
        self.assertIsNotNone(thumbnails_module.Image)
        self.assertTrue(thumbnails_module.is_pillow_available())
        self.assertEqual(normalize_thumbnail_size(96), (96, 96))
        self.assertTrue(
            build_cache_key(
                source,
                size_bytes=100,
                mtime_ns=200,
                requested_size=(96, 96),
                exif_orientation=1,
            )
        )


if __name__ == "__main__":
    unittest.main()


class IncrementalEvictionTests(unittest.TestCase):
    """Eviction used to walk the whole cache after every new thumbnail."""

    def _photos(self, folder: Path, count: int) -> list[Path]:
        from PIL import Image as _Image

        paths = []
        for index in range(count):
            path = folder / f"p{index:03}.jpg"
            _Image.new("RGB", (64, 48), (index * 3 % 256, 90, 160)).save(path, "JPEG")
            paths.append(path)
        return paths

    def test_writes_under_budget_do_not_walk_the_cache_each_time(self):
        from unittest import mock

        from duplicate_transfer_manager.previews.thumbnails import ThumbnailCache

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            photos = self._photos(root, 60)
            cache = ThumbnailCache(cache_dir=str(root / "cache"), max_bytes=64 * 1024 * 1024)
            walks = []
            real = cache._collect_cache_entries
            with mock.patch.object(cache, "_collect_cache_entries", side_effect=lambda: walks.append(1) or real()):
                for photo in photos:
                    self.assertIsNotNone(cache.get_or_create(str(photo), 32).thumbnail_path)
            self.assertLessEqual(len(walks), 2, f"{len(walks)} full cache walks for 60 writes")

    def test_the_budget_is_still_enforced(self):
        from duplicate_transfer_manager.previews.thumbnails import ThumbnailCache

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            photos = self._photos(root, 40)
            budget = 8 * 1024
            cache = ThumbnailCache(cache_dir=str(root / "cache"), max_bytes=budget)
            for photo in photos:
                cache.get_or_create(str(photo), 32)
            on_disk = sum(p.stat().st_size for p in (root / "cache").rglob("*") if p.is_file())
            self.assertLessEqual(on_disk, budget)
