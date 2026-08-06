"""Render deterministic offscreen screenshots for visual regression review."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path


os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = PROJECT_ROOT / "src"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from duplicate_transfer_manager.core import AppSettings
from duplicate_transfer_manager.runtime_paths import get_runtime_paths
from duplicate_transfer_manager.services import SettingsService
from duplicate_transfer_manager.ui.app import create_application
from PySide6.QtTest import QTest


def render(output: Path, width: int, height: int, theme: str, route: str) -> Path:
    runtime_root = output / ".runtime"
    os.environ["DTM_DATA_DIR"] = str(runtime_root)
    SettingsService(get_runtime_paths(runtime_root)).save(AppSettings(onboarding_completed=True))
    application, window = create_application([], legacy_root=PROJECT_ROOT, data_root=runtime_root)
    window.set_theme(theme)
    window.navigate(route)
    window.resize(width, height)
    window.show()
    QTest.qWait(100)
    application.processEvents()
    output.mkdir(parents=True, exist_ok=True)
    target = output / f"{route}-{theme}-{width}x{height}.png"
    if not window.grab().save(str(target), "PNG"):
        raise RuntimeError(f"Could not save screenshot: {target}")
    window.close()
    application.processEvents()
    return target


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=PROJECT_ROOT / "ui-renders")
    parser.add_argument("--width", type=int, default=1360)
    parser.add_argument("--height", type=int, default=860)
    parser.add_argument("--theme", choices=["light", "dark"], default="light")
    parser.add_argument(
        "--route",
        choices=[
            "overview",
            "duplicates",
            "import",
            "sort",
            "activity",
            "quarantine",
            "settings",
            "help",
        ],
        default="overview",
    )
    arguments = parser.parse_args()
    print(
        render(
            arguments.output,
            arguments.width,
            arguments.height,
            arguments.theme,
            arguments.route,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
