import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from duplicate_transfer_manager.core import AppSettings, QuarantineRecord
from duplicate_transfer_manager.runtime_paths import get_runtime_paths
from duplicate_transfer_manager.services import (
    DashboardService,
    DiagnosticsService,
    OperationRecordService,
    QuarantineService,
    ReportService,
    SettingsService,
)


class Phase5OverviewActivitySettingsTests(unittest.TestCase):
    def test_operation_records_are_persisted_and_listed_newest_first(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            service = OperationRecordService(get_runtime_paths(temp_dir))
            first = service.record("import", "completed", title="First import", counts={"transferred": 1})
            second = service.record("duplicate_scan", "warning", title="Scan", resume_available=True)

            records = service.list_records()

        self.assertEqual(records[0]["id"], second["id"])
        self.assertEqual(records[1]["id"], first["id"])
        self.assertTrue(records[0]["resume_available"])

    def test_activity_includes_existing_transfer_reports_and_report_actions(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            paths = get_runtime_paths(temp_dir)
            report = paths.reports / "transfer_report.json"
            report.write_text(
                json.dumps(
                    {
                        "created_at": "2026-07-06T12:00:00",
                        "transferred": 3,
                        "duplicates": 2,
                        "errors": 0,
                        "resumed": 1,
                    }
                ),
                encoding="utf-8",
            )
            operations = OperationRecordService(paths)
            reports = ReportService(paths)

            listed = operations.list_records()
            exported = reports.export_report(report, paths.root / "exported.json")
            self.assertTrue(Path(exported).exists())
            reports.remove_report(report)
            self.assertFalse(report.exists())

        self.assertEqual(listed[0]["type"], "import")
        self.assertEqual(listed[0]["counts"]["transferred"], 3)

    def test_dashboard_summarizes_recent_interrupted_storage_and_quarantine(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            paths = get_runtime_paths(temp_dir)
            OperationRecordService(paths).record("import", "cancelled", resume_available=True)
            (paths.cache / "cache.bin").write_bytes(b"1234")
            record = QuarantineRecord(
                original_path="a.jpg",
                stored_path=str(paths.quarantine / "op" / "a.jpg"),
                hash="hash",
                size=10,
                reason="duplicate",
                operation_id="op",
            )
            (paths.quarantine / "op").mkdir()
            (paths.quarantine / "op" / "manifest.json").write_text(
                json.dumps({"records": [record.to_dict()]}),
                encoding="utf-8",
            )

            with patch(
                "duplicate_transfer_manager.services.support_services.ADBBridge.list_devices",
                return_value=[{"serial": "phone-1", "state": "device"}],
            ):
                summary = DashboardService(paths).summary()

        self.assertEqual(len(summary["recent_operations"]), 1)
        self.assertEqual(len(summary["interrupted_transfers"]), 1)
        self.assertEqual(summary["connected_devices"][0]["serial"], "phone-1")
        self.assertEqual(summary["recoverable_bytes"], 10)
        self.assertGreaterEqual(summary["storage"]["cache_bytes"], 4)

    def test_settings_round_trip_phase5_fields(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            service = SettingsService(get_runtime_paths(temp_dir))
            expected = AppSettings(
                appearance="dark",
                experience_mode="advanced",
                default_file_categories=["pictures", "audio"],
                default_transfer_profile="Reliable",
                cache_retention_days=30,
                android_enabled=False,
                android_default_path="/sdcard/Pictures",
                keep_android_awake=False,
                diagnostic_consent=True,
                update_channel="beta",
                onboarding_completed=True,
            )
            service.save(expected)

            loaded = service.load()

        self.assertEqual(loaded.default_transfer_profile, "Reliable")
        self.assertEqual(loaded.default_file_categories, ["pictures", "audio"])
        self.assertEqual(loaded.cache_retention_days, 30)
        self.assertFalse(loaded.android_enabled)
        self.assertEqual(loaded.update_channel, "beta")
        self.assertTrue(loaded.onboarding_completed)

    def test_diagnostics_reports_pinned_platform_tools_without_system_mutation(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            diagnostics = DiagnosticsService(get_runtime_paths(temp_dir)).collect(include_devices=False)

        platform_tools = diagnostics["android_platform_tools"]
        self.assertEqual(platform_tools["pinned_version"], "37.0.0")
        self.assertFalse(platform_tools["system_path_modified"])
        self.assertIn("Never modify", platform_tools["system_path_policy"])

    def test_quarantine_service_lists_manifest_records_for_grouping_and_filtering(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            paths = get_runtime_paths(temp_dir)
            records = [
                QuarantineRecord("local.jpg", "stored-local", "h1", 1, "duplicate", "op1"),
                QuarantineRecord("phone.jpg", "stored-phone", "h2", 2, "duplicate", "op1", source_is_adb=True),
                QuarantineRecord("restored.jpg", "stored-restored", "h3", 3, "duplicate", "op2", restored_at="now"),
            ]
            (paths.quarantine / "op1").mkdir()
            (paths.quarantine / "op1" / "manifest.json").write_text(
                json.dumps({"records": [record.to_dict() for record in records[:2]]}),
                encoding="utf-8",
            )
            (paths.quarantine / "op2").mkdir()
            (paths.quarantine / "op2" / "manifest.json").write_text(
                json.dumps({"records": [records[2].to_dict()]}),
                encoding="utf-8",
            )

            loaded = QuarantineService(paths).list_records()

        self.assertEqual(len(loaded), 3)
        self.assertEqual(len({record.operation_id for record in loaded}), 2)
        self.assertTrue(any(record.source_is_adb for record in loaded))


if __name__ == "__main__":
    unittest.main()
