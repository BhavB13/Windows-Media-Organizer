"""Pure planning stage for rules, ML suggestions, templates, and conflicts."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from ..core import ErrorCode, ServiceError
from ..runtime_paths import RuntimePaths, get_runtime_paths
from .ml import LocalMLService
from .models import (
    Association,
    ConflictPolicy,
    FileMetadata,
    SortAction,
    SortPlan,
    SortPlanItem,
    SortingProfile,
)
from .rules import RuleEvaluator


class SortPlanner:
    def __init__(
        self,
        paths: RuntimePaths | None = None,
        evaluator: RuleEvaluator | None = None,
        ml_service: LocalMLService | None = None,
    ) -> None:
        self.paths = paths or get_runtime_paths()
        self.evaluator = evaluator or RuleEvaluator()
        self.ml = ml_service or LocalMLService(self.paths)

    def build(
        self,
        profile: SortingProfile,
        files: Iterable[FileMetadata],
        *,
        sources: Iterable[str] = (),
        default_destination: str = "",
        prior_destinations: Iterable[dict] = (),
        dry_run: bool = True,
    ) -> SortPlan:
        if not profile.enabled:
            raise ServiceError(ErrorCode.VALIDATION, "Enable the sorting profile before building a plan.")
        reserved: set[Path] = set()
        seen_sources: set[Path] = set()
        items: list[SortPlanItem] = []
        for metadata in files:
            source = Path(metadata.path).resolve()
            if source in seen_sources:
                continue
            seen_sources.add(source)
            match = self.evaluator.evaluate(metadata, profile.associations)
            if match.association is not None:
                item = self._from_association(metadata, match.association, match.explanation)
            else:
                suggestion = self.ml.predict(
                    metadata, profile, default_destination=default_destination,
                    prior_destinations=prior_destinations,
                )
                if suggestion is None:
                    if profile.unmatched_policy == "exclude" and not match.ambiguous:
                        continue
                    explanation = match.explanation if match.ambiguous else "No user association matched and ML is disabled."
                    item = SortPlanItem(
                        metadata, "ambiguous_rule" if match.ambiguous else "unmatched", SortAction.IGNORE, "",
                        profile.default_conflict_policy, 0.0, explanation,
                        warnings=("Review and choose an action or destination.",), requires_review=True, approved=False,
                    )
                else:
                    explanation = suggestion.explanation
                    if match.ambiguous:
                        explanation = f"{match.explanation} {explanation}"
                    requires_review = suggestion.confidence < profile.high_confidence or bool(match.ambiguous)
                    confidence_warning = (
                        "Low-confidence ML suggestion requires review."
                        if suggestion.confidence < profile.review_confidence
                        else "Medium-confidence ML suggestion requires review."
                        if suggestion.confidence < profile.high_confidence
                        else ""
                    )
                    item = SortPlanItem(
                        metadata, "ml", SortAction.MOVE if suggestion.destination else SortAction.IGNORE,
                        self._target_for_suggestion(metadata, suggestion.destination), profile.default_conflict_policy,
                        suggestion.confidence, explanation, category=suggestion.category,
                        warnings=tuple(value for value in (confidence_warning, "Ambiguous user rules require review." if match.ambiguous else "") if value),
                        requires_review=requires_review, approved=False,
                    )
            item = self._resolve_conflict(item, reserved)
            destination = Path(item.destination).resolve() if item.destination else None
            if destination and destination == source:
                item = replace(item, conflict="self", selected=False, warnings=(*item.warnings, "Source and destination are the same file."), requires_review=True, approved=False)
            if destination and any(Path(value).is_dir() and _within(destination, Path(value).resolve()) for value in sources):
                item = replace(item, conflict="recursive", selected=False, warnings=(*item.warnings, "Destination would create recursive sorting."), requires_review=True, approved=False)
            if destination:
                reserved.add(destination)
            items.append(item)
        return SortPlan(profile.id, tuple(str(value) for value in sources), tuple(items), dry_run=dry_run)

    def with_manual_destination(self, item: SortPlanItem, destination: str) -> SortPlanItem:
        target = Path(destination).expanduser().resolve()
        if target == Path(item.metadata.path).resolve():
            raise ServiceError(ErrorCode.VALIDATION, "A file cannot be sorted onto itself.")
        return replace(item, destination=str(target), decision_source="manual", confidence=1.0,
                       explanation="Destination was selected manually.", requires_review=False, approved=True)

    def _from_association(self, metadata: FileMetadata, association: Association, explanation: str) -> SortPlanItem:
        destination = self._render_destination(metadata, association)
        return SortPlanItem(
            metadata=metadata, decision_source="rule", action=association.action, destination=destination,
            conflict_policy=association.conflict_policy, confidence=1.0, explanation=explanation,
            matched_association_id=association.id, matched_association_name=association.name,
            requires_review=False, approved=False,
        )

    def _render_destination(self, metadata: FileMetadata, association: Association) -> str:
        source = Path(metadata.path)
        if association.action in {SortAction.IGNORE, SortAction.RECYCLE}:
            return ""
        if association.action == SortAction.QUARANTINE:
            root = self.paths.sorting / "quarantine"
        else:
            root = Path(association.destination).expanduser() if association.destination else source.parent
        captured = metadata.captured or metadata.modified
        stamp = datetime.fromtimestamp(captured, timezone.utc)
        values = {
            "name": source.name, "stem": source.stem, "suffix": source.suffix,
            "extension": metadata.extension.lstrip(".") or "extensionless",
            "media_type": metadata.media_type.title() or "Other",
            "year": f"{stamp.year:04d}", "month": f"{stamp.month:02d}", "day": f"{stamp.day:02d}",
        }
        try:
            folder = str(root).format_map(values)
            name = association.rename_template.format_map(values) if association.action == SortAction.RENAME else source.name
        except (KeyError, ValueError) as exc:
            raise ServiceError(ErrorCode.VALIDATION, f"{association.name} contains an invalid destination template.", technical_detail=str(exc)) from exc
        return str((Path(folder) / name).resolve())

    @staticmethod
    def _target_for_suggestion(metadata: FileMetadata, destination: str) -> str:
        return str((Path(destination).expanduser() / metadata.name).resolve()) if destination else ""

    def _resolve_conflict(self, item: SortPlanItem, reserved: set[Path]) -> SortPlanItem:
        if not item.destination or item.action in {SortAction.IGNORE, SortAction.RECYCLE}:
            return item
        target = Path(item.destination)
        if target in reserved:
            if item.conflict_policy == ConflictPolicy.RENAME:
                candidate = self._renamed_target(target, reserved)
                return replace(item, destination=str(candidate), conflict="rename", warnings=(*item.warnings, "Another planned item used this destination; a unique name will be used."))
            return replace(item, conflict="duplicate_operation", selected=False, requires_review=True, approved=False,
                           warnings=(*item.warnings, "Multiple planned items target the same destination."))
        exists = target.exists()
        if not exists:
            return item
        policy = item.conflict_policy
        if policy == ConflictPolicy.SKIP:
            return replace(item, conflict="skip", selected=False, warnings=(*item.warnings, "Destination exists; this item will be skipped."))
        if policy == ConflictPolicy.RENAME:
            candidate = self._renamed_target(target, reserved)
            return replace(item, destination=str(candidate), conflict="rename", warnings=(*item.warnings, "Destination exists; a unique name will be used."))
        if policy == ConflictPolicy.OVERWRITE:
            return replace(item, conflict="overwrite", warnings=(*item.warnings, "Existing destination will be preserved in the transaction journal before overwrite."), requires_review=True)
        if policy in {ConflictPolicy.KEEP_NEWEST, ConflictPolicy.KEEP_LARGEST} and target.exists():
            target_stat = target.stat()
            keep_source = item.metadata.modified > target_stat.st_mtime if policy == ConflictPolicy.KEEP_NEWEST else item.metadata.size > target_stat.st_size
            if keep_source:
                return replace(item, conflict="overwrite", warnings=(*item.warnings, f"Source wins the {policy.value.replace('_', ' ')} comparison."), requires_review=True)
            return replace(item, conflict="skip", selected=False, warnings=(*item.warnings, f"Existing destination wins the {policy.value.replace('_', ' ')} comparison."))
        return replace(item, conflict="review", warnings=(*item.warnings, "Destination conflict requires a decision."), requires_review=True, approved=False)

    @staticmethod
    def _renamed_target(target: Path, reserved: set[Path]) -> Path:
        index = 1
        candidate = target
        while candidate.exists() or candidate in reserved:
            candidate = target.with_name(f"{target.stem} ({index}){target.suffix}")
            index += 1
        return candidate


def _within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False
