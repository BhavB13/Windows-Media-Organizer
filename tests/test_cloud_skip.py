"""Scanning only the files on this PC when a folder holds cloud-only files.

Placeholders are faked by patching ``discovery.get_file_attributes``, so no
real OneDrive folder is needed and nothing is downloaded.
"""

from __future__ import annotations

import builtins
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import discovery
import engine as engine_module
from models import Settings, TransferSettings
from utils import HashCache

from duplicate_transfer_manager.core import CancellationToken, OperationReporter
from duplicate_transfer_manager.services.cloud_guard import CloudDownloadAssessment
from duplicate_transfer_manager.services.duplicate_service import DuplicateScanService
from duplicate_transfer_manager.services.import_workflow import summarize_transfer_result

from tests.test_engine import DummyLogger

RECALL = discovery.FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS


def _attributes_for(cloud_names):
    def fake(path):
        return RECALL if os.path.basename(path) in cloud_names else 0x20

    return fake


def _scan_settings(root: str, **extra) -> Settings:
    settings = Settings(
        scan_root=root, output_root="", criteria="hash", hash_algo="sha256", hash_mode="full",
        only_media=False, extensions=[], min_size_kb=0, exclude_dirs=[], skip_hidden_system=False,
        dry_run=True, preserve_structure=True, max_hash_workers=1,
    )
    for key, value in extra.items():
        setattr(settings, key, value)
    return settings


class DiscoverySkipTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        for name in ("local.jpg", "cloud1.jpg", "cloud2.jpg"):
            (self.root / name).write_bytes(name.encode())

    def _discover(self, settings):
        with patch.object(discovery, "get_file_attributes", _attributes_for({"cloud1.jpg", "cloud2.jpg"})):
            return discovery.discover_files(str(self.root), settings, threading.Event())

    def test_placeholders_are_left_out_and_counted_when_asked(self):
        result = self._discover(_scan_settings(str(self.root), skip_cloud_placeholders=True))
        self.assertEqual([Path(f.path).name for f in result.files], ["local.jpg"])
        self.assertEqual(result.skipped_cloud_files, 2)

    def test_default_behaviour_is_unchanged(self):
        result = self._discover(_scan_settings(str(self.root)))
        self.assertEqual(sorted(Path(f.path).name for f in result.files), ["cloud1.jpg", "cloud2.jpg", "local.jpg"])
        self.assertEqual(result.skipped_cloud_files, 0)

    def test_a_skipped_placeholder_is_never_opened(self):
        real_open = builtins.open
        opened = []

        def watching_open(file, *args, **kwargs):
            opened.append(os.path.basename(str(file)))
            return real_open(file, *args, **kwargs)

        with patch.object(builtins, "open", watching_open):
            self._discover(_scan_settings(str(self.root), skip_cloud_placeholders=True))
        self.assertNotIn("cloud1.jpg", opened)
        self.assertNotIn("cloud2.jpg", opened)

    def test_attribute_lookup_failure_includes_the_file(self):
        def broken(_path):
            raise OSError("attributes unavailable")

        with patch.object(discovery, "get_file_attributes", broken):
            result = discovery.discover_files(
                str(self.root), _scan_settings(str(self.root), skip_cloud_placeholders=True), threading.Event()
            )
        self.assertEqual(len(result.files), 3)


class ServiceCountTests(unittest.TestCase):
    def test_duplicate_scan_reports_the_skipped_count(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            for name in ("a.jpg", "b.jpg", "cloud.jpg"):
                (root / name).write_bytes(b"same" if name != "cloud.jpg" else b"other")
            settings = _scan_settings(str(root), skip_cloud_placeholders=True)
            with patch.object(discovery, "get_file_attributes", _attributes_for({"cloud.jpg"})):
                result = DuplicateScanService(HashCache(str(root / "c.json"))).run(
                    settings, CancellationToken(), OperationReporter()
                )
        self.assertEqual(result.counts["skipped_cloud_files"], 1)
        self.assertEqual(result.counts["files_scanned"], 2)

    def test_import_skips_source_placeholders_but_still_compares_the_whole_library(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source, library, output = root / "src", root / "lib", root / "out"
            for folder in (source, library, output):
                folder.mkdir()
            (source / "new.jpg").write_bytes(b"new photo")
            (source / "cloud.jpg").write_bytes(b"cloud photo")
            # Already in the library, but cloud-only there: it must still be
            # compared, or the source copy would be imported a second time.
            (source / "known.jpg").write_bytes(b"known photo")
            (library / "known-in-cloud.jpg").write_bytes(b"known photo")
            settings = TransferSettings(
                source_root=str(source), dest_root=str(library), output_root=str(output),
                criteria="hash", hash_algo="sha256", hash_mode="full", only_media=False,
                extensions=[], min_size_kb=0, exclude_dirs=[], skip_hidden_system=False,
                dry_run=False, preserve_structure=True, max_hash_workers=1,
                transfer_mode="copy", duplicate_policy="skip", use_dest_cache=False,
                source_is_adb=False, update_drive_cache=False, use_adb_cache=False,
                conflict_policy="rename",
            )
            settings.journal_path = str(root / "journal.json")
            settings.skip_cloud_placeholders = True
            with patch.object(discovery, "get_file_attributes", _attributes_for({"cloud.jpg", "known-in-cloud.jpg"})):
                raw = engine_module.execute_smart_transfer(
                    settings, threading.Event(), HashCache(str(root / "c.json")), DummyLogger()
                )
            copied = sorted(p.name for p in output.iterdir() if p.is_file())
        self.assertEqual(raw["skipped_cloud_files"], 1)
        self.assertEqual(copied, ["new.jpg"], "the cloud-only source file and the known file must not be copied")
        self.assertEqual(raw["duplicates"], 1)

    def test_summary_names_the_skipped_files_only_when_there_are_some(self):
        result = SimpleNamespace(
            data={"engine_result": {"skipped_cloud_files": 4}},
            counts={"transferred": 1, "duplicates": 0, "resumed": 0, "errors": 0},
            report_path="",
        )
        self.assertEqual(summarize_transfer_result(result)["Cloud-only files left out (not downloaded)"], "4")
        result.data = {"engine_result": {}}
        self.assertNotIn("Cloud-only files left out (not downloaded)", summarize_transfer_result(result))


try:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    from duplicate_transfer_manager.core.contracts import AppSettings
    from duplicate_transfer_manager.runtime_paths import get_runtime_paths
    from duplicate_transfer_manager.services.support_services import SettingsService
    from duplicate_transfer_manager.ui.shell import MainWindow
    from duplicate_transfer_manager.ui.theme import ThemeManager
except Exception:  # pragma: no cover
    QApplication = None


@unittest.skipIf(QApplication is None, "PySide6 is required for page tests")
class ChoiceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.application = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.window = MainWindow(
            ThemeManager(self.application), AppSettings(onboarding_completed=True),
            SettingsService(get_runtime_paths(self.temp_dir.name)),
        )
        self.page = self.window.pages["duplicates"]
        source = Path(self.temp_dir.name) / "photos"
        source.mkdir()
        self.page.path.set_path(str(source))

    def tearDown(self):
        self.window.close()
        self.window.deleteLater()
        self.application.processEvents()

    def _start(self, choice):
        started = []
        assessment = CloudDownloadAssessment(would_download=True, files=3, bytes=30, message="3 files are in the cloud.")
        with patch("duplicate_transfer_manager.ui.pages.assess_cloud_downloads", return_value=assessment), \
                patch("duplicate_transfer_manager.ui.pages.ask_cloud_choice", return_value=choice), \
                patch.object(self.page.controller, "start", side_effect=lambda s: started.append(s) or True):
            self.page._start_scan()
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline and not started and not self.page.scan_button.isEnabled():
                self.application.processEvents()
                time.sleep(0.01)
        return started

    def test_scan_only_local_files_sets_the_skip_flag(self):
        started = self._start("skip")
        self.assertEqual(len(started), 1)
        self.assertTrue(started[0].skip_cloud_placeholders)

    def test_download_everything_leaves_the_flag_off(self):
        started = self._start("download")
        self.assertEqual(len(started), 1)
        self.assertFalse(started[0].skip_cloud_placeholders)

    def test_cancel_starts_nothing(self):
        self.assertEqual(self._start("cancel"), [])


if __name__ == "__main__":
    unittest.main()
