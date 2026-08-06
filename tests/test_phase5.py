import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
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
    ScheduledScanService,
    build_import_settings,
)
from duplicate_transfer_manager.scheduled_scan import run_scheduled_scan
from duplicate_transfer_manager.scheduled_organizer import run_scheduled_preview


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

    def test_audit_history_is_retained_after_activity_record_removal(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            service = OperationRecordService(get_runtime_paths(temp_dir))
            record = service.record("import", "completed", title="Camera import", counts={"transferred": 2})
            service.remove_record(record["id"])
            audit = service.list_audit_events()

        self.assertEqual(len(audit), 1)
        self.assertEqual(audit[0]["title"], "Camera import")
        self.assertNotIn("report_path", audit[0])

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
            dry_export = reports.export_report(report, paths.root / "dry-export.json", dry_run=True)
            self.assertEqual(dry_export, str(paths.root / "dry-export.json"))
            self.assertFalse((paths.root / "dry-export.json").exists())
            exported = reports.export_report(report, paths.root / "exported.json")
            self.assertTrue(Path(exported).exists())
            reports.remove_report(report, dry_run=True)
            self.assertTrue(report.exists())
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
                summary = DashboardService(paths).summary(include_devices=True)

        self.assertEqual(len(summary["recent_operations"]), 1)
        self.assertEqual(len(summary["interrupted_transfers"]), 1)
        self.assertEqual(summary["connected_devices"][0]["serial"], "phone-1")
        self.assertEqual(summary["recoverable_bytes"], 10)
        self.assertGreaterEqual(summary["storage"]["cache_bytes"], 4)

    def test_activity_csv_export_is_path_free_and_supports_dry_run(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            paths = get_runtime_paths(temp_dir)
            service = OperationRecordService(paths)
            record = service.record(
                "import",
                "warning",
                title="Camera import",
                counts={"transferred": 2, "errors": 1},
                warnings=["Destination space is low"],
                resume_available=True,
            )
            destination = paths.root / "activity-export.csv"

            preview = service.export_records_csv([record], destination, dry_run=True)
            self.assertEqual(preview, str(destination))
            self.assertFalse(destination.exists())
            exported = service.export_records_csv([record], destination)
            content = Path(exported).read_text(encoding="utf-8")

        self.assertIn("Camera import", content)
        self.assertIn("transferred: 2", content)
        self.assertNotIn(str(paths.root), content)

    def test_cache_retention_prunes_only_expired_cache_files(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            paths = get_runtime_paths(temp_dir)
            old_cache = paths.cache / "old.bin"
            new_cache = paths.cache / "new.bin"
            protected_report = paths.reports / "old-report.json"
            old_cache.write_bytes(b"old")
            new_cache.write_bytes(b"new")
            protected_report.write_bytes(b"report")
            old_time = (datetime.now(timezone.utc) - timedelta(days=10)).timestamp()
            os.utime(old_cache, (old_time, old_time))
            removed = DashboardService(paths).prune_cache(5)
            self.assertEqual(removed, 3)
            self.assertFalse(old_cache.exists())
            self.assertTrue(new_cache.exists())
            self.assertTrue(protected_report.exists())

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
                favorite_locations=["D:/Photos", "E:/Camera Backup"],
                scheduled_scan_path="D:/Photos",
                scheduled_scan_frequency="weekly",
                organization_retention_days=45,
                organization_presets=[{"name": "Downloads", "mode": "type"}],
                organization_schedule_frequency="weekly",
                onboarding_completed=True,
            )
            service.save(expected)

            loaded = service.load()

        self.assertEqual(loaded.default_transfer_profile, "Reliable")
        self.assertEqual(loaded.default_file_categories, ["pictures", "audio"])
        self.assertEqual(loaded.cache_retention_days, 30)
        self.assertFalse(loaded.android_enabled)
        self.assertEqual(loaded.update_channel, "beta")
        self.assertEqual(loaded.favorite_locations, ["D:/Photos", "E:/Camera Backup"])
        self.assertEqual(loaded.scheduled_scan_frequency, "weekly")
        self.assertEqual(loaded.scheduled_scan_path, "D:/Photos")
        self.assertEqual(loaded.organization_retention_days, 45)
        self.assertEqual(loaded.organization_presets[0]["name"], "Downloads")
        self.assertEqual(loaded.organization_schedule_frequency, "weekly")
        self.assertTrue(loaded.onboarding_completed)

    def test_import_settings_can_be_dry_run(self):
        settings = build_import_settings(
            source_root="C:/source",
            existing_library="C:/library",
            dry_run=True,
        )

        self.assertTrue(settings.dry_run)

    def test_scheduled_scan_is_read_only_and_records_the_outcome(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "source"
            source.mkdir()
            first = source / "first.jpg"
            second = source / "second.jpg"
            first.write_bytes(b"same")
            second.write_bytes(b"same")
            data_root = root / "data"

            exit_code = run_scheduled_scan(str(source), data_root=str(data_root))
            records = OperationRecordService(get_runtime_paths(data_root)).list_records()
            self.assertEqual(exit_code, 0)
            self.assertTrue(first.exists())
            self.assertTrue(second.exists())
            self.assertEqual(records[0]["type"], "scheduled_duplicate_scan")
            self.assertTrue(records[0]["summary"]["read_only"])

    def test_windows_scheduler_creates_a_read_only_cli_task(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            source = Path(temp_dir) / "source"
            source.mkdir()
            scheduler = ScheduledScanService()
            completed = __import__("subprocess").CompletedProcess([], 0, "", "")
            with patch("duplicate_transfer_manager.services.support_services.os.name", "nt"), patch(
                "duplicate_transfer_manager.services.support_services.subprocess.run",
                return_value=completed,
            ) as run:
                scheduler.configure(str(source), "weekly", data_root=str(Path(temp_dir) / "data"))

        command = run.call_args.args[0]
        self.assertIn("/Create", command)
        self.assertIn("WEEKLY", command)
        self.assertIn("duplicate_transfer_manager.scheduled_scan", command[-1])
        self.assertIn("--source", command[-1])

    def test_scheduled_organizer_preview_is_read_only_and_records_plan(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "source"
            destination = root / "destination"
            (source / "nested").mkdir(parents=True)
            destination.mkdir()
            original = source / "nested" / "photo.jpg"
            original.write_bytes(b"image")
            data_root = root / "data"

            exit_code = run_scheduled_preview(str(source), str(destination), mode="flatten", data_root=str(data_root))
            records = OperationRecordService(get_runtime_paths(data_root)).list_records()

            self.assertEqual(exit_code, 0)
            self.assertTrue(original.exists())
            self.assertEqual(records[0]["type"], "scheduled_organization_preview")
            self.assertTrue(records[0]["summary"]["read_only"])

    def test_windows_scheduler_creates_organizer_preview_task(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "source"
            destination = root / "destination"
            source.mkdir()
            destination.mkdir()
            scheduler = ScheduledScanService()
            completed = __import__("subprocess").CompletedProcess([], 0, "", "")
            with patch("duplicate_transfer_manager.services.support_services.os.name", "nt"), patch(
                "duplicate_transfer_manager.services.support_services.subprocess.run", return_value=completed
            ) as run:
                scheduler.configure_organizer_preview(str(source), str(destination), "type", "daily", data_root=str(root / "data"))

        command = run.call_args.args[0]
        self.assertIn(ScheduledScanService.organizer_task_name, command)
        self.assertIn("DAILY", command)
        self.assertIn("duplicate_transfer_manager.scheduled_organizer", command[-1])

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
