"""End-to-end scan/metadata/rules/ML/plan/execute orchestration."""

from __future__ import annotations

from pathlib import Path
from typing import Iterable

from ..core import CancellationToken, OperationPhase, OperationReporter, OperationState
from ..runtime_paths import RuntimePaths, get_runtime_paths
from .executor import SortExecutionControl, SortExecutor
from .metadata import SortScanner
from .models import SortExecutionResult, SortPlan, SortingProfile
from .planner import SortPlanner


class HybridSortService:
    def __init__(
        self,
        paths: RuntimePaths | None = None,
        scanner: SortScanner | None = None,
        planner: SortPlanner | None = None,
        executor: SortExecutor | None = None,
    ) -> None:
        self.paths = paths or get_runtime_paths()
        self.scanner = scanner or SortScanner()
        self.planner = planner or SortPlanner(self.paths)
        self.executor = executor or SortExecutor(self.paths)

    def prepare(
        self,
        profile: SortingProfile,
        sources: Iterable[str],
        *,
        default_destination: str = "",
        dry_run: bool = True,
        cancellation: CancellationToken | None = None,
        reporter: OperationReporter | None = None,
    ) -> SortPlan:
        source_values = tuple(str(Path(value).expanduser()) for value in sources)
        reporter = reporter or OperationReporter()
        reporter.set_state(OperationState.SCANNING, phase=OperationPhase.DISCOVERY, message="Scanning sorting sources…")
        excluded = self._destination_roots(profile)
        excluded.add(self.paths.root.resolve())
        files = self.scanner.scan(
            source_values, excluded_roots=excluded, cancellation=cancellation, reporter=reporter,
        )
        reporter.set_state(OperationState.COMPARING, phase=OperationPhase.COMPARISON, message="Matching associations and local suggestions…")
        plan = self.planner.build(
            profile, files, sources=source_values, default_destination=default_destination,
            prior_destinations=self._prior_destinations(), dry_run=dry_run,
        )
        reporter.set_state(OperationState.COMPLETED, phase=OperationPhase.FINALIZATION, message="Sorting preview is ready.")
        return plan

    def execute(
        self,
        plan: SortPlan,
        *,
        approved_sources: Iterable[str],
        confirmed: bool,
        control: SortExecutionControl,
        reporter: OperationReporter | None = None,
        retry_attempts: int = 1,
    ) -> SortExecutionResult:
        reporter = reporter or OperationReporter()
        reporter.set_state(OperationState.TRANSFERRING, phase=OperationPhase.TRANSFER, message="Processing approved sorting plan…")
        result = self.executor.execute(
            plan, approved_sources=approved_sources, confirmed=confirmed, control=control,
            reporter=reporter, retry_attempts=retry_attempts,
        )
        state = OperationState.CANCELLED if result.status == "cancelled" else OperationState.COMPLETED
        reporter.set_state(state, phase=OperationPhase.FINALIZATION, message="Sorting run finished.")
        return result

    def _prior_destinations(self) -> tuple[dict, ...]:
        values: list[dict] = []
        for run in reversed(self.executor.list_runs()[-100:]):
            for record in run.get("records", []):
                destination = str(record.get("destination", ""))
                if record.get("status") != "completed" or not destination:
                    continue
                values.append({
                    "extension": str(record.get("extension", "")),
                    "media_type": str(record.get("media_type", "")),
                    "destination": str(Path(destination).parent),
                })
        return tuple(values[-500:])

    @staticmethod
    def _destination_roots(profile: SortingProfile) -> set[Path]:
        roots: set[Path] = set()
        for association in profile.associations:
            value = association.destination.strip()
            if not value:
                continue
            static = value.split("{", 1)[0].rstrip("/\\")
            if static:
                roots.add(Path(static).expanduser().resolve())
        return roots
