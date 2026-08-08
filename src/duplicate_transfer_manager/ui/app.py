"""Application bootstrap for the PySide6 frontend."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Sequence

from PySide6.QtCore import QCoreApplication, Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import QApplication, QMessageBox

from ..runtime_paths import initialize_runtime_data
from ..services import CrashReportService, DashboardService, FileOrganizerService, SettingsService
from ..version import __version__
from adb_bridge import set_adb_executable
from utils import HashCache
from .shell import MainWindow
from ..sorting import SortExecutor, SortingMigrationService
from .theme import ThemeManager


def create_application(
    argv: Sequence[str] | None = None,
    *,
    legacy_root: str | Path | None = None,
    data_root: str | Path | None = None,
) -> tuple[QApplication, MainWindow]:
    existing = QApplication.instance()
    application = existing or QApplication(list(argv or sys.argv))
    QCoreApplication.setOrganizationName("BhavB13")
    QCoreApplication.setOrganizationDomain("github.com/BhavB13")
    QCoreApplication.setApplicationName("Duplicate & Transfer Manager")
    QCoreApplication.setApplicationVersion(__version__)
    application.setProperty("reducedMotion", True)
    application.setFont(QFont("Segoe UI", 10))

    source_root = Path(legacy_root) if legacy_root else Path.cwd()
    paths, _migration = initialize_runtime_data(source_root, root=data_root)
    settings_service = SettingsService(paths)
    settings = settings_service.load()
    # Honour a configured adb location. The setting was persisted but never
    # read, so a user who pointed the app at their own platform-tools was
    # silently ignored.
    if settings.android_platform_tools_path:
        set_adb_executable(settings.android_platform_tools_path)
    DashboardService(paths).prune_cache(settings.cache_retention_days)
    FileOrganizerService(paths).prune_manifests(settings.organization_retention_days)
    SortingMigrationService(paths).migrate_legacy_runs()
    SortExecutor(paths).prune_runs(settings.sorting_history_retention_days)
    _install_crash_handler(application, paths, settings.diagnostic_consent)
    hash_cache = HashCache(str(paths.hash_cache))
    hash_cache.load()
    theme_manager = ThemeManager(application, settings.appearance)
    theme_manager.apply()
    window = MainWindow(theme_manager, settings, settings_service, hash_cache=hash_cache, runtime_paths=paths)
    return application, window


def _install_crash_handler(application: QApplication, paths, diagnostic_consent: bool) -> None:
    previous_hook = sys.excepthook

    def handle_exception(exc_type, exc_value, traceback) -> None:
        report = CrashReportService(paths).create_report(
            exc_value,
            context={"traceback_type": getattr(exc_type, "__name__", str(exc_type))},
        )
        message = QMessageBox()
        message.setIcon(QMessageBox.Icon.Critical)
        message.setWindowTitle("Duplicate & Transfer Manager")
        message.setText("The application hit an unexpected error.")
        message.setInformativeText(
            "A sanitized local crash report was created. Diagnostics are not sent "
            "unless you explicitly opt in."
        )
        message.setDetailedText(Path(report["path"]).read_text(encoding="utf-8"))
        copy_button = message.addButton("Copy sanitized report", QMessageBox.ButtonRole.ActionRole)
        message.addButton(QMessageBox.StandardButton.Close)
        message.exec()
        if message.clickedButton() is copy_button:
            application.clipboard().setText(Path(report["path"]).read_text(encoding="utf-8"))
        if diagnostic_consent:
            # Phase 7 prepares the opt-in boundary; production Sentry transport is configured after release keys exist.
            pass
        previous_hook(exc_type, exc_value, traceback)

    sys.excepthook = handle_exception


def run(
    argv: Sequence[str] | None = None,
    *,
    legacy_root: str | Path | None = None,
    data_root: str | Path | None = None,
) -> int:
    application, window = create_application(argv, legacy_root=legacy_root, data_root=data_root)
    window.show()
    return application.exec()
