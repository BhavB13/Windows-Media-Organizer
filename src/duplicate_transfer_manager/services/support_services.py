"""Small framework-neutral services used by non-operation controllers."""

from __future__ import annotations

import json
import os
import platform
import hashlib
import csv
import shutil
import subprocess
import sys
import tempfile
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.error import URLError
from urllib.parse import urlsplit
from urllib.request import urlopen

from packaging.version import InvalidVersion, Version

from adb_bridge import ADBBridge

from ..core import (
    AppSettings,
    ErrorCode,
    QuarantineRecord,
    ServiceError,
    sanitize_payload,
    verify_rsa_sha256_signature,
)
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
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_name, path)
    except Exception:
        try:
            os.remove(temporary_name)
        except OSError:
            pass
        raise


def _atomic_csv_write(path: Path, rows: list[dict[str, Any]]) -> None:
    """Write a portable activity export without exposing local file paths by default."""
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
        text=True,
    )
    fields = ("id", "type", "status", "created_at", "title", "counts", "warnings", "failures", "resume_available")
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=fields)
            writer.writeheader()
            for record in rows:
                writer.writerow(
                    {
                        "id": record.get("id", ""),
                        "type": record.get("type", ""),
                        "status": record.get("status", ""),
                        "created_at": record.get("created_at", ""),
                        "title": record.get("title", ""),
                        "counts": "; ".join(
                            f"{key}: {value}" for key, value in record.get("counts", {}).items()
                        ),
                        "warnings": "; ".join(str(value) for value in record.get("warnings", [])),
                        "failures": "; ".join(str(value) for value in record.get("failures", [])),
                        "resume_available": bool(record.get("resume_available", False)),
                    }
                )
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


class ScheduledScanService:
    """Manage an explicitly configured, read-only Windows scheduled scan."""

    task_name = "DuplicateTransferManager-ReadOnlyDuplicateScan"
    organizer_task_name = "DuplicateTransferManager-OrganizationPreview"

    def command(self, source: str, *, data_root: str = "") -> str:
        executable = shutil.which("dtm-scheduled-scan")
        parts = [executable] if executable else [sys.executable, "-m", "duplicate_transfer_manager.scheduled_scan"]
        parts.extend(["--source", source])
        if data_root:
            parts.extend(["--data-root", data_root])
        return subprocess.list2cmdline(parts)

    def configure(self, source: str, frequency: str, *, data_root: str = "") -> None:
        normalized = frequency.lower().strip()
        if normalized not in {"off", "daily", "weekly"}:
            raise ServiceError(ErrorCode.VALIDATION, "Choose daily, weekly, or off for scheduled scans.")
        if os.name != "nt":
            raise ServiceError(ErrorCode.VALIDATION, "Scheduled scans are available on Windows only.")
        if normalized == "off":
            subprocess.run(
                ["schtasks", "/Delete", "/F", "/TN", self.task_name],
                capture_output=True,
                text=True,
                check=False,
            )
            return
        if not source.strip() or not Path(source).expanduser().is_dir():
            raise ServiceError(ErrorCode.VALIDATION, "Choose an available local folder for scheduled scans.")
        completed = subprocess.run(
            [
                "schtasks", "/Create", "/F", "/TN", self.task_name,
                "/SC", normalized.upper(),
                "/TR", self.command(str(Path(source).expanduser()), data_root=data_root),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        if completed.returncode != 0:
            raise ServiceError(
                ErrorCode.IO_ERROR,
                "Windows could not schedule the duplicate scan.",
                technical_detail=(completed.stderr or completed.stdout or "schtasks failed").strip(),
            )

    def configure_organizer_preview(self, source: str, destination: str, mode: str, frequency: str, *, data_root: str = "") -> None:
        normalized = frequency.lower().strip()
        if normalized not in {"off", "daily", "weekly"}:
            raise ServiceError(ErrorCode.VALIDATION, "Choose daily, weekly, or off for organizer previews.")
        if os.name != "nt":
            raise ServiceError(ErrorCode.VALIDATION, "Scheduled organizer previews are available on Windows only.")
        if normalized == "off":
            subprocess.run(["schtasks", "/Delete", "/F", "/TN", self.organizer_task_name], capture_output=True, text=True, check=False)
            return
        if not source.strip() or not destination.strip() or not Path(source).expanduser().is_dir() or not Path(destination).expanduser().is_dir():
            raise ServiceError(ErrorCode.VALIDATION, "Choose available source and destination folders for organizer previews.")
        if mode not in {"flatten", "type", "date", "ml"}:
            raise ServiceError(ErrorCode.VALIDATION, "Choose a supported organization mode.")
        executable = shutil.which("dtm-scheduled-organizer")
        parts = [executable] if executable else [sys.executable, "-m", "duplicate_transfer_manager.scheduled_organizer"]
        parts.extend(["--source", str(Path(source).expanduser()), "--destination", str(Path(destination).expanduser()), "--mode", mode])
        if data_root:
            parts.extend(["--data-root", data_root])
        completed = subprocess.run(
            ["schtasks", "/Create", "/F", "/TN", self.organizer_task_name, "/SC", normalized.upper(), "/TR", subprocess.list2cmdline(parts)],
            capture_output=True, text=True, check=False,
        )
        if completed.returncode != 0:
            raise ServiceError(ErrorCode.IO_ERROR, "Windows could not schedule the organization preview.", technical_detail=(completed.stderr or completed.stdout or "schtasks failed").strip())


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
        *,
        dry_run: bool = False,
    ) -> str:
        payload = self.load_report(path)
        target = Path(destination)
        if target.is_dir():
            target = target / Path(path).name
        if dry_run:
            return str(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        _atomic_json_write(target, payload)
        return str(target)

    def remove_report(self, path: str | os.PathLike[str], *, dry_run: bool = False) -> bool:
        candidate = Path(path).resolve()
        report_root = self.paths.reports.resolve()
        if candidate.parent != report_root:
            raise ValueError("Report path is outside the application report folder.")
        if not dry_run:
            candidate.unlink(missing_ok=True)
        return True

    def open_report_location(self, path: str | os.PathLike[str]) -> str:
        candidate = Path(path).resolve()
        report_root = self.paths.reports.resolve()
        if candidate.parent != report_root:
            raise ValueError("Report path is outside the application report folder.")
        if not candidate.exists():
            raise FileNotFoundError(candidate)
        _open_path(candidate)
        return str(candidate)

    def open_reports_folder(self) -> str:
        self.paths.reports.mkdir(parents=True, exist_ok=True)
        _open_path(self.paths.reports)
        return str(self.paths.reports)


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

    def collect(self, include_devices: bool = True, *, sanitized: bool = True) -> dict[str, Any]:
        bundled_adb = self.paths.root / "platform-tools" / ("adb.exe" if os.name == "nt" else "adb")
        manifest = _resource_path("packaging/android_platform_tools_manifest.json")
        manifest_payload: dict[str, Any] = {}
        try:
            manifest_payload = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            pass
        bundled_version = "Not bundled"
        pinned_version = str(manifest_payload.get("version", "Unknown"))
        if bundled_adb.exists():
            bundled_version = pinned_version
        payload = {
            "application_version": __version__,
            "python_version": platform.python_version(),
            "platform": platform.platform(),
            "diagnostics_sanitized": sanitized,
            "sentry": {
                "configured": False,
                "enabled": False,
                "requires_explicit_consent": True,
            },
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
        return sanitize_payload(payload) if sanitized else payload


class CrashReportService:
    def __init__(self, paths: RuntimePaths | None = None) -> None:
        self.paths = paths or get_runtime_paths()

    def create_report(
        self,
        error: BaseException | str,
        *,
        context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        message = str(error)
        payload = {
            "created_at": datetime.now(timezone.utc).isoformat(),
            "application_version": __version__,
            "error_type": type(error).__name__ if isinstance(error, BaseException) else "Error",
            "message": message,
            "context": context or {},
            "sanitized": True,
            "submission": {
                "sentry_enabled": False,
                "requires_user_consent": True,
            },
        }
        sanitized = sanitize_payload(payload)
        report_id = datetime.now(timezone.utc).strftime("crash_%Y%m%d_%H%M%S_%f")
        report_path = self.paths.logs / f"{report_id}.json"
        _atomic_json_write(report_path, sanitized)
        sanitized["path"] = str(report_path)
        return sanitized


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
        self._append_audit_entry(payload)
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

    @property
    def audit_path(self) -> Path:
        return self.paths.logs / "operation_audit.jsonl"

    def list_audit_events(self) -> list[dict[str, Any]]:
        """Return append-only local history without paths, media metadata, or mutable report content."""
        events: list[dict[str, Any]] = []
        try:
            with self.audit_path.open("r", encoding="utf-8") as stream:
                for line in stream:
                    try:
                        payload = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(payload, dict):
                        events.append(payload)
        except OSError:
            pass
        return sorted(events, key=lambda item: item.get("created_at", ""), reverse=True)

    def _append_audit_entry(self, record: dict[str, Any]) -> None:
        entry = {
            "id": record.get("id", ""),
            "type": record.get("type", ""),
            "title": record.get("title", ""),
            "status": record.get("status", ""),
            "created_at": record.get("created_at", ""),
            "counts": record.get("counts", {}),
            "warning_count": len(record.get("warnings", [])),
            "failure_count": len(record.get("failures", [])),
            "resume_available": bool(record.get("resume_available", False)),
        }
        self.audit_path.parent.mkdir(parents=True, exist_ok=True)
        with self.audit_path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(entry, ensure_ascii=False, sort_keys=True) + "\n")
            stream.flush()
            os.fsync(stream.fileno())

    def export_records_csv(
        self,
        records: list[dict[str, Any]],
        destination: str | os.PathLike[str],
        *,
        dry_run: bool = False,
    ) -> str:
        """Export a safe, path-free activity summary for spreadsheet review."""
        target = Path(destination)
        if target.suffix.lower() != ".csv":
            target = target.with_suffix(".csv")
        if not dry_run:
            _atomic_csv_write(target, records)
        return str(target)

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

    def summary(self, *, include_devices: bool = False) -> dict[str, Any]:
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
        connected_devices = []
        if include_devices:
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

    def prune_cache(self, retention_days: int, *, now: datetime | None = None) -> int:
        """Remove only expired app cache files; user data and recovery state are untouched."""
        cutoff = (now or datetime.now(timezone.utc)) - timedelta(days=max(1, retention_days))
        removed = 0
        for path in self.paths.cache.rglob("*"):
            if not path.is_file():
                continue
            try:
                modified = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)
                if modified >= cutoff:
                    continue
                removed += path.stat().st_size
                path.unlink()
            except OSError:
                continue
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


def _open_path(path: Path) -> None:
    if os.name == "nt":
        os.startfile(str(path))  # type: ignore[attr-defined]
        return
    opener = "open" if sys.platform == "darwin" else "xdg-open"
    subprocess.Popen([opener, str(path)])


class UpdateService:
    """Verify signed update manifests before an installer is allowed to run."""

    def __init__(self, paths: RuntimePaths | None = None) -> None:
        self.paths = paths or get_runtime_paths()
        self.public_key_file = _resource_path("packaging/update_public_key.json")

    def status(self) -> dict[str, Any]:
        last_check = self._last_check()
        return {
            "current_version": __version__,
            "channel": "stable",
            "configured": self.public_key_file.exists(),
            "last_check_file": str(self.paths.updates / "last_check.json"),
            "last_checked_at": last_check.get("checked_at", ""),
            "check_due": self.check_due(),
            "message": "Signed updates are enabled when a release manifest is available.",
        }

    def load_manifest(self, path: str | os.PathLike[str]) -> dict[str, Any]:
        try:
            payload = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ServiceError(
                ErrorCode.INVALID_DATA,
                "Update manifest could not be read.",
                technical_detail=str(exc),
            ) from exc
        if not isinstance(payload, dict):
            raise ServiceError(ErrorCode.INVALID_DATA, "Update manifest is not an object.")
        return payload

    def check_due(self, *, now: datetime | None = None) -> bool:
        last_checked = self._last_check().get("checked_at", "")
        if not last_checked:
            return True
        try:
            checked_at = datetime.fromisoformat(str(last_checked))
        except ValueError:
            return True
        current = now or datetime.now(timezone.utc)
        if checked_at.tzinfo is None:
            checked_at = checked_at.replace(tzinfo=timezone.utc)
        return current - checked_at >= timedelta(days=1)

    def check_manifest_url(
        self,
        manifest_url: str,
        *,
        channel: str = "stable",
        force: bool = False,
    ) -> dict[str, Any]:
        _validate_update_url(manifest_url, "manifest URL")
        if not force and not self.check_due():
            result = {
                "checked": False,
                "reason": "last check was less than 24 hours ago",
            }
            return result
        try:
            with urlopen(manifest_url, timeout=15) as response:
                _validate_update_response_url(response, manifest_url, "manifest redirect URL")
                payload = json.loads(response.read().decode("utf-8"))
        except (OSError, URLError, json.JSONDecodeError) as exc:
            raise ServiceError(
                ErrorCode.IO_ERROR,
                "Could not check for updates.",
                technical_detail=str(exc),
            ) from exc
        if not isinstance(payload, dict):
            raise ServiceError(ErrorCode.INVALID_DATA, "Update manifest is not an object.")
        verification = self.verify_manifest(payload, channel=channel)
        self.record_check({"checked": True, "manifest": verification})
        return verification

    def verify_manifest(
        self,
        manifest: dict[str, Any],
        *,
        installer_path: str | os.PathLike[str] | None = None,
        channel: str = "stable",
        require_newer: bool = True,
    ) -> dict[str, Any]:
        missing = [
            field
            for field in (
                "version",
                "channel",
                "installer_url",
                "size",
                "sha256",
                "release_notes_url",
                "minimum_supported_version",
                "signature",
                "authenticode_thumbprint",
            )
            if field not in manifest
        ]
        if missing:
            raise ServiceError(
                ErrorCode.INVALID_DATA,
                "Update manifest is missing required fields.",
                technical_detail=", ".join(missing),
            )
        if not str(manifest.get("authenticode_thumbprint", "")).strip():
            raise ServiceError(
                ErrorCode.INVALID_DATA,
                "Update manifest publisher identity is missing.",
                recoverable=False,
            )
        _validate_update_url(str(manifest["installer_url"]), "installer URL")
        _validate_update_url(str(manifest["release_notes_url"]), "release notes URL")
        if manifest["channel"] != channel:
            raise ServiceError(
                ErrorCode.VALIDATION,
                "Update channel does not match this installation.",
                technical_detail=f"manifest={manifest['channel']} expected={channel}",
            )
        if require_newer and _compare_versions(str(manifest["version"]), __version__) <= 0:
            raise ServiceError(
                ErrorCode.VALIDATION,
                "Update manifest does not describe a newer version.",
                technical_detail=f"manifest={manifest['version']} current={__version__}",
            )
        if _compare_versions(__version__, str(manifest["minimum_supported_version"])) < 0:
            raise ServiceError(
                ErrorCode.VALIDATION,
                "This version is too old for the update path.",
                technical_detail=f"minimum={manifest['minimum_supported_version']} current={__version__}",
            )

        public_key = self._load_public_key()
        if not verify_rsa_sha256_signature(manifest, public_key):
            raise ServiceError(
                ErrorCode.INVALID_DATA,
                "Update manifest signature is invalid.",
                technical_detail="RSA-SHA256 verification failed.",
                recoverable=False,
            )

        checksum_verified = False
        authenticode_verified = False
        if installer_path is not None:
            installer = Path(installer_path)
            actual_size = installer.stat().st_size
            expected_size = int(manifest["size"])
            if actual_size != expected_size:
                raise ServiceError(
                    ErrorCode.INVALID_DATA,
                    "Downloaded installer size does not match the manifest.",
                    technical_detail=f"actual={actual_size} expected={expected_size}",
                )
            actual_hash = _sha256_file(installer)
            if actual_hash.lower() != str(manifest["sha256"]).lower():
                raise ServiceError(
                    ErrorCode.INVALID_DATA,
                    "Downloaded installer checksum does not match the manifest.",
                    technical_detail=f"actual={actual_hash} expected={manifest['sha256']}",
                )
            checksum_verified = True
            authenticode_verified = self.verify_authenticode(
                installer,
                expected_thumbprint=str(manifest.get("authenticode_thumbprint", "")),
            )

        return {
            "valid": True,
            "version": manifest["version"],
            "channel": manifest["channel"],
            "signature_verified": True,
            "checksum_verified": checksum_verified,
            "authenticode_verified": authenticode_verified,
            "installer_url": manifest["installer_url"],
        }

    def download_installer(
        self,
        manifest: dict[str, Any],
        *,
        approved: bool = False,
    ) -> Path:
        if not approved:
            raise ServiceError(
                ErrorCode.VALIDATION,
                "Update download requires user approval.",
                recoverable=True,
            )
        self.verify_manifest(manifest, require_newer=True)
        target = self.paths.updates / f"DuplicateTransferManagerSetup-{manifest['version']}.exe"
        partial = target.with_suffix(target.suffix + ".partial")
        expected_size = int(manifest["size"])
        if expected_size <= 0 or expected_size > 2 * 1024 * 1024 * 1024:
            raise ServiceError(
                ErrorCode.INVALID_DATA,
                "Update installer size is outside the supported range.",
                technical_detail=f"declared size={expected_size}",
                recoverable=False,
            )
        try:
            with urlopen(str(manifest["installer_url"]), timeout=60) as response:
                _validate_update_response_url(
                    response,
                    str(manifest["installer_url"]),
                    "installer redirect URL",
                )
                received = 0
                with partial.open("wb") as stream:
                    while True:
                        chunk = response.read(1024 * 1024)
                        if not chunk:
                            break
                        received += len(chunk)
                        if received > expected_size:
                            raise ServiceError(
                                ErrorCode.INVALID_DATA,
                                "Downloaded installer exceeds the signed manifest size.",
                                recoverable=False,
                            )
                        stream.write(chunk)
                    stream.flush()
                    os.fsync(stream.fileno())
            if received != expected_size:
                raise ServiceError(
                    ErrorCode.INVALID_DATA,
                    "Downloaded installer size does not match the signed manifest.",
                    technical_detail=f"received={received} expected={expected_size}",
                )
            self.verify_manifest(manifest, installer_path=partial)
            os.replace(partial, target)
        except (OSError, URLError, ServiceError) as exc:
            try:
                partial.unlink(missing_ok=True)
            except OSError:
                pass
            if isinstance(exc, ServiceError):
                raise
            raise ServiceError(
                ErrorCode.IO_ERROR,
                "Could not download the update installer.",
                technical_detail=str(exc),
            ) from exc
        return target

    def prepare_update_state(self, manifest: dict[str, Any]) -> Path:
        payload = {
            "prepared_at": datetime.now(timezone.utc).isoformat(),
            "current_version": __version__,
            "target_version": manifest.get("version", ""),
            "operation_records": OperationRecordService(self.paths).list_records(),
        }
        target = self.paths.updates / "pre_update_state.json"
        _atomic_json_write(target, sanitize_payload(payload))
        return target

    def launch_verified_installer(
        self,
        manifest: dict[str, Any],
        installer_path: str | os.PathLike[str],
        *,
        approved: bool = False,
    ) -> dict[str, Any]:
        if not approved:
            raise ServiceError(
                ErrorCode.VALIDATION,
                "Launching the installer requires user approval.",
                recoverable=True,
            )
        verification = self.verify_manifest(manifest, installer_path=installer_path)
        if os.name == "nt" and not verification["authenticode_verified"]:
            raise ServiceError(
                ErrorCode.INVALID_DATA,
                "Installer signature could not be verified.",
                recoverable=False,
            )
        state_file = self.prepare_update_state(manifest)
        if os.name != "nt":
            return {
                "launched": False,
                "reason": "installer launch is only available on Windows",
                "state_file": str(state_file),
                "verification": verification,
            }
        subprocess.Popen([str(Path(installer_path)), "/SP-", "/NOCANCEL"])
        return {
            "launched": True,
            "state_file": str(state_file),
            "verification": verification,
        }

    def verify_authenticode(
        self,
        installer_path: str | os.PathLike[str],
        *,
        expected_thumbprint: str = "",
    ) -> bool:
        if os.name != "nt":
            return False
        escaped_path = str(Path(installer_path)).replace("'", "''")
        command = [
            "powershell",
            "-NoProfile",
            "-Command",
            (
                "$sig = Get-AuthenticodeSignature -LiteralPath "
                f"'{escaped_path}'; "
                "$sig | ConvertTo-Json -Compress"
            ),
        ]
        try:
            completed = subprocess.run(
                command,
                check=True,
                capture_output=True,
                text=True,
                timeout=30,
            )
            signature = json.loads(completed.stdout)
        except (OSError, subprocess.SubprocessError, json.JSONDecodeError):
            return False
        if signature.get("Status") != 0 and signature.get("Status") != "Valid":
            return False
        if not expected_thumbprint.strip():
            return False
        certificate = signature.get("SignerCertificate") or {}
        thumbprint = str(certificate.get("Thumbprint", "")).replace(" ", "")
        return thumbprint.lower() == expected_thumbprint.replace(" ", "").lower()

    def record_check(self, result: dict[str, Any]) -> None:
        payload = {
            "checked_at": datetime.now(timezone.utc).isoformat(),
            "result": result,
        }
        _atomic_json_write(self.paths.updates / "last_check.json", payload)

    def _last_check(self) -> dict[str, Any]:
        path = self.paths.updates / "last_check.json"
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return payload if isinstance(payload, dict) else {}

    def _load_public_key(self) -> dict[str, Any]:
        try:
            payload = json.loads(self.public_key_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ServiceError(
                ErrorCode.INVALID_DATA,
                "Update public key is unavailable.",
                technical_detail=str(exc),
                recoverable=False,
            ) from exc
        if not isinstance(payload, dict):
            raise ServiceError(ErrorCode.INVALID_DATA, "Update public key is invalid.")
        return payload


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _resource_path(relative_path: str) -> Path:
    bundled_root = getattr(sys, "_MEIPASS", "")
    if bundled_root:
        candidate = Path(bundled_root) / relative_path
        if candidate.exists():
            return candidate
    source_root = Path(__file__).resolve().parents[3]
    source_candidate = source_root / relative_path
    if source_candidate.exists():
        return source_candidate
    raise FileNotFoundError(f"Required application resource is unavailable: {relative_path}")


def _compare_versions(left: str, right: str) -> int:
    try:
        left_version = Version(left)
        right_version = Version(right)
    except InvalidVersion as exc:
        raise ValueError(f"Invalid application version: {exc}") from exc
    return (left_version > right_version) - (left_version < right_version)


_UPDATE_HOSTS = {
    "api.github.com",
    "github.com",
    "github-releases.githubusercontent.com",
    "objects.githubusercontent.com",
    "raw.githubusercontent.com",
    "release-assets.githubusercontent.com",
}


def _validate_update_url(url: str, label: str) -> None:
    parsed = urlsplit(str(url))
    if parsed.scheme.lower() != "https" or not parsed.hostname:
        raise ServiceError(
            ErrorCode.VALIDATION,
            f"The {label} must use HTTPS.",
            recoverable=False,
        )
    if parsed.username or parsed.password or parsed.hostname.lower() not in _UPDATE_HOSTS:
        raise ServiceError(
            ErrorCode.VALIDATION,
            f"The {label} does not use an approved release host.",
            technical_detail=parsed.hostname or "missing host",
            recoverable=False,
        )


def _validate_update_response_url(response: Any, requested_url: str, label: str) -> None:
    getter = getattr(response, "geturl", None)
    final_url = str(getter()) if callable(getter) else requested_url
    _validate_update_url(final_url, label)
