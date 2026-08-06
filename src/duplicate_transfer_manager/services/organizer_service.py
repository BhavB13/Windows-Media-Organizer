"""Safe, manifest-backed local file organization and rollback."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import struct
from dataclasses import asdict, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Iterable

from ..core import (
    CancellationToken,
    ErrorCode,
    OrganizationPlanItem,
    OrganizationResult,
    OrganizerSettings,
    OperationPhase,
    OperationReporter,
    OperationResult,
    OperationState,
)
from ..core.errors import ServiceError
from ..runtime_paths import RuntimePaths, get_runtime_paths


_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".tif", ".tiff", ".heic", ".webp", ".dng", ".cr2", ".cr3", ".nef", ".arw", ".raf", ".orf", ".rw2"}
_VIDEO_EXTENSIONS = {".mp4", ".mov", ".avi", ".mkv", ".wmv", ".flv", ".mpeg", ".mpg", ".3gp", ".mts", ".m2ts", ".hevc"}
_AUDIO_EXTENSIONS = {".mp3", ".aac", ".m4a", ".wav", ".flac", ".ogg"}
_DOCUMENT_EXTENSIONS = {".pdf", ".txt", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".csv", ".md"}
_ARCHIVE_EXTENSIONS = {".zip", ".rar", ".7z", ".tar", ".gz", ".bz2"}
_INSTALLER_EXTENSIONS = {".exe", ".msi", ".msix", ".appx", ".bat", ".cmd"}
_ML_CATEGORIES = ("Screenshots", "Receipts", "People", "Pets", "Travel", "Food", "Artwork", "Miscellaneous")


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _operation_id() -> str:
    return "organize_" + datetime.now(UTC).strftime("%Y%m%d_%H%M%S_%f")


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def _category(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix in _IMAGE_EXTENSIONS:
        return "Pictures"
    if suffix in _VIDEO_EXTENSIONS:
        return "Videos"
    if suffix in _AUDIO_EXTENSIONS:
        return "Audio"
    if suffix in _DOCUMENT_EXTENSIONS:
        return "Documents"
    if suffix in _ARCHIVE_EXTENSIONS:
        return "Archives"
    if suffix in _INSTALLER_EXTENSIONS:
        return "Installers"
    if any(part.lower() in {"project", "projects", "src", "source"} for part in path.parts):
        return "Projects"
    return "Other"


def _ml_suggestion(path: Path, overrides: dict | None = None) -> tuple[str, float, str]:
    """Private deterministic fallback until an optional signed ONNX pack is installed."""
    overrides = overrides or {}
    excluded = [str(value) for value in overrides.get("excluded_folders", [])]
    if any(_is_within(path, Path(folder)) for folder in excluded if Path(folder).is_dir()):
        return _category(path), 0.0, "This folder is excluded from automatic ML selection."
    rule = str(overrides.get("extension_rules", {}).get(path.suffix.lower(), ""))
    if rule in _ML_CATEGORIES:
        return rule, 1.0, f"Local rule for {path.suffix.lower() or 'extensionless files'} applied."
    name = path.stem.lower()
    matches = {
        "screenshot": "Screenshots",
        "screen shot": "Screenshots",
        "receipt": "Receipts",
        "invoice": "Receipts",
        "pet": "Pets",
        "dog": "Pets",
        "cat": "Pets",
        "travel": "Travel",
        "vacation": "Travel",
        "food": "Food",
        "art": "Artwork",
    }
    for token, label in matches.items():
        if token in name:
            return label, 0.95, f"Local filename rule matched '{token}'."
    return _category(path), 0.60, "No local ML model is installed; deterministic file-type rule used."


def _embedded_capture_time(path: Path) -> datetime | None:
    """Read JPEG/TIFF DateTimeOriginal without adding a UI or image dependency.

    It deliberately treats malformed or unsupported metadata as absent; Date
    Rules then use the UTC filesystem timestamp as documented.
    """
    if path.suffix.lower() not in {".jpg", ".jpeg", ".tif", ".tiff"}:
        return None
    try:
        data = path.read_bytes()[:512 * 1024]
    except OSError:
        return None
    marker = data.find(b"Exif\x00\x00")
    tiff = marker + 6 if marker >= 0 else 0
    if len(data) < tiff + 8:
        return None
    byte_order = data[tiff:tiff + 2]
    if byte_order == b"II":
        order = "<"
    elif byte_order == b"MM":
        order = ">"
    else:
        return None
    try:
        if struct.unpack_from(f"{order}H", data, tiff + 2)[0] != 42:
            return None

        def read_ifd(offset: int) -> dict[int, tuple[int, int, int]]:
            start = tiff + offset
            count = struct.unpack_from(f"{order}H", data, start)[0]
            entries: dict[int, tuple[int, int, int]] = {}
            for index in range(count):
                entry = start + 2 + index * 12
                tag, value_type, value_count, value = struct.unpack_from(f"{order}HHII", data, entry)
                entries[tag] = (value_type, value_count, value)
            return entries

        first_ifd = struct.unpack_from(f"{order}I", data, tiff + 4)[0]
        entries = read_ifd(first_ifd)
        exif_pointer = entries.get(0x8769)
        if exif_pointer:
            entries = {**entries, **read_ifd(exif_pointer[2])}
        date_entry = entries.get(0x9003) or entries.get(0x0132)
        if not date_entry or date_entry[0] != 2:
            return None
        _, count, value = date_entry
        raw = struct.pack(f"{order}I", value)[:count] if count <= 4 else data[tiff + value:tiff + value + count]
        return datetime.strptime(raw.rstrip(b"\x00").decode("ascii"), "%Y:%m:%d %H:%M:%S").replace(tzinfo=UTC)
    except (IndexError, OSError, UnicodeDecodeError, ValueError, struct.error):
        return None


class FileOrganizerService:
    """Plan, execute, and reverse safe local organization operations."""

    def __init__(self, paths: RuntimePaths | None = None) -> None:
        self.paths = paths or get_runtime_paths()

    @property
    def _overrides_path(self) -> Path:
        return self.paths.organization / "ml_overrides.json"

    def get_ml_overrides(self) -> dict:
        """Return local correction rules; nothing is uploaded or shared."""
        try:
            payload = json.loads(self._overrides_path.read_text(encoding="utf-8"))
            if isinstance(payload, dict):
                return {
                    "extension_rules": payload.get("extension_rules", {}) if isinstance(payload.get("extension_rules", {}), dict) else {},
                    "excluded_folders": payload.get("excluded_folders", []) if isinstance(payload.get("excluded_folders", []), list) else [],
                }
        except (OSError, json.JSONDecodeError):
            pass
        return {"extension_rules": {}, "excluded_folders": []}

    def set_ml_extension_rule(self, extension: str, category: str) -> None:
        if category not in _ML_CATEGORIES:
            raise ServiceError(ErrorCode.VALIDATION, "Choose a supported local ML category.")
        extension = extension.lower().strip()
        if extension and not extension.startswith("."):
            extension = f".{extension}"
        overrides = self.get_ml_overrides()
        overrides["extension_rules"][extension] = category
        self._save_ml_overrides(overrides)

    def exclude_ml_folder(self, folder: str) -> None:
        candidate = Path(folder).expanduser().resolve()
        if not candidate.is_dir():
            raise ServiceError(ErrorCode.NOT_FOUND, "Choose an available folder to exclude from automatic ML selection.")
        overrides = self.get_ml_overrides()
        folders = {str(Path(value).expanduser().resolve()) for value in overrides["excluded_folders"]}
        folders.add(str(candidate))
        overrides["excluded_folders"] = sorted(folders)
        self._save_ml_overrides(overrides)

    def relabel_plan_item(self, settings: OrganizerSettings, item: OrganizationPlanItem, category: str) -> OrganizationPlanItem:
        """Create a collision-aware manual correction for the current reviewed plan."""
        if category not in _ML_CATEGORIES:
            raise ServiceError(ErrorCode.VALIDATION, "Choose a supported local ML category.")
        source = Path(item.source_path)
        destination = Path(settings.destination_root).expanduser().resolve() / category / source.name
        try:
            if destination.exists() and destination.is_file() and self._same_content(source, destination, item.size):
                return replace(item, destination_path="", category=category, confidence=1.0, selected=False,
                               collision="duplicate", reason="Manual label matches an identical destination file; left for duplicate review.")
        except OSError:
            pass
        target, collision = self._resolve_target(destination, set(), settings.conflict_policy)
        return replace(item, destination_path=str(target) if target else "", category=category, confidence=1.0,
                       selected=bool(target), collision=collision, reason=f"Manual local label: {category}.")

    def discover_folders(self, source_root: str) -> list[str]:
        root = Path(source_root).expanduser()
        if not root.is_dir():
            return []
        return sorted(str(path) for path in root.iterdir() if path.is_dir() and not self._excluded(path))

    def build_plan(
        self,
        settings: OrganizerSettings,
        cancellation: CancellationToken | None = None,
        reporter: OperationReporter | None = None,
    ) -> tuple[OrganizationPlanItem, ...]:
        source = Path(settings.source_root).expanduser().resolve()
        destination = Path(settings.destination_root).expanduser().resolve()
        self._validate(settings, source, destination)
        folders = self._selected_roots(settings, source)
        planned_targets: set[Path] = set()
        items: list[OrganizationPlanItem] = []
        ml_overrides = self.get_ml_overrides() if settings.mode == "ml" else {}
        for folder in folders:
            for path in folder.rglob("*"):
                if cancellation:
                    cancellation.raise_if_cancelled()
                if not path.is_file() or self._excluded(path) or (destination != source and _is_within(path, destination)):
                    continue
                try:
                    stat = path.stat()
                except OSError:
                    continue
                category = _category(path)
                confidence = 1.0
                reason = "Flatten selected subfolder."
                if settings.mode == "type":
                    reason = f"File type mapped to {category}."
                    target_root = destination / category
                elif settings.mode == "date":
                    capture_time = _embedded_capture_time(path)
                    stamp = (capture_time or datetime.fromtimestamp(stat.st_mtime, UTC)).strftime("%Y/%m")
                    reason = f"{'Embedded capture time' if capture_time else 'Filesystem timestamp'} mapped to {stamp}."
                    target_root = destination / category / stamp
                elif settings.mode == "ml":
                    category, confidence, reason = _ml_suggestion(path, ml_overrides)
                    target_root = destination / category
                else:
                    target_root = destination
                if settings.mode == "flatten":
                    target_root = destination
                requested_target = target_root / path.name
                if requested_target.exists() and requested_target.is_file() and self._same_content(path, requested_target, stat.st_size):
                    items.append(
                        OrganizationPlanItem(
                            source_path=str(path), destination_path="", size=stat.st_size, modified=stat.st_mtime,
                            category=category, reason="Identical destination file detected; left for duplicate review.",
                            confidence=confidence, selected=False, collision="duplicate",
                        )
                    )
                    continue
                target, collision = self._resolve_target(requested_target, planned_targets, settings.conflict_policy)
                selected = not collision or collision != "skip"
                if settings.mode == "ml" and settings.ml_auto_organize:
                    selected = confidence >= settings.ml_confidence_threshold and collision != "skip"
                items.append(
                    OrganizationPlanItem(
                        source_path=str(path), destination_path=str(target) if target else "", size=stat.st_size,
                        modified=stat.st_mtime, category=category, reason=reason, confidence=confidence,
                        selected=selected, collision=collision,
                    )
                )
                if target:
                    planned_targets.add(target)
                if reporter and len(items) % 50 == 0:
                    reporter.progress_callback(len(items), 0, f"Planning organization: {len(items)} files")
        return tuple(items)

    def organize(
        self,
        settings: OrganizerSettings,
        selected_sources: Iterable[str] | None = None,
        cancellation: CancellationToken | None = None,
        reporter: OperationReporter | None = None,
        *,
        reviewed_plan: Iterable[OrganizationPlanItem] | None = None,
    ) -> OperationResult:
        cancellation = cancellation or CancellationToken()
        reporter = reporter or OperationReporter()
        reporter.set_state(OperationState.VALIDATING, phase=OperationPhase.VALIDATION, message="Validating organization setup…")
        # A live run must use the mapping the person reviewed.  Rebuilding the
        # plan here could silently choose a different collision name after a
        # destination changes between review and confirmation.
        plan = tuple(reviewed_plan) if reviewed_plan is not None else self.build_plan(settings, cancellation, reporter)
        self._validate_reviewed_plan(settings, plan)
        selected_set = set(selected_sources or [item.source_path for item in plan if item.selected])
        selected = [item for item in plan if item.source_path in selected_set and item.destination_path]
        operation_id = _operation_id()
        operation_root = self.paths.organization / operation_id
        manifest_path = operation_root / "manifest.json"
        records: list[dict] = []
        skipped: list[str] = [item.source_path for item in plan if item.source_path not in selected_set or not item.destination_path]
        failures: list[str] = []
        if settings.dry_run:
            self._write_manifest(manifest_path, operation_id, settings, records, skipped, failures, dry_run=True, plan=plan)
            return self._result(operation_id, manifest_path, (), skipped, failures, (), True, plan)
        reporter.set_state(OperationState.TRANSFERRING, phase=OperationPhase.TRANSFER, message="Moving reviewed files…")
        for index, item in enumerate(selected, 1):
            if cancellation.is_cancelled():
                self._write_manifest(manifest_path, operation_id, settings, records, skipped, failures, dry_run=False, plan=plan, status="cancelled")
                return self._result(operation_id, manifest_path, tuple(record["destination_path"] for record in records), skipped, failures, (), False, plan, OperationState.CANCELLED)
            source = Path(item.source_path)
            target = Path(item.destination_path)
            replaced_backup = ""
            try:
                if not source.exists():
                    raise FileNotFoundError(source)
                current = source.stat()
                if current.st_size != item.size or current.st_mtime != item.modified:
                    raise OSError("Source file changed after the reviewed plan was created. Review again before moving it.")
                if target.exists() and item.collision != "replace":
                    raise OSError("Destination changed after review. Review again before moving this file.")
                target.parent.mkdir(parents=True, exist_ok=True)
                if target.exists():
                    # Replace is an Advanced choice, but remains recoverable:
                    # store the displaced destination inside this operation
                    # before moving the reviewed source into its place.
                    backup = operation_root / "replaced_destinations" / f"{len(records):06d}_{target.name}"
                    backup.parent.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(target), str(backup))
                    replaced_backup = str(backup)
                shutil.move(str(source), str(target))
                records.append({
                    **asdict(item), "destination_path": str(target), "moved_at": _now(), "restored_at": "",
                    "source_fingerprint": self._fingerprint(target), "replaced_destination_backup": replaced_backup,
                })
                self._write_manifest(manifest_path, operation_id, settings, records, skipped, failures, dry_run=False, plan=plan)
            except OSError as exc:
                # If replacing failed after moving the existing destination
                # aside, put it back before continuing with the other files.
                if replaced_backup and Path(replaced_backup).exists() and not target.exists():
                    try:
                        shutil.move(replaced_backup, str(target))
                    except OSError:
                        pass
                failures.append(f"{source}: {exc}")
            reporter.progress_callback(index, len(selected), f"Organizing {source.name}", phase=OperationPhase.TRANSFER)
        cleaned = self._cleanup_empty(settings, self._selected_roots(settings, Path(settings.source_root).resolve()))
        self._write_manifest(manifest_path, operation_id, settings, records, skipped, failures, dry_run=False, plan=plan)
        self._update_catalog(records, operation_id)
        return self._result(operation_id, manifest_path, tuple(record["destination_path"] for record in records), skipped, failures, cleaned, False, plan)

    def rollback(self, operation_id: str, *, conflict_policy: str = "rename", dry_run: bool = False) -> OrganizationResult:
        manifest = self.paths.organization / operation_id / "manifest.json"
        try:
            payload = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ServiceError(ErrorCode.NOT_FOUND, "Organization recovery record is unavailable.", technical_detail=str(exc)) from exc
        restored: list[str] = []
        skipped: list[str] = []
        failures: list[str] = []
        for record in payload.get("records", []):
            if record.get("restored_at"):
                continue
            source = Path(record.get("destination_path", ""))
            target = Path(record.get("source_path", ""))
            try:
                if not source.exists():
                    raise FileNotFoundError(source)
                if source.stat().st_size != int(record.get("size", -1)):
                    raise OSError("Organized file size changed after the operation.")
                fingerprint = str(record.get("source_fingerprint", ""))
                if fingerprint and self._fingerprint(source) != fingerprint:
                    raise OSError("Organized file content changed after the operation.")
                resolved, collision = self._resolve_target(target, set(), conflict_policy)
                if not resolved:
                    skipped.append(str(target))
                    continue
                if not dry_run:
                    resolved.parent.mkdir(parents=True, exist_ok=True)
                    if conflict_policy == "replace" and resolved.exists():
                        resolved.unlink()
                    shutil.move(str(source), str(resolved))
                    backup_text = str(record.get("replaced_destination_backup", ""))
                    backup = Path(backup_text) if backup_text else None
                    if backup and backup.exists():
                        # The destination is now free because the reviewed
                        # source has returned to its original location.
                        shutil.move(str(backup), str(Path(record["destination_path"])))
                    record["restored_at"] = _now()
                    record["restored_path"] = str(resolved)
                restored.append(str(resolved))
            except OSError as exc:
                failures.append(f"{target}: {exc}")
        if not dry_run:
            manifest.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        return OrganizationResult(operation_id, str(manifest), tuple(restored), tuple(skipped), tuple(failures), (), dry_run)

    def prune_manifests(self, retention_days: int = 90) -> int:
        cutoff = datetime.now(UTC) - timedelta(days=max(1, retention_days))
        removed = 0
        for manifest in self.paths.organization.glob("*/manifest.json"):
            try:
                modified = datetime.fromtimestamp(manifest.stat().st_mtime, UTC)
                if modified < cutoff:
                    shutil.rmtree(manifest.parent)
                    removed += 1
            except OSError:
                continue
        return removed

    def list_operations(self) -> list[dict]:
        operations: list[dict] = []
        for manifest in sorted(self.paths.organization.glob("*/manifest.json"), reverse=True):
            try:
                operations.append(json.loads(manifest.read_text(encoding="utf-8")))
            except (OSError, json.JSONDecodeError):
                continue
        return operations

    def search_catalog(self, query: str, *, limit: int = 50) -> list[dict]:
        """Search the opt-in-by-use local organization catalog; no data leaves disk."""
        try:
            entries = json.loads((self.paths.cache / "organization_catalog.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
        needle = query.lower().strip()
        if not needle:
            return []
        return [
            entry for entry in entries
            if needle in " ".join(str(entry.get(key, "")) for key in ("destination_path", "category", "operation_id")).lower()
        ][:max(1, limit)]

    def _validate(self, settings: OrganizerSettings, source: Path, destination: Path) -> None:
        if settings.mode not in {"flatten", "type", "date", "ml"}:
            raise ServiceError(ErrorCode.VALIDATION, "Choose a supported organization mode.")
        if settings.conflict_policy not in {"rename", "skip", "replace"}:
            raise ServiceError(ErrorCode.VALIDATION, "Choose rename, skip, or replace for file conflicts.")
        if not source.is_dir():
            raise ServiceError(ErrorCode.NOT_FOUND, "Choose an available source folder.")
        if not destination.is_dir():
            raise ServiceError(ErrorCode.NOT_FOUND, "Choose an available destination folder.")
        if source == destination and settings.mode != "flatten":
            raise ServiceError(ErrorCode.VALIDATION, "Type, date, and ML organization need a separate destination folder.")

    def _validate_reviewed_plan(self, settings: OrganizerSettings, plan: Iterable[OrganizationPlanItem]) -> None:
        """Reject stale or externally supplied mappings before any file move."""
        source = Path(settings.source_root).expanduser().resolve()
        destination = Path(settings.destination_root).expanduser().resolve()
        self._validate(settings, source, destination)
        selected_roots = self._selected_roots(settings, source)
        seen_sources: set[Path] = set()
        seen_destinations: set[Path] = set()
        for item in plan:
            source_path = Path(item.source_path).expanduser().resolve()
            if source_path in seen_sources:
                raise ServiceError(ErrorCode.VALIDATION, "The reviewed organization plan contains a file more than once.")
            seen_sources.add(source_path)
            if not _is_within(source_path, source) or not any(_is_within(source_path, root) for root in selected_roots):
                raise ServiceError(ErrorCode.VALIDATION, "The reviewed plan includes a file outside the selected source folders.")
            if not item.destination_path:
                continue
            destination_path = Path(item.destination_path).expanduser().resolve()
            if not _is_within(destination_path, destination):
                raise ServiceError(ErrorCode.VALIDATION, "The reviewed plan includes a destination outside the selected destination folder.")
            if destination_path in seen_destinations:
                raise ServiceError(ErrorCode.VALIDATION, "The reviewed plan maps multiple files to the same destination.")
            seen_destinations.add(destination_path)

    def _selected_roots(self, settings: OrganizerSettings, source: Path) -> list[Path]:
        values = [Path(value).expanduser().resolve() for value in settings.selected_folders]
        if not values:
            values = [Path(value).expanduser().resolve() for value in self.discover_folders(str(source))]
        roots: list[Path] = []
        for folder in values:
            if not folder.is_dir() or not _is_within(folder, source):
                continue
            if not any(_is_within(folder, existing) for existing in roots):
                roots.append(folder)
        return roots

    def _excluded(self, path: Path) -> bool:
        return _is_within(path, self.paths.root) or path.name.lower() in {"$recycle.bin", "system volume information", ".git", ".venv"}

    @staticmethod
    def _resolve_target(target: Path, planned: set[Path], policy: str) -> tuple[Path | None, str]:
        if not target.exists() and target not in planned:
            return target, ""
        if policy == "skip":
            return None, "skip"
        if policy == "replace" and target not in planned:
            return target, "replace"
        stem, suffix = target.stem, target.suffix
        index = 1
        candidate = target
        while candidate.exists() or candidate in planned:
            candidate = target.with_name(f"{stem} ({index}){suffix}")
            index += 1
        return candidate, "rename"

    @staticmethod
    def _same_content(source: Path, target: Path, size: int) -> bool:
        try:
            if target.stat().st_size != size:
                return False
            source_hash = hashlib.sha256()
            target_hash = hashlib.sha256()
            with source.open("rb") as source_stream, target.open("rb") as target_stream:
                while True:
                    source_chunk = source_stream.read(1024 * 1024)
                    target_chunk = target_stream.read(1024 * 1024)
                    if source_chunk != target_chunk:
                        return False
                    if not source_chunk:
                        return True
                    source_hash.update(source_chunk)
                    target_hash.update(target_chunk)
            return source_hash.digest() == target_hash.digest()
        except OSError:
            return False

    @staticmethod
    def _fingerprint(path: Path) -> str:
        """Return the manifest fingerprint used to prevent unsafe rollback."""
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    @staticmethod
    def _cleanup_empty(settings: OrganizerSettings, roots: list[Path]) -> tuple[str, ...]:
        if not settings.cleanup_empty_folders:
            return ()
        removed: list[str] = []
        source_root = Path(settings.source_root).expanduser().resolve()
        for root in sorted(roots, key=lambda path: len(path.parts), reverse=True):
            folders = [root, *(path for path in root.rglob("*") if path.is_dir())]
            for folder in sorted(folders, key=lambda path: len(path.parts), reverse=True):
                if folder.resolve() == source_root:
                    continue
                try:
                    folder.rmdir()
                    removed.append(str(folder))
                except OSError:
                    continue
        return tuple(removed)

    @staticmethod
    def _write_manifest(manifest: Path, operation_id: str, settings: OrganizerSettings, records: list[dict], skipped: list[str], failures: list[str], *, dry_run: bool, plan: Iterable[OrganizationPlanItem], status: str = "completed") -> None:
        manifest.parent.mkdir(parents=True, exist_ok=True)
        temporary = manifest.with_suffix(".tmp")
        payload = {
            "operation_id": operation_id, "created_at": _now(), "status": status,
            "dry_run": dry_run, "settings": asdict(settings), "records": records,
            "skipped": skipped, "failures": failures, "plan": [asdict(item) for item in plan],
            "safety": "Files can be restored through Duplicate & Transfer Manager for the configured retention period.",
        }
        temporary.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        temporary.replace(manifest)

    def _update_catalog(self, records: list[dict], operation_id: str) -> None:
        path = self.paths.cache / "organization_catalog.json"
        try:
            entries = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(entries, list):
                entries = []
        except (OSError, json.JSONDecodeError):
            entries = []
        for record in records:
            destination = str(record.get("destination_path", ""))
            if not destination:
                continue
            entries = [entry for entry in entries if entry.get("destination_path") != destination]
            entries.append({
                "destination_path": destination,
                "category": record.get("category", ""),
                "modified": record.get("modified", 0),
                "size": record.get("size", 0),
                "operation_id": operation_id,
            })
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(entries, indent=2), encoding="utf-8")
        temporary.replace(path)

    def _save_ml_overrides(self, overrides: dict) -> None:
        self._overrides_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self._overrides_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(overrides, indent=2), encoding="utf-8")
        temporary.replace(self._overrides_path)

    @staticmethod
    def _result(operation_id: str, manifest: Path, moved: tuple[str, ...], skipped: list[str], failures: list[str], cleaned: tuple[str, ...], dry_run: bool, plan: Iterable[OrganizationPlanItem], state: OperationState = OperationState.COMPLETED) -> OperationResult:
        return OperationResult(
            status=state,
            counts={"moved": len(moved), "skipped": len(skipped), "errors": len(failures), "planned": len(tuple(plan))},
            warnings=tuple(failures),
            report_path=str(manifest),
            resume_information={"rollback_available": bool(moved) and not dry_run},
            data={"organization": OrganizationResult(operation_id, str(manifest), moved, tuple(skipped), tuple(failures), cleaned, dry_run)},
        )
