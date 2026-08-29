import os
import json
import shutil
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch


os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QCoreApplication, QEvent, QSize, QThreadPool
from PySide6.QtGui import QResizeEvent
from PySide6.QtWidgets import QApplication, QPushButton
from shiboken6 import delete

from duplicate_transfer_manager.controllers import DiskAuditController
from duplicate_transfer_manager.core import (
    AppSettings,
    AuditTier,
    BackupStatus,
    CancellationToken,
    DiskAuditItem,
    DiskAuditSettings,
    DiskAuditSnapshot,
    OperationCancelled,
    OperationReporter,
    OperationResult,
    OperationState,
)
from duplicate_transfer_manager.runtime_paths import get_runtime_paths
from duplicate_transfer_manager.services import (
    AuditSnapshotStore,
    DashboardService,
    DiskAuditService,
    OperationRecordService,
    ReportService,
    SettingsService,
    VolumeDescriptor,
    WindowsDiskInventory,
    compare_snapshots,
)
from duplicate_transfer_manager.services.disk_audit import CatalogEntry
from duplicate_transfer_manager.ui.disk_audit_page import DiskAuditPage, FindingCard, open_audit_path
from duplicate_transfer_manager.ui.pages import ActivityPage
from duplicate_transfer_manager.ui.widgets import ResponsiveGrid


def make_snapshot(audit_id, volume_id="volume-test", mode="quick", free=100, items=(), completed=""):
    completed = completed or datetime.now(timezone.utc).isoformat()
    return DiskAuditSnapshot(
        schema_version=1,
        audit_id=audit_id,
        volume_id=volume_id,
        volume_label="Fixture",
        scanned_root="X:\\",
        mode=mode,
        started_at=completed,
        completed_at=completed,
        capacity_bytes=1000,
        used_bytes=1000 - free,
        free_bytes=free,
        items=tuple(items),
    )


class FixtureInventory:
    def __init__(self, root, catalog=(), applications=()):
        self.root = str(root)
        self.entries = list(catalog)
        self.applications = list(applications)

    def describe_volume(self, root):
        usage = shutil.disk_usage(self.root)
        return VolumeDescriptor(
            root=self.root,
            volume_id="volume-fixture",
            label="Fixture drive",
            drive_type="fixed",
            capacity_bytes=usage.total,
            used_bytes=usage.used,
            free_bytes=usage.free,
        )

    def list_volumes(self):
        return [self.describe_volume(self.root)]

    def catalog(self, _root):
        return list(self.entries)

    def installed_apps(self, _root):
        return list(self.applications)


class DiskAuditServiceTests(unittest.TestCase):
    def test_legacy_settings_load_with_audit_defaults(self):
        settings = AppSettings.from_dict({"onboarding_completed": True})
        self.assertEqual(settings.last_audit_drive, "")
        self.assertEqual(settings.last_audit_mode, "quick")

    def test_snapshot_round_trip_retention_and_review_state(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = AuditSnapshotStore(get_runtime_paths(Path(temp_dir) / "app"), retention=10)
            base = datetime(2026, 1, 1, tzinfo=timezone.utc)
            for index in range(12):
                completed = (base + timedelta(minutes=index)).isoformat()
                store.save(make_snapshot(f"audit-{index:02d}", completed=completed))

            snapshots = store.list_snapshots(volume_id="volume-test")
            self.assertEqual(len(snapshots), 10)
            self.assertEqual(snapshots[0].audit_id, "audit-11")
            self.assertEqual(snapshots[-1].audit_id, "audit-02")

            immutable = store.snapshot_dir / "audit-11.json"
            original = immutable.read_bytes()
            with self.assertRaises(FileExistsError):
                store.save(
                    make_snapshot(
                        "audit-11",
                        free=999,
                        completed=(base + timedelta(days=1)).isoformat(),
                    )
                )
            self.assertEqual(immutable.read_bytes(), original)

            state = store.set_review_state(
                "volume-test",
                "item-1",
                reviewed=True,
                hidden=True,
                backup_status=BackupStatus.USER_CONFIRMED,
            )
            self.assertTrue(state["reviewed"])
            self.assertTrue(state["hidden"])
            self.assertEqual(state["backup_status"], "user_confirmed")
            self.assertEqual(store.review_state("volume-test", "item-1")["backup_status"], "user_confirmed")

            unsupported = store.snapshot_dir / "unsupported.json"
            payload = make_snapshot("unsupported").to_dict()
            payload["schema_version"] = 99
            unsupported.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaises(ValueError):
                store.load(unsupported)

    def test_quick_scan_classifies_catalog_and_apps_without_changing_sources(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "drive"
            data = root / "Data"
            cache = root / "Cache"
            app = root / "App"
            for folder in (data, cache, app):
                folder.mkdir(parents=True)
            (data / "photo.bin").write_bytes(b"valuable")
            (cache / "cache.bin").write_bytes(b"cache")
            app_file = app / "app.bin"
            with app_file.open("wb") as stream:
                stream.truncate(101 << 20)
            before = (data / "photo.bin").read_bytes()
            entries = [
                CatalogEntry("data", "Personal data", str(data), AuditTier.PROTECT_RECLAIM, "personal_data", "protect first", "Valuable data.", "Confirm a second copy.", always_show=True),
                CatalogEntry("cache", "Build cache", str(cache), AuditTier.REGENERABLE_CACHE, "cache", "regenerable", "Can rebuild.", "Use the supported cleanup.", always_show=True),
                CatalogEntry("system", "Windows storage", str(root / "missing.sys"), AuditTier.WINDOWS_MANAGED, "windows", "leave alone", "Windows owns it.", "Never delete directly.", always_show=True),
            ]
            inventory = FixtureInventory(
                root,
                entries,
                [
                    {"name": "Fixture App", "location": str(app), "estimated_size": 101 << 20},
                    {"name": "No-location App", "location": "", "estimated_size": 120 << 20},
                ],
            )
            paths = get_runtime_paths(Path(temp_dir) / "appdata")
            service = DiskAuditService(paths, inventory=inventory)
            result = service.run(DiskAuditSettings(str(root), "quick"), CancellationToken(), OperationReporter())

            snapshot = result.data["snapshot"]
            self.assertEqual(result.status.value, "completed")
            self.assertEqual(
                {item.tier for item in snapshot.items},
                {AuditTier.PROTECT_RECLAIM, AuditTier.REGENERABLE_CACHE, AuditTier.APPS_REVIEW, AuditTier.WINDOWS_MANAGED},
            )
            self.assertEqual((data / "photo.bin").read_bytes(), before)
            self.assertTrue(Path(result.report_path).is_file())
            self.assertTrue(str(Path(result.report_path)).startswith(str(paths.audits)))
            no_location = next(item for item in snapshot.items if item.label == "No-location App")
            self.assertEqual(no_location.paths, ())

    def test_unicode_long_paths_and_permission_failures_are_nonfatal(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "drive"
            deep = root
            for index in range(8):
                deep = deep / (f"long-segment-{index}-" + "x" * 18)
            deep.mkdir(parents=True)
            value = deep / "résumé-写真.bin"
            value.write_bytes(b"content")
            service = DiskAuditService(get_runtime_paths(Path(temp_dir) / "appdata"), inventory=FixtureInventory(root))
            size, count, warnings = service._measure_path(str(root), CancellationToken())
            self.assertEqual(size, len(b"content"))
            self.assertEqual(count, 1)
            self.assertEqual(warnings, [])
            with patch("duplicate_transfer_manager.services.disk_audit.os.scandir", side_effect=PermissionError("denied")):
                size, count, warnings = service._measure_path(str(root), CancellationToken())
            self.assertEqual((size, count), (0, 0))
            self.assertTrue(warnings)

    def test_disappearing_entries_and_inaccessible_drives_fail_safely(self):
        class VanishingEntry:
            path = "X:\\vanished.bin"

            def stat(self, *, follow_symlinks=False):
                raise FileNotFoundError("vanished")

        class ScanContext:
            def __enter__(self):
                return iter([VanishingEntry()])

            def __exit__(self, *_args):
                return False

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "drive"
            root.mkdir()
            service = DiskAuditService(
                get_runtime_paths(Path(temp_dir) / "appdata"),
                inventory=FixtureInventory(root),
            )
            with patch("duplicate_transfer_manager.services.disk_audit.os.scandir", return_value=ScanContext()):
                size, count, warnings = service._measure_path(str(root), CancellationToken())
            self.assertEqual((size, count), (0, 0))
            self.assertTrue(any("vanished.bin" in warning for warning in warnings))

            class InaccessibleInventory(FixtureInventory):
                def describe_volume(self, root):
                    raise PermissionError("drive unavailable")

            blocked = DiskAuditService(
                get_runtime_paths(Path(temp_dir) / "blocked-appdata"),
                inventory=InaccessibleInventory(root),
            )
            with self.assertRaises(PermissionError):
                blocked.run(DiskAuditSettings(str(root)), CancellationToken(), OperationReporter())
            self.assertEqual(blocked.store.list_snapshots(), [])

    def test_deep_scan_surfaces_nonoverlapping_large_folder_and_file(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "drive"
            folder = root / "large-folder"
            folder.mkdir(parents=True)
            (folder / "inside.bin").write_bytes(b"x" * 20)
            standalone = root / "standalone.bin"
            standalone.write_bytes(b"y" * 8)
            service = DiskAuditService(
                get_runtime_paths(Path(temp_dir) / "appdata"),
                inventory=FixtureInventory(root),
            )
            result = service.run(
                DiskAuditSettings(str(root), "deep", large_file_threshold=5, large_folder_threshold=10),
                CancellationToken(),
                OperationReporter(),
            )
            deep = [item for item in result.data["snapshot"].items if item.kind.startswith("large_")]
            self.assertEqual({item.kind for item in deep}, {"large_folder", "large_file"})
            self.assertIn(str(folder), {item.paths[0] for item in deep})
            self.assertIn(str(standalone), {item.paths[0] for item in deep})
            self.assertNotIn(str(folder / "inside.bin"), {item.paths[0] for item in deep})

    def test_deep_scan_enforces_thresholds_and_separate_top_fifty_caps(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "drive"
            root.mkdir()
            for index in range(55):
                folder = root / f"folder-{index:02d}"
                folder.mkdir()
                (folder / "payload.bin").write_bytes(b"x" * (20 + index))
                (root / f"file-{index:02d}.bin").write_bytes(b"y" * (10 + index))
            below = root / "below.bin"
            below.write_bytes(b"z" * 9)
            service = DiskAuditService(
                get_runtime_paths(Path(temp_dir) / "appdata"),
                inventory=FixtureInventory(root),
            )
            result = service.run(
                DiskAuditSettings(
                    str(root),
                    "deep",
                    large_file_threshold=10,
                    large_folder_threshold=20,
                ),
                CancellationToken(),
                OperationReporter(),
            )
            deep = [
                item
                for item in result.data["snapshot"].items
                if item.kind.startswith("large_")
            ]
            self.assertEqual(len([item for item in deep if item.kind == "large_folder"]), 50)
            self.assertEqual(len([item for item in deep if item.kind == "large_file"]), 50)
            self.assertNotIn(str(below), {item.paths[0] for item in deep})
            defaults = DiskAuditSettings(str(root))
            self.assertEqual(defaults.large_file_threshold, 1 << 30)
            self.assertEqual(defaults.large_folder_threshold, 2 << 30)

    def test_specific_allowlisted_cache_wins_over_overlapping_application(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "drive"
            app = root / "Application"
            cache = app / "Cache"
            cache.mkdir(parents=True)
            (cache / "payload.bin").write_bytes(b"cache")
            service = DiskAuditService(
                get_runtime_paths(Path(temp_dir) / "appdata"),
                inventory=FixtureInventory(root),
            )
            volume = service.inventory.describe_volume(str(root))
            cache_item = service._catalog_item(
                volume,
                CatalogEntry(
                    "cache",
                    "Allowlisted cache",
                    str(cache),
                    AuditTier.REGENERABLE_CACHE,
                    "cache",
                    "regenerable",
                    "Reason",
                    "Review",
                ),
                5,
                1,
                [],
            )
            app_item = DiskAuditItem(
                "app",
                "Application",
                "application",
                (str(app),),
                500,
                1,
                AuditTier.APPS_REVIEW,
                "review",
                "Reason",
                "Review",
            )
            self.assertEqual(service._deduplicate_known([app_item, cache_item]), [cache_item])

    def test_cache_catalog_is_profile_specific_and_does_not_claim_full_app_configuration(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "drive"
            home = root / "Users" / "Fixture"
            local = home / "AppData" / "Local"
            roaming = home / "AppData" / "Roaming"
            chrome_profile = local / "Google" / "Chrome" / "User Data" / "Default"
            chrome_cache = chrome_profile / "Cache"
            chrome_cache.mkdir(parents=True)
            (chrome_profile / "Preferences").write_text("configuration", encoding="utf-8")
            snapshots = home / ".android" / "avd" / "Pixel.avd" / "snapshots"
            snapshots.mkdir(parents=True)
            (snapshots.parent / "userdata-qemu.img").write_bytes(b"device data")

            with (
                patch("duplicate_transfer_manager.services.disk_audit.Path.home", return_value=home),
                patch.dict(
                    os.environ,
                    {"LOCALAPPDATA": str(local), "APPDATA": str(roaming)},
                    clear=False,
                ),
            ):
                entries = WindowsDiskInventory().catalog(str(root))

            cache_paths = {
                Path(entry.path)
                for entry in entries
                if entry.tier == AuditTier.REGENERABLE_CACHE
            }
            self.assertIn(chrome_cache, cache_paths)
            self.assertIn(snapshots, cache_paths)
            self.assertNotIn(chrome_profile, cache_paths)
            self.assertNotIn(snapshots.parent.parent, cache_paths)

    def test_reparse_points_are_not_followed(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "drive"
            target = root / "target"
            target.mkdir(parents=True)
            (target / "value.bin").write_bytes(b"value")
            link = root / "link"
            try:
                os.symlink(target, link, target_is_directory=True)
            except OSError:
                self.skipTest("Creating directory symlinks is not available")
            service = DiskAuditService(get_runtime_paths(Path(temp_dir) / "appdata"), inventory=FixtureInventory(root))
            size, count, warnings = service._measure_path(str(link), CancellationToken())
            self.assertEqual((size, count), (0, 0))
            self.assertTrue(any("reparse" in warning.lower() for warning in warnings))

    def test_comparison_requires_same_volume_and_mode_and_skips_unavailable(self):
        old_item = DiskAuditItem("stable", "Cache", "cache", ("X:\\Cache",), 100, 1, AuditTier.REGENERABLE_CACHE, "regenerable", "Reason", "Review")
        new_item = DiskAuditItem("stable", "Cache", "cache", ("X:\\Cache",), 40, 1, AuditTier.REGENERABLE_CACHE, "regenerable", "Reason", "Review")
        previous = make_snapshot("old", free=100, items=[old_item])
        current = make_snapshot("new", free=160, items=[new_item])
        comparison = compare_snapshots(current, previous)
        self.assertTrue(comparison["baseline_available"])
        self.assertEqual(comparison["free_delta"], 60)
        self.assertEqual(comparison["changes"][0]["delta_bytes"], -60)
        self.assertFalse(compare_snapshots(make_snapshot("deep", mode="deep"), previous)["baseline_available"])
        unavailable = DiskAuditItem("stable", "Cache", "cache", ("X:\\Cache",), 0, 0, AuditTier.REGENERABLE_CACHE, "regenerable", "Reason", "Review", measurement_confidence="unavailable")
        self.assertEqual(compare_snapshots(make_snapshot("newer", items=[unavailable]), previous)["changes"], [])
        self.assertEqual(compare_snapshots(make_snapshot("missing", items=[]), previous)["changes"], [])

    def test_rescan_after_fixture_change_uses_matching_successful_baseline(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "drive"
            root.mkdir()
            fixture = root / "disposable.bin"
            fixture.write_bytes(b"x" * 20)
            entry = CatalogEntry(
                "fixture",
                "Disposable fixture",
                str(fixture),
                AuditTier.REGENERABLE_CACHE,
                "cache",
                "regenerable",
                "Reason",
                "Review",
                always_show=True,
            )
            service = DiskAuditService(
                get_runtime_paths(Path(temp_dir) / "appdata"),
                inventory=FixtureInventory(root, [entry]),
            )
            first = service.run(
                DiskAuditSettings(str(root), "quick"),
                CancellationToken(),
                OperationReporter(),
            )
            fixture.write_bytes(b"x" * 5)
            second = service.run(
                DiskAuditSettings(str(root), "quick"),
                CancellationToken(),
                OperationReporter(),
            )
            comparison = second.data["comparison"]
            self.assertTrue(comparison["baseline_available"])
            self.assertEqual(
                second.data["snapshot"].previous_snapshot_id,
                first.data["snapshot"].audit_id,
            )
            self.assertEqual(comparison["changes"][0]["delta_bytes"], -15)

    def test_stable_ids_survive_drive_letter_or_mount_root_changes(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root_a = Path(temp_dir) / "A"
            root_b = Path(temp_dir) / "B"
            (root_a / "Data").mkdir(parents=True)
            (root_b / "Data").mkdir(parents=True)
            service = DiskAuditService(get_runtime_paths(Path(temp_dir) / "appdata"), inventory=FixtureInventory(root_a))
            volume_a = VolumeDescriptor(str(root_a), "stable-volume", "A", "fixed", 1, 1, 0)
            volume_b = VolumeDescriptor(str(root_b), "stable-volume", "B", "fixed", 1, 1, 0)
            entry_a = CatalogEntry("data", "Data", str(root_a / "Data"), AuditTier.PROTECT_RECLAIM, "personal_data", "review", "Reason", "Review", always_show=True)
            entry_b = CatalogEntry("data", "Data", str(root_b / "Data"), AuditTier.PROTECT_RECLAIM, "personal_data", "review", "Reason", "Review", always_show=True)
            self.assertEqual(
                service._catalog_item(volume_a, entry_a, 10, 1, []).stable_id,
                service._catalog_item(volume_b, entry_b, 10, 1, []).stable_id,
            )

    def test_cancelled_scan_never_saves_a_snapshot(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "drive"
            root.mkdir()
            paths = get_runtime_paths(Path(temp_dir) / "appdata")
            service = DiskAuditService(paths, inventory=FixtureInventory(root))
            token = CancellationToken()
            token.cancel()
            with self.assertRaises(OperationCancelled):
                service.run(DiskAuditSettings(str(root)), token, OperationReporter())
            self.assertEqual(service.store.list_snapshots(), [])


class AuditSnapshotStoreCacheTests(unittest.TestCase):
    """Caching must never hand back a state the user has moved on from."""

    def test_review_state_written_through_one_store_is_seen_by_another(self):
        # The dashboard builds its own store per refresh, so the cache is shared
        # across instances. A stale entry would show a finding as unreviewed
        # after the user had just hidden it.
        with tempfile.TemporaryDirectory() as temp_dir:
            paths = get_runtime_paths(Path(temp_dir))
            reader = AuditSnapshotStore(paths)
            self.assertEqual(reader.review_state("vol", "item"), {})

            AuditSnapshotStore(paths).set_review_state("vol", "item", hidden=True)

            self.assertTrue(reader.review_state("vol", "item").get("hidden"))
            self.assertTrue(AuditSnapshotStore(paths).review_state("vol", "item").get("hidden"))

    def test_review_state_edited_outside_the_process_is_re_read(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            paths = get_runtime_paths(Path(temp_dir))
            store = AuditSnapshotStore(paths)
            store.set_review_state("vol", "item", hidden=True)
            self.assertTrue(store.review_state("vol", "item").get("hidden"))

            # Another process rewrites the file underneath us.
            store.review_path.write_text(
                json.dumps({"vol": {"item": {"hidden": False, "reviewed": True}}}),
                encoding="utf-8",
            )
            os.utime(store.review_path, (time.time() + 2, time.time() + 2))

            refreshed = store.review_state("vol", "item")
            self.assertFalse(refreshed.get("hidden"))
            self.assertTrue(refreshed.get("reviewed"))

    def test_a_returned_review_state_cannot_corrupt_the_cache(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            paths = get_runtime_paths(Path(temp_dir))
            store = AuditSnapshotStore(paths)
            store.set_review_state("vol", "item", hidden=True)

            leaked = store.review_state("vol", "item")
            leaked["hidden"] = False
            leaked["injected"] = True

            fresh = store.review_state("vol", "item")
            self.assertTrue(fresh.get("hidden"))
            self.assertNotIn("injected", fresh)

    def test_a_deleted_snapshot_stops_being_returned(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            paths = get_runtime_paths(Path(temp_dir))
            store = AuditSnapshotStore(paths)
            snapshot = DiskAuditSnapshot(
                schema_version=1,
                audit_id="disk_audit_cache_check",
                volume_id="vol",
                volume_label="C",
                scanned_root="C:/",
                mode="quick",
                started_at="2026-01-01T00:00:00+00:00",
                completed_at="2026-01-01T00:00:01+00:00",
                capacity_bytes=100,
                used_bytes=50,
                free_bytes=50,
            )
            path = Path(store.save(snapshot))
            self.assertIsNotNone(store.latest(volume_id="vol"))

            path.unlink()

            self.assertIsNone(store.latest(volume_id="vol"))
            self.assertEqual(store.list_snapshots(volume_id="vol"), [])


class DiskAuditControllerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.application = QApplication.instance() or QApplication([])

    def test_controller_rejects_concurrent_scan_and_cancels_worker(self):
        class SlowService:
            def run(self, _settings, cancellation, _reporter):
                while not cancellation.is_cancelled():
                    time.sleep(0.005)
                cancellation.raise_if_cancelled()

        pool = QThreadPool()
        pool.setMaxThreadCount(1)
        controller = DiskAuditController(SlowService(), pool)
        cancelled = []
        controller.cancelled.connect(cancelled.append)
        self.assertTrue(controller.start(object()))
        self.assertFalse(controller.start(object()))
        self.assertTrue(controller.cancel())
        self.assertTrue(controller.wait_for_done(3000))
        self.application.processEvents()
        self.assertEqual(len(cancelled), 1)

    def test_controller_runs_service_off_main_thread_and_delivers_result_on_main_thread(self):
        main_thread = threading.get_ident()
        worker_threads = []
        callback_threads = []

        class ThreadRecordingService:
            def run(self, _settings, _cancellation, _reporter):
                worker_threads.append(threading.get_ident())
                return OperationResult(status=OperationState.COMPLETED)

        pool = QThreadPool()
        pool.setMaxThreadCount(1)
        controller = DiskAuditController(ThreadRecordingService(), pool)
        controller.completed.connect(
            lambda _result: callback_threads.append(threading.get_ident())
        )
        self.assertTrue(controller.start(object()))
        self.assertTrue(controller.wait_for_done(3000))
        for _ in range(20):
            self.application.processEvents()
            if callback_threads:
                break
            time.sleep(0.005)
        self.assertEqual(len(worker_threads), 1)
        self.assertNotEqual(worker_threads[0], main_thread)
        self.assertEqual(callback_threads, [main_thread])


class DiskAuditUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.application = QApplication.instance() or QApplication([])

    def test_page_renders_all_tiers_and_persists_hidden_state(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "drive"
            root.mkdir()
            paths = get_runtime_paths(Path(temp_dir) / "appdata")
            service = DiskAuditService(paths, inventory=FixtureInventory(root))
            items = [
                DiskAuditItem(
                    f"item-{index}",
                    f"Finding {index}",
                    "fixture",
                    (str(root),),
                    100 - index,
                    1,
                    tier,
                    "review",
                    "Reason",
                    "Recommendation",
                    "safe-tool --inspect" if index == 1 else "",
                )
                for index, tier in enumerate(AuditTier)
            ]
            snapshot = make_snapshot("ui", volume_id="volume-fixture", items=items)
            page = DiskAuditPage(
                paths,
                AppSettings(onboarding_completed=True),
                SettingsService(paths),
                OperationRecordService(paths),
                service,
            )
            page.load_snapshot(snapshot, {"baseline_available": False, "changes": []})
            self.application.processEvents()
            self.assertEqual(len(page.findChildren(FindingCard)), 4)
            page._set_hidden("item-0", True)
            self.application.processEvents()
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
            self.assertEqual(len(page.findChildren(FindingCard)), 3)
            page.show_hidden.setChecked(True)
            self.application.processEvents()
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
            self.assertEqual(len(page.findChildren(FindingCard)), 4)
            page._set_backup("item-0", BackupStatus.USER_CONFIRMED.value)
            self.assertEqual(service.store.review_state("volume-fixture", "item-0")["backup_status"], "user_confirmed")
            page._set_reviewed("item-0", True)
            self.assertTrue(service.store.review_state("volume-fixture", "item-0")["reviewed"])

            search = page.search
            search.setText("Finding 1")
            self.application.processEvents()
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
            self.application.processEvents()
            self.assertEqual(len(page.tier_host.findChildren(FindingCard)), 1)
            search.clear()
            page.show_hidden.setChecked(True)
            self.application.processEvents()
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)

            page._copy_text("safe-tool --inspect")
            self.assertEqual(QApplication.clipboard().text(), "safe-tool --inspect")
            page._copy_tier(AuditTier.REGENERABLE_CACHE)
            self.assertEqual(QApplication.clipboard().text(), str(root))

            grids = page.findChildren(ResponsiveGrid)
            self.assertTrue(grids)
            card_grid = next(grid for grid in grids if grid.max_columns == 4 and len(grid.widgets) == 4)
            QCoreApplication.sendEvent(
                card_grid,
                QResizeEvent(QSize(1000, 300), QSize(200, 300)),
            )
            self.assertEqual(card_grid._columns, 4)
            QCoreApplication.sendEvent(
                card_grid,
                QResizeEvent(QSize(200, 300), QSize(1000, 300)),
            )
            self.assertEqual(card_grid._columns, 1)
            action_texts = {button.text() for button in page.findChildren(QPushButton)}
            self.assertIn("Copy command", action_texts)
            self.assertIn("Open Apps settings", action_texts)
            self.assertFalse({"Delete", "Clean", "Uninstall", "Execute"} & action_texts)
            page.close()
            delete(page)

    def test_drive_mode_selection_persists_and_progress_reaches_completion(self):
        class MultiVolumeInventory(FixtureInventory):
            def __init__(self, fixed_root, removable_root):
                super().__init__(fixed_root)
                self.fixed_root = str(fixed_root)
                self.removable_root = str(removable_root)

            def describe_volume(self, root):
                root = str(root)
                usage = shutil.disk_usage(root)
                removable = root == self.removable_root
                return VolumeDescriptor(
                    root=root,
                    volume_id="volume-removable" if removable else "volume-fixed",
                    label="Removable fixture" if removable else "Fixed fixture",
                    drive_type="removable" if removable else "fixed",
                    capacity_bytes=usage.total,
                    used_bytes=usage.used,
                    free_bytes=usage.free,
                )

            def list_volumes(self):
                return [
                    self.describe_volume(self.fixed_root),
                    self.describe_volume(self.removable_root),
                ]

        with tempfile.TemporaryDirectory() as temp_dir:
            fixed = Path(temp_dir) / "fixed"
            removable = Path(temp_dir) / "removable"
            fixed.mkdir()
            removable.mkdir()
            paths = get_runtime_paths(Path(temp_dir) / "appdata")
            settings_service = SettingsService(paths)
            settings = AppSettings(
                onboarding_completed=True,
                last_audit_drive=str(removable),
                last_audit_mode="deep",
            )
            service = DiskAuditService(
                paths,
                inventory=MultiVolumeInventory(fixed, removable),
            )
            page = DiskAuditPage(
                paths,
                settings,
                settings_service,
                OperationRecordService(paths),
                service,
            )
            self.assertEqual(page.drive.count(), 2)
            self.assertEqual(page.drive.currentData(), str(removable))
            self.assertEqual(page.mode.currentData(), "deep")

            page.mode.setCurrentIndex(page.mode.findData("quick"))
            page.start_audit()
            self.assertTrue(page.progress_panel.isVisibleTo(page))
            self.assertFalse(page.run_button.isEnabled())
            self.assertTrue(page.controller.wait_for_done(3000))
            for _ in range(30):
                self.application.processEvents()
                if page.snapshot is not None:
                    break
                time.sleep(0.005)
            self.assertIsNotNone(page.snapshot)
            self.assertFalse(page.progress_panel.isVisibleTo(page))
            self.assertTrue(page.run_button.isEnabled())
            persisted = settings_service.load()
            self.assertEqual(persisted.last_audit_drive, str(removable))
            self.assertEqual(persisted.last_audit_mode, "quick")
            page.close()
            delete(page)

    def test_activity_reopens_audit_in_native_page_and_dashboard_reads_latest(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            paths = get_runtime_paths(Path(temp_dir) / "appdata")
            store = AuditSnapshotStore(paths)
            snapshot_path = store.save(make_snapshot("activity-audit", free=250))
            operations = OperationRecordService(paths)
            operations.record(
                "disk_audit",
                "completed",
                title="Quick disk audit",
                summary={"snapshot_path": snapshot_path},
            )
            page = ActivityPage(operations, ReportService(paths))
            selected = []
            page.audit_requested.connect(selected.append)
            page.table.selectRow(0)
            page.view_selected_audit()
            self.assertEqual(selected, [snapshot_path])
            health = DashboardService(paths).summary(include_storage=False)["audit_health"]
            self.assertTrue(health["available"])
            self.assertEqual(health["audit_id"], "activity-audit")
            page.close()
            delete(page)

    def test_open_path_selects_files_and_opens_folders(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            folder = Path(temp_dir)
            file_path = folder / "item.txt"
            file_path.write_text("data", encoding="utf-8")
            if os.name == "nt":
                with patch("duplicate_transfer_manager.ui.disk_audit_page.subprocess.Popen") as opened:
                    self.assertTrue(open_audit_path(str(file_path)))
                    opened.assert_called_once_with(
                        ["explorer.exe", "/select,", str(file_path)]
                    )
            with patch("duplicate_transfer_manager.ui.disk_audit_page.QDesktopServices.openUrl", return_value=True) as opened:
                self.assertTrue(open_audit_path(str(folder)))
                opened.assert_called_once()
            self.assertFalse(open_audit_path(str(folder / "missing")))

    def test_page_runs_worker_end_to_end_and_records_all_terminal_states(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "drive"
            root.mkdir()
            fixture = root / "fixture.bin"
            fixture.write_bytes(b"fixture")
            entry = CatalogEntry("fixture", "Fixture", str(fixture), AuditTier.PROTECT_RECLAIM, "personal_data", "review", "Reason", "Review", always_show=True)
            paths = get_runtime_paths(Path(temp_dir) / "appdata")
            service = DiskAuditService(paths, inventory=FixtureInventory(root, [entry]))
            operations = OperationRecordService(paths)
            page = DiskAuditPage(paths, AppSettings(onboarding_completed=True), SettingsService(paths), operations, service)
            page.refresh_drives()
            page.start_audit()
            self.assertTrue(page.controller.wait_for_done(3000))
            for _ in range(20):
                self.application.processEvents()
                if page.snapshot is not None:
                    break
                time.sleep(0.01)
            self.assertIsNotNone(page.snapshot)
            self.assertEqual(operations.list_records()[0]["status"], "completed")

            page._active_settings = DiskAuditSettings(str(root), "quick")
            page._on_cancelled(OperationResult(status=OperationState.CANCELLED))
            page._on_failed(RuntimeError("fixture failure"))
            statuses = [record["status"] for record in operations.list_records()[:3]]
            self.assertEqual(statuses, ["failed", "cancelled", "completed"])
            page.close()
            delete(page)


if __name__ == "__main__":
    unittest.main()
