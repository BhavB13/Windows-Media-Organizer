"""Primary PySide6 launcher for Duplicate & Transfer Manager."""

import sys
from pathlib import Path

# The compatibility import makes the src-layout package available when this
# file is launched directly from a source checkout.
import runtime_paths  # noqa: F401

from duplicate_transfer_manager.scheduled_tasks import RUN_TASK_FLAG, run_scheduled_task
from duplicate_transfer_manager.ui import run


def main() -> int:
    argv = sys.argv[1:]
    # Handled before anything Qt exists. A packaged build has no console
    # scripts, so scheduled tasks re-enter this executable with a flag; without
    # this branch the trigger would just open the application window.
    if argv and argv[0] == RUN_TASK_FLAG:
        if len(argv) < 2:
            sys.stderr.write(f"{RUN_TASK_FLAG} requires a task kind\n")
            return 2
        return run_scheduled_task(argv[1], argv[2:])
    return run(legacy_root=Path(__file__).resolve().parent)


if __name__ == "__main__":
    raise SystemExit(main())
