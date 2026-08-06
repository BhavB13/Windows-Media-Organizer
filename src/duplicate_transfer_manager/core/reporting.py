"""Framework-neutral progress and logging reporter."""

from __future__ import annotations

import threading
import time
import math
from collections.abc import Callable
from typing import Any

from .contracts import OperationEvent, OperationPhase, OperationState, Severity


EventSink = Callable[[OperationEvent], None]
StateSink = Callable[[OperationState], None]
LogSink = Callable[[str], None]


class OperationReporter:
    """Converts engine callbacks into stable structured events."""

    def __init__(
        self,
        event_sink: EventSink | None = None,
        state_sink: StateSink | None = None,
        log_sink: LogSink | None = None,
    ) -> None:
        self._event_sink = event_sink
        self._state_sink = state_sink
        self._log_sink = log_sink
        self._state = OperationState.IDLE
        self._phase = OperationPhase.VALIDATION
        self._started = time.monotonic()
        self._lock = threading.RLock()

    @property
    def state(self) -> OperationState:
        with self._lock:
            return self._state

    def set_state(
        self,
        state: OperationState,
        *,
        phase: OperationPhase | None = None,
        message: str = "",
    ) -> None:
        with self._lock:
            self._state = state
            if phase is not None:
                self._phase = phase
        if self._state_sink:
            self._state_sink(state)
        if message:
            self.emit(message, phase=phase)

    def emit(
        self,
        message: str,
        *,
        phase: OperationPhase | None = None,
        severity: Severity = Severity.INFO,
        current: int = 0,
        total: int = 0,
        current_item: str = "",
        bytes_processed: int = 0,
        total_bytes: int = 0,
        details: dict[str, Any] | None = None,
    ) -> OperationEvent:
        elapsed = max(time.monotonic() - self._started, 0.000001)
        current = max(0, int(current or 0))
        total = max(0, int(total or 0))
        bytes_processed = max(0, int(bytes_processed or 0))
        total_bytes = max(0, int(total_bytes or 0))
        if total and current > total:
            current = total
        if total_bytes and bytes_processed > total_bytes:
            bytes_processed = total_bytes
        byte_progress = total_bytes > 0
        progress_current = bytes_processed if byte_progress else current
        progress_total = total_bytes if byte_progress else total
        progress = (
            min(1.0, max(0.0, progress_current / progress_total))
            if progress_total > 0
            else None
        )
        rate = progress_current / elapsed if progress_current else 0.0
        if not math.isfinite(rate) or rate < 0:
            rate = 0.0
        eta = (
            max(0.0, (progress_total - progress_current) / rate)
            if progress_total > 0 and rate > 0
            else None
        )
        if eta is not None and (not math.isfinite(eta) or eta < 0):
            eta = None
        with self._lock:
            if phase is not None:
                self._phase = phase
            event = OperationEvent(
                phase=self._phase,
                state=self._state,
                message=message,
                progress=progress,
                bytes_processed=bytes_processed,
                total_bytes=total_bytes,
                processed_items=current,
                total_items=total,
                current_item=current_item,
                rate=rate,
                eta_seconds=eta,
                severity=severity,
                details=details or {},
            )
        if self._event_sink:
            self._event_sink(event)
        return event

    def progress_callback(
        self,
        current: int,
        total: int,
        text: str = "",
        *,
        phase: OperationPhase | None = None,
    ) -> None:
        is_bytes = "Overall " in text or text.startswith("Pulling ")
        self.emit(
            text or "Working…",
            phase=phase,
            current=0 if is_bytes else current,
            total=0 if is_bytes else total,
            bytes_processed=current if is_bytes else 0,
            total_bytes=total if is_bytes else 0,
        )

    def log(self, message: str) -> None:
        if self._log_sink:
            self._log_sink(message)
        upper = message.lstrip().upper()
        severity = Severity.INFO
        if upper.startswith("ERROR"):
            severity = Severity.ERROR
        elif upper.startswith("WARNING"):
            severity = Severity.WARNING
        self.emit(message, severity=severity)
