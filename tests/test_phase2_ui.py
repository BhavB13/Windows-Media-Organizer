import os
import tempfile
import unittest
from pathlib import Path


os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication
except ImportError as exc:  # pragma: no cover - Phase 2 runtime installs PySide6
    raise unittest.SkipTest(f"PySide6 is required for UI tests: {exc}")

from duplicate_transfer_manager.core import AppSettings
from duplicate_transfer_manager.runtime_paths import get_runtime_paths
from duplicate_transfer_manager.services import SettingsService
from duplicate_transfer_manager.ui.app import create_application
from duplicate_transfer_manager.ui.pages import (
    ActivityPage,
    DuplicatesPage,
    HelpPage,
    ImportPage,
    OverviewPage,
    QuarantinePage,
    SettingsPage,
)
from duplicate_transfer_manager.ui.shell import MainWindow, ROUTES
from duplicate_transfer_manager.ui.theme import DARK, LIGHT, ThemeManager
from duplicate_transfer_manager.ui.widgets import (
    CompletionSummary,
    ConfirmationDialog,
    EmptyState,
    InlineMessage,
    PathSelector,
    ProgressPanel,
    SourceCard,
    ToastBanner,
)
from duplicate_transfer_manager.version import __version__


class Phase2UiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.application = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.paths = get_runtime_paths(self.temp_dir.name)
        self.service = SettingsService(self.paths)
        self.settings = AppSettings()
        self.theme = ThemeManager(self.application)
        self.theme.apply("light")
        self.window = MainWindow(self.theme, self.settings, self.service)
        self.window.show()
        self.application.processEvents()

    def tearDown(self):
        self.window.close()
        self.window.deleteLater()
        self.application.processEvents()
        self.temp_dir.cleanup()

    def test_shell_contains_all_required_routes(self):
        self.assertEqual(
            set(self.window.pages),
            {
                "overview",
                "duplicates",
                "import",
                "activity",
                "quarantine",
                "settings",
                "help",
            },
        )
        expected_types = {
            "overview": OverviewPage,
            "duplicates": DuplicatesPage,
            "import": ImportPage,
            "activity": ActivityPage,
            "quarantine": QuarantinePage,
            "settings": SettingsPage,
            "help": HelpPage,
        }
        for key, page_type in expected_types.items():
            self.window.navigate(key)
            self.application.processEvents()
            self.assertIsInstance(self.window.stack.currentWidget(), page_type)
            self.assertTrue(self.window.sidebar.buttons[key].isChecked())

    def test_contextual_device_indicator_only_appears_on_source_pages(self):
        for key in ("overview", "activity", "quarantine", "settings", "help"):
            self.window.navigate(key)
            self.application.processEvents()
            self.assertFalse(self.window.top_bar.device.isVisible())
        for key in ("duplicates", "import"):
            self.window.navigate(key)
            self.application.processEvents()
            self.assertTrue(self.window.top_bar.device.isVisible())

    def test_sidebar_collapses_and_expands_responsively(self):
        self.window.resize(960, 700)
        self.application.processEvents()
        self.assertTrue(self.window.sidebar.collapsed)
        self.assertEqual(self.window.sidebar.width(), 76)
        self.window.resize(1360, 860)
        self.application.processEvents()
        self.assertFalse(self.window.sidebar.collapsed)
        self.assertEqual(self.window.sidebar.width(), 248)

    def test_theme_manager_supports_light_dark_and_system(self):
        self.assertEqual(self.theme.apply("light"), "light")
        self.assertEqual(self.theme.colors, LIGHT)
        self.assertEqual(self.theme.apply("dark"), "dark")
        self.assertEqual(self.theme.colors, DARK)
        resolved = self.theme.apply("system")
        self.assertIn(resolved, {"light", "dark"})
        self.assertEqual(self.theme.preference, "system")

    def test_settings_theme_selection_updates_application(self):
        page = self.window.pages["settings"]
        page.theme.setCurrentIndex(page.theme.findData("dark"))
        self.application.processEvents()
        self.assertEqual(self.theme.active_theme, "dark")
        self.assertEqual(self.settings.appearance, "dark")

    def test_duplicate_setup_uses_inline_validation_and_notification(self):
        page = self.window.pages["duplicates"]
        page.path.set_path("")
        page._review()
        self.assertFalse(page.path.validation.isHidden())
        self.assertTrue(page.banner.isHidden())
        page.path.set_path(str(Path(self.temp_dir.name)))
        page._review()
        self.assertTrue(page.path.validation.isHidden())
        self.assertFalse(page.banner.isHidden())

    def test_import_setup_requires_source_and_library(self):
        page = self.window.pages["import"]
        page._review()
        self.assertFalse(page.source_path.validation.isHidden())
        self.assertFalse(page.library_path.validation.isHidden())
        page.source_path.set_path("/sdcard/DCIM")
        page.library_path.set_path(self.temp_dir.name)
        page._review()
        self.assertFalse(page.banner.isHidden())

    def test_reusable_controls_have_accessible_names(self):
        controls = [
            PathSelector("Location", "Choose location"),
            SourceCard("Folder", "On this PC", "folder"),
            EmptyState("Nothing here", "Description", "Start"),
            InlineMessage("Helpful message"),
            ToastBanner("Saved", "success"),
            ProgressPanel(),
            CompletionSummary(),
            ConfirmationDialog("Confirm", "Continue?"),
        ]
        self.assertTrue(controls[0].entry.accessibleName())
        self.assertTrue(controls[1].accessibleName())
        self.assertTrue(controls[2].accessibleName())
        self.assertTrue(controls[3].accessibleName())
        self.assertTrue(controls[5].progress.accessibleName())
        for control in controls:
            control.deleteLater()

    def test_navigation_controls_have_keyboard_shortcuts_and_focus_policy(self):
        self.assertEqual(len(self.window.sidebar.shortcuts), len(ROUTES))
        for route in ROUTES:
            button = self.window.sidebar.buttons[route.key]
            self.assertTrue(button.accessibleName())
            self.assertNotEqual(button.focusPolicy(), Qt.FocusPolicy.NoFocus)

    def test_create_application_builds_the_primary_shell(self):
        application, window = create_application(
            [],
            legacy_root=self.temp_dir.name,
        )
        self.assertIs(application, self.application)
        self.assertIsInstance(window, MainWindow)
        self.assertEqual(application.applicationName(), "Duplicate & Transfer Manager")
        self.assertEqual(application.applicationVersion(), __version__)
        self.assertTrue(application.property("reducedMotion"))
        window.close()
        window.deleteLater()


if __name__ == "__main__":
    unittest.main()
