"""Framework-neutral state machine for one guided Sort Files run."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Iterable

from ..core import ErrorCode, ServiceError
from .models import SortAction, SortExecutionResult, SortPlan


class SortWorkflowStage(str, Enum):
    SOURCE = "source"
    RULES = "rules"
    REVIEW = "review"
    PROCESSING = "processing"
    RESULTS = "results"


@dataclass(frozen=True)
class SortSetup:
    profile_id: str
    sources: tuple[str, ...]
    default_destination: str = ""
    dry_run: bool = True

    @classmethod
    def create(
        cls,
        profile_id: str,
        sources: Iterable[str],
        *,
        default_destination: str = "",
        dry_run: bool = True,
    ) -> "SortSetup":
        normalized: list[str] = []
        for value in sources:
            path = str(Path(value).expanduser().resolve())
            if path not in normalized:
                normalized.append(path)
        return cls(
            profile_id=str(profile_id).strip(),
            sources=tuple(normalized),
            default_destination=str(Path(default_destination).expanduser().resolve()) if default_destination else "",
            dry_run=bool(dry_run),
        )


@dataclass
class SortWorkflowSession:
    """Owns valid setup → review → processing → results transitions."""

    stage: SortWorkflowStage = SortWorkflowStage.SOURCE
    setup: SortSetup | None = None
    plan: SortPlan | None = None
    approved_sources: tuple[str, ...] = field(default_factory=tuple)
    result: SortExecutionResult | None = None
    previewing: bool = False

    def configure(self, setup: SortSetup) -> None:
        if not setup.profile_id:
            raise ServiceError(ErrorCode.VALIDATION, "Choose a sorting profile before continuing.")
        if not setup.sources:
            raise ServiceError(ErrorCode.VALIDATION, "Add at least one file or folder before continuing.")
        if any(not Path(source).exists() for source in setup.sources):
            raise ServiceError(ErrorCode.VALIDATION, "One or more sorting sources are no longer available.")
        if setup != self.setup:
            self.plan = None
            self.approved_sources = ()
            self.result = None
        self.setup = setup
        self.stage = SortWorkflowStage.RULES

    def begin_preview(self) -> SortSetup:
        if self.setup is None:
            raise ServiceError(ErrorCode.VALIDATION, "Complete the Source step before building a review.")
        self.previewing = True
        self.plan = None
        self.approved_sources = ()
        return self.setup

    def accept_plan(self, plan: SortPlan) -> None:
        if self.setup is None or not self.previewing:
            raise ServiceError(ErrorCode.VALIDATION, "This sorting plan was not created from the active setup.")
        if plan.profile_id != self.setup.profile_id or tuple(plan.sources) != self.setup.sources or plan.dry_run != self.setup.dry_run:
            raise ServiceError(ErrorCode.VALIDATION, "The sorting setup changed while the review was being built. Build it again.")
        self.previewing = False
        self.plan = plan
        self.approved_sources = ()
        self.stage = SortWorkflowStage.REVIEW

    def approve(self, sources: Iterable[str]) -> tuple[str, ...]:
        if self.plan is None or self.stage is not SortWorkflowStage.REVIEW:
            raise ServiceError(ErrorCode.VALIDATION, "Build and review a sorting plan before processing files.")
        executable = {
            str(Path(item.metadata.path).resolve())
            for item in self.plan.items
            if item.selected and item.action is not SortAction.IGNORE
        }
        approved = tuple(
            source for source in dict.fromkeys(str(Path(value).resolve()) for value in sources)
            if source in executable
        )
        if not self.plan.dry_run and not approved:
            raise ServiceError(ErrorCode.VALIDATION, "Approve at least one file before processing a live plan.")
        self.approved_sources = approved
        return approved

    def revise_plan(self, plan: SortPlan) -> None:
        """Accept a user-reviewed destination edit without changing setup identity."""
        if self.plan is None or self.stage is not SortWorkflowStage.REVIEW:
            raise ServiceError(ErrorCode.VALIDATION, "A plan can be edited only during Review.")
        if (
            plan.profile_id != self.plan.profile_id
            or plan.sources != self.plan.sources
            or plan.dry_run != self.plan.dry_run
        ):
            raise ServiceError(ErrorCode.VALIDATION, "The edited plan no longer matches the reviewed setup.")
        self.plan = plan
        self.approved_sources = ()

    def begin_processing(self) -> None:
        if self.plan is None or self.stage is not SortWorkflowStage.REVIEW:
            raise ServiceError(ErrorCode.VALIDATION, "The sorting plan must be reviewed before processing.")
        if not self.plan.dry_run and not self.approved_sources:
            raise ServiceError(ErrorCode.VALIDATION, "Approve at least one file before processing a live plan.")
        self.stage = SortWorkflowStage.PROCESSING

    def complete(self, result: SortExecutionResult) -> None:
        if self.stage is not SortWorkflowStage.PROCESSING:
            raise ServiceError(ErrorCode.VALIDATION, "No sorting plan is currently processing.")
        self.result = result
        self.stage = SortWorkflowStage.RESULTS

    def cancel_preview(self) -> None:
        self.previewing = False
        self.plan = None
        self.approved_sources = ()
        self.stage = SortWorkflowStage.RULES if self.setup else SortWorkflowStage.SOURCE

    def cancel_processing(self) -> None:
        self.stage = SortWorkflowStage.RESULTS

    def fail_processing(self) -> None:
        """Return a failed worker start/run to Review so it can be corrected or retried."""
        if self.plan is not None:
            self.approved_sources = ()
            self.stage = SortWorkflowStage.REVIEW
        else:
            self.stage = SortWorkflowStage.SOURCE

    def invalidate(self) -> None:
        self.previewing = False
        self.plan = None
        self.approved_sources = ()
        self.result = None
        self.stage = SortWorkflowStage.SOURCE

    def restart(self) -> None:
        self.invalidate()
