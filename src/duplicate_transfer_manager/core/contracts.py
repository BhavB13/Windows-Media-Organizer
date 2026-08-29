"""Stable data contracts shared by services, controllers, and frontends."""

from __future__ import annotations

import os

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any


class OperationState(str, Enum):
    IDLE = "idle"
    VALIDATING = "validating"
    SCANNING = "scanning"
    COMPARING = "comparing"
    TRANSFERRING = "transferring"
    PAUSED = "paused"
    RECONNECTING = "reconnecting"
    CANCELLING = "cancelling"
    CANCELLED = "cancelled"
    COMPLETED = "completed"
    FAILED = "failed"

    @property
    def terminal(self) -> bool:
        return self in {self.CANCELLED, self.COMPLETED, self.FAILED}


class OperationPhase(str, Enum):
    VALIDATION = "validation"
    DISCOVERY = "discovery"
    HASHING = "hashing"
    COMPARISON = "comparison"
    PREFLIGHT = "preflight"
    TRANSFER = "transfer"
    VERIFICATION = "verification"
    RECONNECT = "reconnect"
    FINALIZATION = "finalization"
    MAINTENANCE = "maintenance"


class Severity(str, Enum):
    DEBUG = "debug"
    INFO = "info"
    SUCCESS = "success"
    WARNING = "warning"
    ERROR = "error"


class AuditTier(str, Enum):
    """Safety-oriented groups used by the native Disk Audit experience."""

    PROTECT_RECLAIM = "protect_reclaim"
    REGENERABLE_CACHE = "regenerable_cache"
    APPS_REVIEW = "apps_review"
    WINDOWS_MANAGED = "windows_managed"


class BackupStatus(str, Enum):
    """A deliberately conservative, user-declared backup state."""

    UNKNOWN = "unknown"
    USER_CONFIRMED = "user_confirmed"


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class OperationEvent:
    phase: OperationPhase
    state: OperationState
    message: str
    progress: float | None = None
    bytes_processed: int = 0
    total_bytes: int = 0
    processed_items: int = 0
    total_items: int = 0
    current_item: str = ""
    rate: float = 0.0
    eta_seconds: float | None = None
    severity: Severity = Severity.INFO
    timestamp: str = field(default_factory=utc_now_iso)
    details: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class OperationFailure:
    code: str
    message: str
    technical_detail: str = ""
    item: str = ""
    recoverable: bool = False


@dataclass(frozen=True)
class OperationResult:
    status: OperationState
    counts: dict[str, int] = field(default_factory=dict)
    duration_seconds: float = 0.0
    warnings: tuple[str, ...] = ()
    failures: tuple[OperationFailure, ...] = ()
    report_path: str = ""
    resume_information: dict[str, Any] = field(default_factory=dict)
    data: dict[str, Any] = field(default_factory=dict)

    @property
    def successful(self) -> bool:
        # A run that reached the end with per-file failures is finished, but it
        # is not successful. Reporting it as such let the import banner say
        # "Import complete" in success green while files had been left behind.
        return self.status == OperationState.COMPLETED and not self.failures

    @property
    def completed_with_problems(self) -> bool:
        return self.status == OperationState.COMPLETED and bool(self.failures)


@dataclass
class AppSettings:
    appearance: str = "system"
    experience_mode: str = "simple"
    default_transfer_profile: str = "Balanced"
    default_file_categories: list[str] = field(
        default_factory=lambda: ["pictures", "videos"]
    )
    diagnostic_consent: bool = False
    update_channel: str = "stable"
    android_enabled: bool = True
    keep_android_awake: bool = True
    check_updates_automatically: bool = True
    cache_retention_days: int = 90
    android_default_path: str = "/sdcard/DCIM"
    android_platform_tools_path: str = ""
    android_platform_tools_version: str = "37.0.0"
    favorite_locations: list[str] = field(default_factory=list)
    scheduled_scan_path: str = ""
    scheduled_scan_frequency: str = "off"
    organization_retention_days: int = 90
    organization_presets: list[dict[str, Any]] = field(default_factory=list)
    organization_schedule_frequency: str = "off"
    active_sorting_profile_id: str = ""
    sorting_history_retention_days: int = 90
    last_audit_drive: str = ""
    last_audit_mode: str = "quick"
    onboarding_completed: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, values: dict[str, Any]) -> "AppSettings":
        known = cls.__dataclass_fields__
        return cls(**{key: value for key, value in values.items() if key in known})


@dataclass(frozen=True)
class DiskAuditSettings:
    """Inputs for a read-only local-volume audit."""

    volume_root: str
    mode: str = "quick"
    large_file_threshold: int = 1 << 30
    large_folder_threshold: int = 2 << 30
    max_large_items: int = 50


@dataclass(frozen=True)
class DiskAuditItem:
    """One surfaced audit finding; full per-file inventories are never persisted."""

    stable_id: str
    label: str
    kind: str
    paths: tuple[str, ...]
    size_bytes: int
    file_count: int
    tier: AuditTier
    verdict: str
    rationale: str
    recommendation: str
    command: str = ""
    measurement_confidence: str = "measured"
    warnings: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        values = asdict(self)
        values["tier"] = self.tier.value
        values["paths"] = list(self.paths)
        values["warnings"] = list(self.warnings)
        return values

    @classmethod
    def from_dict(cls, values: dict[str, Any]) -> "DiskAuditItem":
        return cls(
            stable_id=str(values.get("stable_id", "")),
            label=str(values.get("label", "")),
            kind=str(values.get("kind", "")),
            paths=tuple(str(path) for path in values.get("paths", ())),
            size_bytes=max(0, int(values.get("size_bytes", 0))),
            file_count=max(0, int(values.get("file_count", 0))),
            tier=AuditTier(str(values.get("tier", AuditTier.PROTECT_RECLAIM.value))),
            verdict=str(values.get("verdict", "review")),
            rationale=str(values.get("rationale", "")),
            recommendation=str(values.get("recommendation", "")),
            command=str(values.get("command", "")),
            measurement_confidence=str(values.get("measurement_confidence", "measured")),
            warnings=tuple(str(value) for value in values.get("warnings", ())),
        )


@dataclass(frozen=True)
class DiskAuditSnapshot:
    """Versioned immutable result for one successful Disk Audit run."""

    schema_version: int
    audit_id: str
    volume_id: str
    volume_label: str
    scanned_root: str
    mode: str
    started_at: str
    completed_at: str
    capacity_bytes: int
    used_bytes: int
    free_bytes: int
    items: tuple[DiskAuditItem, ...] = ()
    warnings: tuple[str, ...] = ()
    inaccessible_count: int = 0
    previous_snapshot_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "audit_id": self.audit_id,
            "volume_id": self.volume_id,
            "volume_label": self.volume_label,
            "scanned_root": self.scanned_root,
            "mode": self.mode,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "capacity_bytes": self.capacity_bytes,
            "used_bytes": self.used_bytes,
            "free_bytes": self.free_bytes,
            "items": [item.to_dict() for item in self.items],
            "warnings": list(self.warnings),
            "inaccessible_count": self.inaccessible_count,
            "previous_snapshot_id": self.previous_snapshot_id,
        }

    @classmethod
    def from_dict(cls, values: dict[str, Any]) -> "DiskAuditSnapshot":
        return cls(
            schema_version=int(values.get("schema_version", 1)),
            audit_id=str(values.get("audit_id", "")),
            volume_id=str(values.get("volume_id", "")),
            volume_label=str(values.get("volume_label", "")),
            scanned_root=str(values.get("scanned_root", "")),
            mode=str(values.get("mode", "quick")),
            started_at=str(values.get("started_at", "")),
            completed_at=str(values.get("completed_at", "")),
            capacity_bytes=max(0, int(values.get("capacity_bytes", 0))),
            used_bytes=max(0, int(values.get("used_bytes", 0))),
            free_bytes=max(0, int(values.get("free_bytes", 0))),
            items=tuple(DiskAuditItem.from_dict(item) for item in values.get("items", ())),
            warnings=tuple(str(value) for value in values.get("warnings", ())),
            inaccessible_count=max(0, int(values.get("inaccessible_count", 0))),
            previous_snapshot_id=str(values.get("previous_snapshot_id", "")),
        )


@dataclass(frozen=True)
class OrganizerSettings:
    source_root: str
    destination_root: str
    selected_folders: tuple[str, ...] = ()
    mode: str = "flatten"
    conflict_policy: str = "rename"
    dry_run: bool = False
    cleanup_empty_folders: bool = False
    ml_auto_organize: bool = False
    ml_confidence_threshold: float = 0.92


@dataclass(frozen=True)
class OrganizationPlanItem:
    source_path: str
    destination_path: str
    size: int
    modified: float
    category: str
    reason: str
    confidence: float = 1.0
    selected: bool = True
    collision: str = ""


@dataclass(frozen=True)
class OrganizationResult:
    operation_id: str
    manifest_path: str
    moved: tuple[str, ...] = ()
    skipped: tuple[str, ...] = ()
    failures: tuple[str, ...] = ()
    cleaned_folders: tuple[str, ...] = ()
    dry_run: bool = False


@dataclass(frozen=True)
class QuarantineRecord:
    original_path: str
    stored_path: str
    hash: str
    size: int
    reason: str
    operation_id: str
    timestamp: str = field(default_factory=utc_now_iso)
    source_is_adb: bool = False
    device_serial: str = ""
    restored_path: str = ""
    restored_at: str = ""
    # "completed" means the move happened. A dry run writes "previewed" and a
    # failed move writes "failed". "pending" is written before the move is
    # attempted and is only ever left behind by a crash mid-operation. Older
    # manifests predate the field entirely and only held real moves, which is
    # why the default is "completed".
    status: str = "completed"

    @property
    def is_recoverable(self) -> bool:
        """Whether this record describes a file that is really in quarantine.

        "previewed" (dry run) and "failed" are decided on the status alone:
        neither ever moved a file. "pending" is the ambiguous one -- the
        process died between the move and the manifest checkpoint, so the file
        may or may not be there. Rejecting those outright would make a genuinely
        moved file unrecoverable, and accepting them all put phantom rows on the
        Quarantine page, so it is settled by looking: the stored file either
        exists or it does not.
        """

        if self.status in {"previewed", "failed"}:
            return False
        if self.status == "pending":
            try:
                return bool(self.stored_path) and os.path.isfile(self.stored_path)
            except OSError:
                return False
        return True

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, values: dict[str, Any]) -> "QuarantineRecord":
        return cls(
            original_path=str(values.get("original_path", "")),
            stored_path=str(values.get("stored_path", "")),
            hash=str(values.get("hash", "")),
            size=int(values.get("size", 0)),
            reason=str(values.get("reason", "")),
            operation_id=str(values.get("operation_id", "")),
            timestamp=str(values.get("timestamp", utc_now_iso())),
            source_is_adb=bool(values.get("source_is_adb", False)),
            device_serial=str(values.get("device_serial", "")),
            restored_path=str(values.get("restored_path", "")),
            restored_at=str(values.get("restored_at", "")),
            status=str(values.get("status", "") or "completed"),
        )
