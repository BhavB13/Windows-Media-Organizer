"""Unit tests for catalog perceptual hash storage and similarity queries (Task W17)."""

from __future__ import annotations

import io
import math
import os
from pathlib import Path
import random
import sqlite3
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

from PIL import Image, ImageDraw

_SOURCE_ROOT = Path(__file__).resolve().parents[1] / "src"
if str(_SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SOURCE_ROOT))

from duplicate_transfer_manager.index import (
    INITIAL_SCHEMA_VERSION,
    SCHEMA_VERSION,
    CatalogFileInput,
    CatalogFileRecord,
    MediaCatalog,
    initialize_schema,
)
from duplicate_transfer_manager.index.catalog import (
    SimilarCatalogGroup,
    SimilarMatch,
    banded_candidate_search,
    partition_bit_slices,
)
from duplicate_transfer_manager.media import perceptual


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


class TestCatalogSimilarity(unittest.TestCase):
    def test_migration_from_v2_preserves_data_and_adds_perceptual_columns(self) -> None:
        """Upgrading from v2 preserves existing rows and adds perceptual hash columns and index."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            db_path = Path(tmp_dir) / "catalog_v2.sqlite3"
            raw_conn = sqlite3.connect(str(db_path), isolation_level=None)
            try:
                version = initialize_schema(raw_conn, target_version=2)
                self.assertEqual(version, 2)
                raw_conn.execute("BEGIN IMMEDIATE")
                raw_conn.execute(
                    "INSERT INTO volumes (volume_key, label, filesystem, last_seen, root_path, is_online) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    ("vol-upgrade", "PhotoDrive", "NTFS", 1700000000.0, "/mnt/photos", 1),
                )
                cursor = raw_conn.execute(
                    """
                    INSERT INTO files (
                        volume_key, relative_path, filename, extension,
                        size, mtime_ns, content_hash, media_kind, indexed_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        "vol-upgrade",
                        "2023/beach.jpg",
                        "beach.jpg",
                        ".jpg",
                        8192,
                        999888777,
                        "sha256contenthash",
                        "image",
                        1700000010.0,
                    ),
                )
                file_id = int(cursor.lastrowid)
                raw_conn.execute(
                    "INSERT INTO metadata (file_id, capture_timestamp, capture_year, camera_make) "
                    "VALUES (?, ?, ?, ?)",
                    (file_id, 1680000000.0, 2023, "Nikon"),
                )
                raw_conn.execute("COMMIT")
            finally:
                raw_conn.close()

            with MediaCatalog(db_path) as catalog:
                self.assertEqual(catalog.user_version, SCHEMA_VERSION)
                self.assertGreaterEqual(catalog.user_version, 3)

                vol = catalog.get_volume("vol-upgrade")
                self.assertIsNotNone(vol)
                assert vol is not None
                self.assertEqual(vol.label, "PhotoDrive")
                self.assertTrue(vol.is_online)

                rec = catalog.get_file("vol-upgrade", "2023/beach.jpg")
                self.assertIsNotNone(rec)
                assert rec is not None
                self.assertEqual(rec.filename, "beach.jpg")
                self.assertEqual(rec.size, 8192)
                self.assertEqual(rec.content_hash, "sha256contenthash")
                self.assertEqual(rec.camera_make, "Nikon")
                self.assertEqual(rec.capture_year, 2023)
                self.assertIsNone(rec.perceptual_hash)

                col_info = catalog.connection.execute("PRAGMA table_info(files)").fetchall()
                col_names = {str(row[1]) for row in col_info}
                self.assertIn("perceptual_hash", col_names)
                self.assertIn("perceptual_algo", col_names)
                self.assertIn("perceptual_rotations", col_names)

                indexes = {
                    str(row[0])
                    for row in catalog.connection.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'index'"
                    ).fetchall()
                }
                self.assertIn("idx_files_perceptual_hash", indexes)

    def test_compute_perceptual_stores_hashes_and_skips_unchanged_on_second_scan(self) -> None:
        """compute_perceptual=True hashes images; second upsert of unchanged files does not decode."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            media_dir = Path(tmp_dir) / "media"
            media_dir.mkdir()
            db_path = Path(tmp_dir) / "catalog.sqlite3"

            orig_img = _create_smooth_image()
            orig_path = media_dir / "landscape.png"
            orig_img.save(orig_path)

            resized_img = orig_img.resize((160, 160), Image.Resampling.LANCZOS)
            resized_path = media_dir / "landscape_thumb.png"
            resized_img.save(resized_path)

            text_path = media_dir / "notes.txt"
            text_path.write_text("not an image file", encoding="utf-8")

            with MediaCatalog(db_path) as catalog:
                res1 = catalog.upsert_files(
                    "vol_photos",
                    volume_root=media_dir,
                    compute_perceptual=True,
                )
                self.assertEqual(res1.added, 3)

                rec_orig = catalog.get_file("vol_photos", "landscape.png")
                self.assertIsNotNone(rec_orig)
                assert rec_orig is not None
                self.assertIsNotNone(rec_orig.perceptual_hash)
                self.assertEqual(rec_orig.perceptual_algo, "phash")

                rec_txt = catalog.get_file("vol_photos", "notes.txt")
                self.assertIsNotNone(rec_txt)
                assert rec_txt is not None
                self.assertIsNone(rec_txt.perceptual_hash)

                with patch(
                    "duplicate_transfer_manager.media.perceptual.compute_image_hash",
                    wraps=perceptual.compute_image_hash,
                ) as mock_hasher:
                    res2 = catalog.upsert_files(
                        "vol_photos",
                        volume_root=media_dir,
                        compute_perceptual=True,
                    )
                    self.assertEqual(res2.unchanged, 3)
                    self.assertEqual(res2.added, 0)
                    self.assertEqual(res2.updated, 0)
                    self.assertEqual(mock_hasher.call_count, 0)

    def test_undecodable_file_gets_no_hash_and_does_not_fail_batch(self) -> None:
        """A corrupt or unreadable image file gets no perceptual hash and does not fail the batch."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            media_dir = Path(tmp_dir) / "media"
            media_dir.mkdir()
            db_path = Path(tmp_dir) / "catalog.sqlite3"

            corrupt_path = media_dir / "corrupt.jpg"
            corrupt_path.write_bytes(b"invalid garbage header not a jpeg")

            valid_img = _create_smooth_image()
            valid_path = media_dir / "valid.jpg"
            valid_img.save(valid_path, format="JPEG")

            with MediaCatalog(db_path) as catalog:
                res = catalog.upsert_files(
                    "vol_test",
                    volume_root=media_dir,
                    compute_perceptual=True,
                )
                self.assertEqual(res.added, 2)

                rec_corrupt = catalog.get_file("vol_test", "corrupt.jpg")
                self.assertIsNotNone(rec_corrupt)
                assert rec_corrupt is not None
                self.assertIsNone(rec_corrupt.perceptual_hash)

                rec_valid = catalog.get_file("vol_test", "valid.jpg")
                self.assertIsNotNone(rec_valid)
                assert rec_valid is not None
                self.assertIsNotNone(rec_valid.perceptual_hash)

    def test_64bit_hashes_with_top_bit_set_round_trip_exactly(self) -> None:
        """Unsigned 64-bit perceptual hashes with the MSB set round-trip without sign errors."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            db_path = Path(tmp_dir) / "catalog.sqlite3"

            hash_top_bit = 0x8000000000000001
            hash_all_ones = 0xFFFFFFFFFFFFFFFF
            hash_mid = 0x8FFFFFFFFFFFFFFF

            with MediaCatalog(db_path) as catalog:
                catalog.upsert_files(
                    "vol_hashes",
                    [
                        CatalogFileInput(
                            relative_path="top_bit.jpg",
                            size=1024,
                            mtime_ns=1000,
                            media_kind="image",
                            perceptual_hash=hash_top_bit,
                        ),
                        CatalogFileInput(
                            relative_path="all_ones.jpg",
                            size=2048,
                            mtime_ns=2000,
                            media_kind="image",
                            perceptual_hash=hash_all_ones,
                        ),
                        CatalogFileInput(
                            relative_path="mid.jpg",
                            size=4096,
                            mtime_ns=3000,
                            media_kind="image",
                            perceptual_hash=hash_mid,
                        ),
                    ],
                )

                rec1 = catalog.get_file("vol_hashes", "top_bit.jpg")
                self.assertIsNotNone(rec1)
                assert rec1 is not None
                self.assertEqual(rec1.perceptual_bits, hash_top_bit)
                self.assertEqual(int(rec1.perceptual_hash or "", 16), hash_top_bit)
                self.assertEqual(
                    perceptual.ImageHash.from_hex(rec1.perceptual_hash or "").bits,
                    hash_top_bit,
                )

                rec2 = catalog.get_file("vol_hashes", "all_ones.jpg")
                self.assertIsNotNone(rec2)
                assert rec2 is not None
                self.assertEqual(rec2.perceptual_bits, hash_all_ones)
                self.assertEqual(int(rec2.perceptual_hash or "", 16), hash_all_ones)

                rec3 = catalog.get_file("vol_hashes", "mid.jpg")
                self.assertIsNotNone(rec3)
                assert rec3 is not None
                self.assertEqual(rec3.perceptual_bits, hash_mid)

    def test_find_similar_returns_resized_and_recompressed_not_unrelated(self) -> None:
        """find_similar returns resized and recompressed copies within max_distance, excluding unrelated."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            media_dir = Path(tmp_dir) / "media"
            media_dir.mkdir()
            db_path = Path(tmp_dir) / "catalog.sqlite3"

            orig_img = _create_smooth_image()
            orig_path = media_dir / "original.png"
            orig_img.save(orig_path)

            resized_img = orig_img.resize((180, 180), Image.Resampling.LANCZOS)
            resized_path = media_dir / "resized.png"
            resized_img.save(resized_path)

            recomp_path = media_dir / "recompressed.jpg"
            orig_img.save(recomp_path, format="JPEG", quality=80)

            unrelated_img = _create_unrelated_image()
            unrelated_path = media_dir / "unrelated.png"
            unrelated_img.save(unrelated_path)

            with MediaCatalog(db_path) as catalog:
                catalog.upsert_files(
                    "vol_photos",
                    volume_root=media_dir,
                    compute_perceptual=True,
                )

                rec_orig = catalog.get_file("vol_photos", "original.png")
                self.assertIsNotNone(rec_orig)
                assert rec_orig is not None

                matches = catalog.find_similar(rec_orig.id, max_distance=8)
                match_paths = [m.relative_path for m in matches]

                self.assertIn("resized.png", match_paths)
                self.assertIn("recompressed.jpg", match_paths)
                self.assertNotIn("unrelated.png", match_paths)
                self.assertNotIn("original.png", match_paths)

                distances = [m.distance for m in matches]
                self.assertEqual(distances, sorted(distances))
                for dist in distances:
                    self.assertLessEqual(dist, 8)

                for match in matches:
                    self.assertIsInstance(match, tuple)
                    self.assertIsInstance(match, SimilarMatch)
                    self.assertIsInstance(match.record, CatalogFileRecord)
                    self.assertEqual(match[0].id, match.record.id)
                    self.assertEqual(match[1], match.distance)

                limited = catalog.find_similar("original.png", max_distance=8, limit=1)
                self.assertEqual(len(limited), 1)

    def test_similar_groups_is_order_independent(self) -> None:
        """similar_groups builds transitive, canonical clusters regardless of row insertion order."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            db_path1 = Path(tmp_dir) / "catalog1.sqlite3"
            db_path2 = Path(tmp_dir) / "catalog2.sqlite3"

            orig_img = _create_smooth_image()
            h_orig = perceptual.compute_image_hash(orig_img, "phash")

            resized_img = orig_img.resize((170, 170), Image.Resampling.LANCZOS)
            h_resized = perceptual.compute_image_hash(resized_img, "phash")

            buf = io.BytesIO()
            orig_img.save(buf, format="JPEG", quality=80)
            buf.seek(0)
            recomp_img = Image.open(buf)
            h_recomp = perceptual.compute_image_hash(recomp_img, "phash")

            unrelated_img = _create_unrelated_image()
            h_unrelated = perceptual.compute_image_hash(unrelated_img, "phash")

            files_order1 = [
                CatalogFileInput(relative_path="a_orig.png", size=1000, mtime_ns=10, media_kind="image", perceptual_hash=h_orig.bits),
                CatalogFileInput(relative_path="b_resized.png", size=500, mtime_ns=10, media_kind="image", perceptual_hash=h_resized.bits),
                CatalogFileInput(relative_path="c_recomp.jpg", size=800, mtime_ns=10, media_kind="image", perceptual_hash=h_recomp.bits),
                CatalogFileInput(relative_path="z_unrelated.png", size=1200, mtime_ns=10, media_kind="image", perceptual_hash=h_unrelated.bits),
            ]

            files_order2 = list(reversed(files_order1))

            with MediaCatalog(db_path1) as cat1:
                cat1.upsert_files("vol", files_order1)
                groups1 = cat1.similar_groups(max_distance=8)

            with MediaCatalog(db_path2) as cat2:
                cat2.upsert_files("vol", files_order2)
                groups2 = cat2.similar_groups(max_distance=8)

            self.assertEqual(len(groups1), 1)
            self.assertEqual(len(groups2), 1)
            self.assertEqual(groups1[0].paths, groups2[0].paths)
            self.assertEqual(
                groups1[0].paths,
                ("a_orig.png", "b_resized.png", "c_recomp.jpg"),
            )

    def test_similar_groups_exclude_exact_duplicates(self) -> None:
        """exclude_exact=True skips files already identical by content_hash."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            db_path = Path(tmp_dir) / "catalog.sqlite3"

            hash_val = 0x1122334455667788

            files = [
                CatalogFileInput(
                    relative_path="photo_copy1.jpg",
                    size=1000,
                    mtime_ns=10,
                    content_hash="sha_exact_match",
                    media_kind="image",
                    perceptual_hash=hash_val,
                ),
                CatalogFileInput(
                    relative_path="photo_copy2.jpg",
                    size=1000,
                    mtime_ns=10,
                    content_hash="sha_exact_match",
                    media_kind="image",
                    perceptual_hash=hash_val,
                ),
                CatalogFileInput(
                    relative_path="photo_resized.jpg",
                    size=500,
                    mtime_ns=20,
                    content_hash="sha_different_bytes",
                    media_kind="image",
                    perceptual_hash=hash_val ^ 1,
                ),
            ]

            with MediaCatalog(db_path) as catalog:
                catalog.upsert_files("vol", files)

                groups_all = catalog.similar_groups(max_distance=8, exclude_exact=False)
                self.assertEqual(len(groups_all), 1)
                self.assertEqual(len(groups_all[0]), 3)

                groups_filtered = catalog.similar_groups(max_distance=8, exclude_exact=True)
                self.assertEqual(len(groups_filtered), 1)
                self.assertEqual(len(groups_filtered[0]), 2)
                self.assertEqual(
                    groups_filtered[0].paths,
                    ("photo_copy1.jpg", "photo_resized.jpg"),
                )

    def test_offline_volume_rows_are_returned_with_offline_state(self) -> None:
        """Offline volume rows remain searchable for similarity and carry offline volume status."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            db_path = Path(tmp_dir) / "catalog.sqlite3"

            shared_hash = 0x5555555555555555

            with MediaCatalog(db_path) as catalog:
                catalog.upsert_files(
                    "vol_external",
                    [
                        CatalogFileInput(
                            relative_path="vacation/pic1.jpg",
                            size=2048,
                            mtime_ns=1000,
                            media_kind="image",
                            perceptual_hash=shared_hash,
                        ),
                        CatalogFileInput(
                            relative_path="vacation/pic2.jpg",
                            size=2048,
                            mtime_ns=1000,
                            media_kind="image",
                            perceptual_hash=shared_hash ^ 2,
                        ),
                    ],
                )

                catalog.mark_volume_offline("vol_external")
                vol = catalog.get_volume("vol_external")
                self.assertIsNotNone(vol)
                assert vol is not None
                self.assertFalse(vol.is_online)
                self.assertEqual(vol.status, "offline")

                matches = catalog.find_similar("vacation/pic1.jpg", max_distance=8)
                self.assertEqual(len(matches), 1)
                match = matches[0]
                self.assertEqual(match.relative_path, "vacation/pic2.jpg")
                self.assertFalse(match.record.volume_online)
                self.assertEqual(match.record.volume_status, "offline")

                groups = catalog.similar_groups(max_distance=8)
                self.assertEqual(len(groups), 1)
                for rec in groups[0]:
                    self.assertFalse(rec.volume_online)
                    self.assertEqual(rec.volume_status, "offline")

    def test_banded_candidate_search_matches_brute_force_reference(self) -> None:
        """Multi-index pigeonhole banded search matches brute-force O(N^2) results on synthetic hashes."""
        random.seed(1337)
        num_hashes = 300
        synthetic_hashes: list[int] = [random.getrandbits(64) for _ in range(num_hashes)]

        # Inject controlled near-neighbours at known Hamming distances
        for i in range(20):
            base = synthetic_hashes[i]
            synthetic_hashes.append(base ^ 1)
            synthetic_hashes.append(base ^ (1 | (1 << 12) | (1 << 34)))
            synthetic_hashes.append(base ^ (1 | (1 << 8) | (1 << 24) | (1 << 48) | (1 << 60)))

        def brute_force_reference(hashes: list[int], max_dist: int) -> set[tuple[int, int]]:
            matches = set()
            count = len(hashes)
            for i in range(count):
                for j in range(i + 1, count):
                    if (hashes[i] ^ hashes[j]).bit_count() <= max_dist:
                        matches.add((i, j))
            return matches

        for distance in (0, 1, 4, 8, 12):
            brute_pairs = brute_force_reference(synthetic_hashes, distance)
            banded_pairs = banded_candidate_search(synthetic_hashes, max_distance=distance)
            self.assertEqual(
                banded_pairs,
                brute_pairs,
                f"Mismatch between banded and brute-force candidate search at max_distance={distance}",
            )

    def test_cancellation_honours_stop_event_promptly(self) -> None:
        """upsert_files, find_similar, and similar_groups terminate promptly when stop_event is set."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            media_dir = Path(tmp_dir) / "media"
            media_dir.mkdir()
            db_path = Path(tmp_dir) / "catalog.sqlite3"

            for i in range(5):
                img = _create_smooth_image(phase=float(i))
                img.save(media_dir / f"img_{i}.png")

            stop_evt = threading.Event()
            stop_evt.set()

            with MediaCatalog(db_path) as catalog:
                res = catalog.upsert_files(
                    "vol_cancel",
                    volume_root=media_dir,
                    compute_perceptual=True,
                    stop_event=stop_evt,
                )
                self.assertTrue(res.cancelled)

                stop_evt.clear()
                catalog.upsert_files(
                    "vol_cancel",
                    volume_root=media_dir,
                    compute_perceptual=True,
                )

                stop_evt.set()
                similar_matches = catalog.find_similar("img_0.png", stop_event=stop_evt)
                self.assertEqual(similar_matches, [])

                groups = catalog.similar_groups(stop_event=stop_evt)
                self.assertEqual(groups, [])


if __name__ == "__main__":
    unittest.main()
