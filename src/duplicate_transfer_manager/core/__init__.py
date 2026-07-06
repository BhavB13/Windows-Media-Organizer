"""Framework-neutral application contracts and operation infrastructure."""

from .cancellation import CancellationToken, OperationCancelled
from .contracts import (
    AppSettings,
    OperationEvent,
    OperationFailure,
    OperationPhase,
    OperationResult,
    OperationState,
    QuarantineRecord,
    Severity,
)
from .errors import ErrorCode, StructuredError, map_exception
from .reporting import OperationReporter

__all__ = [
    "AppSettings",
    "CancellationToken",
    "ErrorCode",
    "OperationCancelled",
    "OperationEvent",
    "OperationFailure",
    "OperationPhase",
    "OperationReporter",
    "OperationResult",
    "OperationState",
    "QuarantineRecord",
    "Severity",
    "StructuredError",
    "map_exception",
]
