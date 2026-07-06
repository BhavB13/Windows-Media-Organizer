"""Stable data contracts shared by services, controllers, and frontends."""

from __future__ import annotations

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
        return self.status == OperationState.COMPLETED


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

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, values: dict[str, Any]) -> "AppSettings":
        known = cls.__dataclass_fields__
        return cls(**{key: value for key, value in values.items() if key in known})


@dataclass(frozen=True)
class QuarantineRecord:
    original_path: str
    stored_path: str
    hash: str
    size: int
    reason: str
    operation_id: str
    timestamp: str = field(default_factory=utc_now_iso)

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
        )
