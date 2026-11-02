"""Pure planning stage for rules, ML suggestions, templates, and conflicts."""

from __future__ import annotations

import os
import string

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



UNKNOWN_CAMERA = "Unknown camera"
_WINDOWS_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{n}" for n in range(1, 10)), *(f"LPT{n}" for n in range(1, 10))}
_FORBIDDEN = set('<>:"/\\|?*')


def folder_component(text: str, fallback: str = UNKNOWN_CAMERA, limit: int = 80) -> str:
    """Make camera-supplied text safe to use as one Windows folder name.

    EXIF Make and Model are written by the device, not the user, and nothing
    stops them containing a slash, a colon, trailing dots or a reserved device
    name. Used unchecked in a template, "{camera}" could create a nested path or
    a folder Explorer cannot open.
    """

    cleaned = "".join(" " if ch in _FORBIDDEN or ord(ch) < 32 else ch for ch in str(text or ""))
    cleaned = " ".join(cleaned.split())[:limit].rstrip(" .")
    if not cleaned:
        return fallback
    if cleaned.split(".")[0].upper() in _WINDOWS_RESERVED:
        cleaned = f"{cleaned}_"
    return cleaned


def camera_tokens(metadata) -> dict[str, str]:
    """{camera}, {camera_make} and {camera_model} for destination templates.

    {camera} avoids repeating the maker when the model already names it, so a
    Canon reads "Canon EOS R5" rather than "Canon Canon EOS R5"; an iPhone reads
    "Apple iPhone 15 Pro". Files without EXIF - most videos, screenshots,
    documents - share one "Unknown camera" folder rather than failing the plan.
    """

    attributes = getattr(metadata, "attributes", None) or {}
    make = str(attributes.get("camera_make", "") or "").strip()
    model = str(attributes.get("camera_model", "") or "").strip()
    if model and make and model.casefold().startswith(make.split()[0].casefold()):
        combined = model
    else:
        combined = " ".join(part for part in (make, model) if part)
    return {
        "camera": folder_component(combined),
        "camera_make": folder_component(make),
        "camera_model": folder_component(model),
    }


# Fixed, documented sample photo used for live template previews when no file
# metadata is explicitly provided (JPEG taken 2024-07-14 by "Apple iPhone 15 Pro").
SAMPLE_PHOTO = FileMetadata(
    path="IMG_0001.jpg",
    name="IMG_0001.jpg",
    extension=".jpg",
    size=2_400_000,
    created=datetime(2024, 7, 14, 12, 0, 0).timestamp(),
    modified=datetime(2024, 7, 14, 12, 0, 0).timestamp(),
    captured=datetime(2024, 7, 14, 12, 0, 0).timestamp(),
    media_type="image",
    attributes={"camera_make": "Apple", "camera_model": "iPhone 15 Pro"},
)


def template_values(metadata: FileMetadata) -> dict[str, str]:
    """Token values for destination and rename templates.

    Builds the dictionary of tokens available in destination templates
    ({camera}, {year}, {month}, etc.) from a file's metadata.
    """

    source = Path(metadata.path)
    captured = metadata.captured or metadata.modified
    # Local time, deliberately. These folders are named for a human date:
    # the one on the camera for an EXIF capture, and the one Windows
    # Explorer shows for the modified-time fallback. Rendering in UTC moved
    # an evening photo into the next day, and around New Year into the next
    # year, while the review table beside it showed the local date.
    stamp = datetime.fromtimestamp(captured)
    return {
        "name": source.name,
        "stem": source.stem,
        "suffix": source.suffix,
        "extension": metadata.extension.lstrip(".") or "extensionless",
        "media_type": metadata.media_type.title() or "Other",
        "year": f"{stamp.year:04d}",
        "month": f"{stamp.month:02d}",
        "day": f"{stamp.day:02d}",
        **camera_tokens(metadata),
    }


def preview_destination(template: str, metadata: FileMetadata | None = None) -> tuple[str, str]:
    """Render a folder destination template for live preview without filesystem access.

    Renders `template` using the same token values as `SortPlanner`.
    When `metadata` is None, a fixed documented sample photo is used
    (JPEG taken 2024-07-14 by "Apple iPhone 15 Pro").

    Returns `(rendered_path, "")` on success, or `("", error_message)` if the
    template contains unknown tokens or malformed braces.
    """

    cleaned = (template or "").strip()
    if not cleaned:
        return "", ""

    target_meta = metadata if metadata is not None else SAMPLE_PHOTO
    values = template_values(target_meta)

    formatter = string.Formatter()
    try:
        parsed = list(formatter.parse(cleaned))
    except ValueError as exc:
        return "", f"Malformed braces in template: {exc}"

    for _, field_name, _, _ in parsed:
        if field_name is not None:
            raw_token = field_name.split(".")[0].split("[")[0]
            if not raw_token:
                return "", "Malformed braces in template: empty token '{}'"
            if raw_token not in values:
                valid_tokens = ", ".join(f"{{{k}}}" for k in sorted(values.keys()))
                return "", f"Unknown token '{{{raw_token}}}'. Valid tokens: {valid_tokens}"

    try:
        rendered = cleaned.format_map(values)
        return rendered, ""
    except KeyError as exc:
        missing = exc.args[0]
        valid_tokens = ", ".join(f"{{{k}}}" for k in sorted(values.keys()))
        return "", f"Unknown token '{{{missing}}}'. Valid tokens: {valid_tokens}"
    except ValueError as exc:
        return "", f"Malformed braces in template: {exc}"

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
                # Sorting a folder into category subfolders OF THAT FOLDER is
                # the headline use case ("sort my Downloads into Pictures,
                # Videos, Documents"). This used to deselect the row and force
                # review, so a plain quick sort came back with every file
                # unchecked and refused to run.
                #
                # It is not recursion. A destination is computed from the
                # file's metadata -- extension, media type, capture date --
                # never from where the file currently sits, so once a file has
                # moved, planning it again yields the same path, which
                # _resolve_conflict now recognises as the file itself and
                # deselects. A second sort of the same folder is therefore a
                # no-op. The warning is worth keeping so the user knows sorted
                # files stay inside the folder they chose; blocking on it is not.
                item = replace(
                    item,
                    conflict=item.conflict or "nested",
                    warnings=(*item.warnings, "Sorted files stay inside the folder you selected."),
                )
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
        values = template_values(metadata)
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
        if _is_same_file(target, Path(item.metadata.path)):
            # The file is already where the rules want it. Every branch below
            # treats target.exists() as a collision, so without this the RENAME
            # policy "resolved" a file against itself and planned a move to
            # photo_1.jpg -- meaning a second sort of the same folder renamed
            # everything again, and a third did it once more.
            return replace(
                item,
                conflict="self",
                selected=False,
                requires_review=True,
                approved=False,
                warnings=(*item.warnings, "This file is already in its sorted location."),
            )
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


def _is_same_file(left: Path, right: Path) -> bool:
    """True when two paths name the same file on disk."""

    try:
        if left.exists() and right.exists():
            return os.path.samefile(left, right)
    except OSError:
        pass
    return os.path.normcase(os.path.abspath(left)) == os.path.normcase(os.path.abspath(right))


def _within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False
