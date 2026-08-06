"""Read-only duplicate scan entry point for Windows Task Scheduler."""

from __future__ import annotations

import argparse
from pathlib import Path

from models import Settings
from utils import DEFAULT_EXCLUDES, DEFAULT_MEDIA_EXTS, HashCache

from .core import CancellationToken, OperationReporter, OperationState
from .runtime_paths import get_runtime_paths
from .services import DuplicateScanService, OperationRecordService


def run_scheduled_scan(source: str, *, data_root: str = "") -> int:
    """Scan only; scheduled runs never quarantine, move, or delete files."""
    paths = get_runtime_paths(data_root or None)
    source_path = Path(source).expanduser()
    if not source_path.is_dir():
        OperationRecordService(paths).record(
            "scheduled_duplicate_scan",
            "failed",
            title="Scheduled duplicate scan",
            failures=["Scheduled scan folder does not exist or is unavailable."],
        )
        return 2
    cache = HashCache(str(paths.hash_cache))
    cache.load()
    settings = Settings(
        scan_root=str(source_path),
        output_root="",
        criteria="hash",
        hash_algo="sha256",
        hash_mode="full",
        only_media=True,
        extensions=list(DEFAULT_MEDIA_EXTS),
        min_size_kb=0,
        exclude_dirs=list(DEFAULT_EXCLUDES),
        skip_hidden_system=True,
        dry_run=True,
        preserve_structure=True,
        max_hash_workers=4,
    )
    messages: list[str] = []
    try:
        result = DuplicateScanService(cache).run(
            settings,
            CancellationToken(),
            OperationReporter(log_sink=messages.append),
        )
    except Exception as exc:  # noqa: BLE001 - scheduled task must persist a recoverable outcome
        OperationRecordService(paths).record(
            "scheduled_duplicate_scan",
            "failed",
            title="Scheduled duplicate scan",
            failures=[str(exc)],
        )
        return 1
    OperationRecordService(paths).record(
        "scheduled_duplicate_scan",
        "completed" if result.status == OperationState.COMPLETED else "cancelled",
        title="Scheduled duplicate scan",
        counts=result.counts,
        summary={"source": str(source_path), "read_only": True},
        warnings=list(result.warnings),
        failures=[failure.message for failure in result.failures],
    )
    return 0 if result.status == OperationState.COMPLETED else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run a read-only scheduled duplicate scan.")
    parser.add_argument("--source", required=True, help="Local folder to scan")
    parser.add_argument("--data-root", default="", help="Optional Duplicate & Transfer Manager data folder")
    arguments = parser.parse_args(argv)
    return run_scheduled_scan(arguments.source, data_root=arguments.data_root)


if __name__ == "__main__":
    raise SystemExit(main())
