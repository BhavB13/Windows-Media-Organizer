"""Small framework-neutral services used by non-operation controllers."""

from __future__ import annotations

import json
import os
import platform
import tempfile
from dataclasses import asdict
from pathlib import Path
from typing import Any

from adb_bridge import ADBBridge

from ..core import AppSettings, QuarantineRecord
from ..runtime_paths import RuntimePaths, get_runtime_paths
from ..version import __version__


def _atomic_json_write(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, indent=2)
        os.replace(temporary_name, path)
    except Exception:
        try:
            os.remove(temporary_name)
        except OSError:
            pass
        raise


class SettingsService:
    def __init__(self, paths: RuntimePaths | None = None) -> None:
        self.paths = paths or get_runtime_paths()

    def load(self) -> AppSettings:
        try:
            values = json.loads(self.paths.settings_file.read_text(encoding="utf-8"))
            if not isinstance(values, dict):
                return AppSettings()
            return AppSettings.from_dict(values)
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            return AppSettings()

    def save(self, settings: AppSettings) -> AppSettings:
        _atomic_json_write(self.paths.settings_file, settings.to_dict())
        return settings


class DeviceService:
    def list_devices(self) -> list[dict[str, Any]]:
        return ADBBridge.list_devices()

    def inspect(self, serial: str) -> dict[str, Any]:
        ready, detail = ADBBridge.probe_device(serial)
        used, total, usage = ADBBridge.get_storage_info(serial=serial)
        return {
            "serial": serial,
            "ready": ready,
            "detail": detail,
            "state": ADBBridge.get_device_state(serial),
            "info": ADBBridge.get_device_info(serial=serial),
            "storage": {"used": used, "total": total, "usage": usage},
        }


class ReportService:
    def __init__(self, paths: RuntimePaths | None = None) -> None:
        self.paths = paths or get_runtime_paths()

    def list_reports(self) -> list[dict[str, Any]]:
        reports: list[dict[str, Any]] = []
        for path in sorted(self.paths.reports.glob("*.json"), reverse=True):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                payload = {}
            reports.append(
                {
                    "path": str(path),
                    "created_at": payload.get("created_at", ""),
                    "transferred": int(payload.get("transferred", 0)),
                    "duplicates": int(payload.get("duplicates", 0)),
                    "errors": int(payload.get("errors", 0)),
                    "valid": bool(payload),
                }
            )
        return reports

    def load_report(self, path: str | os.PathLike[str]) -> dict[str, Any]:
        candidate = Path(path).resolve()
        report_root = self.paths.reports.resolve()
        if candidate.parent != report_root:
            raise ValueError("Report path is outside the application report folder.")
        return json.loads(candidate.read_text(encoding="utf-8"))


class QuarantineService:
    def __init__(self, paths: RuntimePaths | None = None) -> None:
        self.paths = paths or get_runtime_paths()

    def list_records(self) -> list[QuarantineRecord]:
        records: list[QuarantineRecord] = []
        for path in sorted(self.paths.quarantine.glob("*.json")):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            values = (
                payload
                if isinstance(payload, list)
                else payload.get("records", [])
                if isinstance(payload, dict)
                else []
            )
            if isinstance(values, list):
                records.extend(
                    QuarantineRecord.from_dict(value)
                    for value in values
                    if isinstance(value, dict)
                )
        return records


class DiagnosticsService:
    def __init__(self, paths: RuntimePaths | None = None) -> None:
        self.paths = paths or get_runtime_paths()

    def collect(self, include_devices: bool = True) -> dict[str, Any]:
        return {
            "application_version": __version__,
            "python_version": platform.python_version(),
            "platform": platform.platform(),
            "runtime_paths": {
                key: str(value) for key, value in asdict(self.paths).items()
            },
            "devices": ADBBridge.list_devices() if include_devices else [],
        }


class UpdateService:
    """Local update status contract; network checking arrives in Phase 7."""

    def status(self) -> dict[str, Any]:
        return {
            "current_version": __version__,
            "channel": "stable",
            "configured": False,
            "message": "Signed automatic updates are scheduled for Phase 7.",
        }
