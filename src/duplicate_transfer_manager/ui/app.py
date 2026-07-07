"""Application bootstrap for the PySide6 frontend."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Sequence

from PySide6.QtCore import QCoreApplication, Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import QApplication

from ..runtime_paths import initialize_runtime_data
from ..services import SettingsService
from ..version import __version__
from utils import HashCache
from .shell import MainWindow
from .theme import ThemeManager


def create_application(
    argv: Sequence[str] | None = None,
    *,
    legacy_root: str | Path | None = None,
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
    paths, _migration = initialize_runtime_data(source_root)
    settings_service = SettingsService(paths)
    settings = settings_service.load()
    hash_cache = HashCache(str(paths.hash_cache))
    hash_cache.load()
    theme_manager = ThemeManager(application, settings.appearance)
    theme_manager.apply()
    window = MainWindow(theme_manager, settings, settings_service, hash_cache=hash_cache, runtime_paths=paths)
    return application, window


def run(
    argv: Sequence[str] | None = None,
    *,
    legacy_root: str | Path | None = None,
) -> int:
    application, window = create_application(argv, legacy_root=legacy_root)
    window.show()
    return application.exec()
