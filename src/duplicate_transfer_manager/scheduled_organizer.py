"""Read-only organizer preview entry point for Windows Task Scheduler."""

from __future__ import annotations

import argparse

from .core import OrganizerSettings
from .runtime_paths import get_runtime_paths
from .services import FileOrganizerService, OperationRecordService


def run_scheduled_preview(source: str, destination: str, *, mode: str = "flatten", data_root: str = "") -> int:
    paths = get_runtime_paths(data_root or None)
    service = FileOrganizerService(paths)
    try:
        result = service.organize(
            OrganizerSettings(source, destination, mode=mode, dry_run=True)
        )
    except Exception as exc:  # noqa: BLE001 - background task must record its safe failure
        OperationRecordService(paths).record(
            "scheduled_organization_preview", "failed", title="Scheduled organization preview", failures=[str(exc)]
        )
        return 1
    OperationRecordService(paths).record(
        "scheduled_organization_preview", "completed", title="Scheduled organization preview",
        counts=result.counts, summary={"manifest_path": result.report_path, "read_only": True, "mode": mode},
        warnings=list(result.warnings),
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Create a read-only scheduled organization preview.")
    parser.add_argument("--source", required=True)
    parser.add_argument("--destination", required=True)
    parser.add_argument("--mode", choices=["flatten", "type", "date", "ml"], default="flatten")
    parser.add_argument("--data-root", default="")
    args = parser.parse_args(argv)
    return run_scheduled_preview(args.source, args.destination, mode=args.mode, data_root=args.data_root)


if __name__ == "__main__":
    raise SystemExit(main())
