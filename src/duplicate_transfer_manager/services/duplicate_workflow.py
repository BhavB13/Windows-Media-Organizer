"""Duplicate review, quarantine, and restore workflow helpers."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

try:
    from PIL import Image, UnidentifiedImageError
except ImportError:  # pragma: no cover - exercised in minimal test environments
    Image = None

    class UnidentifiedImageError(OSError):
        pass

from adb_bridge import ADBBridge
from models import FileInfo
from utils import ensure_unique_path

from ..core import QuarantineRecord
from ..runtime_paths import RuntimePaths, get_runtime_paths


IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".tif", ".tiff"}


@dataclass(frozen=True)
class DuplicateItem:
    """UI-ready duplicate file metadata."""

    id: str
    path: str
    filename: str
    size: int
    modified: float
    created_label: str
    device: str
    is_adb: bool = False
    dimensions: str = ""
    thumbnail_path: str = ""
    preview_status: str = ""


@dataclass(frozen=True)
class DuplicateGroup:
    """A duplicate group with selection defaults."""

    id: str
    hash: str
    items: tuple[DuplicateItem, ...]
    keep_item_id: str
    selected_item_ids: tuple[str, ...]

    @property
    def recoverable_size(self) -> int:
        return sum(item.size for item in self.items if item.id in self.selected_item_ids)


@dataclass(frozen=True)
class DuplicateReview:
    groups: tuple[DuplicateGroup, ...]
    scanned_files: int = 0
    warnings: tuple[str, ...] = ()

    @property
    def selected_count(self) -> int:
        return sum(len(group.selected_item_ids) for group in self.groups)

    @property
    def recoverable_size(self) -> int:
        return sum(group.recoverable_size for group in self.groups)


@dataclass(frozen=True)
class QuarantineResult:
    operation_id: str
    manifest_path: str
    records: tuple[QuarantineRecord, ...] = ()
    failures: tuple[str, ...] = ()
    dry_run: bool = False

    @property
    def quarantined_count(self) -> int:
        return len(self.records)


@dataclass(frozen=True)
class RestoreResult:
    restored: tuple[str, ...] = ()
    skipped: tuple[str, ...] = ()
    failures: tuple[str, ...] = ()
    dry_run: bool = False


def _utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_token(value: str) -> str:
    digest = hashlib.sha256(value.encode("utf-8", errors="replace")).hexdigest()[:12]
    cleaned = "".join(ch if ch.isalnum() or ch in ("-", "_") else "_" for ch in value)
    cleaned = cleaned.strip("._")[:48] or "file"
    return f"{cleaned}_{digest}"


def _format_bytes(value: int) -> str:
    amount = float(max(0, value))
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if amount < 1024 or unit == "TB":
            return f"{amount:.1f} {unit}" if unit != "B" else f"{int(amount)} B"
        amount /= 1024
    return f"{int(value)} B"


def format_duplicate_size(value: int) -> str:
    return _format_bytes(value)


def _date_label(timestamp: float) -> str:
    try:
        return datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d %H:%M")
    except (OSError, ValueError, OverflowError):
        return "Unknown date"


def _dimensions(path: str, is_adb: bool) -> tuple[str, str]:
    if is_adb:
        return "", "Preview unavailable for Android files until they are pulled."
    if Image is None:
        return "", "Preview metadata requires Pillow."
    if Path(path).suffix.lower() not in IMAGE_EXTENSIONS:
        return "", "No image preview for this file type."
    try:
        with Image.open(path) as image:
            width, height = image.size
        return f"{width} × {height}", ""
    except (OSError, UnidentifiedImageError):
        return "", "Preview metadata is unavailable."


def _thumbnail(path: str, target: Path, is_adb: bool) -> tuple[str, str]:
    if is_adb:
        return "", "Android preview unavailable before quarantine."
    if Image is None:
        return "", "Thumbnail generation requires Pillow."
    if Path(path).suffix.lower() not in IMAGE_EXTENSIONS:
        return "", "No thumbnail for this file type."
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with Image.open(path) as image:
            image.thumbnail((96, 96))
            image.convert("RGB").save(target, "JPEG", quality=82)
        return str(target), ""
    except (OSError, UnidentifiedImageError):
        return "", "Thumbnail unavailable."


def _quality_key(item: DuplicateItem) -> tuple[int, int, float, str]:
    """Prefer usable pixel detail, then file size when metadata is unavailable."""
    pixels = 0
    if "×" in item.dimensions:
        try:
            width, height = (int(value.strip()) for value in item.dimensions.split("×", 1))
            pixels = max(0, width) * max(0, height)
        except ValueError:
            pass
    return pixels, max(0, item.size), item.modified, item.path.lower()


def build_duplicate_review(
    raw_groups: Iterable[Iterable[FileInfo]],
    *,
    prefer: str = "oldest",
    thumbnail_root: str | os.PathLike[str] | None = None,
    scanned_files: int = 0,
    warnings: Iterable[str] = (),
) -> DuplicateReview:
    """Convert engine duplicate groups into stable review data."""

    thumb_root = Path(thumbnail_root) if thumbnail_root else get_runtime_paths().cache / "thumbnails"
    groups: list[DuplicateGroup] = []
    for group_index, raw_group in enumerate(raw_groups, 1):
        files = list(raw_group)
        if len(files) < 2:
            continue
        group_hash = hashlib.sha256(
            "\n".join(sorted(file.path for file in files)).encode("utf-8", errors="replace")
        ).hexdigest()[:16]
        items: list[DuplicateItem] = []
        for item_index, file_info in enumerate(files, 1):
            item_id = f"{group_hash}-{item_index}"
            dimensions, dimension_status = _dimensions(file_info.path, file_info.is_adb)
            thumbnail_path, thumbnail_status = _thumbnail(
                file_info.path,
                thumb_root / group_hash / f"{item_index}.jpg",
                file_info.is_adb,
            )
            status = thumbnail_status or dimension_status
            items.append(
                DuplicateItem(
                    id=item_id,
                    path=file_info.path,
                    filename=os.path.basename(file_info.path.rstrip("/\\")) or file_info.path,
                    size=int(file_info.size),
                    modified=float(file_info.created),
                    created_label=_date_label(float(file_info.created)),
                    device="Android" if file_info.is_adb else "This PC",
                    is_adb=bool(file_info.is_adb),
                    dimensions=dimensions,
                    thumbnail_path=thumbnail_path,
                    preview_status=status,
                )
            )
        ordered = sorted(items, key=lambda item: (item.modified, item.path.lower()))
        if prefer == "newest":
            keep = ordered[-1]
        elif prefer == "quality":
            keep = max(items, key=_quality_key)
        else:
            keep = ordered[0]
        selected = tuple(item.id for item in items if item.id != keep.id)
        groups.append(
            DuplicateGroup(
                id=f"group-{group_index}-{group_hash}",
                hash=group_hash,
                items=tuple(items),
                keep_item_id=keep.id,
                selected_item_ids=selected,
            )
        )
    return DuplicateReview(
        groups=tuple(groups),
        scanned_files=scanned_files,
        warnings=tuple(warnings),
    )


class DuplicateQuarantineService:
    """Move reviewed duplicates into app-owned quarantine and restore them."""

    def __init__(self, paths: RuntimePaths | None = None) -> None:
        self.paths = paths or get_runtime_paths()

    def quarantine(
        self,
        review: DuplicateReview,
        selected_ids: Iterable[str],
        *,
        operation_id: str | None = None,
        adb_serial: str = "",
        dry_run: bool = False,
    ) -> QuarantineResult:
        selected = set(selected_ids)
        operation = operation_id or f"duplicate_{_utc_stamp()}"
        operation_root = self.paths.quarantine / operation
        operation_root.mkdir(parents=True, exist_ok=True)
        records: list[QuarantineRecord] = []
        failures: list[str] = []

        for group in review.groups:
            for item in group.items:
                if item.id not in selected:
                    continue
                target_name = _safe_token(item.filename)
                suffix = Path(item.filename).suffix
                if suffix and not target_name.endswith(suffix):
                    target_name = f"{target_name}{suffix}"
                stored_path = operation_root / target_name
                try:
                    if dry_run:
                        if not item.is_adb and not Path(item.path).exists():
                            raise FileNotFoundError(item.path)
                    elif item.is_adb:
                        ADBBridge.pull(item.path, str(stored_path), serial=adb_serial or None)
                    else:
                        if not Path(item.path).exists():
                            raise FileNotFoundError(item.path)
                        shutil.move(item.path, stored_path)
                    records.append(
                        QuarantineRecord(
                            original_path=item.path,
                            stored_path=str(stored_path),
                            hash=group.hash,
                            size=item.size,
                            reason="duplicate",
                            operation_id=operation,
                            source_is_adb=item.is_adb,
                            device_serial=adb_serial if item.is_adb else "",
                        )
                    )
                except Exception as exc:  # noqa: BLE001 - item-level recoverable failure
                    failures.append(f"{item.path}: {exc}")

        manifest_path = self._write_manifest(operation, records, failures, dry_run=dry_run)
        return QuarantineResult(
            operation_id=operation,
            manifest_path=str(manifest_path),
            records=tuple(records),
            failures=tuple(failures),
            dry_run=dry_run,
        )

    def restore_record(
        self,
        record: QuarantineRecord,
        *,
        conflict_policy: str = "rename",
        dry_run: bool = False,
    ) -> RestoreResult:
        if record.source_is_adb:
            return RestoreResult(
                skipped=(
                    f"{record.original_path}: Android originals are not deleted; the quarantined copy can be exported manually.",
                ),
                dry_run=dry_run,
            )
        source = Path(record.stored_path)
        target = Path(record.original_path)
        if not source.exists():
            return RestoreResult(failures=(f"{source}: quarantined file is missing.",), dry_run=dry_run)
        resolved = self._resolve_restore_path(target, conflict_policy)
        if not resolved:
            return RestoreResult(skipped=(str(target),), dry_run=dry_run)
        if dry_run:
            return RestoreResult(restored=(str(resolved),), dry_run=True)
        try:
            resolved.parent.mkdir(parents=True, exist_ok=True)
            if conflict_policy == "replace" and resolved.exists():
                if resolved.is_dir():
                    return RestoreResult(failures=(f"{resolved}: cannot replace a folder with a file.",))
                resolved.unlink()
            shutil.move(str(source), str(resolved))
            self._mark_restored(record, str(resolved))
            return RestoreResult(restored=(str(resolved),), dry_run=dry_run)
        except OSError as exc:
            return RestoreResult(failures=(f"{target}: {exc}",), dry_run=dry_run)

    def restore_operation(
        self,
        operation_id: str,
        *,
        conflict_policy: str = "rename",
        dry_run: bool = False,
    ) -> RestoreResult:
        records = [
            record
            for record in self.list_records()
            if record.operation_id == operation_id and not record.restored_at
        ]
        restored: list[str] = []
        skipped: list[str] = []
        failures: list[str] = []
        for record in records:
            result = self.restore_record(record, conflict_policy=conflict_policy, dry_run=dry_run)
            restored.extend(result.restored)
            skipped.extend(result.skipped)
            failures.extend(result.failures)
        return RestoreResult(tuple(restored), tuple(skipped), tuple(failures), dry_run=dry_run)

    @staticmethod
    def record_paths(record: QuarantineRecord) -> dict[str, str]:
        return {
            "original": record.original_path,
            "quarantined": record.stored_path,
            "quarantined_exists": str(Path(record.stored_path).exists()),
            "original_exists": str(Path(record.original_path).exists()) if not record.source_is_adb else "android",
        }

    def list_records(self) -> list[QuarantineRecord]:
        records: list[QuarantineRecord] = []
        for manifest in sorted(self.paths.quarantine.glob("*/manifest.json")):
            try:
                payload = json.loads(manifest.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            for value in payload.get("records", []):
                if isinstance(value, dict):
                    records.append(QuarantineRecord.from_dict(value))
        return records

    def _write_manifest(
        self,
        operation_id: str,
        records: list[QuarantineRecord],
        failures: list[str],
        *,
        dry_run: bool = False,
    ) -> Path:
        operation_root = self.paths.quarantine / operation_id
        manifest = operation_root / "manifest.json"
        payload = {
            "version": 1,
            "operation_id": operation_id,
            "created_at": _iso_now(),
            "dry_run": dry_run,
            "records": [record.to_dict() for record in records],
            "failures": failures,
            "safety": "Files are moved into app quarantine or copied from Android; no permanent deletion is performed.",
        }
        manifest.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        return manifest

    def _mark_restored(self, record: QuarantineRecord, restored_path: str) -> None:
        manifest = self.paths.quarantine / record.operation_id / "manifest.json"
        try:
            payload = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        for value in payload.get("records", []):
            if not isinstance(value, dict):
                continue
            if value.get("stored_path") == record.stored_path:
                value["restored_path"] = restored_path
                value["restored_at"] = _iso_now()
        manifest.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    @staticmethod
    def _resolve_restore_path(target: Path, policy: str) -> Path | None:
        if not target.exists():
            return target
        if policy == "replace":
            return target
        if policy == "skip":
            return None
        return Path(ensure_unique_path(str(target)))


def duplicate_review_to_dict(review: DuplicateReview) -> dict:
    return {
        "groups": [
            {
                "id": group.id,
                "hash": group.hash,
                "keep_item_id": group.keep_item_id,
                "selected_item_ids": list(group.selected_item_ids),
                "recoverable_size": group.recoverable_size,
                "items": [asdict(item) for item in group.items],
            }
            for group in review.groups
        ],
        "scanned_files": review.scanned_files,
        "selected_count": review.selected_count,
        "recoverable_size": review.recoverable_size,
        "warnings": list(review.warnings),
    }
