"""Fast PySide6 startup smoke test for CI and release checks.

The normal first-run experience intentionally displays onboarding. This script
uses an isolated temporary data directory with onboarding already completed so
offscreen test runs can construct and close the shell without waiting on a
modal dialog.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import runtime_paths  # noqa: E402,F401
from shiboken6 import delete  # noqa: E402
from duplicate_transfer_manager.core import AppSettings  # noqa: E402
from duplicate_transfer_manager.runtime_paths import get_runtime_paths  # noqa: E402
from duplicate_transfer_manager.services import SettingsService  # noqa: E402
from duplicate_transfer_manager.ui.app import create_application  # noqa: E402


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="dtm-ui-smoke-") as temp_dir:
        paths = get_runtime_paths(temp_dir)
        SettingsService(paths).save(AppSettings(onboarding_completed=True))
        application, window = create_application(
            [],
            legacy_root=temp_dir,
            data_root=temp_dir,
        )
        application.processEvents()
        print(application.applicationName())
        print(application.applicationVersion())
        print(window.windowTitle())
        sys.stdout.flush()
        window.close()
        application.processEvents()
        delete(window)
        application.quit()
        application.processEvents()
    os._exit(0)


if __name__ == "__main__":
    raise SystemExit(main())
