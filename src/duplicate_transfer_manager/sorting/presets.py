"""Beginner-friendly media and document sorting presets."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
from typing import Iterable

from ..core import ErrorCode, ServiceError
from .models import Association, ConflictPolicy, ConditionField, ConditionOperator, SortAction, SortCondition, SortingProfile


@dataclass(frozen=True)
class SortCategory:
    key: str
    label: str
    folder: str
    extensions: tuple[str, ...]
    description: str


DEFAULT_SORT_CATEGORIES: tuple[SortCategory, ...] = (
    SortCategory(
        "pictures", "Pictures", "Pictures",
        (".jpg", ".jpeg", ".png", ".gif", ".bmp", ".tif", ".tiff", ".webp", ".heic", ".heif", ".dng", ".cr2", ".cr3", ".nef", ".arw", ".orf", ".rw2"),
        "Photos, screenshots, scans, and common camera RAW files.",
    ),
    SortCategory(
        "videos", "Videos", "Videos",
        (".mp4", ".mov", ".m4v", ".avi", ".mkv", ".wmv", ".webm", ".mts", ".m2ts", ".3gp"),
        "Phone, camera, screen-recording, and downloaded video files.",
    ),
    SortCategory(
        "audio", "Audio", "Audio",
        (".mp3", ".m4a", ".aac", ".wav", ".flac", ".ogg", ".opus", ".wma", ".aiff"),
        "Music, voice recordings, podcasts, and other audio.",
    ),
    SortCategory(
        "documents", "Documents", "Documents",
        (".pdf", ".doc", ".docx", ".odt", ".rtf", ".txt", ".md", ".xls", ".xlsx", ".ods", ".csv", ".ppt", ".pptx", ".odp", ".epub"),
        "PDFs, office files, text, spreadsheets, presentations, and ebooks.",
    ),
    SortCategory(
        "archives", "Archives", "Archives",
        (".zip", ".7z", ".rar", ".tar", ".gz", ".bz2", ".xz"),
        "Compressed packages and backups. Off by default.",
    ),
)

CATEGORY_BY_KEY = {category.key: category for category in DEFAULT_SORT_CATEGORIES}
DEFAULT_SELECTED_CATEGORIES = ("pictures", "videos", "audio", "documents")


def parse_extensions(value: str | Iterable[str]) -> tuple[str, ...]:
    """Normalize `.jpg, *.PDF; docx` into a stable extension tuple."""
    raw = re.split(r"[\s,;]+", value.strip()) if isinstance(value, str) else list(value)
    normalized: list[str] = []
    for item in raw:
        extension = str(item).strip().casefold()
        if not extension:
            continue
        if extension.startswith("*."):
            extension = extension[1:]
        elif extension.startswith("*"):
            extension = extension[1:]
        if not extension.startswith("."):
            extension = f".{extension}"
        if extension == "." or not re.fullmatch(r"\.[a-z0-9][a-z0-9_+-]*", extension):
            raise ServiceError(ErrorCode.VALIDATION, f"“{item}” is not a valid file extension.")
        if extension not in normalized:
            normalized.append(extension)
    return tuple(normalized)


@dataclass(frozen=True)
class QuickSortOptions:
    destination_root: str
    selected_categories: tuple[str, ...] = DEFAULT_SELECTED_CATEGORIES
    custom_extensions: tuple[str, ...] = ()
    custom_category: str = "documents"
    action: SortAction = SortAction.MOVE
    conflict_policy: ConflictPolicy = ConflictPolicy.RENAME


def build_quick_profile(options: QuickSortOptions) -> SortingProfile:
    """Create deterministic rules from the simple media/document controls."""
    destination = Path(options.destination_root).expanduser()
    try:
        action = SortAction(options.action)
        conflict_policy = ConflictPolicy(options.conflict_policy)
    except ValueError as exc:
        raise ServiceError(ErrorCode.VALIDATION, "Choose a supported sorting action and existing-file option.") from exc
    if not str(options.destination_root).strip():
        raise ServiceError(ErrorCode.VALIDATION, "Choose where sorted files should be saved.")
    if action not in {SortAction.MOVE, SortAction.COPY}:
        raise ServiceError(ErrorCode.VALIDATION, "Simple sorting supports Move or Copy. Use Advanced options for other actions.")
    selected = tuple(dict.fromkeys(str(key) for key in options.selected_categories))
    unknown = [key for key in selected if key not in CATEGORY_BY_KEY]
    if unknown:
        raise ServiceError(ErrorCode.VALIDATION, f"Unknown sorting category: {unknown[0]}")
    custom = parse_extensions(options.custom_extensions)
    if not selected and not custom:
        raise ServiceError(ErrorCode.VALIDATION, "Choose at least one category or enter a file extension.")
    if custom and options.custom_category not in CATEGORY_BY_KEY:
        raise ServiceError(ErrorCode.VALIDATION, "Choose a destination category for the custom extensions.")

    associations: list[Association] = []
    if custom:
        category = CATEGORY_BY_KEY[options.custom_category]
        associations.append(Association(
            name=f"Selected extensions → {category.label}", enabled=True, priority=1000,
            conditions=(SortCondition(ConditionField.EXTENSION, ConditionOperator.IN, custom),),
            action=action, destination=str(destination / category.folder),
            conflict_policy=conflict_policy,
        ))
    for priority, key in enumerate(selected, 100):
        category = CATEGORY_BY_KEY[key]
        associations.append(Association(
            name=category.label, enabled=True, priority=priority,
            conditions=(SortCondition(ConditionField.EXTENSION, ConditionOperator.IN, category.extensions),),
            action=action, destination=str(destination / category.folder),
            conflict_policy=conflict_policy,
        ))
    return SortingProfile(
        name="Media and documents",
        associations=tuple(associations), enabled=True, ml_enabled=False,
        default_conflict_policy=conflict_policy, unmatched_policy="exclude",
        id="quick_media_documents",
    )
