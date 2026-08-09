import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, PropertyMock, patch


os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication, QCheckBox, QLabel, QMessageBox, QPushButton
    from shiboken6 import delete
except ImportError as exc:  # pragma: no cover - Phase 2 runtime installs PySide6
    raise unittest.SkipTest(f"PySide6 is required for UI tests: {exc}")

from duplicate_transfer_manager.core import AppSettings, QuarantineRecord, ServiceError
from duplicate_transfer_manager.runtime_paths import get_runtime_paths
from duplicate_transfer_manager.services import (
    DuplicateQuarantineService,
    build_duplicate_review,
    OperationRecordService,
    ReportService,
    SettingsService,
)
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
from duplicate_transfer_manager.ui.sort_workspace import SortWorkspace
from duplicate_transfer_manager.sorting import (
    Association,
    ConditionField,
    ConditionOperator,
    SortAction,
    SortCondition,
    SortingProfile,
)
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
from models import FileInfo


class Phase2UiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.application = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.paths = get_runtime_paths(self.temp_dir.name)
        self.service = SettingsService(self.paths)
        self.settings = AppSettings(onboarding_completed=True)
        self.theme = ThemeManager(self.application)
        self.theme.apply("light")
        self.window = MainWindow(self.theme, self.settings, self.service)
        self.window.show()
        self.application.processEvents()

    def tearDown(self):
        self.window.close()
        delete(self.window)
        self.application.processEvents()
        self.temp_dir.cleanup()

    def test_shell_contains_all_required_routes(self):
        self.assertEqual(
            set(self.window.pages),
            {
                "overview",
                "duplicates",
                "import",
                "sort",
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
            "sort": SortWorkspace,
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
        for key in ("overview", "sort", "activity", "quarantine", "settings", "help"):
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

    def test_overview_layout_fits_common_windows_sizes_without_horizontal_scroll(self):
        self.window.navigate("overview")
        for width, height in ((1366, 768), (1920, 1080)):
            self.window.resize(width, height)
            self.application.processEvents()
            overview = self.window.pages["overview"]
            self.assertEqual(overview.horizontalScrollBar().maximum(), 0)
            self.assertLessEqual(self.window.sidebar.width(), 248)
            self.assertIn(overview.recent, overview.summary_sections.widgets)
            self.assertIn(overview.interrupted, overview.summary_sections.widgets)
            self.assertIn(overview.storage, overview.summary_sections.widgets)

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
        self.assertTrue(page.scan_button.isHidden())
        page.path.set_path("")
        page._review()
        self.assertFalse(page.path.validation.isHidden())
        self.assertTrue(page.banner.isHidden())
        page.path.set_path(str(Path(self.temp_dir.name)))
        self.assertTrue(page.dry_run_quarantine.text().startswith("Dry run"))
        page.dry_run_quarantine.setChecked(True)
        page._review()
        self.assertTrue(page.path.validation.isHidden())
        self.assertFalse(page.banner.isHidden())
        self.assertIn("Dry run", page.summary.text())
        self.assertFalse(page.scan_button.isHidden())
        page.dry_run_quarantine.setChecked(False)
        self.assertFalse(page.scan_button.isHidden())
        self.assertIn("Live", page.summary.text())
        page.path.set_path("")
        self.assertTrue(page.scan_button.isHidden())

    def test_duplicate_android_browse_selects_nested_folder(self):
        page = self.window.pages["duplicates"]
        page.source_picker.cards["android"].setChecked(True)
        page.device_choice.addItem("Pixel — device", "phone-1")
        page.path.set_path("/storage/emulated/0/DCIM")

        with patch(
            "adb_bridge.ADBBridge.get_directory_structure",
            return_value=[{"name": "Camera", "path": "/sdcard/DCIM/Camera"}],
        ), patch(
            "duplicate_transfer_manager.ui.pages.QInputDialog.getItem",
            return_value=("Camera — /sdcard/DCIM/Camera", True),
        ):
            page._browse()

        self.assertEqual(page.path.path(), "/sdcard/DCIM/Camera")

    def test_import_setup_requires_source_and_library(self):
        page = self.window.pages["import"]
        self.assertTrue(page.run_button.isHidden())
        page._review()
        self.assertFalse(page.source_path.validation.isHidden())
        self.assertFalse(page.library_path.validation.isHidden())
        self.assertTrue(page.run_button.isHidden())
        page.source_path.set_path("/sdcard/DCIM")
        page.library_path.set_path(self.temp_dir.name)
        page._review()
        self.assertFalse(page.banner.isHidden())
        self.assertTrue(page.run_button.isEnabled())
        self.assertFalse(page.run_button.isHidden())
        page.library_path.set_path("")
        self.assertFalse(page.run_button.isEnabled())
        self.assertTrue(page.run_button.isHidden())

    def test_import_page_exposes_safe_date_folder_template(self):
        page = self.window.pages["import"]
        self.assertEqual(page.destination_template.currentData(), "preserve")
        page.destination_template.setCurrentIndex(page.destination_template.findData("date"))
        page.source_path.set_path(self.temp_dir.name)
        page.library_path.set_path(self.temp_dir.name)
        page._review()

        self.assertFalse(page.run_button.isHidden())
        self.assertTrue(
            any("date folders" in label.text().lower() for label in page.review_card.findChildren(QLabel))
        )

    def test_saved_category_and_profile_defaults_apply_to_new_workflows(self):
        self.settings.default_file_categories = ["audio"]
        self.settings.default_transfer_profile = "Reliable"
        self.settings.favorite_locations = [str(Path(self.temp_dir.name) / "Photos")]
        duplicates = DuplicatesPage(settings=self.settings)
        imports = ImportPage(settings=self.settings)

        self.assertFalse(duplicates.pictures.isChecked())
        self.assertFalse(duplicates.videos.isChecked())
        self.assertTrue(duplicates.audio.isChecked())
        self.assertEqual(imports.profile.currentData(), "Reliable")
        self.assertTrue(imports.category_checks["audio"].isChecked())
        self.assertFalse(imports.category_checks["pictures"].isChecked())
        duplicates.favorite_location.setCurrentIndex(1)
        imports.favorite_library.setCurrentIndex(1)
        self.assertEqual(duplicates.path.path(), self.settings.favorite_locations[0])
        self.assertEqual(imports.library_path.path(), self.settings.favorite_locations[0])
        duplicates.deleteLater()
        imports.deleteLater()

    def test_saved_locations_update_open_workflow_pages_without_restart(self):
        settings_page = self.window.pages["settings"]
        settings_page.favorite_locations.setText("D:/Photos, E:/Camera")
        settings_page._save()
        duplicates = self.window.pages["duplicates"]
        imports = self.window.pages["import"]

        self.assertEqual(duplicates.favorite_location.count(), 3)
        self.assertEqual(imports.favorite_source.count(), 3)
        self.assertFalse(duplicates.favorite_location.isHidden())

    def test_invalid_scheduled_scan_is_not_persisted(self):
        page = self.window.pages["settings"]
        page.scheduled_frequency.setCurrentIndex(page.scheduled_frequency.findData("weekly"))
        page.scheduled_path.set_path("")
        page._save()

        self.assertEqual(self.settings.scheduled_scan_frequency, "off")
        self.assertFalse(page.banner.isHidden())
        self.assertIn("not changed", page.banner.label.text().lower())

    def test_sort_workspace_exposes_profile_rules_review_and_history_sections(self):
        page = self.window.pages["sort"]

        self.assertIsInstance(page, SortWorkspace)
        self.assertEqual(page.stack.count(), 0)
        self.assertGreater(page.profile_choice.count(), 0)
        self.assertFalse(page.advanced_panel.toggle.isChecked())
        self.assertFalse(page.source_page.isHidden())
        self.assertTrue(page.review_page.isHidden())
        self.assertTrue(page.process_page.isHidden())
        self.assertTrue(page.results_page.isHidden())
        self.assertEqual(
            {key for key, option in page.category_checks.items() if option.isChecked()},
            {"pictures", "videos", "audio", "documents"},
        )

        self.assertFalse(page.category_checks["archives"].isChecked())
        self.assertTrue(page.run_button.isHidden())
        self.assertTrue(page.drop_zone.acceptDrops())
        for widget in (
            page.source_table, page.default_destination, page.custom_extensions,
            page.custom_category, page.simple_action, page.simple_conflict,
            page.association_table, page.monitor_table, page.review_filter,
            page.confidence_filter, page.review_table, page.processing_summary,
            page.history_table, page.use_advanced_profile,
        ):
            self.assertTrue(widget.accessibleName())

        self.window.resize(960, 720)
        self.window.navigate("sort")
        QTest.qWait(50)
        self.application.processEvents()
        self.assertEqual(page.horizontalScrollBar().maximum(), 0)
        page.advanced_panel.toggle.click()
        self.application.processEvents()
        self.assertEqual(page.horizontalScrollBar().maximum(), 0)

    def test_verified_update_launch_refuses_busy_work_and_quits_after_success(self):
        class UpdateServiceStub:
            def __init__(self):
                self.calls = []

            def launch_verified_installer(self, manifest, installer_path, *, approved):
                self.calls.append((manifest, installer_path, approved))
                return {"launched": True}

        service = UpdateServiceStub()
        controller = self.window.pages["duplicates"].controller
        with patch.object(type(controller), "busy", new_callable=PropertyMock, return_value=True):
            with self.assertRaises(ServiceError):
                self.window.launch_verified_update(service, {}, "installer.exe", approved=True)
        self.assertEqual(service.calls, [])

        fake_application = Mock()
        with patch.object(self.window, "close") as close, patch(
            "duplicate_transfer_manager.ui.shell.QApplication.instance",
            return_value=fake_application,
        ):
            result = self.window.launch_verified_update(service, {"version": "1.0"}, "installer.exe", approved=True)

        self.assertTrue(result["launched"])
        close.assert_called_once_with()
        fake_application.quit.assert_called_once_with()

    def test_sort_workspace_previews_executes_and_undoes_reviewed_rule(self):
        paths = get_runtime_paths(Path(self.temp_dir.name) / "sort-workspace-data")
        source = Path(self.temp_dir.name) / "sort-workspace-source"
        destination = Path(self.temp_dir.name) / "sort-workspace-destination"
        source.mkdir()
        destination.mkdir()
        original = source / "notes.txt"
        original.write_text("notes", encoding="utf-8")
        page = SortWorkspace(paths, AppSettings(onboarding_completed=True), operations=OperationRecordService(paths))
        page._add_sources([str(source)])
        page.default_destination.setText(str(destination))
        page.dry_run.setChecked(False)

        page._preview()
        page.controller.wait_for_done(10_000)
        QTest.qWait(100)
        self.application.processEvents()

        self.assertIsNotNone(page.plan)
        self.assertEqual(page._current_stage, page.REVIEW_STAGE)
        self.assertEqual(page.review_table.rowCount(), 1)
        self.assertEqual(page.review_table.item(0, 2).text(), "Documents")
        self.assertFalse(page.run_button.isHidden())
        with patch(
            "duplicate_transfer_manager.ui.sort_workspace.QMessageBox.question",
            return_value=QMessageBox.StandardButton.Yes,
        ):
            page._run()
        self.assertEqual(page._current_stage, page.PROCESS_STAGE)
        page.controller.wait_for_done(10_000)
        QTest.qWait(100)
        self.application.processEvents()

        self.assertFalse(original.exists())
        self.assertTrue((destination / "Documents" / "notes.txt").exists())
        self.assertEqual(page._current_stage, page.RESULTS_STAGE)
        self.assertGreater(page.history_table.rowCount(), 0)
        page.history_table.selectRow(0)
        with patch(
            "duplicate_transfer_manager.ui.sort_workspace.QMessageBox.question",
            return_value=QMessageBox.StandardButton.Yes,
        ):
            page._undo_selected()
        # Undo now runs on a worker rather than blocking the window, so the
        # test waits for it the same way it waits for a run.
        page.controller.wait_for_done(10_000)
        QTest.qWait(100)
        self.application.processEvents()
        self.assertTrue(original.exists())
        page.deleteLater()

    def test_sort_workspace_quick_categories_custom_extensions_and_copy(self):
        paths = get_runtime_paths(Path(self.temp_dir.name) / "sort-quick-copy-data")
        source = Path(self.temp_dir.name) / "sort-quick-copy-source"
        destination = Path(self.temp_dir.name) / "sort-quick-copy-destination"
        source.mkdir()
        destination.mkdir()
        pdf = source / "notes.pdf"
        psd = source / "design.psd"
        jpg = source / "photo.jpg"
        executable = source / "tool.exe"
        for file in (pdf, psd, jpg, executable):
            file.write_bytes(file.suffix.encode("ascii"))
        page = SortWorkspace(paths, AppSettings(onboarding_completed=True), operations=OperationRecordService(paths))
        page.resize(884, 650)
        page.show()
        page._add_sources([str(source)])
        page.default_destination.setText(str(destination))
        for key, option in page.category_checks.items():
            option.setChecked(key == "documents")
        page.custom_extensions.setText("psd, *.PSD")
        page.custom_category.setCurrentIndex(page.custom_category.findData("pictures"))
        page.simple_action.setCurrentIndex(page.simple_action.findData(SortAction.COPY))

        page._preview()
        page.controller.wait_for_done(10_000)
        QTest.qWait(100)
        self.application.processEvents()

        self.assertEqual({item.metadata.name for item in page.plan.items}, {"notes.pdf", "design.psd"})
        self.assertTrue(all(item.action == SortAction.COPY for item in page.plan.items))
        self.assertEqual({page.review_table.item(row, 2).text() for row in range(2)}, {"Documents", "Selected extensions → Pictures"})
        self.assertEqual(page.horizontalScrollBar().maximum(), 0)
        with patch(
            "duplicate_transfer_manager.ui.sort_workspace.QMessageBox.question",
            return_value=QMessageBox.StandardButton.Yes,
        ):
            page._run()
        page.controller.wait_for_done(10_000)
        QTest.qWait(100)
        self.application.processEvents()

        self.assertTrue(pdf.exists())
        self.assertTrue(psd.exists())
        self.assertTrue((destination / "Documents" / "notes.pdf").exists())
        self.assertTrue((destination / "Pictures" / "design.psd").exists())
        self.assertFalse((destination / "Pictures" / "photo.jpg").exists())
        self.assertTrue(executable.exists())
        page.deleteLater()

    def test_sort_workspace_manual_ml_destination_saves_feedback_only(self):
        paths = get_runtime_paths(Path(self.temp_dir.name) / "sort-feedback-data")
        source = Path(self.temp_dir.name) / "sort-feedback-source"
        destination = Path(self.temp_dir.name) / "sort-feedback-destination"
        source.mkdir()
        destination.mkdir()
        original = source / "receipt.jpg"
        original.write_bytes(b"image")
        page = SortWorkspace(paths, AppSettings(onboarding_completed=True), operations=OperationRecordService(paths))
        profile = page.store.save(SortingProfile("ML feedback", (), ml_enabled=True))
        page._refresh_profiles(profile.id)
        page.use_advanced_profile.setChecked(True)
        page._add_sources([str(original)])
        page.default_destination.setText(str(destination))
        page._preview()
        page.controller.wait_for_done(10_000)
        QTest.qWait(100)
        self.application.processEvents()
        corrected = destination / "Finance" / original.name

        page.review_table.item(0, 4).setText(str(corrected))
        self.application.processEvents()

        feedback = page.service.planner.ml.list_feedback()
        self.assertEqual(len(feedback), 1)
        self.assertEqual(feedback[0].corrected_category, "Finance")
        self.assertTrue(original.exists())
        self.assertFalse(corrected.exists())
        page.deleteLater()

    def test_sort_workspace_setup_changes_require_a_fresh_review(self):
        paths = get_runtime_paths(Path(self.temp_dir.name) / "sort-invalidation-data")
        source = Path(self.temp_dir.name) / "sort-invalidation-source"
        destination = Path(self.temp_dir.name) / "sort-invalidation-destination"
        source.mkdir()
        destination.mkdir()
        original = source / "notes.txt"
        original.write_text("notes", encoding="utf-8")
        page = SortWorkspace(paths, AppSettings(onboarding_completed=True), operations=OperationRecordService(paths))
        page._add_sources([str(source)])
        page.default_destination.setText(str(destination))
        page._preview()
        page.controller.wait_for_done(10_000)
        QTest.qWait(100)
        self.application.processEvents()

        self.assertIsNotNone(page.plan)
        self.assertEqual(page._current_stage, page.REVIEW_STAGE)
        page._show_section(page.SOURCE_STAGE)
        page.default_destination.setText(str(destination / "fallback"))
        self.application.processEvents()

        self.assertIsNone(page.plan)
        self.assertIsNone(page.session.plan)
        self.assertTrue(page.run_button.isHidden())
        self.assertIn("Build a new review", page.review_summary.text())
        page.deleteLater()

    def test_selection_boxes_disable_mouse_wheel_changes(self):
        import_page = self.window.pages["import"]
        settings_page = self.window.pages["settings"]
        self.assertTrue(import_page.reconnect_timeout.property("wheelChangesDisabled"))
        self.assertTrue(import_page.stall_timeout.property("wheelChangesDisabled"))
        self.assertTrue(settings_page.theme.property("wheelChangesDisabled"))

    def test_import_android_browse_selects_nested_folder(self):
        page = self.window.pages["import"]
        page.source_picker.cards["phone"].setChecked(True)
        page.device_choice.addItem("Pixel — device", "phone-1")
        page.source_path.set_path("/storage/self/primary/DCIM")

        with patch(
            "adb_bridge.ADBBridge.get_directory_structure",
            return_value=[{"name": "Camera", "path": "/sdcard/DCIM/Camera"}],
        ), patch(
            "duplicate_transfer_manager.ui.pages.QInputDialog.getItem",
            return_value=("Camera — /sdcard/DCIM/Camera", True),
        ):
            page._browse(page.source_path, "Choose import source")

        self.assertEqual(page.source_path.path(), "/sdcard/DCIM/Camera")

    def test_import_page_shows_ios_placeholder_without_ios_action(self):
        page = self.window.pages["import"]
        labels = [label.text() for label in page.findChildren(QLabel)]
        buttons = [button.text() for button in page.findChildren(QPushButton)]

        self.assertIn("iOS transfer support coming soon.", labels)
        self.assertFalse(any("iOS" in text and "coming soon" not in text for text in buttons))

    def test_activity_page_exposes_report_open_and_dry_run_controls(self):
        page = self.window.pages["activity"]
        buttons = {button.text() for button in page.findChildren(QPushButton)}

        self.assertIn("Open report", buttons)
        self.assertIn("Open reports folder", buttons)
        self.assertTrue(page.dry_run_reports.text().startswith("Dry run"))
        self.assertEqual(page.table.columnCount(), 6)
        self.assertGreaterEqual(page.table.minimumHeight(), 320)
        self.assertIn(page.detail_card, page.findChildren(type(page.detail_card)))

    def test_activity_actions_apply_to_selected_operation_record(self):
        paths = get_runtime_paths(Path(self.temp_dir.name) / "activity-data")
        operations = OperationRecordService(paths)
        operations.record("duplicate_scan", "completed", title="Duplicate scan", counts={"files": 2})
        page = ActivityPage(operations, ReportService(paths))
        page.refresh()
        page.table.selectRow(0)
        check = page.table.cellWidget(0, 0)
        self.assertIsInstance(check, QCheckBox)
        check.setChecked(True)
        self.application.processEvents()
        self.assertEqual(page.table.currentRow(), 0)
        self.assertIn("Duplicate scan", page.detail_title.text())
        artifact = Path(page._record_artifact_path(page.records[0]))

        with patch("duplicate_transfer_manager.ui.pages.QDesktopServices.openUrl", return_value=True) as opened:
            page.open_selected_report()

        self.assertTrue(opened.called)
        self.assertTrue(artifact.exists())
        page.dry_run_reports.setChecked(True)
        page.remove_selected_report()
        self.assertTrue(artifact.exists())
        page.dry_run_reports.setChecked(False)
        with patch(
            "duplicate_transfer_manager.ui.pages.QMessageBox.question",
            return_value=QMessageBox.StandardButton.Yes,
        ):
            page.remove_selected_report()
        self.assertFalse(artifact.exists())
        page.deleteLater()

    def test_activity_restores_resumable_import_setup_for_review(self):
        paths = get_runtime_paths(Path(self.temp_dir.name) / "resume-data")
        operations = OperationRecordService(paths)
        setup = {
            "source_kind": "folder",
            "source_root": str(Path(self.temp_dir.name) / "source"),
            "existing_library": str(Path(self.temp_dir.name) / "library"),
            "save_to": "",
            "categories": ["pictures"],
            "profile": "Fast",
        }
        operations.record("import", "cancelled", title="Cancelled import", summary={"resume_setup": setup}, resume_available=True)
        page = ActivityPage(operations, ReportService(paths))
        resumed = []
        page.resume_requested.connect(resumed.append)
        page.table.cellWidget(0, 0).setChecked(True)
        page.resume_selected_import()

        self.assertEqual(resumed, [setup])
        imports = ImportPage(settings=self.settings)
        imports.apply_resume_setup(setup)
        self.assertEqual(imports.source_path.path(), setup["source_root"])
        self.assertEqual(imports.profile.currentData(), "Fast")
        self.assertTrue(imports.run_button.isHidden())
        page.deleteLater()
        imports.deleteLater()

    def test_duplicate_review_selection_controls_keep_details_in_sync(self):
        from duplicate_transfer_manager.services import build_duplicate_review
        from models import FileInfo

        root = Path(self.temp_dir.name)
        old = root / "old.jpg"
        new = root / "new.jpg"
        old.write_bytes(b"same")
        new.write_bytes(b"same")
        page = self.window.pages["duplicates"]
        page.review = build_duplicate_review(
            [[FileInfo(str(old), 4, 100), FileInfo(str(new), 4, 200)]],
            thumbnail_root=root / "thumbs",
        )
        page._render_review()
        while page._render_queue:
            page._render_next_batch()
        self.application.processEvents()

        page._clear_duplicate_selection()
        self.assertFalse(page.quarantine_button.isEnabled())
        page._select_recommended_duplicates()
        self.assertTrue(page.quarantine_button.isEnabled())
        checked = next(check for check in page.quarantine_checks.values() if check.isChecked())
        checked.setChecked(False)
        self.application.processEvents()
        self.assertIn("Group", page.duplicate_detail.text())

    def test_quarantine_page_exposes_preview_open_restore_controls(self):
        stored = Path(self.temp_dir.name) / "stored.jpg"
        stored.write_bytes(b"not-an-image")
        paths = get_runtime_paths(Path(self.temp_dir.name) / "qdata")
        operation = paths.quarantine / "op"
        operation.mkdir(parents=True)
        record = QuarantineRecord(
            original_path=str(Path(self.temp_dir.name) / "original.jpg"),
            stored_path=str(stored),
            hash="a" * 64,
            size=stored.stat().st_size,
            reason="duplicate",
            operation_id="op",
        )
        (operation / "manifest.json").write_text(
            '{"records": [' + __import__("json").dumps(record.to_dict()) + "]}",
            encoding="utf-8",
        )
        page = QuarantinePage(DuplicateQuarantineService(paths))
        page.refresh()
        page.table.selectRow(0)
        page._update_preview()
        buttons = {button.text() for button in page.findChildren(QPushButton)}

        self.assertIn("Open quarantined copy", buttons)
        self.assertIn("Open original folder", buttons)
        self.assertIn("Restore quarantine", buttons)
        self.assertGreaterEqual(page.table.minimumHeight(), 320)
        self.assertIn("Preview unavailable", page.preview_detail.text())
        page.deleteLater()

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
        SettingsService(self.paths).save(AppSettings(onboarding_completed=True))
        application, window = create_application(
            [],
            legacy_root=self.temp_dir.name,
            data_root=self.temp_dir.name,
        )
        self.assertIs(application, self.application)
        self.assertIsInstance(window, MainWindow)
        self.assertEqual(application.applicationName(), "Duplicate & Transfer Manager")
        self.assertEqual(application.applicationVersion(), __version__)
        self.assertTrue(application.property("reducedMotion"))
        window.close()
        window.deleteLater()

    def test_select_recommended_never_selects_the_kept_copy(self):
        # The button's tooltip promises "every copy except the file chosen to
        # keep". It selected every enabled box instead, and the keeper's box is
        # only disabled once the user changes a keeper, so on a freshly
        # rendered review confirming quarantined every copy in the group.
        import tempfile
        from pathlib import Path as _Path
        from duplicate_transfer_manager.services import build_duplicate_review
        from models import FileInfo

        with tempfile.TemporaryDirectory() as temp_dir:
            root = _Path(temp_dir)
            files = [root / "a.jpg", root / "b.jpg", root / "c.jpg"]
            for item in files:
                item.write_bytes(b"identical")
            review = build_duplicate_review(
                [[FileInfo(str(item), 9, 100 + index) for index, item in enumerate(files)]],
                thumbnail_root=root / "thumbs",
            )
            page = DuplicatesPage()
            page.review = review
            page._render_review()
            for _ in range(10):
                page._render_next_batch()

            group = review.groups[0]
            keeper = page.quarantine_checks[group.keep_item_id]
            self.assertFalse(keeper.isEnabled(), "the kept copy must not be selectable")

            page._select_recommended_duplicates()
            self.assertFalse(keeper.isChecked())
            selected = [item_id for item_id, check in page.quarantine_checks.items() if check.isChecked()]
            self.assertEqual(len(selected), len(files) - 1)
            self.assertNotIn(group.keep_item_id, selected)

    def test_quarantine_confirm_runs_on_a_worker_and_still_moves_the_file(self):
        # Quarantine moves and pulls files one at a time. Run inline it froze
        # the window for the whole operation with no way to tell it from a hang.
        from PySide6.QtCore import QThreadPool

        root = Path(self.temp_dir.name) / "quarantine-worker"
        root.mkdir(parents=True, exist_ok=True)
        keeper = root / "keep.jpg"
        duplicate = root / "copy.jpg"
        keeper.write_bytes(b"identical")
        duplicate.write_bytes(b"identical")

        page = self.window.pages["duplicates"]
        page.review = build_duplicate_review(
            [[FileInfo(str(keeper), 9, 100), FileInfo(str(duplicate), 9, 200)]],
            thumbnail_root=root / "thumbs",
        )
        page._render_review()
        while page._render_queue:
            page._render_next_batch()
        page._select_recommended_duplicates()
        self.application.processEvents()

        with patch(
            "duplicate_transfer_manager.ui.pages.QMessageBox.question",
            return_value=QMessageBox.StandardButton.Yes,
        ):
            page._confirm_quarantine()

        # The button is held disabled while the worker runs.
        self.assertFalse(page.quarantine_button.isEnabled())
        self.assertTrue(QThreadPool.globalInstance().waitForDone(15_000))
        QTest.qWait(150)
        self.application.processEvents()

        self.assertTrue(page.quarantine_button.isEnabled())
        self.assertTrue(keeper.exists(), "the kept copy must stay in place")
        self.assertFalse(duplicate.exists(), "the duplicate should have moved to quarantine")
        page.deleteLater()


if __name__ == "__main__":
    unittest.main()
