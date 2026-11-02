"""Find photos that look alike, and quarantine the ones the user picks.

Exact-duplicate review only finds byte-identical files. A photo that was
resized, recompressed or exported again shares no bytes with its original and
is invisible to it. This service groups such photos with perceptual hashes
(``media.perceptual``), recommends which copy to keep, and hands a selection the
user made to the existing reversible quarantine.

It never removes anything on its own. A scan only reads, nothing is selected by
default, and a selection that would remove every member of a group is refused,
so at least one copy of every photo always stays in place.
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

from discovery import is_cloud_placeholder
from models import FileInfo
from utils import DEFAULT_EXCLUDES

from ..core import ErrorCode, ServiceError
from ..media import perceptual
from ..media.hash_store import PerceptualHashStore
from ..runtime_paths import RuntimePaths, get_runtime_paths
from .duplicate_workflow import (
    IMAGE_EXTENSIONS,
    DuplicateGroup,
    DuplicateQuarantineService,
    DuplicateReview,
    QuarantineResult,
    build_duplicate_review,
)

# Hamming distance thresholds on a 64-bit hash. A resized or recompressed copy
# of a photo measures 0-4; unrelated photos sit far above 20.
SENSITIVITY = {"strict": 6, "balanced": 10, "loose": 14}
DEFAULT_SENSITIVITY = "balanced"

ProgressCallback = Callable[[int, int, str], None]


@dataclass(frozen=True)
class SimilarPhotoGroup:
    group: DuplicateGroup
    kind: str  # "similar", or "identical" when every member has the same bytes
    keeper_id: str
    reasons: tuple[str, ...]

    @property
    def id(self) -> str:
        return self.group.id

    @property
    def items(self):
        return self.group.items


@dataclass(frozen=True)
class SimilarPhotoReview:
    groups: tuple[SimilarPhotoGroup, ...] = ()
    scanned_images: int = 0
    errors: tuple[str, ...] = ()
    cancelled: bool = False
    max_distance: int = SENSITIVITY[DEFAULT_SENSITIVITY]
    duplicate_review: DuplicateReview = field(default_factory=lambda: DuplicateReview(groups=()))

    @property
    def photo_count(self) -> int:
        return sum(len(group.items) for group in self.groups)


def _is_cancelled(stop_event: Any) -> bool:
    return bool(stop_event is not None and stop_event.is_set())


def find_images(roots: Iterable[str], *, stop_event: Any = None, skip_cloud: bool = False) -> list[str]:
    """Image files under ``roots``, skipping system folders and the app's own.

    With ``skip_cloud`` set, cloud-only placeholders are left out by their
    attributes alone; they are never opened, so nothing is downloaded.
    """

    excluded = {name.casefold() for name in DEFAULT_EXCLUDES}
    found: list[str] = []
    for root in roots:
        for folder, dirs, files in os.walk(root):
            if _is_cancelled(stop_event):
                return found
            dirs[:] = [d for d in dirs if d.casefold() not in excluded and not d.startswith(".")]
            for name in files:
                if Path(name).suffix.lower() in IMAGE_EXTENSIONS:
                    path = os.path.join(folder, name)
                    if skip_cloud and is_cloud_placeholder(path):
                        continue
                    found.append(path)
    return sorted(found)


def _sha256(path: str, stop_event: Any) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            if _is_cancelled(stop_event):
                return ""
            digest.update(chunk)
    return digest.hexdigest()


def _all_identical(paths: list[str], stop_event: Any) -> bool:
    """True when every file in the group has the same bytes.

    Sizes are compared first, so content is only read for groups that could
    possibly be identical.
    """

    try:
        if len({os.path.getsize(path) for path in paths}) != 1:
            return False
        digests = {_sha256(path, stop_event) for path in paths}
    except OSError:
        return False
    return len(digests) == 1 and "" not in digests


def _entry_path(candidate: Any) -> str:
    return str(getattr(candidate, "path", candidate))


class SimilarPhotoService:
    def __init__(self, paths: RuntimePaths | None = None) -> None:
        self.paths = paths or get_runtime_paths()

    def scan(
        self,
        roots: Iterable[str],
        *,
        sensitivity: str = DEFAULT_SENSITIVITY,
        skip_cloud: bool = False,
        stop_event: Any = None,
        progress: ProgressCallback | None = None,
        thumbnail_root: str | os.PathLike[str] | None = None,
    ) -> SimilarPhotoReview:
        max_distance = SENSITIVITY.get(sensitivity, SENSITIVITY[DEFAULT_SENSITIVITY])

        def report(done: int, total: int, message: str) -> None:
            if progress is not None:
                progress(done, total, message)

        report(0, 0, "Finding photos…")
        images = find_images(roots, stop_event=stop_event, skip_cloud=skip_cloud)
        if _is_cancelled(stop_event):
            return SimilarPhotoReview(cancelled=True, max_distance=max_distance)

        # Photos unchanged since an earlier scan reuse their stored hash, so a
        # repeat scan decodes only what is new or edited.
        with PerceptualHashStore(self.paths.cache / "perceptual_hashes.sqlite3") as store:
            cached, to_hash = store.lookup(images)
            report(len(cached), len(images), f"Comparing {len(images):,} photos ({len(cached):,} already known)…")
            batch = perceptual.hash_images(to_hash, rotation_invariant=True, stop_event=stop_event)
            if not batch.cancelled:
                store.store(batch.entries)
        errors = tuple(str(error) for error in (batch.errors or ()))
        if batch.cancelled or _is_cancelled(stop_event):
            return SimilarPhotoReview(scanned_images=len(images), errors=errors, cancelled=True, max_distance=max_distance)
        fresh = {entry.path: entry for entry in batch.entries}
        entries = [cached.get(path) or fresh.get(path) for path in images]
        entries = [entry for entry in entries if entry is not None]

        grouped = perceptual.find_similar_groups(
            entries, max_distance=max_distance, rotation_invariant=True, stop_event=stop_event
        )
        if getattr(grouped, "cancelled", False) or _is_cancelled(stop_event):
            return SimilarPhotoReview(scanned_images=len(images), errors=errors, cancelled=True, max_distance=max_distance)

        raw_groups: list[list[FileInfo]] = []
        recommendations: list[Any] = []
        kinds: list[str] = []
        for index, similar in enumerate(grouped.groups, 1):
            paths = [str(path) for path in similar.paths]
            if len(paths) < 2:
                continue
            report(index, len(grouped.groups), "Recommending which copy to keep…")
            infos = []
            for path in paths:
                try:
                    stat = os.stat(path)
                except OSError:
                    continue
                infos.append(FileInfo(path, int(stat.st_size), float(stat.st_mtime)))
            if len(infos) < 2:
                continue
            raw_groups.append(infos)
            recommendations.append(perceptual.recommend_keeper(similar.entries, stop_event=stop_event))
            kinds.append("identical" if _all_identical([info.path for info in infos], stop_event) else "similar")

        review = build_duplicate_review(
            raw_groups,
            prefer="quality",
            thumbnail_root=thumbnail_root,
            scanned_files=len(images),
        )

        groups: list[SimilarPhotoGroup] = []
        for duplicate_group, recommendation, kind in zip(review.groups, recommendations, kinds):
            keeper_path = os.path.normcase(_entry_path(recommendation.keeper))
            keeper = next(
                (item for item in duplicate_group.items if os.path.normcase(item.path) == keeper_path),
                duplicate_group.items[0],
            )
            # Nothing is selected for removal by default. The user chooses.
            neutral = DuplicateGroup(
                id=duplicate_group.id,
                hash=duplicate_group.hash,
                items=duplicate_group.items,
                keep_item_id=keeper.id,
                selected_item_ids=(),
            )
            groups.append(
                SimilarPhotoGroup(
                    group=neutral,
                    kind=kind,
                    keeper_id=keeper.id,
                    reasons=tuple(str(reason) for reason in (recommendation.reasons or ())),
                )
            )

        report(len(groups), len(groups), "Done")
        return SimilarPhotoReview(
            groups=tuple(groups),
            scanned_images=len(images),
            errors=errors,
            max_distance=max_distance,
            duplicate_review=DuplicateReview(groups=tuple(group.group for group in groups), scanned_files=len(images)),
        )

    def quarantine(
        self,
        review: SimilarPhotoReview,
        selected_ids: Iterable[str],
        *,
        dry_run: bool = False,
    ) -> QuarantineResult:
        """Move the chosen photos into the app's reversible quarantine."""

        selected = set(selected_ids)
        if not selected:
            raise ServiceError(ErrorCode.VALIDATION, "Select at least one photo to quarantine.")
        known = {item.id for group in review.groups for item in group.items}
        unknown = selected - known
        if unknown:
            raise ServiceError(ErrorCode.VALIDATION, "The selection does not match this review. Scan again.")
        for group in review.groups:
            if not any(item.id in selected for item in group.items):
                continue
            # Compare against what is still in place, not just this selection:
            # quarantining B and then A in two separate actions would otherwise
            # pass both times and leave the group with no copy where it was.
            staying = [
                item for item in group.items
                if item.id not in selected and os.path.exists(item.path)
            ]
            if not staying:
                raise ServiceError(
                    ErrorCode.VALIDATION,
                    "Keep at least one photo from every group. This would leave no copy of one photo in place.",
                )
        # Byte-identical groups are true duplicates; the rest are only similar.
        reasons = {
            item.id: "duplicate" if group.kind == "identical" else "similar"
            for group in review.groups
            for item in group.items
            if item.id in selected
        }
        return DuplicateQuarantineService(self.paths).quarantine(
            review.duplicate_review, selected, dry_run=dry_run, reason=reasons
        )
