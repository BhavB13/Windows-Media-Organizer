"""Smart-transfer orchestration without frontend dependencies."""

from __future__ import annotations

import time
from typing import Any

from engine import execute_smart_transfer, normalize_settings, validate_transfer_paths

from .import_workflow import classify_transfer_stage
from ..core import (
    CancellationToken,
    ErrorCode,
    OperationFailure,
    OperationPhase,
    OperationReporter,
    OperationResult,
    OperationState,
)
from ..core.errors import ServiceError


class TransferService:
    """Validate and execute a copy-only smart transfer."""

    def __init__(self, hash_cache: Any) -> None:
        self.hash_cache = hash_cache

    def run(
        self,
        settings: Any,
        cancellation: CancellationToken,
        reporter: OperationReporter,
    ) -> OperationResult:
        started = time.monotonic()
        reporter.set_state(
            OperationState.VALIDATING,
            phase=OperationPhase.VALIDATION,
            message="Validating transfer settings…",
        )
        settings = normalize_settings(settings)
        validation_error = validate_transfer_paths(settings)
        if validation_error:
            raise ServiceError(
                ErrorCode.VALIDATION,
                validation_error,
                title="Transfer settings need attention",
                suggested_action="Review the source, library, output, and device selections.",
            )
        cancellation.raise_if_cancelled()

        reporter.set_state(
            OperationState.SCANNING,
            phase=OperationPhase.DISCOVERY,
            message="Discovering source and library files…",
        )

        def on_progress(current: int, total: int, text: str = "") -> None:
            stage = classify_transfer_stage(text)
            state = OperationState.TRANSFERRING
            phase = OperationPhase.TRANSFER
            if stage == "discovery":
                state = OperationState.SCANNING
                phase = OperationPhase.DISCOVERY
            elif stage == "comparison":
                state = OperationState.COMPARING
                phase = OperationPhase.COMPARISON
            elif stage == "verification":
                state = OperationState.TRANSFERRING
                phase = OperationPhase.VERIFICATION
            elif stage == "reconnect":
                state = OperationState.RECONNECTING
                phase = OperationPhase.RECONNECT
            elif stage == "finalization":
                state = OperationState.TRANSFERRING
                phase = OperationPhase.FINALIZATION
            if reporter.state != state:
                reporter.set_state(state, phase=phase)
            reporter.progress_callback(current, total, text, phase=phase)

        raw = execute_smart_transfer(
            settings,
            cancellation,
            self.hash_cache,
            reporter,
            on_progress,
        )
        if hasattr(self.hash_cache, "save"):
            self.hash_cache.save()

        if cancellation.is_cancelled():
            status = OperationState.CANCELLED
        elif raw.get("preflight_failed") or raw.get("adb_device_failed"):
            status = OperationState.FAILED
        else:
            status = OperationState.COMPLETED

        error_count = int(raw.get("errors", 0))
        failures = ()
        if error_count:
            failures = (
                OperationFailure(
                    code="transfer_errors",
                    message=f"{error_count} file operation(s) could not be completed.",
                    technical_detail="See the transfer report and activity log for item-level details.",
                    recoverable=True,
                ),
            )

        reporter.set_state(
            status,
            phase=OperationPhase.FINALIZATION,
            message=(
                "Transfer cancelled safely."
                if status == OperationState.CANCELLED
                else "Transfer completed."
                if status == OperationState.COMPLETED
                else "Transfer stopped because attention is required."
            ),
        )
        return OperationResult(
            status=status,
            counts={
                "transferred": int(raw.get("transferred", 0)),
                "duplicates": int(raw.get("duplicates", 0)),
                "isolated": int(raw.get("isolated", 0)),
                "skipped": int(raw.get("skipped", 0)),
                "resumed": int(raw.get("resumed", 0)),
                "errors": error_count,
            },
            duration_seconds=time.monotonic() - started,
            failures=failures,
            report_path=str(raw.get("report_path", "")),
            resume_information={"resumed_files": int(raw.get("resumed", 0))},
            data={"engine_result": raw},
        )
