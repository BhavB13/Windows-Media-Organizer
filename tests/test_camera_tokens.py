"""Sorting by the camera or phone that took a photo.

The {camera} destination token comes from EXIF Make and Model. That text is
written by the device, so it is treated as untrusted when it becomes a folder.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from duplicate_transfer_manager.runtime_paths import get_runtime_paths
from duplicate_transfer_manager.sorting import Association, SortAction, SortingProfile, SortPlanner
from duplicate_transfer_manager.sorting.metadata import MetadataExtractor
from duplicate_transfer_manager.sorting.planner import UNKNOWN_CAMERA, camera_tokens, folder_component

try:
    from PIL import Image
except Exception:  # pragma: no cover
    Image = None


def _photo(path: Path, make: str | None, model: str | None) -> None:
    image = Image.new("RGB", (32, 24), (90, 120, 150))
    exif = Image.Exif()
    if make is not None:
        exif[271] = make
    if model is not None:
        exif[272] = model
    image.save(path, "JPEG", exif=exif.tobytes())


class CameraTokenTests(unittest.TestCase):
    def test_model_that_names_the_maker_is_not_doubled(self):
        tokens = camera_tokens(SimpleNamespace(attributes={"camera_make": "Canon", "camera_model": "Canon EOS R5"}))
        self.assertEqual(tokens["camera"], "Canon EOS R5")

    def test_maker_is_prefixed_when_the_model_omits_it(self):
        tokens = camera_tokens(SimpleNamespace(attributes={"camera_make": "Apple", "camera_model": "iPhone 15 Pro"}))
        self.assertEqual(tokens["camera"], "Apple iPhone 15 Pro")

    def test_corporate_suffix_on_the_maker_still_matches_the_model(self):
        tokens = camera_tokens(SimpleNamespace(attributes={"camera_make": "NIKON CORPORATION", "camera_model": "NIKON Z 6"}))
        self.assertEqual(tokens["camera"], "NIKON Z 6")

    def test_files_without_exif_share_one_folder(self):
        tokens = camera_tokens(SimpleNamespace(attributes={}))
        self.assertEqual(tokens, {"camera": UNKNOWN_CAMERA, "camera_make": UNKNOWN_CAMERA, "camera_model": UNKNOWN_CAMERA})

    def test_device_text_cannot_escape_or_break_a_folder_name(self):
        self.assertEqual(folder_component("Evil/..\\Co:"), "Evil .. Co")
        self.assertEqual(folder_component("trailing dots..."), "trailing dots")
        self.assertEqual(folder_component("CON"), "CON_")
        self.assertEqual(folder_component("lpt1.cam"), "lpt1.cam_")
        self.assertEqual(folder_component("tab\tand\x00null"), "tab and null")
        self.assertEqual(folder_component("   "), UNKNOWN_CAMERA)
        self.assertLessEqual(len(folder_component("x" * 500)), 80)


@unittest.skipIf(Image is None, "Pillow is required to write EXIF fixtures")
class CameraSortingTests(unittest.TestCase):
    def test_extractor_reads_make_and_model(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "IMG_0001.jpg"
            _photo(path, "Google", "Pixel 8")
            metadata = MetadataExtractor().extract(path)
        self.assertEqual(metadata.attributes.get("camera_make"), "Google")
        self.assertEqual(metadata.attributes.get("camera_model"), "Pixel 8")

    def test_plan_files_photos_under_their_camera(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            inbox = root / "inbox"
            inbox.mkdir()
            pixel = inbox / "a.jpg"
            canon = inbox / "b.jpg"
            plain = inbox / "c.jpg"
            _photo(pixel, "Google", "Pixel 8")
            _photo(canon, "Canon", "Canon EOS R6")
            _photo(plain, None, None)

            association = Association("By camera", (), SortAction.MOVE, str(root / "sorted" / "{camera}"))
            plan = SortPlanner(get_runtime_paths(root / "data")).build(
                SortingProfile("By camera", (association,), ml_enabled=False),
                [MetadataExtractor().extract(p) for p in (pixel, canon, plain)],
                sources=[str(inbox)],
                dry_run=True,
            )
            folders = {Path(item.metadata.path).name: Path(item.destination).parent.name for item in plan.items}

            self.assertEqual(folders, {"a.jpg": "Google Pixel 8", "b.jpg": "Canon EOS R6", "c.jpg": UNKNOWN_CAMERA})
            # A dry-run plan moves nothing.
            self.assertTrue(all(p.exists() for p in (pixel, canon, plain)))


if __name__ == "__main__":
    unittest.main()
