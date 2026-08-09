"""Controllers for duplicate discovery and smart transfers."""

from __future__ import annotations

from typing import Any

from ..services import DuplicateScanService, FileOrganizerService, TransferService
from ..sorting import HybridSortService, SortExecutionControl
from ..core import OperationPhase, OperationResult, OperationState
from .base import BaseOperationController
from .qt_compat import QObject, QThreadPool


class DuplicateScanController(BaseOperationController):
    def __init__(
        self,
        hash_cache: Any,
        thread_pool: QThreadPool | None = None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(thread_pool, parent)
        self.service = DuplicateScanService(hash_cache)

    def start(self, settings: Any) -> bool:
        return self.start_task(
            lambda cancellation, reporter: self.service.run(
                settings,
                cancellation,
                reporter,
            )
        )


class TransferController(BaseOperationController):
    def __init__(
        self,
        hash_cache: Any,
        thread_pool: QThreadPool | None = None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(thread_pool, parent)
        self.service = TransferService(hash_cache)

    def start(self, settings: Any) -> bool:
        return self.start_task(
            lambda cancellation, reporter: self.service.run(
                settings,
                cancellation,
                reporter,
            )
        )


class FileOrganizerController(BaseOperationController):
    def __init__(
        self,
        service: FileOrganizerService | None = None,
        thread_pool: QThreadPool | None = None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(thread_pool, parent)
        self.service = service or FileOrganizerService()

    def plan(self, settings: Any) -> bool:
        def task(cancellation, reporter):
            reporter.set_state(OperationState.SCANNING, phase=OperationPhase.DISCOVERY, message="Discovering files to organize…")
            plan = self.service.build_plan(settings, cancellation, reporter)
            reporter.set_state(OperationState.COMPLETED, phase=OperationPhase.FINALIZATION, message="Organization plan ready.")
            return OperationResult(
                status=OperationState.COMPLETED,
                counts={"planned": len(plan)},
                data={"plan": plan},
            )
        return self.start_task(task)

    def organize(self, settings: Any, selected_sources: list[str], reviewed_plan: list[Any]) -> bool:
        return self.start_task(
            lambda cancellation, reporter: self.service.organize(
                settings,
                selected_sources,
                cancellation,
                reporter,
                reviewed_plan=reviewed_plan,
            )
        )


class SortController(BaseOperationController):
    """Qt worker boundary for the new hybrid sorting pipeline."""

    def __init__(
        self,
        service: HybridSortService | None = None,
        thread_pool: QThreadPool | None = None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(thread_pool, parent)
        self.service = service or HybridSortService()
        self.execution_control: SortExecutionControl | None = None

    def preview(self, profile: Any, sources: list[str], *, default_destination: str = "", dry_run: bool = True) -> bool:
        return self.start_task(
            lambda cancellation, reporter: OperationResult(
                status=OperationState.COMPLETED,
                data={"sort_plan": self.service.prepare(
                    profile, sources, default_destination=default_destination, dry_run=dry_run,
                    cancellation=cancellation, reporter=reporter,
                )},
            )
        )

    def execute(self, plan: Any, approved_sources: list[str], *, confirmed: bool, retry_attempts: int = 1) -> bool:
        def task(cancellation, reporter):
            self.execution_control = SortExecutionControl(cancellation)
            result = self.service.execute(
                plan, approved_sources=approved_sources, confirmed=confirmed,
                control=self.execution_control, reporter=reporter, retry_attempts=retry_attempts,
            )
            status = OperationState.CANCELLED if result.status == "cancelled" else OperationState.COMPLETED
            return OperationResult(
                status=status,
                counts={"completed": result.completed, "skipped": result.skipped, "errors": result.failed, "verified": result.verified},
                warnings=result.failures, report_path=result.journal_path,
                resume_information={"undo_available": result.undo_available}, data={"sort_result": result},
            )
        return self.start_task(task)

    def undo(self, run_id: str, *, conflict_policy: Any = None, dry_run: bool = False) -> bool:
        """Roll a sort run back on a worker thread.

        Undo re-hashes and moves every restored file. Running that inline on
        the Qt main thread froze the window with no progress and no cancel,
        which on a large run was indistinguishable from a hang.
        """

        def task(cancellation, reporter):
            self.execution_control = SortExecutionControl(cancellation)
            executor = self.service.executor
            if conflict_policy is None:
                result = executor.undo(run_id, dry_run=dry_run)
            else:
                result = executor.undo(run_id, conflict_policy=conflict_policy, dry_run=dry_run)
            return OperationResult(
                status=OperationState.COMPLETED,
                counts={"restored": result.completed, "skipped": result.skipped, "errors": result.failed},
                warnings=result.failures,
                report_path=result.journal_path,
                data={"sort_result": result, "sort_operation": "undo"},
            )

        return self.start_task(task)

    def retry(self, run_id: str, *, confirmed: bool, retry_attempts: int = 1, resume: bool = False) -> bool:
        """Retry failed items, or resume interrupted ones, on a worker thread."""

        def task(cancellation, reporter):
            self.execution_control = SortExecutionControl(cancellation)
            executor = self.service.executor
            runner = executor.resume_run if resume else executor.retry_failed
            result = runner(run_id, confirmed=confirmed, retry_attempts=retry_attempts)
            status = OperationState.CANCELLED if result.status == "cancelled" else OperationState.COMPLETED
            return OperationResult(
                status=status,
                counts={"completed": result.completed, "skipped": result.skipped, "errors": result.failed},
                warnings=result.failures,
                report_path=result.journal_path,
                resume_information={"undo_available": result.undo_available},
                data={"sort_result": result, "sort_operation": "retry"},
            )

        return self.start_task(task)

    def pause(self) -> bool:
        if not self.execution_control or not self.busy:
            return False
        self.execution_control.pause()
        self._set_state(OperationState.PAUSED)
        return True

    def resume(self) -> bool:
        if not self.execution_control or not self.busy:
            return False
        self.execution_control.resume()
        self._set_state(OperationState.TRANSFERRING)
        return True

    def skip_current(self, source_path: str) -> bool:
        if not self.execution_control or not self.busy:
            return False
        self.execution_control.skip(source_path)
        return True

    def cancel(self) -> bool:
        if self.execution_control:
            self.execution_control.cancel()
        return super().cancel()
