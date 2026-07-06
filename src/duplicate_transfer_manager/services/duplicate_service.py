"""Duplicate discovery orchestration without frontend dependencies."""

from __future__ import annotations

import time
from typing import Any

from discovery import ScanProgress, discover_files
from engine import group_duplicates, normalize_settings, validate_scan_paths

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


class DuplicateScanService:
    """Run validation, discovery, and hash comparison as one operation."""

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
            message="Validating scan settings…",
        )
        settings = normalize_settings(settings)
        validation_error = validate_scan_paths(settings)
        if validation_error:
            raise ServiceError(
                ErrorCode.VALIDATION,
                validation_error,
                title="Scan settings need attention",
                suggested_action="Review the selected device and scan location.",
            )
        cancellation.raise_if_cancelled()

        reporter.set_state(
            OperationState.SCANNING,
            phase=OperationPhase.DISCOVERY,
            message="Discovering files…",
        )

        def on_discovery(progress: ScanProgress) -> None:
            reporter.emit(
                progress.message or "Discovering files…",
                phase=OperationPhase.DISCOVERY,
                current=progress.files_found,
                current_item=progress.current_path,
                details={
                    "folders_scanned": progress.folders_scanned,
                    "files_found": progress.files_found,
                    "errors": progress.errors,
                    "is_adb": progress.is_adb,
                },
            )

        discovery = discover_files(
            settings.scan_root,
            settings,
            cancellation,
            progress_callback=on_discovery,
            logger=reporter,
        )
        if discovery.cancelled or cancellation.is_cancelled():
            return OperationResult(
                status=OperationState.CANCELLED,
                counts={
                    "files_scanned": discovery.files_found,
                    "folders_scanned": discovery.folders_scanned,
                },
                duration_seconds=time.monotonic() - started,
            )

        reporter.set_state(
            OperationState.COMPARING,
            phase=OperationPhase.HASHING,
            message="Comparing file contents…",
        )
        duplicates = group_duplicates(
            discovery.files,
            settings,
            cancellation,
            self.hash_cache,
            reporter,
            lambda current, total, text: reporter.progress_callback(
                current,
                total,
                text,
                phase=OperationPhase.HASHING,
            ),
        )
        if cancellation.is_cancelled():
            return OperationResult(
                status=OperationState.CANCELLED,
                counts={"files_scanned": discovery.files_found},
                duration_seconds=time.monotonic() - started,
            )

        if hasattr(self.hash_cache, "save"):
            self.hash_cache.save()

        duplicate_count = sum(max(0, len(group) - 1) for group in duplicates)
        failures = tuple(
            OperationFailure(
                code="scan_warning",
                message=message,
                technical_detail=message,
                recoverable=True,
            )
            for message in discovery.errors
        )
        reporter.set_state(
            OperationState.COMPLETED,
            phase=OperationPhase.FINALIZATION,
            message="Duplicate scan complete.",
        )
        return OperationResult(
            status=OperationState.COMPLETED,
            counts={
                "files_scanned": discovery.files_found,
                "folders_scanned": discovery.folders_scanned,
                "duplicate_groups": len(duplicates),
                "duplicate_files": duplicate_count,
                "scan_errors": len(discovery.errors),
            },
            duration_seconds=time.monotonic() - started,
            warnings=tuple(discovery.errors),
            failures=failures,
            data={"groups": duplicates},
        )
