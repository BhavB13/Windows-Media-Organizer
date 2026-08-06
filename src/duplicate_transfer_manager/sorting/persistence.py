"""Atomic profile persistence and compatibility migration."""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from ..core import AppSettings, ErrorCode, ServiceError
from ..runtime_paths import RuntimePaths, get_runtime_paths
from .models import Association, ConflictPolicy, SortAction, SortCondition, ConditionField, ConditionOperator, SortingProfile


STORE_VERSION = 1


def _atomic_write(path: Path, payload: dict) -> None:
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


class SortingProfileStore:
    """CRUD/import/export for local reusable sorting profiles."""

    def __init__(self, paths: RuntimePaths | None = None) -> None:
        self.paths = paths or get_runtime_paths()
        self.path = self.paths.sorting / "profiles.json"

    def list(self) -> list[SortingProfile]:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return []
        except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
            raise ServiceError(ErrorCode.INVALID_DATA, "Sorting profiles could not be read.", technical_detail=str(exc)) from exc
        profiles = payload.get("profiles", []) if isinstance(payload, dict) else []
        return [SortingProfile.from_dict(item) for item in profiles if isinstance(item, dict)]

    def get(self, profile_id: str) -> SortingProfile | None:
        return next((profile for profile in self.list() if profile.id == profile_id), None)

    def save(self, profile: SortingProfile) -> SortingProfile:
        self._validate(profile)
        profiles = self.list()
        now = datetime.now(timezone.utc).isoformat()
        saved = replace(profile, updated_at=now)
        profiles = [saved if item.id == saved.id else item for item in profiles]
        if not any(item.id == saved.id for item in profiles):
            profiles.append(saved)
        self._write(profiles)
        return saved

    def duplicate(self, profile_id: str, name: str | None = None) -> SortingProfile:
        source = self.get(profile_id)
        if source is None:
            raise ServiceError(ErrorCode.NOT_FOUND, "The sorting profile is unavailable.")
        now = datetime.now(timezone.utc).isoformat()
        duplicate = replace(source, id=f"profile_{uuid4().hex}", name=name or f"{source.name} copy", created_at=now, updated_at=now)
        return self.save(duplicate)

    def delete(self, profile_id: str) -> bool:
        profiles = self.list()
        remaining = [profile for profile in profiles if profile.id != profile_id]
        if len(remaining) == len(profiles):
            return False
        self._write(remaining)
        return True

    def set_enabled(self, profile_id: str, enabled: bool) -> SortingProfile:
        profile = self.get(profile_id)
        if profile is None:
            raise ServiceError(ErrorCode.NOT_FOUND, "The sorting profile is unavailable.")
        return self.save(replace(profile, enabled=enabled))

    def export_profile(self, profile_id: str, destination: str | os.PathLike[str]) -> Path:
        profile = self.get(profile_id)
        if profile is None:
            raise ServiceError(ErrorCode.NOT_FOUND, "The sorting profile is unavailable.")
        target = Path(destination).expanduser()
        _atomic_write(target, {"schema_version": STORE_VERSION, "profile": profile.to_dict()})
        return target

    def import_profile(self, source: str | os.PathLike[str]) -> SortingProfile:
        try:
            payload = json.loads(Path(source).expanduser().read_text(encoding="utf-8"))
            values = payload["profile"]
            profile = SortingProfile.from_dict(values)
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ServiceError(ErrorCode.INVALID_DATA, "Choose a valid sorting profile export.", technical_detail=str(exc)) from exc
        return self.save(replace(profile, id=f"profile_{uuid4().hex}", name=f"{profile.name} (imported)"))

    def migrate_organizer_presets(self, settings: AppSettings) -> list[SortingProfile]:
        """Non-destructively translate old organizer presets once by ID/name."""
        existing = self.list()
        existing_names = {profile.name.casefold() for profile in existing}
        created: list[SortingProfile] = []
        for preset in settings.organization_presets:
            if not isinstance(preset, dict) or not str(preset.get("name", "")).strip():
                continue
            name = str(preset["name"]).strip()
            if name.casefold() in existing_names:
                continue
            mode = str(preset.get("mode", "flatten"))
            old_policy = str(preset.get("conflict_policy", "rename"))
            policy = ConflictPolicy.OVERWRITE if old_policy == "replace" else ConflictPolicy(old_policy)
            extension_condition = SortCondition(ConditionField.SOURCE_PATH, ConditionOperator.CONTAINS, "")
            association = Association(
                name=f"{name} default",
                conditions=(extension_condition,),
                action=SortAction.MOVE,
                destination=str(preset.get("destination_root", "")),
                conflict_policy=policy,
                rename_template="{stem}{suffix}" if mode == "flatten" else f"{{media_type}}/{{stem}}{{suffix}}",
            )
            profile = self.save(SortingProfile(name=name, associations=(association,), ml_enabled=bool(preset.get("ml_auto_organize", False))))
            created.append(profile)
            existing_names.add(name.casefold())
        return created

    def _write(self, profiles: list[SortingProfile]) -> None:
        _atomic_write(self.path, {"schema_version": STORE_VERSION, "profiles": [profile.to_dict() for profile in profiles]})

    @staticmethod
    def _validate(profile: SortingProfile) -> None:
        if not profile.name.strip():
            raise ServiceError(ErrorCode.VALIDATION, "Give the sorting profile a name.")
        if not 0 <= profile.review_confidence <= profile.high_confidence <= 1:
            raise ServiceError(ErrorCode.VALIDATION, "Sorting confidence thresholds must be between 0 and 1.")
        if profile.unmatched_policy not in {"review", "exclude"}:
            raise ServiceError(ErrorCode.VALIDATION, "Choose review or exclude for unmatched files.")
        ids: set[str] = set()
        for association in profile.associations:
            if association.id in ids:
                raise ServiceError(ErrorCode.VALIDATION, "Association IDs must be unique within a profile.")
            ids.add(association.id)
            if not association.name.strip():
                raise ServiceError(ErrorCode.VALIDATION, "Every association needs a name.")
            if association.action in {SortAction.MOVE, SortAction.COPY} and not association.destination.strip():
                raise ServiceError(ErrorCode.VALIDATION, f"{association.name} needs a destination.")
