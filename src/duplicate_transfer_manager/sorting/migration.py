"""Non-destructive migration of legacy organizer manifests into Sort history."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from ..runtime_paths import RuntimePaths, get_runtime_paths
from .models import ConflictPolicy, FileMetadata, SortAction, SortPlanItem
from .executor import SortExecutor


class SortingMigrationService:
    marker_name = ".legacy-organizer-import-v1.json"

    def __init__(self, paths: RuntimePaths | None = None) -> None:
        self.paths = paths or get_runtime_paths()
        self.marker = self.paths.sorting / self.marker_name

    def migrate_legacy_runs(self) -> int:
        if self.marker.exists():
            return 0
        imported: list[str] = []
        errors: list[str] = []
        for manifest in sorted(self.paths.organization.glob("*/manifest.json")):
            try:
                payload = json.loads(manifest.read_text(encoding="utf-8"))
                operation_id = str(payload.get("operation_id") or manifest.parent.name)
                safe_id = "".join(value for value in operation_id if value.isalnum() or value in "_-")
                run_id = f"sort_legacy_{safe_id}"
                target = self.paths.sorting / "runs" / run_id / "journal.json"
                if target.exists():
                    imported.append(run_id)
                    continue
                plan_values = payload.get("plan", [])
                planned = [self._planned_item(value) for value in plan_values if isinstance(value, dict)]
                records = [self._record(value, bool(payload.get("dry_run", False))) for value in payload.get("records", []) if isinstance(value, dict)]
                if payload.get("dry_run") and not records:
                    records = [
                        {
                            "source": value.get("source_path", ""), "destination": value.get("destination_path", ""),
                            "action": SortAction.MOVE.value, "decision_source": "legacy_organizer", "association": "Legacy organizer",
                            "confidence": value.get("confidence", 1.0), "explanation": value.get("reason", "Legacy organizer preview"),
                            "status": "previewed", "size": value.get("size", 0), "created_at": payload.get("created_at", ""),
                            "conflict": value.get("collision", ""), "conflict_policy": payload.get("settings", {}).get("conflict_policy", "rename"),
                        }
                        for value in plan_values if isinstance(value, dict)
                    ]
                journal = {
                    "schema_version": 1, "run_id": run_id, "profile_id": "legacy_organizer",
                    "created_at": payload.get("created_at", ""), "updated_at": payload.get("created_at", ""),
                    "status": "legacy_imported", "dry_run": bool(payload.get("dry_run", False)),
                    "sources": [payload.get("settings", {}).get("source_root", "")],
                    "planned_items": planned, "records": records,
                    "legacy_manifest": str(manifest),
                }
                self._write(target, journal)
                imported.append(run_id)
            except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
                errors.append(f"{manifest}: {exc}")
        self._write(self.marker, {"schema_version": 1, "imported": imported, "errors": errors})
        return len(imported)

    @staticmethod
    def _planned_item(value: dict) -> dict:
        path = Path(str(value.get("source_path", "")))
        metadata = FileMetadata(
            path=str(path), name=path.name, extension=path.suffix.lower(), size=int(value.get("size", 0)),
            created=float(value.get("modified", 0)), modified=float(value.get("modified", 0)),
            media_type=str(value.get("category", "other")).casefold(),
        )
        item = SortPlanItem(
            metadata, "legacy_organizer", SortAction.MOVE, str(value.get("destination_path", "")),
            ConflictPolicy.RENAME, float(value.get("confidence", 1)), str(value.get("reason", "Legacy organizer plan")),
            category=str(value.get("category", "")), conflict=str(value.get("collision", "")),
            requires_review=False, selected=bool(value.get("selected", True)),
        )
        return SortExecutor._serialize_item(item)

    @staticmethod
    def _record(value: dict, dry_run: bool) -> dict:
        return {
            "source": value.get("source_path", ""), "destination": value.get("destination_path", ""),
            "action": SortAction.MOVE.value, "decision_source": "legacy_organizer", "association": "Legacy organizer",
            "confidence": value.get("confidence", 1.0), "explanation": value.get("reason", "Legacy organizer operation"),
            "status": "previewed" if dry_run else "undone" if value.get("restored_at") else "completed",
            "size": value.get("size", 0), "created_at": value.get("moved_at", ""),
            "conflict": value.get("collision", ""), "conflict_policy": "rename",
            "fingerprint": value.get("source_fingerprint", ""),
            "replaced_backup": value.get("replaced_destination_backup", ""),
            "undone_at": value.get("restored_at", ""),
        }

    @staticmethod
    def _write(path: Path, payload: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        handle, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as stream:
                json.dump(payload, stream, indent=2)
            os.replace(temporary, path)
        except Exception:
            try:
                os.remove(temporary)
            except OSError:
                pass
            raise
