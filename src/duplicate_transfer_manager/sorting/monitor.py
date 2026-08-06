"""Local monitored-folder snapshots for scheduled and change-driven scans."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from ..core import ErrorCode, ServiceError
from ..runtime_paths import RuntimePaths, get_runtime_paths
from .metadata import MetadataExtractor
from .models import FileMetadata, MonitoredFolder


class SortMonitorService:
    """Detect new/changed files without requiring a platform-specific watcher.

    The UI may poll this service for filesystem-change mode; Task Scheduler may
    call it for scheduled mode. Snapshots are local and contain path/size/mtime.
    """

    def __init__(self, paths: RuntimePaths | None = None, extractor: MetadataExtractor | None = None) -> None:
        self.paths = paths or get_runtime_paths()
        self.extractor = extractor or MetadataExtractor()
        self.snapshot_root = self.paths.sorting / "monitor_snapshots"

    def poll(self, folder: MonitoredFolder) -> tuple[FileMetadata, ...]:
        self.validate(folder)
        root = Path(folder.path).expanduser().resolve()
        current: dict[str, dict[str, float | int]] = {}
        changed: list[FileMetadata] = []
        previous = self._load(folder.id)
        candidates = root.rglob("*") if folder.recursive else root.glob("*")
        for path in candidates:
            try:
                if not path.is_file():
                    continue
                stat = path.stat()
                resolved = str(path.resolve())
                current[resolved] = {"size": stat.st_size, "modified": stat.st_mtime}
                old = previous.get(resolved)
                if old is None or int(old.get("size", -1)) != stat.st_size or float(old.get("modified", -1)) != stat.st_mtime:
                    changed.append(self.extractor.extract(path))
            except OSError:
                continue
        self._save(folder.id, current)
        return tuple(changed)

    def reset(self, monitor_id: str) -> bool:
        path = self._path(monitor_id)
        try:
            path.unlink()
            return True
        except FileNotFoundError:
            return False

    @staticmethod
    def validate(folder: MonitoredFolder) -> None:
        if not folder.enabled:
            raise ServiceError(ErrorCode.VALIDATION, "Enable the monitored folder before scanning it.")
        if folder.scan_mode not in {"scheduled", "filesystem_change"}:
            raise ServiceError(ErrorCode.VALIDATION, "Choose scheduled or filesystem-change monitoring.")
        if folder.schedule not in {"off", "hourly", "daily", "weekly"}:
            raise ServiceError(ErrorCode.VALIDATION, "Choose off, hourly, daily, or weekly monitoring.")
        if not Path(folder.path).expanduser().is_dir():
            raise ServiceError(ErrorCode.NOT_FOUND, "The monitored folder is unavailable.")
        if not folder.dry_run and not folder.live_approved:
            raise ServiceError(ErrorCode.VALIDATION, "Live monitored sorting requires explicit profile approval.")

    def _path(self, monitor_id: str) -> Path:
        safe = "".join(character for character in monitor_id if character.isalnum() or character in "_-")
        if not safe or safe != monitor_id:
            raise ServiceError(ErrorCode.VALIDATION, "Choose a valid monitored-folder record.")
        return self.snapshot_root / f"{safe}.json"

    def _load(self, monitor_id: str) -> dict[str, dict]:
        try:
            payload = json.loads(self._path(monitor_id).read_text(encoding="utf-8"))
            return payload.get("files", {}) if isinstance(payload, dict) else {}
        except (OSError, json.JSONDecodeError):
            return {}

    def _save(self, monitor_id: str, files: dict) -> None:
        path = self._path(monitor_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        handle, temporary = tempfile.mkstemp(prefix=".monitor.", suffix=".tmp", dir=path.parent)
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as stream:
                json.dump({"schema_version": 1, "files": files}, stream, indent=2)
            os.replace(temporary, path)
        except Exception:
            try:
                os.remove(temporary)
            except OSError:
                pass
            raise
