import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from duplicate_transfer_manager.core import CancellationToken, OperationPhase, OperationReporter, OperationState
from duplicate_transfer_manager.runtime_paths import get_runtime_paths
from duplicate_transfer_manager.services import (
    TRANSFER_PROFILES,
    TransferService,
    build_import_review,
    build_import_settings,
    classify_transfer_stage,
    selected_extensions,
    summarize_transfer_result,
)
from utils import HashCache


class Phase4ImportWorkflowTests(unittest.TestCase):
    def test_balanced_import_defaults_are_copy_only_and_structure_preserving(self):
        settings = build_import_settings(
            source_root="/sdcard/DCIM",
            existing_library="C:/Users/Bhav/Pictures",
            save_to="",
            source_kind="phone",
            categories=["pictures", "videos"],
            profile="Balanced",
            adb_serial="device-1",
        )

        self.assertEqual(settings.transfer_mode, "copy")
        self.assertEqual(settings.duplicate_policy, "skip")
        self.assertTrue(settings.preserve_structure)
        self.assertFalse(settings.dry_run)
        self.assertTrue(settings.source_is_adb)
        self.assertEqual(settings.output_root, "")
        self.assertEqual(settings.destination_template, "preserve")
        self.assertEqual(settings.hash_mode, TRANSFER_PROFILES["Balanced"]["hash_mode"])

    def test_reliable_and_fast_presets_map_to_plain_language_profiles(self):
        reliable = build_import_settings(
            source_root="D:/Camera",
            existing_library="D:/Library",
            source_kind="folder",
            profile="Reliable",
        )
        fast = build_import_settings(
            source_root="D:/Camera",
            existing_library="D:/Library",
            source_kind="folder",
            profile="Fast",
        )

        self.assertEqual(reliable.hash_mode, "full")
        self.assertGreater(reliable.retry_attempts, fast.retry_attempts)
        self.assertEqual(fast.hash_mode, "fast")
        self.assertFalse(fast.source_is_adb)

    def test_date_folder_template_is_preserved_in_import_settings_and_review(self):
        settings = build_import_settings(
            source_root="D:/Camera",
            existing_library="D:/Library",
            destination_template="date",
        )
        review = build_import_review(
            source_label="Folder",
            source_root="D:/Camera",
            existing_library="D:/Library",
            save_to="",
            categories=["pictures"],
            profile="Balanced",
            settings=settings,
        )

        self.assertEqual(settings.destination_template, "date")
        self.assertIn("date folders", review.advanced_summary)

    def test_categories_support_media_and_all_files_modes(self):
        only_media, extensions = selected_extensions(["pictures", "audio"])
        self.assertTrue(only_media)
        self.assertIn(".jpg", extensions)
        self.assertIn(".mp3", extensions)
        self.assertIn(".dng", extensions)
        self.assertIn(".cr3", extensions)

        only_media, extensions = selected_extensions(["other"])
        self.assertFalse(only_media)
        self.assertEqual(extensions, [])

    def test_review_explains_when_library_and_save_location_match(self):
        settings = build_import_settings(
            source_root="D:/Camera",
            existing_library="D:/Library",
            save_to="",
            source_kind="folder",
            profile="Balanced",
        )
        review = build_import_review(
            source_label="Folder",
            source_root="D:/Camera",
            existing_library="D:/Library",
            save_to="",
            categories=["pictures"],
            profile="Balanced",
            settings=settings,
        )

        self.assertTrue(review.same_library_and_save)
        self.assertEqual(review.save_to, "D:/Library")
        self.assertIn("copy", review.profile_description.lower())

    def test_transfer_stage_classifier_covers_phase4_stages(self):
        cases = {
            "Discovering source files": "discovery",
            "Building destination cache": "comparison",
            "Preflight passed: device, destination access, and free space checked.": "preflight",
            "Pulling photo.jpg": "transfer",
            "Post-transfer hash verification failed": "verification",
            "Waiting for destination drive": "reconnect",
            "Transfer report written": "finalization",
        }
        for message, expected in cases.items():
            self.assertEqual(classify_transfer_stage(message), expected)

    def test_transfer_service_emits_distinct_phase_events_and_summary_counts(self):
        events = []
        states = []
        raw = {
            "transferred": 2,
            "duplicates": 3,
            "skipped": 3,
            "resumed": 1,
            "errors": 0,
            "bytes_transferred": 4096,
            "report_path": "report.json",
        }

        def fake_execute(_settings, _cancellation, _cache, _logger, progress):
            progress(0, 0, "Discovering source files")
            progress(0, 0, "Preflight passed: device, destination access, and free space checked")
            progress(1, 3, "Building destination cache")
            progress(2, 3, "Pulling photo.jpg")
            progress(3, 3, "Post-transfer hash verification complete")
            progress(1, 1, "Transfer report written")
            return raw

        settings = build_import_settings(
            source_root="source",
            existing_library="library",
            source_kind="folder",
            profile="Balanced",
        )
        with patch(
            "duplicate_transfer_manager.services.transfer_service.validate_transfer_paths",
            return_value="",
        ), patch(
            "duplicate_transfer_manager.services.transfer_service.execute_smart_transfer",
            side_effect=fake_execute,
        ):
            result = TransferService(HashCache(os.devnull)).run(
                settings,
                CancellationToken(),
                OperationReporter(events.append, states.append),
            )

        self.assertEqual(result.status, OperationState.COMPLETED)
        self.assertEqual(result.counts["transferred"], 2)
        self.assertEqual(result.data["engine_result"]["bytes_transferred"], 4096)
        phases = {event.phase for event in events}
        self.assertIn(OperationPhase.DISCOVERY, phases)
        self.assertIn(OperationPhase.COMPARISON, phases)
        self.assertIn(OperationPhase.PREFLIGHT, phases)
        self.assertIn(OperationPhase.TRANSFER, phases)
        self.assertIn(OperationPhase.VERIFICATION, phases)
        self.assertIn(OperationPhase.FINALIZATION, phases)
        summary = summarize_transfer_result(result)
        self.assertEqual(summary["New files copied"], "2")
        self.assertEqual(summary["Duplicates skipped"], "3")
        self.assertEqual(summary["Files resumed"], "1")
        self.assertEqual(summary["Data transferred"], "4.0 KB")
        self.assertEqual(summary["Report location"], "report.json")

    def test_partial_cleanup_helper_removes_transfer_partials(self):
        from transfer_safety import APP_STAGING_DIRECTORY, TransferJournal, cleanup_partial_files

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            user_partial = root / "download.partial"
            staging = root / APP_STAGING_DIRECTORY
            staging.mkdir()
            partial = staging / "photo.jpg.dtm-partial"
            keep = root / "photo.jpg"
            partial.write_bytes(b"incomplete")
            user_partial.write_bytes(b"user data")
            keep.write_bytes(b"complete")

            preview = cleanup_partial_files(str(root), dry_run=True)

            self.assertEqual(preview, [str(partial)])
            self.assertTrue(partial.exists())

            removed = cleanup_partial_files(str(root))

            self.assertEqual(removed, [str(partial)])
            self.assertFalse(partial.exists())
            self.assertEqual(user_partial.read_bytes(), b"user data")
            self.assertTrue(keep.exists())

            journal_partial = root / "journal-owned.dtm-partial"
            journal_partial.write_bytes(b"incomplete")
            paths = get_runtime_paths(root / "data")
            journal = TransferJournal(str(paths.journals / "cleanup.journal.json"))
            journal.fail("source", "interrupted", str(journal_partial))
            journal.save(force=True)
            with patch("transfer_safety.get_runtime_paths", return_value=paths):
                removed = cleanup_partial_files(str(root))
            self.assertEqual(removed, [str(journal_partial)])
            self.assertFalse(journal_partial.exists())

    def test_transfer_service_explains_preflight_failure_as_recoverable(self):
        settings = build_import_settings(
            source_root="source",
            existing_library="library",
            source_kind="folder",
        )
        raw = {
            "transferred": 0,
            "duplicates": 0,
            "skipped": 0,
            "errors": 1,
            "preflight_failed": True,
            "preflight_errors": ["Insufficient free space"],
            "preflight_warnings": ["Destination is nearly full"],
        }
        with patch(
            "duplicate_transfer_manager.services.transfer_service.validate_transfer_paths",
            return_value="",
        ), patch(
            "duplicate_transfer_manager.services.transfer_service.execute_smart_transfer",
            return_value=raw,
        ):
            result = TransferService(HashCache(os.devnull)).run(
                settings,
                CancellationToken(),
                OperationReporter(),
            )

        self.assertEqual(result.status, OperationState.FAILED)
        self.assertEqual(result.failures[0].code, "preflight_failed")
        self.assertIn("Insufficient free space", result.failures[0].technical_detail)
        self.assertEqual(result.warnings, ("Destination is nearly full",))
        self.assertTrue(result.resume_information["can_resume"])


if __name__ == "__main__":
    unittest.main()
