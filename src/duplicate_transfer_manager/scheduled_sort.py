"""Safe background entry point for one configured monitored-folder run."""

from __future__ import annotations

import argparse

from .runtime_paths import get_runtime_paths
from .services import OperationRecordService
from .sorting import SortExecutionControl, SortExecutor, SortMonitorService, SortPlanner, SortingProfileStore


_TERMINAL_STATUSES = frozenset({"completed", "ignored", "skipped", "previewed"})


def _handled_sources(executor: SortExecutor, result) -> list[str]:
    """Source paths whose sorting record reached a terminal, non-failed status."""

    try:
        journal = executor.load_run(result.run_id)
    except Exception:  # noqa: BLE001 - an unreadable journal must not retire files
        return []
    return [
        str(record.get("source", ""))
        for record in journal.get("records", [])
        if record.get("status") in _TERMINAL_STATUSES and record.get("source")
    ]


def run_scheduled_sort(profile_id: str, monitor_id: str, *, data_root: str = "") -> int:
    paths = get_runtime_paths(data_root or None)
    records = OperationRecordService(paths)
    try:
        profile = SortingProfileStore(paths).get(profile_id)
        if profile is None or not profile.enabled:
            raise ValueError("Sorting profile is unavailable or disabled.")
        monitor = next((value for value in profile.monitored_folders if value.id == monitor_id), None)
        if monitor is None:
            raise ValueError("Monitored folder is unavailable.")
        monitor_service = SortMonitorService(paths)
        poll = monitor_service.poll(monitor)
        plan = SortPlanner(paths).build(profile, poll.changed, sources=[monitor.path], dry_run=monitor.dry_run)
        approved = [
            item.metadata.path for item in plan.items
            if not item.requires_review and item.conflict not in {"review", "duplicate_operation", "self"}
        ]
        executor = SortExecutor(paths)
        result = executor.execute(
            plan, approved_sources=approved, confirmed=monitor.live_approved,
            control=SortExecutionControl(), retry_attempts=1,
        )
        # Only now, and only for files that actually reached a terminal status.
        # The snapshot used to be written by poll() before any of this ran, so a
        # planner or executor failure -- swallowed by the except below into a
        # single "failed" row -- silently retired the change set and no future
        # run would ever see those files again.
        monitor_service.commit(monitor.id, poll, handled_paths=_handled_sources(executor, result))
        records.record(
            "scheduled_sort", "warning" if result.failed else "completed", title="Scheduled Sort Files run",
            counts={"completed": result.completed, "skipped": result.skipped, "errors": result.failed, "verified": result.verified},
            summary={"run_id": result.run_id, "journal_path": result.journal_path, "dry_run": monitor.dry_run},
            resume_available=result.undo_available, warnings=list(result.failures),
        )
        return 1 if result.failed else 0
    except Exception as exc:  # noqa: BLE001 - background runs must leave a local record
        records.record("scheduled_sort", "failed", title="Scheduled Sort Files run", failures=[str(exc)])
        return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run one configured monitored sorting profile.")
    parser.add_argument("--profile-id", required=True)
    parser.add_argument("--monitor-id", required=True)
    parser.add_argument("--data-root", default="")
    values = parser.parse_args(argv)
    return run_scheduled_sort(values.profile_id, values.monitor_id, data_root=values.data_root)


if __name__ == "__main__":
    raise SystemExit(main())
