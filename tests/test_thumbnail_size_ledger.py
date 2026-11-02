"""Unit tests for ThumbnailCache size ledger persistence and startup optimization."""

from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from duplicate_transfer_manager.previews.thumbnails import (
    LEDGER_FILENAME,
    ThumbnailCache,
)


class ThumbnailSizeLedgerTests(unittest.TestCase):
    """Verify ThumbnailCache size ledger persistence, corruption handling, and eviction."""

    def setUp(self) -> None:
        self._temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self._temp_dir.name)
        self.cache_dir = self.root / "cache"
        self.sources_dir = self.root / "sources"
        self.sources_dir.mkdir(parents=True, exist_ok=True)
        self.ledger_path = self.cache_dir / LEDGER_FILENAME

    def tearDown(self) -> None:
        self._temp_dir.cleanup()

    def _create_source(self, name: str, size: tuple[int, int] = (160, 120), color: tuple[int, int, int] = (50, 100, 150)) -> Path:
        path = self.sources_dir / name
        path.parent.mkdir(parents=True, exist_ok=True)
        image = Image.new("RGB", size, color)
        image.save(path, format="JPEG", quality=85)
        return path

    def test_new_instance_first_write_performs_no_full_walk_when_ledger_present(self) -> None:
        cache1 = ThumbnailCache(cache_dir=self.cache_dir, default_size=(64, 64))
        sources = [self._create_source(f"pop_{i}.jpg", color=(i * 20, 80, 120)) for i in range(5)]
        for src in sources:
            res = cache1.get_or_create(src)
            self.assertTrue(res.ok)

        # Eviction/recount ensures ledger is written and accurate
        cache1.evict()
        self.assertTrue(self.ledger_path.is_file())
        ledger_data = json.loads(self.ledger_path.read_text(encoding="utf-8"))
        self.assertGreater(ledger_data["total_bytes"], 0)

        # A new instance pointing to the same cache directory seeds from ledger
        cache2 = ThumbnailCache(cache_dir=self.cache_dir, default_size=(64, 64))
        self.assertEqual(cache2._estimated_bytes, ledger_data["total_bytes"])

        walk_calls: list[int] = []
        real_collect = cache2._collect_cache_entries

        def spy_collect() -> list[tuple[int, int, Path]]:
            walk_calls.append(1)
            return real_collect()

        new_photo = self._create_source("new_photo.jpg", color=(200, 100, 50))
        with patch.object(cache2, "_collect_cache_entries", side_effect=spy_collect):
            res_new = cache2.get_or_create(new_photo)
            self.assertTrue(res_new.ok)
            self.assertTrue(res_new.generated)

        # First write must perform ZERO full cache walks
        self.assertEqual(len(walk_calls), 0, "Expected zero full walks on first write with valid ledger")

    def test_corrupt_ledger_falls_back_to_one_walk_and_is_rewritten(self) -> None:
        cache1 = ThumbnailCache(cache_dir=self.cache_dir, default_size=(64, 64))
        for i in range(3):
            src = self._create_source(f"init_{i}.jpg")
            cache1.get_or_create(src)
        cache1.evict()
        self.assertTrue(self.ledger_path.is_file())

        # Corrupt the ledger file with malformed JSON
        self.ledger_path.write_text("{ this is corrupt json : not valid }", encoding="utf-8")

        cache2 = ThumbnailCache(cache_dir=self.cache_dir, default_size=(64, 64))
        # Seeding should fail safely and fall back to None
        self.assertIsNone(cache2._estimated_bytes)

        walk_calls: list[int] = []
        real_collect = cache2._collect_cache_entries

        def spy_collect() -> list[tuple[int, int, Path]]:
            walk_calls.append(1)
            return real_collect()

        photo4 = self._create_source("photo_4.jpg", color=(90, 180, 20))
        with patch.object(cache2, "_collect_cache_entries", side_effect=spy_collect):
            res4 = cache2.get_or_create(photo4)
            self.assertTrue(res4.ok)

        # Falls back to exactly one walk
        self.assertEqual(len(walk_calls), 1, "Expected exactly one walk on first write with corrupt ledger")

        # The ledger must be rewritten with valid data
        self.assertTrue(self.ledger_path.is_file())
        rewritten = json.loads(self.ledger_path.read_text(encoding="utf-8"))
        self.assertIsInstance(rewritten, dict)
        self.assertGreater(rewritten["total_bytes"], 0)
        self.assertIsInstance(rewritten["written_at"], (int, float))

        # Subsequent writes do not perform another walk
        photo5 = self._create_source("photo_5.jpg", color=(40, 60, 80))
        with patch.object(cache2, "_collect_cache_entries", side_effect=spy_collect):
            res5 = cache2.get_or_create(photo5)
            self.assertTrue(res5.ok)

        self.assertEqual(len(walk_calls), 1, "Expected no additional walk on second write")

    def test_implausible_ledger_negative_or_non_numeric_falls_back_to_walk(self) -> None:
        cache1 = ThumbnailCache(cache_dir=self.cache_dir, default_size=(64, 64))
        cache1.get_or_create(self._create_source("base.jpg"))
        cache1.evict()

        bad_payloads = [
            {"version": 1, "total_bytes": -500, "writes_since_recount": 1, "written_at": 100.0},
            {"version": 1, "total_bytes": "not-a-number", "writes_since_recount": 1},
            {"version": 1, "total_bytes": True, "writes_since_recount": 1},
            {"version": 1, "total_bytes": float("nan"), "writes_since_recount": 1},
            {"version": 1, "total_bytes": 100, "writes_since_recount": -1},
            {"version": 1, "total_bytes": 100, "writes_since_recount": "bad"},
        ]

        for idx, bad_payload in enumerate(bad_payloads):
            self.ledger_path.write_text(json.dumps(bad_payload), encoding="utf-8")
            instance = ThumbnailCache(cache_dir=self.cache_dir, default_size=(64, 64))
            self.assertIsNone(
                instance._estimated_bytes,
                f"Payload {idx} should have been rejected as implausible: {bad_payload}",
            )

    def test_missing_ledger_falls_back_to_one_walk_and_writes_ledger(self) -> None:
        cache1 = ThumbnailCache(cache_dir=self.cache_dir, default_size=(64, 64))
        cache1.get_or_create(self._create_source("initial.jpg"))
        cache1.evict()
        self.assertTrue(self.ledger_path.is_file())
        self.ledger_path.unlink()
        self.assertFalse(self.ledger_path.exists())

        cache2 = ThumbnailCache(cache_dir=self.cache_dir, default_size=(64, 64))
        self.assertIsNone(cache2._estimated_bytes)

        walk_calls: list[int] = []
        real_collect = cache2._collect_cache_entries

        def spy_collect() -> list[tuple[int, int, Path]]:
            walk_calls.append(1)
            return real_collect()

        with patch.object(cache2, "_collect_cache_entries", side_effect=spy_collect):
            cache2.get_or_create(self._create_source("second.jpg"))

        self.assertEqual(len(walk_calls), 1)
        self.assertTrue(self.ledger_path.is_file())
        data = json.loads(self.ledger_path.read_text(encoding="utf-8"))

    def test_ledger_file_is_never_counted_as_entry_or_in_total_bytes(self) -> None:
        cache = ThumbnailCache(cache_dir=self.cache_dir, default_size=(64, 64))
        sources = [self._create_source(f"count_{i}.jpg") for i in range(4)]
        for src in sources:
            cache.get_or_create(src)
        cache.evict()

        self.assertTrue(self.ledger_path.is_file())
        stats = cache.stats()
        entries = cache._collect_cache_entries()

        # Exactly 4 thumbnail entries, ledger file excluded
        self.assertEqual(stats.entry_count, 4)
        self.assertEqual(len(entries), 4)

        entry_paths = [path.resolve() for _, _, path in entries]
        self.assertNotIn(self.ledger_path.resolve(), entry_paths)
        for path in entry_paths:
            self.assertIn(path.suffix.lower(), {".jpg", ".png"})
            self.assertNotEqual(path.name, LEDGER_FILENAME)

        expected_bytes = sum(size for _, size, _ in entries)
        self.assertEqual(stats.total_bytes, expected_bytes)
        self.assertNotIn(self.ledger_path.stat().st_size, [stats.total_bytes - expected_bytes])

    def test_ledger_file_is_never_evicted(self) -> None:
        cache = ThumbnailCache(cache_dir=self.cache_dir, default_size=(64, 64))
        for i in range(3):
            cache.get_or_create(self._create_source(f"evict_me_{i}.jpg"))
        cache.evict()
        self.assertTrue(self.ledger_path.is_file())

        # Clear evicts all entries down to 0 bytes
        clear_result = cache.clear()
        self.assertEqual(clear_result.evicted_count, 3)
        self.assertEqual(clear_result.remaining_count, 0)
        self.assertEqual(clear_result.remaining_bytes, 0)

        # Ledger file must NOT be deleted
        self.assertTrue(self.ledger_path.is_file(), "Ledger file was unexpectedly deleted during cache clear")
        data = json.loads(self.ledger_path.read_text(encoding="utf-8"))
        self.assertEqual(data["total_bytes"], 0)

    def test_cache_over_budget_evicts_down_to_budget_when_seeded_from_under_reporting_ledger(self) -> None:
        # Create 6 photos and thumbnails
        cache1 = ThumbnailCache(cache_dir=self.cache_dir, default_size=(128, 128))
        sources = [self._create_source(f"budget_{i}.jpg", size=(400, 400)) for i in range(6)]
        for src in sources:
            cache1.get_or_create(src)
        cache1.evict()

        entries = cache1._collect_cache_entries()
        actual_total = sum(size for _, size, _ in entries)
        self.assertGreater(actual_total, 1000)

        # Tamper with the ledger to significantly under-report the cache size (e.g. 500 bytes)
        fake_ledger = {
            "version": 1,
            "total_bytes": 500,
            "writes_since_recount": 1,
            "written_at": time.time(),
        }
        self.ledger_path.write_text(json.dumps(fake_ledger), encoding="utf-8")

        # Set budget lower than actual_total (say 50% of actual)
        budget = int(actual_total * 0.5)
        cache2 = ThumbnailCache(cache_dir=self.cache_dir, max_bytes=budget, default_size=(128, 128))
        self.assertEqual(cache2._estimated_bytes, 500)

        # Trigger eviction either by writes or explicit evict
        evict_res = cache2.evict()
        self.assertGreater(evict_res.evicted_count, 0)
        self.assertLessEqual(evict_res.remaining_bytes, budget)

        # Verify on-disk actual remaining thumbnails conform to the real budget
        remaining_entries = cache2._collect_cache_entries()
        remaining_actual_bytes = sum(size for _, size, _ in remaining_entries)
        self.assertLessEqual(remaining_actual_bytes, budget)
        self.assertEqual(remaining_actual_bytes, evict_res.remaining_bytes)

        # Verify ledger has been updated with the true remaining size
        updated_ledger = json.loads(self.ledger_path.read_text(encoding="utf-8"))
        self.assertEqual(updated_ledger["total_bytes"], remaining_actual_bytes)

    def test_write_over_budget_evicts_down_to_budget_when_seeded_from_under_reporting_ledger(self) -> None:
        # Verify that an under-reporting ledger does not prevent a write from evicting
        # down to the true budget once estimated exceeds max_bytes.
        cache1 = ThumbnailCache(cache_dir=self.cache_dir, default_size=(128, 128))
        sources = [self._create_source(f"wbudget_{i}.jpg", size=(400, 400)) for i in range(5)]
        for src in sources:
            cache1.get_or_create(src)
        cache1.evict()

        entries = cache1._collect_cache_entries()
        actual_total = sum(size for _, size, _ in entries)

        # Set budget lower than actual_total
        budget = int(actual_total * 0.6)
        # Fake ledger reports budget - 50 bytes (under-reporting actual by a lot)
        fake_ledger = {
            "version": 1,
            "total_bytes": budget - 50,
            "writes_since_recount": 2,
            "written_at": time.time(),
        }
        self.ledger_path.write_text(json.dumps(fake_ledger), encoding="utf-8")

        cache2 = ThumbnailCache(cache_dir=self.cache_dir, max_bytes=budget, default_size=(128, 128))
        self.assertEqual(cache2._estimated_bytes, budget - 50)

        # Writing a new photo adds >50 bytes, pushing estimated over budget
        new_photo = self._create_source("push_over_budget.jpg", size=(400, 400))
        res = cache2.get_or_create(new_photo)
        self.assertTrue(res.ok)

        # Eviction must have brought actual real files on disk down to <= budget
        remaining_entries = cache2._collect_cache_entries()
        remaining_actual_bytes = sum(size for _, size, _ in remaining_entries)
        self.assertLessEqual(remaining_actual_bytes, budget)

    def test_periodic_recount_corrects_drift(self) -> None:
        cache = ThumbnailCache(cache_dir=self.cache_dir, default_size=(64, 64))
        # Monkeypatch recount frequency to 4 for deterministic test
        with patch.object(cache, "RECOUNT_EVERY_WRITES", 4):
            walk_calls: list[int] = []
            real_collect = cache._collect_cache_entries

            def spy_collect() -> list[tuple[int, int, Path]]:
                walk_calls.append(1)
                return real_collect()

            # First write walks because cache is new and ledger missing
            with patch.object(cache, "_collect_cache_entries", side_effect=spy_collect):
                cache.get_or_create(self._create_source("drift_0.jpg"))
            self.assertEqual(len(walk_calls), 1)

            # Writes 1, 2, 3, 4 do not walk (increment counter from 0 to 4)
            with patch.object(cache, "_collect_cache_entries", side_effect=spy_collect):
                cache.get_or_create(self._create_source("drift_1.jpg"))
                cache.get_or_create(self._create_source("drift_2.jpg"))
                cache.get_or_create(self._create_source("drift_3.jpg"))
                cache.get_or_create(self._create_source("drift_4.jpg"))
            self.assertEqual(len(walk_calls), 1)

            # 5th write sees counter >= 4 and triggers periodic recount walk
            with patch.object(cache, "_collect_cache_entries", side_effect=spy_collect):
                cache.get_or_create(self._create_source("drift_5.jpg"))
            self.assertEqual(len(walk_calls), 2, "Expected periodic recount walk on 5th write")


    def test_short_lived_instances_still_reach_a_periodic_recount(self) -> None:
        # One instance per scan, each writing far fewer than RECOUNT_EVERY_WRITES
        # thumbnails, must not keep trusting a stale ledger forever.
        walks: list[int] = []
        with patch.object(ThumbnailCache, "RECOUNT_EVERY_WRITES", 8), patch.object(
            ThumbnailCache, "LEDGER_EVERY_WRITES", 2
        ):
            for scan in range(8):
                cache = ThumbnailCache(cache_dir=self.cache_dir, default_size=(64, 64))
                real_collect = cache._collect_cache_entries

                def spy_collect(real=real_collect) -> list[tuple[int, int, Path]]:
                    walks.append(scan)
                    return real()

                with patch.object(cache, "_collect_cache_entries", side_effect=spy_collect):
                    for index in range(2):
                        cache.get_or_create(self._create_source(f"scan{scan}_{index}.jpg", color=(scan * 25, index * 90, 40)))
        # The first scan walks once (no ledger); 16 writes later at least one more recount ran.
        self.assertGreaterEqual(len(walks), 2)
        self.assertGreater(walks[-1], 0)


if __name__ == "__main__":
    unittest.main()
