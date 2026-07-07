import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from duplicate_transfer_manager.runtime_paths import get_runtime_paths
from duplicate_transfer_manager.services import (
    DuplicateQuarantineService,
    build_duplicate_review,
    duplicate_review_to_dict,
)
from models import FileInfo


class Phase3DuplicateWorkflowTests(unittest.TestCase):
    def test_review_defaults_to_keeping_oldest_and_estimates_recoverable_size(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            old = root / "old.jpg"
            new = root / "new.jpg"
            old.write_bytes(b"same")
            new.write_bytes(b"same")
            review = build_duplicate_review(
                [
                    [
                        FileInfo(str(new), new.stat().st_size, 200),
                        FileInfo(str(old), old.stat().st_size, 100),
                    ]
                ],
                prefer="oldest",
                thumbnail_root=root / "thumbs",
                scanned_files=2,
            )

        self.assertEqual(len(review.groups), 1)
        group = review.groups[0]
        kept = next(item for item in group.items if item.id == group.keep_item_id)
        self.assertEqual(kept.filename, "old.jpg")
        self.assertEqual(review.selected_count, 1)
        self.assertEqual(review.recoverable_size, 4)

    def test_review_can_prefer_newest_and_serializes_for_reports(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            old = root / "old.bin"
            new = root / "new.bin"
            old.write_bytes(b"same")
            new.write_bytes(b"same")
            review = build_duplicate_review(
                [[FileInfo(str(old), 4, 100), FileInfo(str(new), 4, 200)]],
                prefer="newest",
                thumbnail_root=root / "thumbs",
                warnings=["metadata unavailable"],
            )
            payload = duplicate_review_to_dict(review)

        group = review.groups[0]
        kept = next(item for item in group.items if item.id == group.keep_item_id)
        self.assertEqual(kept.filename, "new.bin")
        self.assertEqual(payload["warnings"], ["metadata unavailable"])
        self.assertEqual(payload["selected_count"], 1)

    def test_quarantine_local_duplicate_moves_file_and_writes_manifest(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            duplicate = root / "duplicate.jpg"
            keep = root / "keep.jpg"
            duplicate.write_bytes(b"same")
            keep.write_bytes(b"same")
            paths = get_runtime_paths(root / "data")
            review = build_duplicate_review(
                [[FileInfo(str(keep), 4, 100), FileInfo(str(duplicate), 4, 200)]],
                thumbnail_root=root / "thumbs",
            )
            selected = review.groups[0].selected_item_ids
            service = DuplicateQuarantineService(paths)
            result = service.quarantine(review, selected, operation_id="op-local")

            self.assertEqual(result.quarantined_count, 1)
            self.assertFalse(duplicate.exists())
            self.assertTrue(Path(result.records[0].stored_path).exists())
            manifest = json.loads(Path(result.manifest_path).read_text(encoding="utf-8"))
            self.assertEqual(manifest["operation_id"], "op-local")
            self.assertIn("no permanent deletion", manifest["safety"])

    def test_restore_record_renames_on_conflict_and_updates_manifest(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            duplicate = root / "duplicate.jpg"
            keep = root / "keep.jpg"
            duplicate.write_bytes(b"same")
            keep.write_bytes(b"same")
            paths = get_runtime_paths(root / "data")
            review = build_duplicate_review(
                [[FileInfo(str(keep), 4, 100), FileInfo(str(duplicate), 4, 200)]],
                thumbnail_root=root / "thumbs",
            )
            service = DuplicateQuarantineService(paths)
            result = service.quarantine(
                review,
                review.groups[0].selected_item_ids,
                operation_id="op-restore",
            )
            duplicate.write_bytes(b"new file in the old location")

            restore = service.restore_record(result.records[0], conflict_policy="rename")

            self.assertEqual(len(restore.restored), 1)
            self.assertNotEqual(restore.restored[0], str(duplicate))
            self.assertTrue(Path(restore.restored[0]).exists())
            manifest = json.loads((paths.quarantine / "op-restore" / "manifest.json").read_text(encoding="utf-8"))
            self.assertTrue(manifest["records"][0]["restored_at"])

    def test_restore_operation_supports_skip_policy(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            duplicate = root / "duplicate.jpg"
            keep = root / "keep.jpg"
            duplicate.write_bytes(b"same")
            keep.write_bytes(b"same")
            paths = get_runtime_paths(root / "data")
            review = build_duplicate_review(
                [[FileInfo(str(keep), 4, 100), FileInfo(str(duplicate), 4, 200)]],
                thumbnail_root=root / "thumbs",
            )
            service = DuplicateQuarantineService(paths)
            service.quarantine(review, review.groups[0].selected_item_ids, operation_id="op-skip")
            duplicate.write_bytes(b"conflict")

            restore = service.restore_operation("op-skip", conflict_policy="skip")

            self.assertEqual(restore.restored, ())
            self.assertEqual(restore.skipped, (str(duplicate),))

    def test_android_quarantine_pulls_copy_without_deleting_original(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            local = root / "local.jpg"
            local.write_bytes(b"same")
            paths = get_runtime_paths(root / "data")
            review = build_duplicate_review(
                [
                    [
                        FileInfo(str(local), 4, 100, False),
                        FileInfo("/sdcard/DCIM/duplicate.jpg", 4, 200, True),
                    ]
                ],
                thumbnail_root=root / "thumbs",
            )
            android_id = next(
                item.id
                for item in review.groups[0].items
                if item.is_adb
            )

            def fake_pull(_src, dst, serial=None):
                Path(dst).write_bytes(b"same")

            with patch("duplicate_transfer_manager.services.duplicate_workflow.ADBBridge.pull", side_effect=fake_pull):
                result = DuplicateQuarantineService(paths).quarantine(
                    review,
                    [android_id],
                    operation_id="op-adb",
                    adb_serial="device-1",
                )

            self.assertEqual(result.quarantined_count, 1)
            self.assertTrue(result.records[0].source_is_adb)
            self.assertEqual(result.records[0].device_serial, "device-1")
            self.assertTrue(Path(result.records[0].stored_path).exists())


if __name__ == "__main__":
    unittest.main()
