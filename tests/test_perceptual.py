"""Unit tests for the framework-neutral perceptual image similarity engine."""

from __future__ import annotations

import importlib.util
import math
import os
import random
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

_SRC_ROOT = Path(__file__).resolve().parents[1] / "src"
if str(_SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(_SRC_ROOT))
if "duplicate_transfer_manager" in sys.modules:
    _dtm_pkg = sys.modules["duplicate_transfer_manager"]
    _local_pkg_dir = str(_SRC_ROOT / "duplicate_transfer_manager")
    if hasattr(_dtm_pkg, "__path__") and _local_pkg_dir not in _dtm_pkg.__path__:
        _dtm_pkg.__path__.insert(0, _local_pkg_dir)

from PIL import Image, ImageDraw

from duplicate_transfer_manager.media import (
     HASH_BITS,
    CandidateScore,
    HashedImageEntry,
    ImageHash,
    average_hash,
    compute_image_hash,
    difference_hash,
    find_similar_groups,
    hash_images,
    min_rotation_distance,
    orientation_invariant_hashes,
    perceptual_hash,
    recommend_keeper,
)
import duplicate_transfer_manager.media.perceptual as perceptual_module


def _draw_scene_a(size: tuple[int, int] = (256, 256)) -> Image.Image:
    """Create a deterministic asymmetric synthetic landscape-like test image."""
    width, height = size
    img = Image.new("RGB", (width, height))
    pixels = img.load()
    assert pixels is not None
    for y in range(height):
        for x in range(width):
            # Smooth asymmetric spatial gradients + low-frequency cosine wave
            u = x / max(1, width - 1)
            v = y / max(1, height - 1)
            r = int(40 + 180 * u * (1.0 - 0.3 * v))
            g = int(30 + 190 * v + 25 * math.cos(2.0 * math.pi * u))
            b = int(50 + 150 * (1.0 - u) * v)
            pixels[x, y] = (
                max(0, min(255, r)),
                max(0, min(255, g)),
                max(0, min(255, b)),
            )
    draw = ImageDraw.Draw(img)
    # Asymmetric bright landmark in top-left quadrant and dark block in bottom-right
    draw.ellipse(
        (int(width * 0.12), int(height * 0.15), int(width * 0.42), int(height * 0.45)),
        fill=(245, 240, 220),
    )
    draw.rectangle(
        (int(width * 0.58), int(height * 0.55), int(width * 0.88), int(height * 0.85)),
        fill=(15, 25, 35),
    )
    return img


def _draw_scene_b(size: tuple[int, int] = (256, 256)) -> Image.Image:
    """Create a genuinely different synthetic pattern with opposite spatial structure."""
    width, height = size
    img = Image.new("RGB", (width, height), (240, 240, 240))
    draw = ImageDraw.Draw(img)
    # High-contrast concentric rings and top-right dark triangle
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


class CountdownStopEvent:
    """Test helper that sets itself after ``trigger_after`` checks."""

    def __init__(self, trigger_after: int) -> None:
        self.remaining = trigger_after
        self.calls = 0

    def is_set(self) -> bool:
        self.calls += 1
        if self.remaining <= 0:
            return True
        self.remaining -= 1
        return False


class PerceptualSimilarityTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self._temp_dir.name)

    def tearDown(self) -> None:
        self._temp_dir.cleanup()

    def test_identical_images_hash_identically(self) -> None:
        img = _draw_scene_a((160, 160))
        path_a = self.root / "copy_a.png"
        path_b = self.root / "copy_b.png"
        img.save(path_a, format="PNG")
        img.save(path_b, format="PNG")

        for fn, expected_algo in (
            (average_hash, "ahash"),
            (difference_hash, "dhash"),
            (perceptual_hash, "phash"),
        ):
            hash_a = fn(path_a)
            hash_b = fn(path_b)
            self.assertEqual(hash_a.algorithm, expected_algo)
            self.assertEqual(hash_a.bit_count, HASH_BITS)
            self.assertEqual(hash_a, hash_b)
            self.assertEqual(hash_a.hamming_distance(hash_b), 0)

    def test_resized_copy_is_within_threshold(self) -> None:
        original = _draw_scene_a((256, 256))
        resized = original.resize((96, 96), Image.Resampling.LANCZOS)
        orig_path = self.root / "orig.png"
        resized_path = self.root / "resized.png"
        original.save(orig_path, format="PNG")
        resized.save(resized_path, format="PNG")

        for algo in ("ahash", "dhash", "phash"):
            h_orig = compute_image_hash(orig_path, algo)
            h_resized = compute_image_hash(resized_path, algo)
            distance = h_orig.hamming_distance(h_resized)
            self.assertLessEqual(distance, 4, f"{algo} distance too large for resized copy: {distance}")

        grouped = find_similar_groups([orig_path, resized_path], max_distance=6, algorithm="phash")
        self.assertEqual(len(grouped.groups), 1)
        self.assertEqual(grouped.groups[0].paths, (str(orig_path), str(resized_path)))

    def test_recompressed_copy_is_within_threshold(self) -> None:
        original = _draw_scene_a((200, 200))
        png_path = self.root / "lossless.png"
        jpg_path = self.root / "recompressed.jpg"
        original.save(png_path, format="PNG")
        original.save(jpg_path, format="JPEG", quality=28)

        for algo in ("ahash", "dhash", "phash"):
            h_png = compute_image_hash(png_path, algo)
            h_jpg = compute_image_hash(jpg_path, algo)
            distance = h_png.hamming_distance(h_jpg)
            self.assertLessEqual(distance, 4, f"{algo} distance too large for JPEG copy: {distance}")

        grouped = find_similar_groups([png_path, jpg_path], max_distance=6, algorithm="phash")
        self.assertEqual(len(grouped.groups), 1)
        self.assertEqual(set(grouped.groups[0].paths), {str(png_path), str(jpg_path)})

    def test_rotated_copy_matched_by_orientation_invariant_path_and_exif(self) -> None:
        original = _draw_scene_a((192, 192))
        orig_path = self.root / "upright.png"
        rot90_path = self.root / "rotated_90.png"
        rot180_path = self.root / "rotated_180.png"
        rot270_path = self.root / "rotated_270.png"
        exif_rot_path = self.root / "exif_oriented.jpg"

        original.save(orig_path, format="PNG")
        original.transpose(Image.Transpose.ROTATE_90).save(rot90_path, format="PNG")
        original.transpose(Image.Transpose.ROTATE_180).save(rot180_path, format="PNG")
        original.transpose(Image.Transpose.ROTATE_270).save(rot270_path, format="PNG")

        # Store pixels rotated 90 degrees CCW (ROTATE_90), with EXIF Orientation=6 (rotate 90 CW / ROTATE_270 to restore)
        exif = Image.Exif()
        exif[0x0112] = 6
        original.transpose(Image.Transpose.ROTATE_90).save(exif_rot_path, format="JPEG", quality=95, exif=exif)

        orig_hashes = orientation_invariant_hashes(orig_path, algorithm="phash")
        self.assertEqual(len(orig_hashes), 4)

        for rot_path in (rot90_path, rot180_path, rot270_path):
            rot_hashes = orientation_invariant_hashes(rot_path, algorithm="phash")
            self.assertEqual(min_rotation_distance(orig_hashes, rot_hashes), 0)
            self.assertIn(perceptual_hash(rot_path), orig_hashes)

        # Verify EXIF orientation tag is honoured when loading
        exif_hash = perceptual_hash(exif_rot_path)
        self.assertLessEqual(orig_hashes[0].hamming_distance(exif_hash), 2)

        # Verify find_similar_groups groups rotated copies when using orientation_invariant_hashes
        grouped = find_similar_groups(
            [orig_path, rot90_path, rot180_path, rot270_path, exif_rot_path],
            max_distance=4,
            algorithm="phash",
            rotation_invariant=True,
        )
        self.assertEqual(len(grouped.groups), 1)
        self.assertEqual(len(grouped.groups[0].paths), 5)

    def test_genuinely_different_images_are_not_grouped(self) -> None:
        img_a = _draw_scene_a((200, 200))
        img_b = _draw_scene_b((200, 200))
        path_a = self.root / "scene_a.png"
        path_b = self.root / "scene_b.png"
        img_a.save(path_a, format="PNG")
        img_b.save(path_b, format="PNG")

        for algo in ("ahash", "dhash", "phash"):
            ha = compute_image_hash(path_a, algo)
            hb = compute_image_hash(path_b, algo)
            self.assertGreater(ha.hamming_distance(hb), 12)
            grouped = find_similar_groups([path_a, path_b], max_distance=8, algorithm=algo)
            self.assertEqual(len(grouped.groups), 0)

    def test_hex_round_trip(self) -> None:
        for algo in ("ahash", "dhash", "phash"):
            for value in (0, 1, 0x000F123456789ABC, 0xFEDCBA9876543210, (1 << 64) - 1):
                original = ImageHash(algorithm=algo, bits=value, bit_count=64)
                hex_str = original.to_hex()
                self.assertEqual(len(hex_str), 16)
                # Round-trip both with and without explicit algorithm argument
                restored_auto = ImageHash.from_hex(hex_str)
                restored_explicit = ImageHash.from_hex(str(hex_str), algorithm=algo)
                self.assertEqual(restored_auto, original)
                self.assertEqual(restored_explicit, original)
                self.assertEqual(restored_auto.to_hex(), hex_str)

        with self.assertRaises(ValueError):
            ImageHash.from_hex("not-a-hex-string", algorithm="phash")
        with self.assertRaises(ValueError):
            ImageHash.from_hex("", algorithm="phash")

    def test_cross_algorithm_comparison_raises(self) -> None:
        h_avg = ImageHash(algorithm="ahash", bits=0x1234, bit_count=64)
        h_diff = ImageHash(algorithm="dhash", bits=0x1234, bit_count=64)
        h_dct = ImageHash(algorithm="phash", bits=0x1234, bit_count=64)

        with self.assertRaises(ValueError):
            h_avg.hamming_distance(h_diff)
        with self.assertRaises(ValueError):
            h_diff.hamming_distance(h_dct)
        with self.assertRaises(ValueError):
            h_avg.hamming_distance(h_dct)
        with self.assertRaises(ValueError):
            find_similar_groups([("a.png", h_avg), ("b.png", h_dct)], max_distance=4, algorithm="phash")

    def test_grouping_is_transitive_and_deterministic_across_input_orderings(self) -> None:
        # Chain A -- B -- C where dist(A, B) = 3, dist(B, C) = 3, dist(A, C) = 6 (> max_distance=3)
        hash_a = ImageHash(algorithm="phash", bits=0b000000, bit_count=64)
        hash_b = ImageHash(algorithm="phash", bits=0b000111, bit_count=64)
        hash_c = ImageHash(algorithm="phash", bits=0b111111, bit_count=64)
        # Separate pair D -- E far away from A/B/C (16 bits away from A/B/C)
        hash_d = ImageHash(algorithm="phash", bits=0xFFFF000000000000, bit_count=64)
        hash_e = ImageHash(algorithm="phash", bits=0xFFFF000000000000 | (0b11 << 32), bit_count=64)
        # Singleton F that matches nothing
        hash_f = ImageHash(algorithm="phash", bits=0xAAAAAAAAAAAAAAAA, bit_count=64)

        base_entries = [
            ("img_a.jpg", hash_a),
            ("img_b.jpg", hash_b),
            ("img_c.jpg", hash_c),
            ("img_d.jpg", hash_d),
            ("img_e.jpg", hash_e),
            ("img_f.jpg", hash_f),
        ]

        expected_groups = (
            ("img_a.jpg", "img_b.jpg", "img_c.jpg"),
            ("img_d.jpg", "img_e.jpg"),
        )

        rng = random.Random(42)
        for _ in range(12):
            shuffled = list(base_entries)
            rng.shuffle(shuffled)
            result = find_similar_groups(shuffled, max_distance=3, algorithm="phash")
            actual = tuple(group.paths for group in result.groups)
            self.assertEqual(actual, expected_groups)

    def test_keep_recommendation_hierarchy_and_deterministic_ties(self) -> None:
        # 1. Higher resolution beats larger byte size / EXIF
        hi_res_path = self.root / "z_high_res.jpg"
        lo_res_path = self.root / "a_low_res.png"
        _draw_scene_a((320, 240)).save(hi_res_path, format="JPEG", quality=60)
        _draw_scene_a((160, 120)).save(lo_res_path, format="PNG")

        rec_res = recommend_keeper([lo_res_path, hi_res_path])
        self.assertEqual(rec_res.keeper, str(hi_res_path))
        self.assertIn("resolution", rec_res.reason_for(lo_res_path).lower())

        # 2. Equal resolution: larger byte size (less lossy compression) wins
        hi_quality_path = self.root / "b_hq.jpg"
        lo_quality_path = self.root / "a_lq.jpg"
        scene = _draw_scene_a((200, 200))
        scene.save(hi_quality_path, format="JPEG", quality=95)
        scene.save(lo_quality_path, format="JPEG", quality=20)
        self.assertGreater(hi_quality_path.stat().st_size, lo_quality_path.stat().st_size)

        rec_size = recommend_keeper([lo_quality_path, hi_quality_path])
        self.assertEqual(rec_size.keeper, str(hi_quality_path))
        self.assertIn("byte size", rec_size.reason_for(lo_quality_path).lower())

        # 3. Equal resolution and byte size: EXIF capture metadata wins
        with_exif = CandidateScore(
            path=str(self.root / "z_with_exif.jpg"),
            width=200,
            height=200,
            pixel_count=40000,
            byte_size=50000,
            has_exif_capture=True,
            mtime=2000.0,
        )
        without_exif = CandidateScore(
            path=str(self.root / "a_no_exif.jpg"),
            width=200,
            height=200,
            pixel_count=40000,
            byte_size=50000,
            has_exif_capture=False,
            mtime=1000.0,
        )
        rec_exif = recommend_keeper([without_exif, with_exif])
        self.assertEqual(rec_exif.keeper, with_exif.path)
        self.assertIn("exif", rec_exif.reason_for(without_exif.path).lower())

        # Also verify real on-disk EXIF capture metadata extraction via Pillow
        exif_disk_path = self.root / "real_exif.jpg"
        exif_data = Image.Exif()
        exif_data[36867] = "2025:06:15 10:20:30"
        scene.save(exif_disk_path, format="JPEG", quality=90, exif=exif_data)
        rec_real_exif = recommend_keeper([exif_disk_path])
        self.assertIsNotNone(rec_real_exif.keeper_score)
        assert rec_real_exif.keeper_score is not None
        self.assertTrue(rec_real_exif.keeper_score.has_exif_capture)

        # 4. Equal resolution, byte size, and EXIF: earliest mtime wins
        early_tie = self.root / "z_early.png"
        late_tie = self.root / "a_late.png"
        scene.save(early_tie, format="PNG")
        scene.save(late_tie, format="PNG")
        os.utime(early_tie, (1700000000.0, 1700000000.0))
        os.utime(late_tie, (1700000500.0, 1700000500.0))

        rec_mtime = recommend_keeper([late_tie, early_tie])
        self.assertEqual(rec_mtime.keeper, str(early_tie))
        self.assertIn("modification time", rec_mtime.reason_for(late_tie).lower())

        # 5. Complete tie on all metrics: full path tie-break is deterministic
        tie_a = self.root / "alpha.png"
        tie_b = self.root / "beta.png"
        scene.save(tie_a, format="PNG")
        scene.save(tie_b, format="PNG")
        os.utime(tie_a, (1700001000.0, 1700001000.0))
        os.utime(tie_b, (1700001000.0, 1700001000.0))

        rec_tie_1 = recommend_keeper([tie_b, tie_a])
        rec_tie_2 = recommend_keeper([tie_a, tie_b])
        self.assertEqual(rec_tie_1.keeper, str(tie_a))
        self.assertEqual(rec_tie_2.keeper, str(tie_a))
        self.assertIn("tie-breaker", rec_tie_1.reason_for(tie_b).lower())

        # Verify no files were deleted, moved, or mutated
        for p in (hi_res_path, lo_res_path, hi_quality_path, lo_quality_path, early_tie, late_tie, tie_a, tie_b):
            self.assertTrue(p.is_file(), f"File should never be deleted: {p}")

    def test_cancellation_returns_early(self) -> None:
        img = _draw_scene_a((128, 128))
        paths = []
        for idx in range(5):
            p = self.root / f"img_{idx}.png"
            img.save(p, format="PNG")
            paths.append(p)

        pre_set = threading.Event()
        pre_set.set()

        self.assertEqual(orientation_invariant_hashes(paths[0], stop_event=pre_set), ())

        batch_res = hash_images(paths, stop_event=pre_set)
        self.assertTrue(batch_res.cancelled)
        self.assertEqual(len(batch_res.entries), 0)

        group_res = find_similar_groups(paths, stop_event=pre_set)
        self.assertTrue(group_res.cancelled)
        self.assertEqual(len(group_res.groups), 0)

        keep_res = recommend_keeper(paths, stop_event=pre_set)
        self.assertTrue(keep_res.cancelled)
        self.assertEqual(keep_res.keeper, "")

        # Mid-flight cancellation during batch grouping
        countdown = CountdownStopEvent(trigger_after=2)
        mid_group_res = find_similar_groups(paths, stop_event=countdown)
        self.assertTrue(mid_group_res.cancelled)
        self.assertEqual(len(mid_group_res.groups), 0)

    def test_corrupt_file_is_reported_as_error_without_raising(self) -> None:
        img = _draw_scene_a((128, 128))
        valid_a = self.root / "valid_a.png"
        valid_b = self.root / "valid_b.png"
        corrupt = self.root / "corrupt.jpg"
        missing = self.root / "missing.png"

        img.save(valid_a, format="PNG")
        img.save(valid_b, format="PNG")
        corrupt.write_bytes(b"not-a-valid-jpeg-stream-\x00\xff\xd8\x00")

        group_result = find_similar_groups([valid_a, corrupt, valid_b, missing], max_distance=4, algorithm="phash")
        self.assertFalse(group_result.cancelled)
        self.assertEqual(len(group_result.groups), 1)
        self.assertEqual(group_result.groups[0].paths, (str(valid_a), str(valid_b)))
        error_paths = {err.path for err in group_result.errors}
        self.assertEqual(error_paths, {str(corrupt), str(missing)})

        keep_result = recommend_keeper([corrupt, valid_a, valid_b])
        self.assertEqual(keep_result.keeper, str(valid_a))
        self.assertEqual({err.path for err in keep_result.errors}, {str(corrupt)})
        self.assertIn(str(corrupt), keep_result.reasons)

    def test_module_imports_cleanly_when_pillow_is_absent(self) -> None:
        module_path = Path(perceptual_module.__file__).resolve()
        spec = importlib.util.spec_from_file_location("perceptual_no_pil_test", module_path)
        self.assertIsNotNone(spec)
        assert spec is not None and spec.loader is not None
        isolated_module = importlib.util.module_from_spec(spec)

        with mock.patch.dict(
            sys.modules,
            {"PIL": None, "PIL.Image": None, "perceptual_no_pil_test": isolated_module},
        ):
            spec.loader.exec_module(isolated_module)

        self.assertIsNone(isolated_module.Image)
        # Pure-Python ImageHash still works without Pillow
        h1 = isolated_module.ImageHash("phash", 0xFF00, 64)
        h2 = isolated_module.ImageHash.from_hex(h1.to_hex())
        self.assertEqual(h1, h2)
        # Batch operations record a structured error rather than raising when Pillow is absent
        res = isolated_module.find_similar_groups([self.root / "any.png"], max_distance=4, algorithm="phash")
        self.assertEqual(len(res.groups), 0)
        self.assertEqual(len(res.errors), 1)

    def test_dct_ii_and_prefix_bucketing_match_brute_force(self) -> None:
        # 1. Orthonormal 1D DCT-II preserves Euclidean energy (Parseval's theorem)
        signal = [float((i * 17 + 5) % 256) for i in range(32)]
        coeffs = perceptual_module._dct_1d(signal)
        self.assertEqual(len(coeffs), 32)
        energy_spatial = sum(v * v for v in signal)
        energy_freq = sum(c * c for c in coeffs)
        self.assertAlmostEqual(energy_spatial, energy_freq, places=6)

        # 2. Multi-index prefix bucketing matches brute-force connected components on 80 hashes
        rng = random.Random(1337)
        entries: list[tuple[str, ImageHash]] = []
        for cluster in range(10):
            center = rng.getrandbits(64)
            entries.append((f"cluster_{cluster}_base.jpg", ImageHash("phash", center, 64)))
            # Create 2 near-neighbours within 3 bits of center
            for variant in range(2):
                mutated = center ^ (1 << ((cluster * 5 + variant) % 64)) ^ (1 << ((cluster * 5 + variant + 1) % 64))
                entries.append((f"cluster_{cluster}_v{variant}.jpg", ImageHash("phash", mutated, 64)))
        for noise_idx in range(30):
            entries.append((f"singleton_{noise_idx}.jpg", ImageHash("phash", rng.getrandbits(64), 64)))

        for threshold in (0, 2, 5):
            bucketed = find_similar_groups(entries, max_distance=threshold, algorithm="phash")
            # Brute-force reference via Union-Find
            uf = perceptual_module._UnionFind(len(entries))
            for i in range(len(entries)):
                for j in range(i + 1, len(entries)):
                    if entries[i][1].hamming_distance(entries[j][1]) <= threshold:
                        uf.union(i, j)
            comp: dict[int, list[str]] = {}
            for idx, (path, _) in enumerate(entries):
                comp.setdefault(uf.find(idx), []).append(path)
            expected = tuple(sorted(tuple(sorted(paths)) for paths in comp.values() if len(paths) >= 2))
            actual = tuple(g.paths for g in bucketed.groups)
            self.assertEqual(actual, expected)

    def test_framework_neutrality_no_qt_imports(self) -> None:
        source_text = Path(perceptual_module.__file__).read_text(encoding="utf-8")
        self.assertNotIn("PySide6", source_text)
        self.assertNotIn("PyQt", source_text)
        self.assertIn("from __future__ import annotations", source_text)


if __name__ == "__main__":
    unittest.main()
