"""Guided import workflow contracts and helpers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from models import TransferSettings
from utils import DEFAULT_EXCLUDES, DEFAULT_MEDIA_EXTS


PICTURE_EXTENSIONS = [
    ".jpg", ".jpeg", ".png", ".gif", ".bmp", ".tiff", ".heic", ".webp",
    ".dng", ".cr2", ".cr3", ".nef", ".arw", ".raf", ".orf", ".rw2",
]
VIDEO_EXTENSIONS = [".mp4", ".mov", ".avi", ".mkv", ".wmv", ".flv", ".mpeg", ".mpg", ".3gp", ".mts", ".m2ts", ".hevc"]
AUDIO_EXTENSIONS = [".mp3", ".aac", ".m4a", ".wav", ".flac", ".ogg"]
DOCUMENT_EXTENSIONS = [".pdf", ".txt", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx"]


TRANSFER_PROFILES = {
    "Reliable": {
        "description": "Most careful. Full hashing, extra retries, conservative Android reads.",
        "hash_mode": "full",
        "retry_attempts": 5,
        "max_hash_workers": 2,
        "transfer_profile": "Reliable",
    },
    "Balanced": {
        "description": "Recommended. Copy-only import with safe verification and practical performance.",
        "hash_mode": "full",
        "retry_attempts": 3,
        "max_hash_workers": 4,
        "transfer_profile": "Balanced",
    },
    "Fast": {
        "description": "Faster comparison for large local libraries. Android copies are still verified.",
        "hash_mode": "fast",
        "retry_attempts": 2,
        "max_hash_workers": 8,
        "transfer_profile": "Fast",
    },
}


STAGE_LABELS = {
    "validation": "Validation",
    "discovery": "Discovery",
    "preflight": "Safety checks",
    "comparison": "Comparison",
    "transfer": "Copying",
    "verification": "Verification",
    "reconnect": "Reconnecting",
    "finalization": "Report generation",
}


@dataclass(frozen=True)
class ImportReview:
    source_label: str
    source_root: str
    existing_library: str
    save_to: str
    same_library_and_save: bool
    categories: tuple[str, ...]
    profile: str
    profile_description: str
    copy_only: bool = True
    preserve_structure: bool = True
    advanced_summary: str = ""


def selected_extensions(categories: Iterable[str]) -> tuple[bool, list[str]]:
    selected = set(categories)
    extensions: list[str] = []
    if "pictures" in selected:
        extensions.extend(PICTURE_EXTENSIONS)
    if "videos" in selected:
        extensions.extend(VIDEO_EXTENSIONS)
    if "audio" in selected:
        extensions.extend(AUDIO_EXTENSIONS)
    if "documents" in selected:
        extensions.extend(DOCUMENT_EXTENSIONS)
    if "other" in selected:
        return False, []
    return True, sorted(set(extensions or DEFAULT_MEDIA_EXTS))


def build_import_settings(
    *,
    source_root: str,
    existing_library: str,
    save_to: str = "",
    source_kind: str = "phone",
    categories: Iterable[str] = ("pictures", "videos"),
    profile: str = "Balanced",
    hash_mode: str | None = None,
    max_hash_workers: int | None = None,
    retry_attempts: int | None = None,
    conflict_policy: str = "rename",
    use_dest_cache: bool = True,
    update_drive_cache: bool = True,
    use_adb_cache: bool = True,
    keep_device_awake: bool = True,
    adb_serial: str = "",
    reconnect_timeout: int = 300,
    stall_timeout: int = 180,
    destination_template: str = "preserve",
    verify_resumed_files: bool = False,
    dry_run: bool = False,
) -> TransferSettings:
    profile_values = TRANSFER_PROFILES.get(profile, TRANSFER_PROFILES["Balanced"])
    only_media, extensions = selected_extensions(categories)
    source_is_adb = source_kind == "phone"
    return TransferSettings(
        source_root=source_root,
        dest_root=existing_library,
        output_root=save_to,
        criteria="hash",
        hash_algo="sha256",
        hash_mode=hash_mode or profile_values["hash_mode"],
        only_media=only_media,
        extensions=extensions,
        min_size_kb=0,
        exclude_dirs=list(DEFAULT_EXCLUDES),
        skip_hidden_system=True,
        dry_run=dry_run,
        preserve_structure=True,
        max_hash_workers=max_hash_workers or int(profile_values["max_hash_workers"]),
        transfer_mode="copy",
        duplicate_policy="skip",
        use_dest_cache=use_dest_cache,
        source_is_adb=source_is_adb,
        adb_serial=adb_serial,
        update_drive_cache=update_drive_cache,
        use_adb_cache=use_adb_cache,
        transfer_profile=str(profile_values["transfer_profile"]),
        retry_attempts=retry_attempts or int(profile_values["retry_attempts"]),
        conflict_policy=conflict_policy,
        keep_device_awake=keep_device_awake,
        reconnect_timeout=reconnect_timeout,
        stall_timeout=stall_timeout,
        destination_template=destination_template if destination_template in {"preserve", "date"} else "preserve",
        verify_resumed_files=verify_resumed_files,
    )


def build_import_review(
    *,
    source_label: str,
    source_root: str,
    existing_library: str,
    save_to: str,
    categories: Iterable[str],
    profile: str,
    settings: TransferSettings,
) -> ImportReview:
    profile_values = TRANSFER_PROFILES.get(profile, TRANSFER_PROFILES["Balanced"])
    effective_save_to = save_to or existing_library
    same = effective_save_to == existing_library
    return ImportReview(
        source_label=source_label,
        source_root=source_root,
        existing_library=existing_library,
        save_to=effective_save_to,
        same_library_and_save=same,
        categories=tuple(categories),
        profile=profile,
        profile_description=str(profile_values["description"]),
        advanced_summary=(
            f"{settings.hash_mode} hashing, {settings.max_hash_workers} worker(s), "
            f"{settings.retry_attempts} retry attempt(s), conflict policy: {settings.conflict_policy}"
            f", organization: {'date folders' if settings.destination_template == 'date' else 'preserve source folders'}"
        ),
    )


def format_transfer_bytes(value: int) -> str:
    amount = float(max(0, value))
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if amount < 1024 or unit == "TB":
            return f"{amount:.1f} {unit}" if unit != "B" else f"{int(amount)} B"
        amount /= 1024
    return f"{int(value)} B"


def summarize_transfer_result(result) -> dict[str, str]:
    raw = result.data.get("engine_result", {}) if hasattr(result, "data") else {}
    bytes_copied = int(raw.get("bytes_transferred", 0) or raw.get("transferred_bytes", 0) or 0)
    summary = {
        "New files copied": str(result.counts.get("transferred", 0)),
        "Duplicates skipped": str(result.counts.get("duplicates", 0)),
        "Files resumed": str(result.counts.get("resumed", 0)),
        "Errors": str(result.counts.get("errors", 0)),
        "Data transferred": format_transfer_bytes(bytes_copied),
        "Report location": result.report_path or "Not written for this run",
    }
    # Shown only when it happened, and named for what it is. These files are
    # unique and were not imported; folding them into "Duplicates skipped" told
    # the user they were already in the library.
    conflict_skipped = int(raw.get("conflict_skipped", 0) or 0)
    if conflict_skipped:
        summary["Not copied, name already taken"] = str(conflict_skipped)
    return summary


def classify_transfer_stage(message: str) -> str:
    lowered = message.lower()
    if "preflight" in lowered or "free space" in lowered or "destination access" in lowered:
        return "preflight"
    if "authorization lost" in lowered or "waiting" in lowered or "reconnect" in lowered:
        return "reconnect"
    if "verify" in lowered or "verification" in lowered or "post-transfer hash" in lowered:
        return "verification"
    if "report" in lowered or "complete" in lowered:
        return "finalization"
    if "cache" in lowered or "compare" in lowered or "duplicate comparison" in lowered:
        return "comparison"
    if "copied" in lowered or "copy" in lowered or "processed" in lowered or "pulling" in lowered:
        return "transfer"
    if "discover" in lowered or "scanning" in lowered or "source scan" in lowered:
        return "discovery"
    return "transfer"
