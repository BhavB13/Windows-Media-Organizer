"""Primary PySide6 launcher for Duplicate & Transfer Manager."""

from pathlib import Path

# The compatibility import makes the src-layout package available when this
# file is launched directly from a source checkout.
import runtime_paths  # noqa: F401

from duplicate_transfer_manager.ui import run


def main() -> int:
    return run(legacy_root=Path(__file__).resolve().parent)


if __name__ == "__main__":
    raise SystemExit(main())
