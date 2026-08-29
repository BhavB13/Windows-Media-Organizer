"""Framework-neutral application contracts and operation infrastructure."""

from .cancellation import CancellationToken, OperationCancelled
from .contracts import (
    AppSettings,
    AuditTier,
    BackupStatus,
    DiskAuditItem,
    DiskAuditSettings,
    DiskAuditSnapshot,
    OperationEvent,
    OperationFailure,
    OperationPhase,
    OperationResult,
    OperationState,
    OrganizationPlanItem,
    OrganizationResult,
    OrganizerSettings,
    QuarantineRecord,
    Severity,
)
from .errors import ErrorCode, ServiceError, StructuredError, map_exception
from .reporting import OperationReporter
from .security import (
    canonical_json,
    correlation_id,
    sanitize_payload,
    sanitize_text,
    verify_rsa_sha256_signature,
)

__all__ = [
    "AppSettings",
    "AuditTier",
    "BackupStatus",
    "CancellationToken",
    "DiskAuditItem",
    "DiskAuditSettings",
    "DiskAuditSnapshot",
    "ErrorCode",
    "OperationCancelled",
    "OperationEvent",
    "OperationFailure",
    "OperationPhase",
    "OperationReporter",
    "OperationResult",
    "OperationState",
    "OrganizationPlanItem",
    "OrganizationResult",
    "OrganizerSettings",
    "QuarantineRecord",
    "Severity",
    "ServiceError",
    "StructuredError",
    "canonical_json",
    "correlation_id",
    "map_exception",
    "sanitize_payload",
    "sanitize_text",
    "verify_rsa_sha256_signature",
]
