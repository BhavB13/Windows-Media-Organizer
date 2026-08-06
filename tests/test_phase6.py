import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from duplicate_transfer_manager.core import sanitize_payload, sanitize_text
from duplicate_transfer_manager.runtime_paths import get_runtime_paths, migrate_legacy_data
from duplicate_transfer_manager.services import (
    CrashReportService,
    DiagnosticsService,
    DuplicateQuarantineService,
    OperationRecordService,
)
from duplicate_transfer_manager.services.duplicate_workflow import (
    DuplicateGroup,
    DuplicateItem,
    DuplicateReview,
)


class Phase6ReliabilitySecurityTests(unittest.TestCase):
    def test_sanitizer_redacts_paths_hashes_and_device_serials(self):
        text = (
            "Failed C:\\Users\\Bhave\\Pictures\\photo.jpg on ABCDEF123456 "
            "with e3b0c44298fc1c149afbf4c8996fb924"
        )

        sanitized = sanitize_text(text)

        self.assertNotIn("Bhave", sanitized)
        self.assertNotIn("ABCDEF123456", sanitized)
        self.assertNotIn("e3b0c44298fc1c149afbf4c8996fb924", sanitized)
        self.assertIn("<redacted:path:", sanitized)
        self.assertIn("<redacted:serial:", sanitized)
        self.assertIn("<redacted:hash:", sanitized)

    def test_diagnostics_are_sanitized_and_sentry_is_opt_in(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            paths = get_runtime_paths(temp_dir)
            with patch(
                "duplicate_transfer_manager.services.support_services.ADBBridge.list_devices",
                return_value=[{"serial": "ABCDEF123456", "path": "C:\\Users\\Bhave\\phone"}],
            ):
                diagnostics = DiagnosticsService(paths).collect(include_devices=True)

        self.assertTrue(diagnostics["diagnostics_sanitized"])
        self.assertFalse(diagnostics["sentry"]["enabled"])
        self.assertTrue(diagnostics["sentry"]["requires_explicit_consent"])
        serialized = json.dumps(diagnostics)
        self.assertNotIn("ABCDEF123456", serialized)
        self.assertNotIn("Bhave", serialized)

    def test_crash_report_is_local_sanitized_and_not_submitted(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            service = CrashReportService(get_runtime_paths(temp_dir))
            report = service.create_report(
                RuntimeError("Could not read C:\\Users\\Bhave\\secret.jpg"),
                context={"device_serial": "ABCDEF123456"},
            )
            report_path = Path(report["path"])
            saved = report_path.read_text(encoding="utf-8")

        self.assertTrue(report["sanitized"])
        self.assertFalse(report["submission"]["sentry_enabled"])
        self.assertTrue(report_path.name.startswith("crash_"))
        self.assertNotIn("Bhave", saved)
        self.assertNotIn("ABCDEF123456", saved)

    def test_quarantine_preflight_records_recoverable_failures_without_moving_missing_file(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            paths = get_runtime_paths(temp_dir)
            missing = Path(temp_dir) / "missing.jpg"
            service = DuplicateQuarantineService(paths)

            item = DuplicateItem(
                id="missing",
                path=str(missing),
                filename="missing.jpg",
                size=3,
                modified=0,
                created_label="",
                device="This PC",
            )
            review = DuplicateReview(
                groups=(
                    DuplicateGroup(
                        id="group",
                        hash="hash",
                        items=(item,),
                        keep_item_id="other",
                        selected_item_ids=("missing",),
                    ),
                )
            )
            result = service.quarantine(review, ["missing"], operation_id="op-preflight")

            self.assertEqual(result.quarantined_count, 0)
            self.assertEqual(len(result.failures), 1)
            self.assertFalse(missing.exists())

    def test_operation_record_captures_recoverable_failure_context(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            service = OperationRecordService(get_runtime_paths(temp_dir))
            service.record(
                "import",
                "failed",
                title="Interrupted import",
                resume_available=True,
                failures=["Device disconnected"],
            )
            records = service.list_records()

        self.assertEqual(records[0]["status"], "failed")
        self.assertTrue(records[0]["resume_available"])
        self.assertEqual(records[0]["failures"], ["Device disconnected"])

    def test_migration_keeps_sources_and_reports_marker_on_second_run(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            legacy = root / "legacy"
            legacy.mkdir()
            (legacy / "hash_cache.json").write_text("{}", encoding="utf-8")
            paths = get_runtime_paths(root / "data")

            first = migrate_legacy_data(legacy, paths)
            second = migrate_legacy_data(legacy, paths)

            self.assertEqual(len(first.copied), 1)
            self.assertTrue((legacy / "hash_cache.json").exists())
            self.assertTrue(second.already_completed)

    def test_sanitize_payload_redacts_sensitive_keys_recursively(self):
        payload = {
            "report_path": "C:\\Users\\Bhave\\report.json",
            "items": [{"hash": "a" * 64, "message": "/home/bhav/photo.jpg"}],
        }

        sanitized = sanitize_payload(payload)
        serialized = json.dumps(sanitized)

        self.assertNotIn("Bhave", serialized)
        self.assertNotIn("a" * 64, serialized)
        self.assertNotIn("/home/bhav", serialized)


if __name__ == "__main__":
    unittest.main()
