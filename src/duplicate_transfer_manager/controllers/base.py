"""Reusable Qt worker and operation-controller infrastructure."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from typing import Any

from ..core import (
    CancellationToken,
    ErrorCode,
    OperationEvent,
    OperationFailure,
    OperationReporter,
    OperationResult,
    OperationState,
    StructuredError,
    map_exception,
)
from .qt_compat import QObject, QRunnable, QThreadPool, Signal, Slot


OperationTask = Callable[[CancellationToken, OperationReporter], OperationResult]


class WorkerSignals(QObject):
    event = Signal(object)
    state_changed = Signal(object)
    result = Signal(object)
    error = Signal(object)
    finished = Signal()
    technical_log = Signal(str)


class OperationWorker(QRunnable):
    """Execute a framework-neutral service on a Qt-owned worker thread."""

    def __init__(self, task: OperationTask, cancellation: CancellationToken) -> None:
        super().__init__()
        self.task = task
        self.cancellation = cancellation
        self.signals = WorkerSignals()

    @Slot()
    def run(self) -> None:
        reporter = OperationReporter(
            event_sink=self.signals.event.emit,
            state_sink=self.signals.state_changed.emit,
            log_sink=self.signals.technical_log.emit,
        )
        try:
            result = self.task(self.cancellation, reporter)
            self.signals.result.emit(result)
        except Exception as exc:
            error = map_exception(exc)
            self.signals.technical_log.emit(error.technical_detail)
            self.signals.error.emit(error)
        finally:
            self.signals.finished.emit()


class BaseOperationController(QObject):
    """Own state, cancellation, and Qt signals for one operation at a time."""

    progress = Signal(object)
    state_changed = Signal(object)
    recoverable_error = Signal(object)
    completed = Signal(object)
    cancelled = Signal(object)
    failed = Signal(object)
    technical_log = Signal(str)
    busy_changed = Signal(bool)

    def __init__(self, thread_pool: QThreadPool | None = None, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._thread_pool = thread_pool or QThreadPool.globalInstance()
        self._state = OperationState.IDLE
        self._cancellation: CancellationToken | None = None
        self._worker: OperationWorker | None = None
        self._lock = threading.RLock()

    @property
    def state(self) -> OperationState:
        with self._lock:
            return self._state

    @property
    def busy(self) -> bool:
        return self.state not in {
            OperationState.IDLE,
            OperationState.CANCELLED,
            OperationState.COMPLETED,
            OperationState.FAILED,
        }

    def _set_state(self, state: OperationState) -> None:
        with self._lock:
            if self._state == state:
                return
            was_busy = self.busy
            self._state = state
            is_busy = self.busy
        self.state_changed.emit(state)
        if was_busy != is_busy:
            self.busy_changed.emit(is_busy)

    def start_task(self, task: OperationTask) -> bool:
        with self._lock:
            if self.busy:
                error = StructuredError(
                    ErrorCode.OPERATION_IN_PROGRESS,
                    "Operation already running",
                    "Wait for the current operation to finish or cancel it first.",
                    "Controller rejected a second task while busy.",
                    recoverable=True,
                )
                self.recoverable_error.emit(error)
                return False
            self._cancellation = CancellationToken()
            worker = OperationWorker(task, self._cancellation)
            self._worker = worker

        worker.signals.event.connect(self.progress.emit)
        worker.signals.state_changed.connect(self._set_state)
        worker.signals.result.connect(self._on_result)
        worker.signals.error.connect(self._on_error)
        worker.signals.technical_log.connect(self.technical_log.emit)
        worker.signals.finished.connect(self._on_finished)
        self._set_state(OperationState.VALIDATING)
        self._thread_pool.start(worker)
        return True

    def cancel(self) -> bool:
        with self._lock:
            cancellation = self._cancellation
            if not cancellation or not self.busy:
                return False
            cancellation.cancel()
        self._set_state(OperationState.CANCELLING)
        return True

    @Slot(object)
    def _on_result(self, result: OperationResult) -> None:
        self._set_state(result.status)
        if result.status == OperationState.CANCELLED:
            self.cancelled.emit(result)
        elif result.status == OperationState.FAILED:
            error = StructuredError(
                ErrorCode.IO_ERROR,
                "Operation needs attention",
                "The operation stopped before it could complete.",
                "; ".join(failure.technical_detail for failure in result.failures),
                recoverable=True,
            )
            self.failed.emit(error)
        else:
            self.completed.emit(result)

    @Slot(object)
    def _on_error(self, error: StructuredError) -> None:
        if error.code == ErrorCode.CANCELLED:
            result = OperationResult(
                status=OperationState.CANCELLED,
                failures=(
                    OperationFailure(
                        code=error.code.value,
                        message=error.message,
                        technical_detail=error.technical_detail,
                        recoverable=True,
                    ),
                ),
            )
            self._set_state(OperationState.CANCELLED)
            self.cancelled.emit(result)
            return
        self._set_state(OperationState.FAILED)
        if error.recoverable:
            self.recoverable_error.emit(error)
        self.failed.emit(error)

    @Slot()
    def _on_finished(self) -> None:
        with self._lock:
            self._worker = None
            self._cancellation = None

    def wait_for_done(self, timeout_ms: int = -1) -> bool:
        """Testing and shutdown helper; normal UI code should remain event-driven."""

        return self._thread_pool.waitForDone(timeout_ms)
