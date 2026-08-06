"""Runtime data locations and non-destructive legacy-data migration."""

from __future__ import annotations

import json
import os
import shutil
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path


APP_DIRECTORY_NAME = "DuplicateTransferManager"
MIGRATION_MARKER_NAME = ".legacy-migration-v1.json"


@dataclass(frozen=True)
class RuntimePaths:
    """All writable locations owned by the application."""

    root: Path
    cache: Path
    drive_caches: Path
    reports: Path
    journals: Path
    quarantine: Path
    logs: Path
    updates: Path
    operations: Path
    organization: Path
    sorting: Path
    hash_cache: Path
    settings_file: Path
    migration_marker: Path

    def create(self) -> "RuntimePaths":
        for directory in (
            self.root,
            self.cache,
            self.drive_caches,
            self.reports,
            self.journals,
            self.quarantine,
            self.logs,
            self.updates,
            self.operations,
            self.organization,
            self.sorting,
        ):
            directory.mkdir(parents=True, exist_ok=True)
        return self


@dataclass(frozen=True)
class MigrationResult:
    """Summary of a legacy-data migration attempt."""

    copied: tuple[str, ...] = ()
    skipped: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()
    already_completed: bool = False


def _default_data_root(platform_name: str | None = None) -> Path:
    override = os.environ.get("DTM_DATA_DIR", "").strip()
    if override:
        return Path(override).expanduser()

    if (platform_name or os.name) == "nt":
        local_app_data = os.environ.get("LOCALAPPDATA", "").strip()
        base = Path(local_app_data) if local_app_data else Path.home() / "AppData" / "Local"
    else:
        xdg_data_home = os.environ.get("XDG_DATA_HOME", "").strip()
        base = Path(xdg_data_home).expanduser() if xdg_data_home else Path.home() / ".local" / "share"
    return base / APP_DIRECTORY_NAME


def get_runtime_paths(root: str | os.PathLike[str] | None = None, *, create: bool = True) -> RuntimePaths:
    """Return application-owned paths, optionally creating their directories."""

    data_root = Path(root).expanduser() if root is not None else _default_data_root()
    cache = data_root / "cache"
    paths = RuntimePaths(
        root=data_root,
        cache=cache,
        drive_caches=cache / "drive_caches",
        reports=data_root / "reports",
        journals=data_root / "journals",
        quarantine=data_root / "quarantine",
        logs=data_root / "logs",
        updates=data_root / "updates",
        operations=data_root / "operations",
        organization=data_root / "organization",
        sorting=data_root / "sorting",
        hash_cache=cache / "hash_cache.json",
        settings_file=data_root / "settings.json",
        migration_marker=data_root / MIGRATION_MARKER_NAME,
    )
    return paths.create() if create else paths


def _copy_file(source: Path, target: Path, copied: list[str], skipped: list[str], errors: list[str]) -> None:
    if not source.is_file():
        return
    if target.exists():
        skipped.append(str(source))
        return
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        copied.append(str(source))
    except OSError as exc:
        errors.append(f"{source}: {exc}")


def migrate_legacy_data(
    legacy_root: str | os.PathLike[str],
    paths: RuntimePaths | None = None,
) -> MigrationResult:
    """Copy compatible legacy runtime files once, without deleting originals."""

    paths = paths or get_runtime_paths()
    paths.create()
    legacy = Path(legacy_root).resolve()

    if paths.migration_marker.exists():
        return MigrationResult(already_completed=True)

    copied: list[str] = []
    skipped: list[str] = []
    errors: list[str] = []

    _copy_file(legacy / "hash_cache.json", paths.hash_cache, copied, skipped, errors)

    directory_mappings = (
        (legacy / "drive_caches", paths.drive_caches, "*.json"),
        (legacy / "transfer_reports", paths.reports, "*.json"),
        (legacy / "transfer_state", paths.journals, "*.json"),
    )
    for source_dir, target_dir, pattern in directory_mappings:
        if not source_dir.is_dir():
            continue
        for source in source_dir.glob(pattern):
            _copy_file(source, target_dir / source.name, copied, skipped, errors)

    for source in legacy.glob("media_organizer_log_*.txt"):
        _copy_file(source, paths.logs / source.name, copied, skipped, errors)

    if not errors:
        marker_payload = {
            "version": 1,
            "legacy_root": str(legacy),
            "completed_at": datetime.now(timezone.utc).isoformat(),
            "copied": copied,
            "skipped": skipped,
            "errors": errors,
            "paths": {key: str(value) for key, value in asdict(paths).items()},
        }
        temporary_marker = paths.migration_marker.with_suffix(".tmp")
        try:
            temporary_marker.write_text(json.dumps(marker_payload, indent=2), encoding="utf-8")
            temporary_marker.replace(paths.migration_marker)
        except OSError as exc:
            errors.append(f"{paths.migration_marker}: {exc}")

    return MigrationResult(tuple(copied), tuple(skipped), tuple(errors))


def initialize_runtime_data(
    legacy_root: str | os.PathLike[str],
    root: str | os.PathLike[str] | None = None,
) -> tuple[RuntimePaths, MigrationResult]:
    """Create runtime directories and perform the one-time legacy import."""

    paths = get_runtime_paths(root)
    return paths, migrate_legacy_data(legacy_root, paths)
