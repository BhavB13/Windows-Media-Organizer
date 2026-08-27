import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from duplicate_transfer_manager.runtime_paths import get_runtime_paths
from duplicate_transfer_manager.core import ServiceError
from duplicate_transfer_manager.services import (
    DashboardService,
    DuplicateQuarantineService,
    OperationRecordService,
    build_duplicate_review,
    duplicate_review_to_dict,
)
from models import FileInfo
from utils import HashCache


class Phase3DuplicateWorkflowTests(unittest.TestCase):
    def test_quarantine_operation_ids_are_unique_and_reject_unsafe_or_existing_values(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            keep = root / "keep.bin"
            duplicate = root / "duplicate.bin"
            keep.write_bytes(b"same")
            duplicate.write_bytes(b"same")
            paths = get_runtime_paths(root / "data")
            review = build_duplicate_review(
                [[FileInfo(str(keep), 4, 100), FileInfo(str(duplicate), 4, 200)]],
                thumbnail_root=root / "thumbs",
            )
            service = DuplicateQuarantineService(paths)

            first = service.quarantine(review, review.groups[0].selected_item_ids, dry_run=True)
            second = service.quarantine(review, review.groups[0].selected_item_ids, dry_run=True)
            self.assertNotEqual(first.operation_id, second.operation_id)

            for unsafe in ("../escape", "nested/name", str(root / "absolute")):
                with self.assertRaises(ServiceError):
                    service.quarantine(review, review.groups[0].selected_item_ids, operation_id=unsafe, dry_run=True)
            with self.assertRaises(ServiceError):
                service.quarantine(review, review.groups[0].selected_item_ids, operation_id=first.operation_id, dry_run=True)
    def test_replace_restore_failure_preserves_incumbent_content(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            duplicate = root / "duplicate.bin"
            keep = root / "keep.bin"
            duplicate.write_bytes(b"quarantined")
            keep.write_bytes(b"quarantined")
            paths = get_runtime_paths(root / "data")
            review = build_duplicate_review(
                [[FileInfo(str(keep), keep.stat().st_size, 100), FileInfo(str(duplicate), duplicate.stat().st_size, 200)]],
                thumbnail_root=root / "thumbs",
            )
            service = DuplicateQuarantineService(paths)
            result = service.quarantine(review, review.groups[0].selected_item_ids, operation_id="op-replace-failure")
            duplicate.write_bytes(b"incumbent")

            with patch(
                "duplicate_transfer_manager.services.duplicate_workflow.shutil.move",
                side_effect=OSError("forced restore failure"),
            ):
                restored = service.restore_record(result.records[0], conflict_policy="replace")

            self.assertEqual(len(restored.failures), 1)
            self.assertEqual(duplicate.read_bytes(), b"incumbent")
            self.assertTrue(Path(result.records[0].stored_path).exists())

    def test_quarantine_initial_manifest_survives_interrupted_checkpoint(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            keep = root / "keep.bin"
            first = root / "first.bin"
            second = root / "second.bin"
            for path in (keep, first, second):
                path.write_bytes(b"same")
            paths = get_runtime_paths(root / "data")
            review = build_duplicate_review(
                [[
                    FileInfo(str(keep), 4, 100),
                    FileInfo(str(first), 4, 200),
                    FileInfo(str(second), 4, 300),
                ]],
                thumbnail_root=root / "thumbs",
            )
            service = DuplicateQuarantineService(paths)
            original_write = service._write_manifest
            calls = 0

            def interrupt_checkpoint(*args, **kwargs):
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise RuntimeError("simulated crash")
                return original_write(*args, **kwargs)

            with patch.object(service, "_write_manifest", side_effect=interrupt_checkpoint):
                with self.assertRaisesRegex(RuntimeError, "simulated crash"):
                    service.quarantine(review, review.groups[0].selected_item_ids, operation_id="op-interrupted")

            records = service.list_records()
            moved = next(record for record in records if Path(record.stored_path).is_file())
            self.assertEqual(moved.operation_id, "op-interrupted")
            restored = service.restore_record(moved)
            self.assertEqual(len(restored.restored), 1)

    def test_truncated_quarantine_manifest_is_reported(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            paths = get_runtime_paths(Path(temp_dir) / "data")
            operation = paths.quarantine / "op-truncated"
            operation.mkdir(parents=True)
            (operation / "manifest.json").write_text('{"records": [', encoding="utf-8")

            with self.assertRaises(ServiceError):
                DuplicateQuarantineService(paths).list_records()

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

    def test_review_can_prefer_highest_resolution_with_size_fallback(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            low = root / "low.jpg"
            high = root / "high.jpg"
            low.write_bytes(b"same")
            high.write_bytes(b"same")
            review = build_duplicate_review(
                [[FileInfo(str(low), 4, 100), FileInfo(str(high), 8, 200)]],
                prefer="quality",
                thumbnail_root=root / "thumbs",
            )

        kept = next(item for item in review.groups[0].items if item.id == review.groups[0].keep_item_id)
        self.assertEqual(kept.filename, "high.jpg")

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

    def test_quarantine_keeps_same_named_files_from_different_folders_apart(self):
        # Two photos called IMG_0001.jpg in different folders are ordinary on a
        # phone. Naming the quarantined copy after the file name alone made the
        # second overwrite the first, destroying an original that had already
        # been moved out of its folder.
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            camera = root / "DCIM" / "Camera"
            whatsapp = root / "Pictures" / "WhatsApp"
            camera.mkdir(parents=True)
            whatsapp.mkdir(parents=True)

            # Two distinct duplicate pairs that happen to share a file name.
            camera_keep = camera / "keep_a.jpg"
            camera_dup = camera / "IMG_0001.jpg"
            whatsapp_keep = whatsapp / "keep_b.jpg"
            whatsapp_dup = whatsapp / "IMG_0001.jpg"
            camera_keep.write_bytes(b"content-A")
            camera_dup.write_bytes(b"content-A")
            whatsapp_keep.write_bytes(b"content-B")
            whatsapp_dup.write_bytes(b"content-B")

            paths = get_runtime_paths(root / "data")
            review = build_duplicate_review(
                [
                    [FileInfo(str(camera_keep), 9, 100), FileInfo(str(camera_dup), 9, 200)],
                    [FileInfo(str(whatsapp_keep), 9, 100), FileInfo(str(whatsapp_dup), 9, 200)],
                ],
                thumbnail_root=root / "thumbs",
            )
            selected = [value for group in review.groups for value in group.selected_item_ids]
            result = DuplicateQuarantineService(paths).quarantine(review, selected, operation_id="op-collide")

            self.assertEqual(result.quarantined_count, 2)
            stored = [Path(record.stored_path) for record in result.records]
            self.assertEqual(len({str(path) for path in stored}), 2, "quarantined copies shared a path")
            for path in stored:
                self.assertTrue(path.exists())
            # Both distinct contents survived; neither overwrote the other.
            self.assertEqual({path.read_bytes() for path in stored}, {b"content-A", b"content-B"})

    def test_quarantine_dry_run_does_not_move_file(self):
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

            result = DuplicateQuarantineService(paths).quarantine(
                review,
                review.groups[0].selected_item_ids,
                operation_id="op-dry",
                dry_run=True,
            )

            self.assertTrue(result.dry_run)
            self.assertTrue(duplicate.exists())
            self.assertFalse(Path(result.records[0].stored_path).exists())
            manifest = json.loads(Path(result.manifest_path).read_text(encoding="utf-8"))
            self.assertTrue(manifest["dry_run"])

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

    def test_restore_dry_run_does_not_move_file(self):
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
            result = service.quarantine(review, review.groups[0].selected_item_ids, operation_id="op-restore-dry")

            restore = service.restore_record(result.records[0], dry_run=True)

            self.assertTrue(restore.dry_run)
            self.assertFalse(duplicate.exists())
            self.assertTrue(Path(result.records[0].stored_path).exists())

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


    def test_a_dry_run_quarantine_is_not_listed_as_recoverable(self):
        # A dry run moves nothing, but its manifest records looked exactly like
        # real ones: the Quarantine page offered to restore files that were
        # still in place and the dashboard counted their bytes as recovered.
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            keep = root / "keep.bin"
            copy = root / "copy.bin"
            for path in (keep, copy):
                path.write_bytes(b"same")
            paths = get_runtime_paths(root / "data")
            review = build_duplicate_review(
                [[FileInfo(str(keep), 4, 100), FileInfo(str(copy), 4, 200)]],
                thumbnail_root=root / "thumbs",
            )
            service = DuplicateQuarantineService(paths)
            result = service.quarantine(
                review, review.groups[0].selected_item_ids, operation_id="op-dry", dry_run=True
            )

            self.assertTrue(result.dry_run)
            self.assertTrue(copy.exists(), "a dry run must not move anything")
            self.assertEqual(
                service.list_records(), [],
                "a previewed record is not a file in quarantine",
            )

    def test_a_failed_quarantine_move_is_not_listed_as_recoverable(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            keep = root / "keep.bin"
            copy = root / "copy.bin"
            for path in (keep, copy):
                path.write_bytes(b"same")
            paths = get_runtime_paths(root / "data")
            review = build_duplicate_review(
                [[FileInfo(str(keep), 4, 100), FileInfo(str(copy), 4, 200)]],
                thumbnail_root=root / "thumbs",
            )
            service = DuplicateQuarantineService(paths)
            with patch(
                "duplicate_transfer_manager.services.duplicate_workflow.shutil.move",
                side_effect=OSError("locked by another process"),
            ):
                result = service.quarantine(
                    review, review.groups[0].selected_item_ids, operation_id="op-failed"
                )

            self.assertTrue(result.failures)
            self.assertTrue(copy.exists(), "the move failed, so the file is still there")
            self.assertEqual(
                service.list_records(), [],
                "a failed move did not put anything in quarantine",
            )

    def test_an_import_appears_once_in_activity_not_once_per_stored_file(self):
        # Every import writes both an operation record and a transfer report,
        # and the record names the report. Listing both showed each import
        # twice in Activity, counted it twice on the dashboard, and exported it
        # twice to CSV.
        with tempfile.TemporaryDirectory() as temp_dir:
            paths = get_runtime_paths(Path(temp_dir) / "data")
            report = paths.reports / "transfer_report_1.json"
            report.parent.mkdir(parents=True, exist_ok=True)
            report.write_text(
                json.dumps({"created_at": "2026-01-01T00:00:00+00:00", "transferred": 3, "errors": 0}),
                encoding="utf-8",
            )
            service = OperationRecordService(paths)
            service.record("import", "completed", title="File import", report_path=str(report))

            records = service.list_records()
            self.assertEqual(len(records), 1, [r.get("id") for r in records])
            self.assertEqual(records[0]["title"], "File import")

    def test_an_orphan_report_is_still_listed(self):
        # De-duplication must not hide a report from an older version, or one
        # whose operation record the user removed.
        with tempfile.TemporaryDirectory() as temp_dir:
            paths = get_runtime_paths(Path(temp_dir) / "data")
            report = paths.reports / "transfer_report_2.json"
            report.parent.mkdir(parents=True, exist_ok=True)
            report.write_text(
                json.dumps({"created_at": "2026-01-01T00:00:00+00:00", "transferred": 1, "errors": 0}),
                encoding="utf-8",
            )
            records = OperationRecordService(paths).list_records()
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0]["title"], "Import report")

    def test_dashboard_reports_real_totals_not_the_size_of_its_preview(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            paths = get_runtime_paths(Path(temp_dir) / "data")
            service = OperationRecordService(paths)
            for index in range(9):
                service.record("duplicate_scan", "completed", title=f"Scan {index}")

            summary = DashboardService(paths).summary(include_storage=False)
            self.assertEqual(len(summary["recent_operations"]), 5, "the card preview stays small")
            self.assertEqual(
                summary["recent_operations_total"], 9,
                "the count tile must report every operation, not just the preview",
            )

    def test_clearing_the_cache_forgets_hashes_the_running_app_still_holds(self):
        # Deleting the file alone changed nothing the user could observe: the
        # running app held the entries and wrote them straight back.
        with tempfile.TemporaryDirectory() as temp_dir:
            cache_path = Path(temp_dir) / "hash_cache.json"
            cache = HashCache(str(cache_path))
            cache.set("C:/photo.jpg", "sha256", "full", 10, 1.0, "abc")
            cache.save()
            self.assertEqual(cache.get("C:/photo.jpg", "sha256", "full", 10, 1.0), "abc")

            cache.clear()

            self.assertIsNone(cache.get("C:/photo.jpg", "sha256", "full", 10, 1.0))
            cache.save()
            reloaded = HashCache(str(cache_path))
            reloaded.load()
            self.assertIsNone(reloaded.get("C:/photo.jpg", "sha256", "full", 10, 1.0))


if __name__ == "__main__":
    unittest.main()
