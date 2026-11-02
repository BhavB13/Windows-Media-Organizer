"""Warning before a scan or import would download cloud-only files.

The storage classifier is driven with a faked attribute backend, so these tests
behave the same on any machine and never touch a real OneDrive folder.
"""

from __future__ import annotations

import functools
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from duplicate_transfer_manager.core import storage_class
from duplicate_transfer_manager.services import cloud_guard
from duplicate_transfer_manager.services.cloud_guard import (
    CloudDownloadAssessment,
    assess_cloud_downloads,
    format_bytes,
)

RECALL = storage_class.FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS
NORMAL = storage_class.FILE_ATTRIBUTE_NORMAL


def _tree(root: Path, names: dict[str, int]) -> None:
    for name, size in names.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x" * min(size, 16))


def _faked_survey(attributes: dict[str, int], sizes: dict[str, int]):
    """survey_tree with the Win32 layer replaced by lookups on file names."""

    def attrs(path: str) -> int:
        return attributes.get(os.path.basename(path), NORMAL)

    def size(path: str) -> int:
        return sizes.get(os.path.basename(path), 1)

    return functools.partial(
        storage_class.survey_tree,
        get_file_attributes=attrs,
        get_reparse_tag=lambda _path: 0,
        get_file_size=size,
        get_drive_type=lambda _root: storage_class.DRIVE_FIXED,
        platform_is_windows=True,
    )


class AssessmentTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def test_a_fully_local_tree_raises_no_warning(self) -> None:
        _tree(self.root, {"a.jpg": 10, "b.jpg": 10})
        result = assess_cloud_downloads(
            [str(self.root)], survey=_faked_survey({}, {}), platform_is_windows=True
        )
        self.assertFalse(result.would_download)
        self.assertEqual(result.message, "")

    def test_cloud_only_files_are_counted_with_their_size(self) -> None:
        _tree(self.root, {"local.jpg": 10, "cloud1.jpg": 10, "sub/cloud2.mov": 10})
        gib = 1024 ** 3
        result = assess_cloud_downloads(
            [str(self.root)],
            survey=_faked_survey(
                {"cloud1.jpg": RECALL, "cloud2.mov": RECALL},
                {"cloud1.jpg": 3 * 1024 ** 2, "cloud2.mov": 2 * gib},
            ),
            platform_is_windows=True,
        )
        self.assertTrue(result.would_download)
        self.assertEqual(result.files, 2)
        self.assertEqual(result.bytes, 2 * gib + 3 * 1024 ** 2)
        self.assertIn("2 files", result.message)
        self.assertIn("2.0 GB", result.message)
        self.assertIn("download", result.message)

    def test_provider_is_named_when_known(self) -> None:
        fake = lambda root, stop_event=None: SimpleNamespace(  # noqa: E731
            recall_files=1, recall_bytes=5, providers=("OneDrive",), cancelled=False
        )
        result = assess_cloud_downloads(["C:\\Users\\me\\OneDrive"], survey=fake, platform_is_windows=True)
        self.assertIn("1 file (5 bytes)", result.message)
        self.assertIn("is stored in OneDrive", result.message)

    def test_unknown_provider_is_not_guessed(self) -> None:
        fake = lambda root, stop_event=None: SimpleNamespace(  # noqa: E731
            recall_files=3, recall_bytes=30, providers=(storage_class.UNKNOWN_PROVIDER,), cancelled=False
        )
        result = assess_cloud_downloads(["X:\\"], survey=fake, platform_is_windows=True)
        self.assertIn("a cloud storage service", result.message)

    def test_android_sources_and_other_platforms_are_not_walked(self) -> None:
        def explode(*_args, **_kwargs):
            raise AssertionError("must not walk")

        self.assertFalse(assess_cloud_downloads(["/sdcard/DCIM"], is_adb=True, survey=explode).would_download)
        self.assertFalse(
            assess_cloud_downloads([str(self.root)], survey=explode, platform_is_windows=False).would_download
        )

    def test_a_failing_check_reports_its_error_instead_of_raising(self) -> None:
        def broken(*_args, **_kwargs):
            raise OSError("access denied")

        result = assess_cloud_downloads([str(self.root)], survey=broken, platform_is_windows=True)
        self.assertFalse(result.would_download)
        self.assertIn("access denied", result.error)

    def test_the_time_budget_stops_the_walk_and_says_at_least(self) -> None:
        ticks = iter([0.0, 0.0, 999.0, 999.0, 999.0])

        def slow_survey(root, stop_event=None):
            stop_event.is_set()  # within budget
            cancelled = stop_event.is_set()  # budget exhausted
            return SimpleNamespace(recall_files=7, recall_bytes=70, providers=(), cancelled=cancelled)

        result = assess_cloud_downloads(
            [str(self.root)], survey=slow_survey, clock=lambda: next(ticks), platform_is_windows=True
        )
        self.assertTrue(result.would_download)
        self.assertFalse(result.complete)
        self.assertTrue(result.message.startswith("At least 7 files"))

    def test_user_cancellation_is_reported_as_cancelled(self) -> None:
        stop = threading.Event()
        stop.set()

        def survey(root, stop_event=None):
            return SimpleNamespace(recall_files=0, recall_bytes=0, providers=(), cancelled=stop_event.is_set())

        result = assess_cloud_downloads(
            [str(self.root)], stop_event=stop, survey=survey, platform_is_windows=True
        )
        self.assertTrue(result.cancelled)
        self.assertFalse(result.would_download)

    def test_the_check_never_opens_a_file(self) -> None:
        _tree(self.root, {"cloud.jpg": 10})
        survey = functools.partial(
            _faked_survey({"cloud.jpg": RECALL}, {}),
            open_hook=lambda *a, **k: (_ for _ in ()).throw(AssertionError("opened a file")),
        )
        result = assess_cloud_downloads([str(self.root)], survey=survey, platform_is_windows=True)
        self.assertTrue(result.would_download)

    def test_byte_formatting(self) -> None:
        self.assertEqual(format_bytes(0), "0 bytes")
        self.assertEqual(format_bytes(1536), "1.5 KB")
        self.assertEqual(format_bytes(5 * 1024 ** 3), "5.0 GB")


try:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication, QMessageBox

    from duplicate_transfer_manager.core.contracts import AppSettings
    from duplicate_transfer_manager.runtime_paths import get_runtime_paths
    from duplicate_transfer_manager.services.support_services import SettingsService
    from duplicate_transfer_manager.ui.shell import MainWindow
    from duplicate_transfer_manager.ui.theme import ThemeManager
except Exception:  # pragma: no cover - UI tests skip without PySide6
    QApplication = None


@unittest.skipIf(QApplication is None, "PySide6 is required for page tests")
class PageWarningTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.application = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        paths = get_runtime_paths(self.temp_dir.name)
        self.window = MainWindow(ThemeManager(self.application), AppSettings(onboarding_completed=True), SettingsService(paths))
        self.page = self.window.pages["duplicates"]
        self.source = Path(self.temp_dir.name) / "photos"
        self.source.mkdir()
        self.page.path.set_path(str(self.source))

    def tearDown(self):
        self.window.close()
        self.window.deleteLater()
        self.application.processEvents()

    def _drain(self, until, timeout=5.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline and not until():
            self.application.processEvents()
            time.sleep(0.01)

    def _assessment(self, would_download):
        return CloudDownloadAssessment(
            would_download=would_download, files=12, bytes=1024 ** 3, providers=("OneDrive",),
            message="12 files (1.0 GB) in this location are stored in OneDrive and not on this PC.",
        )

    def _run(self, assessment, answer):
        starts = []
        with patch("duplicate_transfer_manager.ui.pages.assess_cloud_downloads", return_value=assessment), \
                patch("duplicate_transfer_manager.ui.pages.ask_cloud_choice", return_value=answer) as asked, \
                patch.object(self.page.controller, "start", side_effect=lambda s: starts.append(s) or True):
            self.page._start_scan()
            self._drain(lambda: starts or self.page.scan_button.isEnabled())
            self.application.processEvents()
        return starts, asked

    def test_declining_the_warning_starts_nothing(self):
        starts, asked = self._run(self._assessment(True), "cancel")
        self.assertEqual(asked.call_count, 1)
        self.assertEqual(starts, [])
        self.assertTrue(self.page.scan_button.isEnabled())

    def test_accepting_the_warning_starts_the_scan_once(self):
        starts, asked = self._run(self._assessment(True), "download")
        self.assertEqual(asked.call_count, 1)
        self.assertEqual(len(starts), 1)

    def test_nothing_to_download_starts_without_asking(self):
        starts, asked = self._run(self._assessment(False), "cancel")
        self.assertEqual(asked.call_count, 0)
        self.assertEqual(len(starts), 1)

    def test_a_broken_check_does_not_block_the_scan(self):
        starts = []
        with patch("duplicate_transfer_manager.ui.pages.assess_cloud_downloads", side_effect=RuntimeError("boom")), \
                patch("duplicate_transfer_manager.ui.pages.ask_cloud_choice") as asked, \
                patch.object(self.page.controller, "start", side_effect=lambda s: starts.append(s) or True):
            self.page._start_scan()
            self._drain(lambda: starts)
        self.assertEqual(asked.call_count, 0)
        self.assertEqual(len(starts), 1)


if __name__ == "__main__":
    unittest.main()
