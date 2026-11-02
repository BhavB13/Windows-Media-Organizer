"""Tests for transfer outcomes: destination disconnect and FAT32 oversize skips."""

from __future__ import annotations

import os
import tempfile
import unittest
from unittest.mock import Mock, patch

from duplicate_transfer_manager.core import (
    AppSettings,
    CancellationToken,
    ErrorCode,
    OperationFailure,
    OperationReporter,
    OperationResult,
    OperationState,
    StructuredError,
)
from duplicate_transfer_manager.runtime_paths import get_runtime_paths
from duplicate_transfer_manager.services.import_workflow import summarize_transfer_result
from duplicate_transfer_manager.services.transfer_service import TransferService
from models import TransferSettings
from utils import HashCache

try:
    from PySide6.QtWidgets import QApplication
    from duplicate_transfer_manager.ui.pages import ImportPage
    HAS_QT = True
except ImportError:  # pragma: no cover
    HAS_QT = False


def _dummy_settings() -> TransferSettings:
    return TransferSettings(
        source_root="source",
        dest_root="library",
        output_root="output",
        criteria="hash",
        hash_algo="sha256",
        hash_mode="full",
        only_media=False,
        extensions=[],
        min_size_kb=0,
        exclude_dirs=[],
        skip_hidden_system=False,
        dry_run=False,
        preserve_structure=True,
        max_hash_workers=1,
        transfer_mode="copy",
        duplicate_policy="skip",
        use_dest_cache=False,
        source_is_adb=False,
        update_drive_cache=False,
        use_adb_cache=False,
        conflict_policy="rename",
    )


class TransferServiceOutcomeTests(unittest.TestCase):
    def test_destination_disconnected_results_in_recoverable_failed_status(self):
        settings = _dummy_settings()
        raw = {
            "transferred": 2,
            "duplicates": 1,
            "isolated": 0,
            "skipped": 1,
            "resumed": 0,
            "errors": 1,
            "bytes_transferred": 2048,
            "destination_disconnected": True,
            "too_large_skipped": 0,
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
        self.assertTrue(result.resume_information["can_resume"])
        self.assertEqual(len(result.failures), 1)

        failure = result.failures[0]
        self.assertEqual(failure.code, "destination_disconnected")
        self.assertTrue(failure.recoverable)
        self.assertEqual(
            failure.message,
            "The destination drive was disconnected. Nothing on the source was changed. "
            "Reconnect the drive and run the import again to continue where it stopped.",
        )
        self.assertNotIn("file operation(s)", failure.message)
        self.assertNotIn("file operation(s)", failure.technical_detail)
        self.assertIn("reconnect", failure.message.lower())
        self.assertIn("continue where it stopped", failure.message.lower())


class SummarizeTransferResultOutcomeTests(unittest.TestCase):
    def test_too_large_count_appears_in_summary_when_greater_than_zero(self):
        result = OperationResult(
            status=OperationState.COMPLETED,
            counts={"transferred": 5, "duplicates": 2, "resumed": 0, "errors": 0},
            data={
                "engine_result": {
                    "bytes_transferred": 1048576,
                    "too_large_skipped": 3,
                    "destination_disconnected": False,
                }
            },
        )
        summary = summarize_transfer_result(result)
        self.assertIn("Too large for this drive (FAT32)", summary)
        self.assertEqual(summary["Too large for this drive (FAT32)"], "3")

    def test_stopped_destination_disconnected_appears_in_summary_when_flag_set(self):
        result = OperationResult(
            status=OperationState.FAILED,
            counts={"transferred": 1, "duplicates": 0, "resumed": 0, "errors": 1},
            data={
                "engine_result": {
                    "bytes_transferred": 512,
                    "too_large_skipped": 0,
                    "destination_disconnected": True,
                }
            },
        )
        summary = summarize_transfer_result(result)
        self.assertIn("Stopped: destination disconnected", summary)
        self.assertEqual(summary["Stopped: destination disconnected"], "Yes")

    def test_neither_key_appears_when_zero_or_false(self):
        result = OperationResult(
            status=OperationState.COMPLETED,
            counts={"transferred": 4, "duplicates": 1, "resumed": 0, "errors": 0},
            data={
                "engine_result": {
                    "bytes_transferred": 4096,
                    "too_large_skipped": 0,
                    "destination_disconnected": False,
                }
            },
        )
        summary = summarize_transfer_result(result)
        self.assertNotIn("Too large for this drive (FAT32)", summary)
        self.assertNotIn("Stopped: destination disconnected", summary)

    def test_existing_summary_keys_unchanged_for_ordinary_run(self):
        result = OperationResult(
            status=OperationState.COMPLETED,
            counts={"transferred": 3, "duplicates": 2, "resumed": 1, "errors": 0},
            report_path="/reports/report.json",
            data={
                "engine_result": {
                    "bytes_transferred": 2048,
                    "too_large_skipped": 0,
                    "destination_disconnected": False,
                }
            },
        )
        summary = summarize_transfer_result(result)
        expected_keys = {
            "New files copied",
            "Duplicates skipped",
            "Files resumed",
            "Errors",
            "Data transferred",
            "Report location",
        }
        self.assertEqual(set(summary.keys()), expected_keys)
        self.assertEqual(summary["New files copied"], "3")
        self.assertEqual(summary["Duplicates skipped"], "2")
        self.assertEqual(summary["Files resumed"], "1")
        self.assertEqual(summary["Errors"], "0")
        self.assertEqual(summary["Data transferred"], "2.0 KB")
        self.assertEqual(summary["Report location"], "/reports/report.json")


@unittest.skipUnless(HAS_QT, "PySide6 required for ImportPage tests")
class ImportPageOutcomeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.application = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.paths = get_runtime_paths(self.temp_dir.name)
        self.settings = AppSettings()
        self.page = ImportPage(settings=self.settings)

    def tearDown(self):
        self.page.deleteLater()
        self.temp_dir.cleanup()

    def test_import_page_shows_disconnect_warning_banner_and_leaves_run_enabled(self):
        disconnect_message = (
            "The destination drive was disconnected. Nothing on the source was changed. "
            "Reconnect the drive and run the import again to continue where it stopped."
        )
        result = OperationResult(
            status=OperationState.FAILED,
            counts={"transferred": 1, "duplicates": 0, "resumed": 0, "errors": 1},
            failures=(
                OperationFailure(
                    code="destination_disconnected",
                    message=disconnect_message,
                    technical_detail=disconnect_message,
                    recoverable=True,
                ),
            ),
            data={
                "engine_result": {
                    "bytes_transferred": 1024,
                    "destination_disconnected": True,
                    "too_large_skipped": 0,
                }
            },
        )

        self.page._on_completed(result)
        self.assertFalse(self.page.banner.isHidden())
        self.assertEqual(self.page.banner.property("banner"), "warning")
        self.assertIn("The destination drive was disconnected", self.page.banner.label.text())
        self.assertIn("Reconnect the drive and run the import again", self.page.banner.label.text())
        self.assertTrue(self.page.run_button.isEnabled())

    def test_import_page_handles_disconnect_failure_signal_and_leaves_run_enabled(self):
        disconnect_message = (
            "The destination drive was disconnected. Nothing on the source was changed. "
            "Reconnect the drive and run the import again to continue where it stopped."
        )
        error = StructuredError(
            code=ErrorCode.IO_ERROR,
            title="Operation needs attention",
            message="The operation stopped before it could complete.",
            technical_detail=disconnect_message,
            recoverable=True,
        )

        self.page._on_failed(error)
        self.assertFalse(self.page.banner.isHidden())
        self.assertEqual(self.page.banner.property("banner"), "warning")
        self.assertIn("The destination drive was disconnected", self.page.banner.label.text())
        self.assertTrue(self.page.run_button.isEnabled())

    def test_import_page_shows_fat32_banner_with_count(self):
        result = OperationResult(
            status=OperationState.COMPLETED,
            counts={"transferred": 3, "duplicates": 0, "resumed": 0, "errors": 0},
            data={
                "engine_result": {
                    "bytes_transferred": 4096,
                    "destination_disconnected": False,
                    "too_large_skipped": 2,
                }
            },
        )

        self.page._on_completed(result)
        self.assertFalse(self.page.banner.isHidden())
        self.assertEqual(self.page.banner.property("banner"), "info")
        banner_text = self.page.banner.label.text()
        self.assertIn("2", banner_text)
        self.assertIn("FAT32", banner_text)
        self.assertTrue("exFAT or NTFS" in banner_text or ("exFAT" in banner_text and "NTFS" in banner_text))

    def test_import_page_single_fat32_file_count_banner(self):
        result = OperationResult(
            status=OperationState.COMPLETED,
            counts={"transferred": 1, "duplicates": 0, "resumed": 0, "errors": 0},
            data={
                "engine_result": {
                    "bytes_transferred": 1024,
                    "destination_disconnected": False,
                    "too_large_skipped": 1,
                }
            },
        )

        self.page._on_completed(result)
        self.assertFalse(self.page.banner.isHidden())
        self.assertEqual(self.page.banner.property("banner"), "info")
        banner_text = self.page.banner.label.text()
        self.assertIn("1", banner_text)
        self.assertIn("FAT32", banner_text)
        self.assertTrue("exFAT or NTFS" in banner_text or ("exFAT" in banner_text and "NTFS" in banner_text))

    def test_import_page_preserves_existing_behaviour_for_ordinary_success_and_error(self):
        success_result = OperationResult(
            status=OperationState.COMPLETED,
            counts={"transferred": 2, "duplicates": 0, "resumed": 0, "errors": 0},
            data={"engine_result": {"bytes_transferred": 2048}},
        )
        self.page._on_completed(success_result)
        self.assertEqual(self.page.banner.property("banner"), "success")
        self.assertIn("Import complete", self.page.banner.label.text())

        error_result = OperationResult(
            status=OperationState.COMPLETED,
            counts={"transferred": 1, "duplicates": 0, "resumed": 0, "errors": 2},
            data={"engine_result": {"bytes_transferred": 1024}},
        )
        self.page._on_completed(error_result)
        self.assertEqual(self.page.banner.property("banner"), "warning")
        self.assertIn("2 file(s) could not be copied", self.page.banner.label.text())


if __name__ == "__main__":
    unittest.main()
