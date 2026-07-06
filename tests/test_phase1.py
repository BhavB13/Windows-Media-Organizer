import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from duplicate_transfer_manager.controllers import (
    DeviceController,
    DiagnosticsController,
    DuplicateScanController,
    QuarantineController,
    ReportsController,
    SettingsController,
    TransferController,
    UpdateController,
)
from duplicate_transfer_manager.controllers.base import BaseOperationController
from duplicate_transfer_manager.controllers.qt_compat import PYSIDE_AVAILABLE
from duplicate_transfer_manager.core import (
    AppSettings,
    CancellationToken,
    ErrorCode,
    OperationEvent,
    OperationPhase,
    OperationReporter,
    OperationResult,
    OperationState,
    QuarantineRecord,
    Severity,
    map_exception,
)
from duplicate_transfer_manager.services import (
    DuplicateScanService,
    QuarantineService,
    ReportService,
    SettingsService,
    TransferService,
)
from duplicate_transfer_manager.runtime_paths import get_runtime_paths
from models import Settings, TransferSettings
from utils import DEFAULT_EXCLUDES, DEFAULT_MEDIA_EXTS, HashCache


if PYSIDE_AVAILABLE:
    from PySide6.QtCore import QCoreApplication


def process_qt_events():
    if PYSIDE_AVAILABLE:
        application = QCoreApplication.instance() or QCoreApplication([])
        application.processEvents()


class CoreContractTests(unittest.TestCase):
    def test_operation_event_contains_all_public_progress_fields(self):
        event = OperationEvent(
            phase=OperationPhase.TRANSFER,
            state=OperationState.TRANSFERRING,
            message="Copying",
            progress=0.5,
            bytes_processed=50,
            total_bytes=100,
            processed_items=2,
            total_items=4,
            current_item="photo.jpg",
            rate=25,
            eta_seconds=2,
            severity=Severity.INFO,
        )
        self.assertEqual(event.progress, 0.5)
        self.assertEqual(event.current_item, "photo.jpg")
        self.assertEqual(event.bytes_processed, 50)
        self.assertEqual(event.total_items, 4)

    def test_reporter_converts_legacy_progress_to_structured_event(self):
        events = []
        states = []
        reporter = OperationReporter(events.append, states.append)
        reporter.set_state(
            OperationState.TRANSFERRING,
            phase=OperationPhase.TRANSFER,
        )
        reporter.progress_callback(25, 100, "Processed 25/100")

        self.assertEqual(states, [OperationState.TRANSFERRING])
        self.assertEqual(events[-1].progress, 0.25)
        self.assertEqual(events[-1].processed_items, 25)
        self.assertEqual(events[-1].phase, OperationPhase.TRANSFER)

    def test_reporter_recognizes_byte_progress(self):
        events = []
        reporter = OperationReporter(events.append)
        reporter.progress_callback(
            1024,
            4096,
            "Pulling photo.jpg: 0.0/0.0 MB | Overall 0.0/0.0 GB",
        )
        self.assertEqual(events[-1].bytes_processed, 1024)
        self.assertEqual(events[-1].total_bytes, 4096)
        self.assertEqual(events[-1].processed_items, 0)

    def test_error_mapping_preserves_safe_and_technical_messages(self):
        error = map_exception(PermissionError("secret technical detail"))
        self.assertEqual(error.code, ErrorCode.PERMISSION_DENIED)
        self.assertTrue(error.recoverable)
        self.assertNotIn("secret technical detail", error.message)
        self.assertIn("secret technical detail", error.technical_detail)

    def test_app_settings_round_trip_ignores_unknown_fields(self):
        values = AppSettings(
            appearance="dark",
            experience_mode="advanced",
            diagnostic_consent=True,
        ).to_dict()
        values["future_setting"] = "ignored"
        loaded = AppSettings.from_dict(values)
        self.assertEqual(loaded.appearance, "dark")
        self.assertEqual(loaded.experience_mode, "advanced")
        self.assertTrue(loaded.diagnostic_consent)

    def test_cancellation_token_is_event_compatible(self):
        token = CancellationToken()
        self.assertFalse(token.is_set())
        token.cancel()
        self.assertTrue(token.is_set())
        with self.assertRaises(RuntimeError):
            token.raise_if_cancelled()


class ServiceTests(unittest.TestCase):
    def test_duplicate_service_finds_groups_without_frontend_objects(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "a.jpg").write_bytes(b"same-content")
            (root / "b.jpg").write_bytes(b"same-content")
            (root / "unique.jpg").write_bytes(b"different")
            cache = HashCache(str(root / "cache.json"))
            service = DuplicateScanService(cache)
            settings = Settings(
                scan_root=str(root),
                output_root="",
                criteria="hash",
                hash_algo="sha256",
                hash_mode="full",
                only_media=True,
                extensions=DEFAULT_MEDIA_EXTS,
                min_size_kb=0,
                exclude_dirs=DEFAULT_EXCLUDES,
                skip_hidden_system=True,
                dry_run=True,
                preserve_structure=True,
                max_hash_workers=2,
            )
            events = []
            result = service.run(
                settings,
                CancellationToken(),
                OperationReporter(events.append),
            )

            self.assertEqual(result.status, OperationState.COMPLETED)
            self.assertEqual(result.counts["duplicate_groups"], 1)
            self.assertEqual(result.counts["duplicate_files"], 1)
            self.assertEqual(len(result.data["groups"][0]), 2)
            self.assertTrue(any(event.phase == OperationPhase.HASHING for event in events))

    def test_duplicate_service_returns_cancelled_result(self):
        token = CancellationToken()
        token.cancel()
        settings = Settings(
            scan_root=".",
            output_root="",
            criteria="hash",
            hash_algo="sha256",
            hash_mode="full",
            only_media=True,
            extensions=DEFAULT_MEDIA_EXTS,
            min_size_kb=0,
            exclude_dirs=DEFAULT_EXCLUDES,
            skip_hidden_system=True,
            dry_run=True,
            preserve_structure=True,
            max_hash_workers=1,
        )
        with self.assertRaises(RuntimeError):
            DuplicateScanService(HashCache("unused.json")).run(
                settings,
                token,
                OperationReporter(),
            )

    def test_transfer_service_maps_engine_result(self):
        settings = TransferSettings(
            source_root="source",
            dest_root="library",
            output_root="",
            criteria="hash",
            hash_algo="sha256",
            hash_mode="full",
            only_media=True,
            extensions=[".jpg"],
            min_size_kb=0,
            exclude_dirs=[],
            skip_hidden_system=True,
            dry_run=True,
            preserve_structure=True,
            max_hash_workers=1,
            transfer_mode="copy",
            duplicate_policy="skip",
            use_dest_cache=False,
        )
        raw = {
            "transferred": 3,
            "duplicates": 2,
            "isolated": 0,
            "skipped": 2,
            "resumed": 1,
            "errors": 0,
            "dry_run": True,
        }
        with patch(
            "duplicate_transfer_manager.services.transfer_service.validate_transfer_paths",
            return_value="",
        ):
            with patch(
                "duplicate_transfer_manager.services.transfer_service.execute_smart_transfer",
                return_value=raw,
            ):
                result = TransferService(HashCache("unused.json")).run(
                    settings,
                    CancellationToken(),
                    OperationReporter(),
                )

        self.assertEqual(result.status, OperationState.COMPLETED)
        self.assertEqual(result.counts["transferred"], 3)
        self.assertEqual(result.resume_information["resumed_files"], 1)

    def test_settings_service_uses_atomic_json_and_defaults_on_invalid_data(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            paths = get_runtime_paths(temp_dir)
            service = SettingsService(paths)
            expected = AppSettings(appearance="dark", experience_mode="advanced")
            service.save(expected)
            self.assertEqual(service.load(), expected)
            paths.settings_file.write_text("{broken", encoding="utf-8")
            self.assertEqual(service.load(), AppSettings())

    def test_report_service_rejects_paths_outside_report_directory(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            paths = get_runtime_paths(temp_dir)
            service = ReportService(paths)
            outside = Path(temp_dir) / "outside.json"
            outside.write_text("{}", encoding="utf-8")
            with self.assertRaises(ValueError):
                service.load_report(outside)

    def test_quarantine_service_reads_list_and_record_manifests(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            paths = get_runtime_paths(temp_dir)
            first = QuarantineRecord("a", "stored-a", "hash-a", 1, "duplicate", "op")
            second = QuarantineRecord("b", "stored-b", "hash-b", 2, "duplicate", "op")
            (paths.quarantine / "one.json").write_text(
                json.dumps([first.to_dict()]),
                encoding="utf-8",
            )
            (paths.quarantine / "two.json").write_text(
                json.dumps({"records": [second.to_dict()]}),
                encoding="utf-8",
            )
            records = QuarantineService(paths).list_records()
            self.assertEqual({record.original_path for record in records}, {"a", "b"})


class ControllerTests(unittest.TestCase):
    def wait(self, controller):
        self.assertTrue(controller.wait_for_done(5000))
        process_qt_events()

    def test_base_controller_runs_task_off_calling_thread_and_emits_completion(self):
        controller = BaseOperationController()
        caller_thread = threading.get_ident()
        worker_threads = []
        results = []
        states = []
        controller.completed.connect(results.append)
        controller.state_changed.connect(states.append)

        def task(_cancellation, reporter):
            worker_threads.append(threading.get_ident())
            reporter.set_state(OperationState.SCANNING)
            return OperationResult(status=OperationState.COMPLETED)

        self.assertTrue(controller.start_task(task))
        self.wait(controller)

        self.assertNotEqual(worker_threads[0], caller_thread)
        self.assertEqual(results[0].status, OperationState.COMPLETED)
        self.assertIn(OperationState.SCANNING, states)
        self.assertEqual(controller.state, OperationState.COMPLETED)

    def test_controller_rejects_overlapping_operations(self):
        controller = BaseOperationController()
        release = threading.Event()
        errors = []
        controller.recoverable_error.connect(errors.append)

        def task(_cancellation, _reporter):
            release.wait(2)
            return OperationResult(status=OperationState.COMPLETED)

        self.assertTrue(controller.start_task(task))
        self.assertFalse(controller.start_task(task))
        release.set()
        self.wait(controller)
        self.assertEqual(errors[0].code, ErrorCode.OPERATION_IN_PROGRESS)

    def test_controller_cancellation_emits_cancelled_result(self):
        controller = BaseOperationController()
        started = threading.Event()
        results = []
        controller.cancelled.connect(results.append)

        def task(cancellation, _reporter):
            started.set()
            while not cancellation.wait(0.01):
                pass
            return OperationResult(status=OperationState.CANCELLED)

        controller.start_task(task)
        self.assertTrue(started.wait(2))
        self.assertTrue(controller.cancel())
        self.wait(controller)
        self.assertEqual(results[0].status, OperationState.CANCELLED)
        self.assertEqual(controller.state, OperationState.CANCELLED)

    def test_controller_maps_recoverable_failures(self):
        controller = BaseOperationController()
        recoverable = []
        failed = []
        technical_logs = []
        controller.recoverable_error.connect(recoverable.append)
        controller.failed.connect(failed.append)
        controller.technical_log.connect(technical_logs.append)

        def task(_cancellation, _reporter):
            raise PermissionError("denied")

        controller.start_task(task)
        self.wait(controller)
        self.assertEqual(recoverable[0].code, ErrorCode.PERMISSION_DENIED)
        self.assertEqual(failed[0].code, ErrorCode.PERMISSION_DENIED)
        self.assertEqual(controller.state, OperationState.FAILED)
        self.assertIn("denied", technical_logs[0])

    @unittest.skipUnless(PYSIDE_AVAILABLE, "Requires real PySide6 queued signals")
    def test_real_qt_delivers_controller_callbacks_on_main_thread(self):
        controller = BaseOperationController()
        main_thread = threading.get_ident()
        callback_threads = []
        controller.completed.connect(
            lambda _result: callback_threads.append(threading.get_ident())
        )

        controller.start_task(
            lambda _cancellation, _reporter: OperationResult(
                status=OperationState.COMPLETED
            )
        )
        self.wait(controller)
        self.assertEqual(callback_threads, [main_thread])

    def test_all_required_controller_types_expose_operation_signals(self):
        controllers = [
            DuplicateScanController(HashCache("unused.json")),
            TransferController(HashCache("unused.json")),
            DeviceController(),
            ReportsController(),
            QuarantineController(),
            SettingsController(),
            DiagnosticsController(),
            UpdateController(),
        ]
        for controller in controllers:
            for signal_name in (
                "progress",
                "state_changed",
                "recoverable_error",
                "completed",
                "cancelled",
                "failed",
            ):
                self.assertTrue(hasattr(controller, signal_name))

    def test_settings_controller_returns_data_through_completion_signal(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            service = SettingsService(get_runtime_paths(temp_dir))
            service.save(AppSettings(appearance="dark"))
            controller = SettingsController(service)
            results = []
            controller.completed.connect(results.append)
            controller.load()
            self.wait(controller)
            self.assertEqual(results[0].data["settings"].appearance, "dark")


if __name__ == "__main__":
    unittest.main()
