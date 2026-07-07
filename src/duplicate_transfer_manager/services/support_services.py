"""Small framework-neutral services used by non-operation controllers."""

from __future__ import annotations

import json
import os
import platform
import shutil
import tempfile
from dataclasses import asdict
from datetime import datetime, timezone
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

    def export_report(
        self,
        path: str | os.PathLike[str],
        destination: str | os.PathLike[str],
    ) -> str:
        payload = self.load_report(path)
        target = Path(destination)
        if target.is_dir():
            target = target / Path(path).name
        target.parent.mkdir(parents=True, exist_ok=True)
        _atomic_json_write(target, payload)
        return str(target)

    def remove_report(self, path: str | os.PathLike[str]) -> bool:
        candidate = Path(path).resolve()
        report_root = self.paths.reports.resolve()
        if candidate.parent != report_root:
            raise ValueError("Report path is outside the application report folder.")
        candidate.unlink(missing_ok=True)
        return True


class QuarantineService:
    def __init__(self, paths: RuntimePaths | None = None) -> None:
        self.paths = paths or get_runtime_paths()

    def list_records(self) -> list[QuarantineRecord]:
        records: list[QuarantineRecord] = []
        manifests = list(self.paths.quarantine.glob("*.json"))
        manifests.extend(self.paths.quarantine.glob("*/manifest.json"))
        for path in sorted(manifests):
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
        bundled_adb = self.paths.root / "platform-tools" / ("adb.exe" if os.name == "nt" else "adb")
        manifest = Path(__file__).resolve().parents[3] / "packaging" / "android_platform_tools_manifest.json"
        manifest_payload: dict[str, Any] = {}
        try:
            manifest_payload = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            pass
        bundled_version = "Not bundled"
        pinned_version = str(manifest_payload.get("version", "Unknown"))
        if bundled_adb.exists():
            bundled_version = pinned_version
        return {
            "application_version": __version__,
            "python_version": platform.python_version(),
            "platform": platform.platform(),
            "android_platform_tools": {
                "bundled_adb": str(bundled_adb),
                "bundled": bundled_adb.exists(),
                "version": bundled_version,
                "pinned_version": pinned_version,
                "license": manifest_payload.get("license", ""),
                "system_path_policy": manifest_payload.get(
                    "system_path_policy",
                    "Never modify system-wide ADB installations or environment variables.",
                ),
                "system_adb": shutil.which("adb") or "",
                "system_path_modified": False,
            },
            "runtime_paths": {
                key: str(value) for key, value in asdict(self.paths).items()
            },
            "devices": ADBBridge.list_devices() if include_devices else [],
        }


class OperationRecordService:
    def __init__(self, paths: RuntimePaths | None = None) -> None:
        self.paths = paths or get_runtime_paths()

    def record(
        self,
        operation_type: str,
        status: str,
        *,
        title: str = "",
        counts: dict[str, int] | None = None,
        summary: dict[str, Any] | None = None,
        report_path: str = "",
        resume_available: bool = False,
        warnings: list[str] | None = None,
        failures: list[str] | None = None,
    ) -> dict[str, Any]:
        now = datetime.now(timezone.utc).isoformat()
        operation_id = f"{operation_type}_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S_%f')}"
        payload = {
            "id": operation_id,
            "type": operation_type,
            "title": title or operation_type.replace("_", " ").title(),
            "status": status,
            "created_at": now,
            "updated_at": now,
            "counts": counts or {},
            "summary": summary or {},
            "report_path": report_path,
            "resume_available": resume_available,
            "warnings": warnings or [],
            "failures": failures or [],
        }
        _atomic_json_write(self.paths.operations / f"{operation_id}.json", payload)
        return payload

    def list_records(self) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        for path in sorted(self.paths.operations.glob("*.json"), reverse=True):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if isinstance(payload, dict):
                payload["path"] = str(path)
                records.append(payload)
        records.extend(self._records_from_reports())
        return sorted(records, key=lambda item: item.get("created_at", ""), reverse=True)

    def remove_record(self, record_id: str) -> bool:
        target = self.paths.operations / f"{record_id}.json"
        target.unlink(missing_ok=True)
        return True

    def _records_from_reports(self) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        for report in sorted(self.paths.reports.glob("*.json"), reverse=True):
            try:
                payload = json.loads(report.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            created = payload.get("created_at", "")
            records.append(
                {
                    "id": f"report_{report.stem}",
                    "type": "import",
                    "title": "Import report",
                    "status": "completed" if int(payload.get("errors", 0)) == 0 else "warning",
                    "created_at": created,
                    "updated_at": created,
                    "counts": {
                        "transferred": int(payload.get("transferred", 0)),
                        "duplicates": int(payload.get("duplicates", 0)),
                        "errors": int(payload.get("errors", 0)),
                        "resumed": int(payload.get("resumed", 0)),
                    },
                    "summary": {"source": "transfer report"},
                    "report_path": str(report),
                    "resume_available": False,
                    "warnings": [],
                    "failures": payload.get("failures", []),
                    "path": str(report),
                }
            )
        return records


class DashboardService:
    def __init__(self, paths: RuntimePaths | None = None) -> None:
        self.paths = paths or get_runtime_paths()
        self.operations = OperationRecordService(self.paths)
        self.quarantine = QuarantineService(self.paths)
        self.devices = DeviceService()

    def summary(self) -> dict[str, Any]:
        records = self.operations.list_records()
        interrupted = [
            record for record in records
            if record.get("status") in {"cancelled", "failed", "warning"} or record.get("resume_available")
        ]
        quarantine_records = [
            record for record in self.quarantine.list_records()
            if not record.restored_at
        ]
        storage = {
            "cache_bytes": _directory_size(self.paths.cache),
            "reports_bytes": _directory_size(self.paths.reports),
            "quarantine_bytes": sum(record.size for record in quarantine_records),
        }
        try:
            connected_devices = self.devices.list_devices()
        except Exception:
            connected_devices = []
        return {
            "connected_devices": connected_devices,
            "recent_operations": records[:5],
            "interrupted_transfers": interrupted[:5],
            "quarantine_count": len(quarantine_records),
            "recoverable_bytes": storage["quarantine_bytes"],
            "storage": storage,
            "runtime_root": str(self.paths.root),
        }

    def clear_cache(self) -> int:
        removed = 0
        for path in self.paths.cache.rglob("*"):
            if path.is_file():
                try:
                    removed += path.stat().st_size
                    path.unlink()
                except OSError:
                    pass
        return removed


def _directory_size(path: Path) -> int:
    total = 0
    if not path.exists():
        return total
    for item in path.rglob("*"):
        if item.is_file():
            try:
                total += item.stat().st_size
            except OSError:
                pass
    return total


class UpdateService:
    """Local update status contract; network checking arrives in Phase 7."""

    def status(self) -> dict[str, Any]:
        return {
            "current_version": __version__,
            "channel": "stable",
            "configured": False,
            "message": "Signed automatic updates are scheduled for Phase 7.",
        }
