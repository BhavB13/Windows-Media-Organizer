"""Versioned contracts for profiles, associations, and scanned files."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any
from uuid import uuid4


SCHEMA_VERSION = 1


def _id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex}"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class SortAction(str, Enum):
    MOVE = "move"
    COPY = "copy"
    RENAME = "rename"
    IGNORE = "ignore"
    QUARANTINE = "quarantine"
    RECYCLE = "recycle"


class ConflictPolicy(str, Enum):
    SKIP = "skip"
    RENAME = "rename"
    OVERWRITE = "overwrite"
    KEEP_NEWEST = "keep_newest"
    KEEP_LARGEST = "keep_largest"
    REVIEW = "review"


class MatchMode(str, Enum):
    ALL = "all"
    ANY = "any"


class ConditionField(str, Enum):
    EXTENSION = "extension"
    FILENAME = "filename"
    SOURCE_PATH = "source_path"
    SIZE = "size"
    CREATED = "created"
    MODIFIED = "modified"
    MEDIA_TYPE = "media_type"
    WIDTH = "width"
    HEIGHT = "height"
    DURATION = "duration"
    CAPTURED = "captured"


class ConditionOperator(str, Enum):
    EQUALS = "equals"
    NOT_EQUALS = "not_equals"
    CONTAINS = "contains"
    STARTS_WITH = "starts_with"
    ENDS_WITH = "ends_with"
    GLOB = "glob"
    REGEX = "regex"
    IN = "in"
    GREATER_THAN = "greater_than"
    GREATER_OR_EQUAL = "greater_or_equal"
    LESS_THAN = "less_than"
    LESS_OR_EQUAL = "less_or_equal"
    BEFORE = "before"
    AFTER = "after"
    BETWEEN = "between"


@dataclass(frozen=True)
class SortCondition:
    field: ConditionField
    operator: ConditionOperator
    value: Any
    exclude: bool = False
    case_sensitive: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, values: dict[str, Any]) -> "SortCondition":
        return cls(
            field=ConditionField(str(values.get("field", ConditionField.EXTENSION.value))),
            operator=ConditionOperator(str(values.get("operator", ConditionOperator.EQUALS.value))),
            value=values.get("value", ""),
            exclude=bool(values.get("exclude", False)),
            case_sensitive=bool(values.get("case_sensitive", False)),
        )


@dataclass(frozen=True)
class Association:
    name: str
    conditions: tuple[SortCondition, ...]
    action: SortAction
    destination: str = ""
    conflict_policy: ConflictPolicy = ConflictPolicy.RENAME
    enabled: bool = True
    priority: int = 100
    match_mode: MatchMode = MatchMode.ALL
    rename_template: str = "{stem}{suffix}"
    id: str = field(default_factory=lambda: _id("association"))

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["conditions"] = [condition.to_dict() for condition in self.conditions]
        return payload

    @classmethod
    def from_dict(cls, values: dict[str, Any]) -> "Association":
        return cls(
            id=str(values.get("id") or _id("association")),
            name=str(values.get("name", "Untitled association")),
            enabled=bool(values.get("enabled", True)),
            priority=int(values.get("priority", 100)),
            match_mode=MatchMode(str(values.get("match_mode", MatchMode.ALL.value))),
            conditions=tuple(SortCondition.from_dict(item) for item in values.get("conditions", []) if isinstance(item, dict)),
            action=SortAction(str(values.get("action", SortAction.MOVE.value))),
            destination=str(values.get("destination", "")),
            conflict_policy=ConflictPolicy(str(values.get("conflict_policy", ConflictPolicy.RENAME.value))),
            rename_template=str(values.get("rename_template", "{stem}{suffix}")),
        )


@dataclass(frozen=True)
class MonitoredFolder:
    path: str
    enabled: bool = True
    scan_mode: str = "scheduled"
    schedule: str = "off"
    recursive: bool = True
    dry_run: bool = True
    live_approved: bool = False
    id: str = field(default_factory=lambda: _id("watch"))

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, values: dict[str, Any]) -> "MonitoredFolder":
        return cls(
            id=str(values.get("id") or _id("watch")),
            path=str(values.get("path", "")),
            enabled=bool(values.get("enabled", True)),
            scan_mode=str(values.get("scan_mode", "scheduled")),
            schedule=str(values.get("schedule", "off")),
            recursive=bool(values.get("recursive", True)),
            dry_run=bool(values.get("dry_run", True)),
            live_approved=bool(values.get("live_approved", False)),
        )


@dataclass(frozen=True)
class SortingProfile:
    name: str
    associations: tuple[Association, ...] = ()
    monitored_folders: tuple[MonitoredFolder, ...] = ()
    enabled: bool = True
    ml_enabled: bool = True
    high_confidence: float = 0.92
    review_confidence: float = 0.65
    default_conflict_policy: ConflictPolicy = ConflictPolicy.REVIEW
    unmatched_policy: str = "review"
    id: str = field(default_factory=lambda: _id("profile"))
    created_at: str = field(default_factory=_now)
    updated_at: str = field(default_factory=_now)

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["schema_version"] = SCHEMA_VERSION
        payload["associations"] = [association.to_dict() for association in self.associations]
        payload["monitored_folders"] = [folder.to_dict() for folder in self.monitored_folders]
        return payload

    @classmethod
    def from_dict(cls, values: dict[str, Any]) -> "SortingProfile":
        return cls(
            id=str(values.get("id") or _id("profile")),
            name=str(values.get("name", "Untitled profile")),
            enabled=bool(values.get("enabled", True)),
            associations=tuple(Association.from_dict(item) for item in values.get("associations", []) if isinstance(item, dict)),
            monitored_folders=tuple(MonitoredFolder.from_dict(item) for item in values.get("monitored_folders", []) if isinstance(item, dict)),
            ml_enabled=bool(values.get("ml_enabled", True)),
            high_confidence=float(values.get("high_confidence", 0.92)),
            review_confidence=float(values.get("review_confidence", 0.65)),
            default_conflict_policy=ConflictPolicy(str(values.get("default_conflict_policy", ConflictPolicy.REVIEW.value))),
            unmatched_policy=str(values.get("unmatched_policy", "review")),
            created_at=str(values.get("created_at") or _now()),
            updated_at=str(values.get("updated_at") or _now()),
        )


@dataclass(frozen=True)
class FileMetadata:
    path: str
    name: str
    extension: str
    size: int
    created: float
    modified: float
    media_type: str
    width: int = 0
    height: int = 0
    duration: float = 0.0
    captured: float = 0.0
    mime_type: str = ""
    attributes: dict[str, Any] = field(default_factory=dict)

    @property
    def source_path(self) -> str:
        return str(Path(self.path).parent)


@dataclass(frozen=True)
class MLSuggestion:
    category: str
    destination: str
    confidence: float
    explanation: str
    provider: str = "local_fallback"
    available: bool = False


@dataclass(frozen=True)
class SortPlanItem:
    metadata: FileMetadata
    decision_source: str
    action: SortAction
    destination: str
    conflict_policy: ConflictPolicy
    confidence: float
    explanation: str
    matched_association_id: str = ""
    matched_association_name: str = ""
    category: str = ""
    conflict: str = ""
    warnings: tuple[str, ...] = ()
    requires_review: bool = True
    approved: bool = False
    selected: bool = True


@dataclass(frozen=True)
class SortPlan:
    profile_id: str
    sources: tuple[str, ...]
    items: tuple[SortPlanItem, ...]
    created_at: str = field(default_factory=_now)
    dry_run: bool = True

    @property
    def total_bytes(self) -> int:
        return sum(item.metadata.size for item in self.items if item.selected and item.action not in {SortAction.IGNORE})


@dataclass(frozen=True)
class SortExecutionResult:
    run_id: str
    status: str
    journal_path: str
    completed: int = 0
    skipped: int = 0
    failed: int = 0
    verified: int = 0
    bytes_processed: int = 0
    failures: tuple[str, ...] = ()
    undo_available: bool = False
