"""Command construction and dispatch for Windows scheduled tasks.

A packaged build has no console scripts, and passing ``-m module`` to the
bundled executable does not run that module: PyInstaller's bootloader ignores
it and starts the application normally. Every scheduled task therefore opened
the GUI on its trigger instead of doing its work. When frozen, the app re-enters
itself through an explicit flag that is handled before any window is created.
"""

from __future__ import annotations

import shutil
import sys

TASK_KINDS = {
    "scan": ("dtm-scheduled-scan", "duplicate_transfer_manager.scheduled_scan"),
    "sort": ("dtm-scheduled-sort", "duplicate_transfer_manager.scheduled_sort"),
    "organizer": ("dtm-scheduled-organizer", "duplicate_transfer_manager.scheduled_organizer"),
}

RUN_TASK_FLAG = "--run-scheduled-task"


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def build_task_command(kind: str, arguments: list[str]) -> list[str]:
    """Build the command Task Scheduler should run for a background task."""

    if kind not in TASK_KINDS:
        raise ValueError(f"Unknown scheduled task kind: {kind}")
    console_script, module = TASK_KINDS[kind]
    if is_frozen():
        return [sys.executable, RUN_TASK_FLAG, kind, *arguments]
    executable = shutil.which(console_script)
    if executable:
        return [executable, *arguments]
    return [sys.executable, "-m", module, *arguments]


def run_scheduled_task(kind: str, arguments: list[str]) -> int:
    """Dispatch a task requested through RUN_TASK_FLAG. Never opens a window."""

    if kind not in TASK_KINDS:
        sys.stderr.write(f"Unknown scheduled task kind: {kind}\n")
        return 2
    if kind == "scan":
        from .scheduled_scan import main as task_main
    elif kind == "sort":
        from .scheduled_sort import main as task_main
    else:
        from .scheduled_organizer import main as task_main
    return int(task_main(list(arguments)))
