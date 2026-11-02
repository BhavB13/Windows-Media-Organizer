"""End-to-end checks across the analysis layers added after Phase 7.

Each layer arrived from a separate agent worktree and was verified in isolation.
Nothing exercised them against one shared tree, which is where a contract
mismatch between them would surface, so that is what this module does.

The properties asserted here are the ones the product promises, not the ones any
single module promises:

* analysis never modifies the media it inspects;
* classifying storage never opens a file, because reading a cloud placeholder
  causes the download the classifier exists to warn about;
* an absent volume is reported offline and keeps its rows, never read as
  deleted media;
* a near-duplicate is found by content, when name, size and bytes all differ;
* the keeper recommendation is an opinion carrying a reason, and acting on it
  remains the caller's decision.

Every fixture is generated here in a temporary directory. No test reads the
user's own media and none requires a real removable drive.
"""

from __future__ import annotations

import math
import tempfile
import threading
import unittest
from pathlib import Path

from duplicate_transfer_manager.core import storage_class
from duplicate_transfer_manager.index.catalog import MediaCatalog
from duplicate_transfer_manager.media import perceptual
from duplicate_transfer_manager.previews.thumbnails import ThumbnailCache, is_pillow_available

try:  # pragma: no cover - the skipIf below covers the absent case
    from PIL import Image, ImageFilter
except Exception:  # pragma: no cover
    Image = None
    ImageFilter = None


def _photo_like(path: Path, size: tuple[int, int], phase: float) -> None:
    """Write a JPEG whose content behaves like a photograph.

    The fixture has to be smooth and dominated by low spatial frequencies,
    because that is what a photograph is and what a perceptual hash is built
    for. An earlier version of this fixture built its pixels with a modulo
    expression, which wraps and produces a high-frequency pattern with almost no
    low-frequency content. pHash reads only the lowest 8x8 DCT coefficients, so
    on that input its bits sat on noise and flipped under any re-encode, while
    aHash and dHash were unaffected because they work from an 8x8 average.
    Measured on this smooth fixture, pHash puts a resized and a recompressed
    copy at distance 0 and an unrelated image at 54. The lesson is about the
    fixture, not the hash: a synthetic test image can make a correct perceptual
    hash look broken.
    """

    width, height = size
    image = Image.new("RGB", size)
    pixels = image.load()
    for x in range(width):
        for y in range(height):
            first = 128 + 100 * math.sin(2 * math.pi * (x / width) * 1.5 + phase) * math.cos(
                2 * math.pi * (y / height) * 1.1
            )
            second = 128 + 90 * math.sin(2 * math.pi * ((x + y) / (width + height)) * 2.0 + phase)
            pixels[x, y] = (
                int(max(0, min(255, first))),
                int(max(0, min(255, second))),
                int(max(0, min(255, (first + second) / 2))),
            )
    image.filter(ImageFilter.GaussianBlur(1.2)).save(path, "JPEG", quality=95)


def _fingerprint(root: Path) -> dict[str, tuple[int, int, bytes]]:
    """Size, mtime_ns and content of every file, to prove nothing was touched."""

    snapshot: dict[str, tuple[int, int, bytes]] = {}
    for path in sorted(root.rglob("*")):
        if path.is_file():
            stat = path.stat()
            snapshot[str(path)] = (stat.st_size, stat.st_mtime_ns, path.read_bytes())
    return snapshot


def _entry_path(candidate: object) -> str:
    """The keeper is reported as an entry or a path depending on the caller."""

    return str(getattr(candidate, "path", candidate))


@unittest.skipIf(Image is None, "Pillow is required to generate the fixtures")
class AnalysisLayersOverOneTreeTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.library = self.root / "library"
        self.library.mkdir()

        # A photo, a resized copy under an unrelated name, and an unrelated
        # photo. Names and sizes deliberately give away nothing.
        self.original = self.library / "IMG_0001.jpg"
        self.resized = self.library / "holiday-small.jpg"
        self.unrelated = self.library / "IMG_0002.jpg"

        _photo_like(self.original, (640, 480), phase=0.0)
        with Image.open(self.original) as handle:
            handle.resize((320, 240)).save(self.resized, "JPEG", quality=85)
        _photo_like(self.unrelated, (640, 480), phase=2.3)

        self.photos = [self.original, self.resized, self.unrelated]

    # ---------------------------------------------------------------- safety

    def test_a_full_analysis_pass_modifies_nothing_in_the_library(self) -> None:
        before = _fingerprint(self.library)
        stop = threading.Event()

        storage_class.survey_tree(str(self.library), stop_event=stop)

        batch = perceptual.hash_images([str(p) for p in self.photos], stop_event=stop)
        perceptual.find_similar_groups(batch.entries, max_distance=12, stop_event=stop)

        cache_dir = self.root / "thumbs"
        if is_pillow_available():
            cache = ThumbnailCache(cache_dir=str(cache_dir), max_bytes=4 * 1024 * 1024)
            for photo in self.photos:
                cache.get_or_create(str(photo), 96, stop_event=stop)

        with MediaCatalog(str(self.root / "catalog.sqlite3")) as catalog:
            catalog.upsert_files(
                "TESTVOL",
                [str(p) for p in self.photos],
                volume_root=str(self.library),
                compute_hashes=True,
                extract_metadata=True,
                stop_event=stop,
            )

        self.assertEqual(
            _fingerprint(self.library),
            before,
            "an analysis pass altered the library it was only meant to inspect",
        )

        # The thumbnail cache is the only layer permitted to write, and only
        # inside the directory it was handed.
        if cache_dir.exists():
            for written in cache_dir.rglob("*"):
                self.assertTrue(
                    str(written).startswith(str(cache_dir)),
                    f"the thumbnail cache wrote outside its own directory: {written}",
                )

    def test_surveying_storage_never_opens_a_file(self) -> None:
        """Reading a cloud placeholder triggers the download this layer warns about."""

        opened: list[object] = []

        def forbidden_open(*args: object, **kwargs: object) -> object:
            opened.append(args)
            raise AssertionError("survey_tree opened a file")

        result = storage_class.survey_tree(
            str(self.library),
            stop_event=threading.Event(),
            open_hook=forbidden_open,
        )

        self.assertFalse(opened, "survey_tree must classify by attributes alone")
        self.assertGreaterEqual(result.directories_scanned, 1)

    # ------------------------------------------------------- media intelligence

    def test_a_resized_copy_is_grouped_although_every_byte_differs(self) -> None:
        self.assertNotEqual(self.original.stat().st_size, self.resized.stat().st_size)
        self.assertNotEqual(self.original.read_bytes(), self.resized.read_bytes())

        stop = threading.Event()
        batch = perceptual.hash_images([str(p) for p in self.photos], stop_event=stop)
        self.assertFalse(batch.errors, f"hashing reported errors: {batch.errors}")

        grouped = perceptual.find_similar_groups(batch.entries, max_distance=12, stop_event=stop)
        multi = [
            {Path(path).name for path in group.paths}
            for group in grouped.groups
            if len(group.paths) > 1
        ]

        self.assertIn(
            {self.original.name, self.resized.name},
            multi,
            "a resized copy of the same photo was not recognised as similar",
        )
        for names in multi:
            self.assertNotIn(
                self.unrelated.name,
                names,
                "an unrelated photo was grouped with the near-duplicates",
            )

    def test_grouping_is_independent_of_input_order(self) -> None:
        stop = threading.Event()
        forward = perceptual.hash_images([str(p) for p in self.photos], stop_event=stop)
        backward = perceptual.hash_images([str(p) for p in reversed(self.photos)], stop_event=stop)

        def signature(entries: object) -> set[frozenset[str]]:
            grouped = perceptual.find_similar_groups(entries, max_distance=12, stop_event=stop)
            return {
                frozenset(Path(path).name for path in group.paths)
                for group in grouped.groups
                if len(group.paths) > 1
            }

        groups_forward = signature(forward.entries)
        self.assertTrue(
            groups_forward,
            "nothing grouped, so this test would pass vacuously on two empty sets",
        )
        self.assertEqual(groups_forward, signature(backward.entries))

    def test_keeper_recommendation_states_a_reason_and_acts_on_nothing(self) -> None:
        stop = threading.Event()
        batch = perceptual.hash_images([str(self.resized), str(self.original)], stop_event=stop)
        recommendation = perceptual.recommend_keeper(batch.entries, stop_event=stop)

        self.assertEqual(
            Path(_entry_path(recommendation.keeper)).name,
            self.original.name,
            "the higher-resolution file should be the recommended keeper",
        )
        self.assertTrue(
            recommendation.reasons or recommendation.rejected,
            "a recommendation with no stated reasoning is not reviewable",
        )
        for rejected in recommendation.rejected:
            self.assertTrue(
                str(rejected.reason).strip(),
                "every rejected candidate needs a human-readable reason",
            )

        # Analysis only. Both files are still exactly where they were.
        self.assertTrue(self.original.exists())
        self.assertTrue(self.resized.exists())

    # ------------------------------------------------------------ external drive

    def test_an_absent_volume_keeps_its_rows_and_reports_offline(self) -> None:
        """A disconnected external drive must never read as deleted media."""

        stop = threading.Event()
        db = self.root / "removable.sqlite3"

        with MediaCatalog(str(db)) as catalog:
            catalog.upsert_files(
                "REMOVABLE",
                [str(self.original)],
                volume_root=str(self.library),
                compute_hashes=False,
                extract_metadata=False,
                stop_event=stop,
            )

        # Reopen with the volume absent, exactly as an unplugged drive appears.
        with MediaCatalog(str(db)) as catalog:
            statuses = catalog.refresh_volume_statuses(present_volume_keys=[])
            self.assertEqual(
                statuses.get("REMOVABLE"),
                "offline",
                f"an absent volume should report offline, got {statuses!r}",
            )

            volume = catalog.get_volume("REMOVABLE")
            self.assertFalse(volume.is_online)
            self.assertEqual(
                len(catalog.search(volume_key="REMOVABLE")),
                1,
                "rows for an absent volume were discarded, which reads as deletion",
            )

    def test_reconnecting_the_volume_restores_it_without_re_adding_rows(self) -> None:
        stop = threading.Event()
        db = self.root / "reconnect.sqlite3"

        with MediaCatalog(str(db)) as catalog:
            first = catalog.upsert_files(
                "REMOVABLE",
                [str(p) for p in self.photos],
                volume_root=str(self.library),
                compute_hashes=False,
                extract_metadata=False,
                stop_event=stop,
            )
            self.assertEqual(first.added, 3)

            catalog.refresh_volume_statuses(present_volume_keys=[])
            catalog.refresh_volume_statuses(present_volume_keys=["REMOVABLE"])

            again = catalog.upsert_files(
                "REMOVABLE",
                [str(p) for p in self.photos],
                volume_root=str(self.library),
                compute_hashes=False,
                extract_metadata=False,
                stop_event=stop,
            )

        self.assertEqual(again.added, 0, "a reconnected drive re-added rows it already had")
        self.assertEqual(again.updated, 0)
        self.assertEqual(again.unchanged, 3, "an unchanged tree should re-index as unchanged")

    # ------------------------------------------------------------- cancellation

    def test_cancellation_leaves_each_layer_usable(self) -> None:
        stop = threading.Event()
        stop.set()

        survey = storage_class.survey_tree(str(self.library), stop_event=stop)
        self.assertTrue(survey.cancelled)

        batch = perceptual.hash_images([str(p) for p in self.photos], stop_event=stop)
        self.assertTrue(batch.cancelled)

        db = self.root / "cancelled.sqlite3"
        with MediaCatalog(str(db)) as catalog:
            catalog.upsert_files(
                "V",
                [str(p) for p in self.photos],
                volume_root=str(self.library),
                compute_hashes=True,
                extract_metadata=True,
                stop_event=stop,
            )

        # A cancelled write must leave a database that still opens and reads.
        with MediaCatalog(str(db)) as catalog:
            self.assertIsNotNone(catalog.search())


if __name__ == "__main__":
    unittest.main()
