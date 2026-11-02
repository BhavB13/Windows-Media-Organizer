"""Tests for live destination preview when editing a sort rule (W29)."""

from __future__ import annotations

import os
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from duplicate_transfer_manager.runtime_paths import get_runtime_paths
from duplicate_transfer_manager.sorting.models import (
    Association,
    FileMetadata,
    SortAction,
    SortingProfile,
)
from duplicate_transfer_manager.sorting.planner import (
    SAMPLE_PHOTO,
    UNKNOWN_CAMERA,
    SortPlanner,
    camera_tokens,
    preview_destination,
    template_values,
)
from duplicate_transfer_manager.ui.sort_workspace import AssociationDialog


class SortTemplatePreviewPlannerTests(unittest.TestCase):
    def test_each_token_renders_for_sample_photo(self):
        expected_tokens = {
            "{name}": "IMG_0001.jpg",
            "{stem}": "IMG_0001",
            "{suffix}": ".jpg",
            "{extension}": "jpg",
            "{media_type}": "Image",
            "{year}": "2024",
            "{month}": "07",
            "{day}": "14",
            "{camera}": "Apple iPhone 15 Pro",
            "{camera_make}": "Apple",
            "{camera_model}": "iPhone 15 Pro",
        }
        for token, expected_val in expected_tokens.items():
            rendered, error = preview_destination(token)
            self.assertEqual(error, "", f"Expected no error for token {token}")
            self.assertEqual(rendered, expected_val, f"Token {token} rendered unexpectedly")

        # Full combined destination template
        template = r"D:\Sorted\{camera}\{year}\{month}\{day}\{media_type}\{extension}"
        rendered, error = preview_destination(template)
        self.assertEqual(error, "")
        self.assertEqual(rendered, r"D:\Sorted\Apple iPhone 15 Pro\2024\07\14\Image\jpg")

    def test_camera_uses_same_deduplication_as_camera_tokens(self):
        test_cases = [
            ("Canon", "Canon EOS R5", "Canon EOS R5"),
            ("Apple", "iPhone 15 Pro", "Apple iPhone 15 Pro"),
            ("NIKON CORPORATION", "NIKON Z 6", "NIKON Z 6"),
            ("", "", UNKNOWN_CAMERA),
        ]
        for make, model, expected_camera in test_cases:
            meta = FileMetadata(
                path="photo.jpg",
                name="photo.jpg",
                extension=".jpg",
                size=1024,
                created=0.0,
                modified=0.0,
                media_type="image",
                attributes={"camera_make": make, "camera_model": model},
            )
            # Both preview_destination and camera_tokens must match
            direct_tokens = camera_tokens(meta)
            self.assertEqual(direct_tokens["camera"], expected_camera)

            rendered, error = preview_destination("{camera}", meta)
            self.assertEqual(error, "")
            self.assertEqual(rendered, expected_camera)
            self.assertEqual(rendered, direct_tokens["camera"])

    def test_unknown_token_returns_error_naming_it_and_listing_valid_tokens(self):
        rendered, error = preview_destination(r"D:\Sorted\{yaer}")
        self.assertEqual(rendered, "")
        self.assertIn("yaer", error)
        # Lists valid tokens
        for valid_token in ("{camera}", "{year}", "{month}", "{media_type}", "{extension}"):
            self.assertIn(valid_token, error)

        # Another unknown token
        rendered, error = preview_destination(r"D:\Sorted\{custom_token}")
        self.assertEqual(rendered, "")
        self.assertIn("custom_token", error)
        self.assertIn("{camera}", error)

    def test_unbalanced_braces_returns_plain_error(self):
        malformed_templates = [
            r"D:\Sorted\{year",
            r"D:\Sorted\year}",
            r"D:\Sorted\{year{month}}",
            r"D:\Sorted\{}",
        ]
        for malformed in malformed_templates:
            rendered, error = preview_destination(malformed)
            self.assertEqual(rendered, "", f"Expected empty rendered string for {malformed}")
            self.assertTrue(bool(error), f"Expected error message for {malformed}")
            self.assertIn("braces", error.lower(), f"Expected braces error for {malformed}")

    def test_supplied_file_metadata_is_used_over_sample(self):
        custom_dt = datetime(2023, 11, 25, 10, 30, 0)
        custom_meta = FileMetadata(
            path="C:/Media/drone_clip.mp4",
            name="drone_clip.mp4",
            extension=".mp4",
            size=15_000_000,
            created=custom_dt.timestamp(),
            modified=custom_dt.timestamp(),
            captured=custom_dt.timestamp(),
            media_type="video",
            attributes={"camera_make": "DJI", "camera_model": "Mini 3 Pro"},
        )
        template = r"D:\Sorted\{camera}\{year}\{month}\{day}\{media_type}\{extension}"
        rendered, error = preview_destination(template, metadata=custom_meta)
        self.assertEqual(error, "")
        self.assertEqual(rendered, r"D:\Sorted\DJI Mini 3 Pro\2023\11\25\Video\mp4")
        self.assertNotIn("Apple", rendered)
        self.assertNotIn("iPhone", rendered)
        self.assertNotIn("jpg", rendered)

    def test_shared_helper_produces_same_destination_as_sort_planner(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            source_file = temp_path / "inbox" / "vacation.jpg"
            source_file.parent.mkdir()
            source_file.write_bytes(b"\xff\xd8\xff\xe0fakejpeg")

            file_dt = datetime(2024, 5, 20, 15, 0, 0)
            meta = FileMetadata(
                path=str(source_file),
                name=source_file.name,
                extension=source_file.suffix,
                size=len(source_file.read_bytes()),
                created=file_dt.timestamp(),
                modified=file_dt.timestamp(),
                captured=file_dt.timestamp(),
                media_type="image",
                attributes={"camera_make": "Sony", "camera_model": "ILCE-7M4"},
            )

            dest_template = str(temp_path / "Sorted" / "{camera}" / "{year}" / "{month}")
            association = Association("By Camera", (), SortAction.MOVE, dest_template)
            profile = SortingProfile("Test Profile", (association,), ml_enabled=False)

            planner = SortPlanner(get_runtime_paths(temp_path / "data"))
            plan = planner.build(profile, [meta], sources=[str(source_file.parent)], dry_run=True)

            planned_item = plan.items[0]
            planned_folder = str(Path(planned_item.destination).parent)

            # Both preview_destination and template_values should match SortPlanner's folder
            previewed_folder, error = preview_destination(dest_template, meta)
            self.assertEqual(error, "")
            self.assertEqual(planned_folder, str(Path(previewed_folder).resolve()))

            # Check that helper template_values directly produces the same result
            values = template_values(meta)
            self.assertEqual(dest_template.format_map(values), previewed_folder)

    def test_preview_destination_does_not_touch_filesystem(self):
        # Using a hypothetical drive and deep path that does not exist
        hypothetical = r"Z:\Hypothetical_NonExistent_Drive\TestFolder\{camera}\{year}"
        rendered, error = preview_destination(hypothetical)
        self.assertEqual(error, "")
        self.assertEqual(rendered, r"Z:\Hypothetical_NonExistent_Drive\TestFolder\Apple iPhone 15 Pro\2024")


class AssociationDialogLivePreviewTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_typing_updates_example_label_and_disables_ok_for_invalid_template(self):
        dialog = AssociationDialog()
        try:
            # 1. Type a valid template
            dialog.destination.setText(r"D:\Sorted\{camera}\{year}")
            QTest.qWait(200)

            self.assertTrue(dialog.save_button.isEnabled(), "OK button should be enabled for valid template")
            self.assertTrue(dialog.destination_preview.text().startswith("Example: "))
            self.assertIn("Apple iPhone 15 Pro", dialog.destination_preview.text())
            self.assertIn("2024", dialog.destination_preview.text())
            self.assertNotEqual(dialog.destination_preview.property("status"), "danger")

            # 2. Type an invalid template (unknown token {yaer})
            dialog.destination.setText(r"D:\Sorted\{yaer}")
            QTest.qWait(200)

            self.assertFalse(dialog.save_button.isEnabled(), "OK button should be disabled for invalid template")
            self.assertIn("yaer", dialog.destination_preview.text())
            self.assertEqual(dialog.destination_preview.property("status"), "danger")

            # 3. Type an invalid template (unbalanced brace)
            dialog.destination.setText(r"D:\Sorted\{year")
            QTest.qWait(200)

            self.assertFalse(dialog.save_button.isEnabled(), "OK button should be disabled for unbalanced brace")
            self.assertEqual(dialog.destination_preview.property("status"), "danger")

            # 4. Fix template back to valid
            dialog.destination.setText(r"D:\Sorted\{year}\{month}")
            QTest.qWait(200)

            self.assertTrue(dialog.save_button.isEnabled(), "OK button should re-enable when fixed")
            self.assertEqual(dialog.destination_preview.text(), r"Example: D:\Sorted\2024\07")
            self.assertNotEqual(dialog.destination_preview.property("status"), "danger")
        finally:
            dialog.reject()

    def test_initial_state_with_existing_association(self):
        assoc = Association(
            "Existing Rule",
            (),
            SortAction.MOVE,
            destination=r"E:\Archive\{camera}\{year}",
        )
        dialog = AssociationDialog(assoc)
        try:
            self.assertTrue(dialog.save_button.isEnabled())
            self.assertEqual(
                dialog.destination_preview.text(),
                r"Example: E:\Archive\Apple iPhone 15 Pro\2024",
            )
        finally:
            dialog.reject()


if __name__ == "__main__":
    unittest.main()
