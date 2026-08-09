"""Journaled sorting execution, verification, cancellation, and undo."""

from __future__ import annotations

import errno
import csv
import hashlib
import json
import os
import shutil
import tempfile
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable

from ..core import CancellationToken, ErrorCode, OperationCancelled, OperationPhase, OperationReporter, ServiceError
from ..runtime_paths import RuntimePaths, get_runtime_paths
from .models import ConflictPolicy, FileMetadata, SortAction, SortExecutionResult, SortPlan, SortPlanItem

try:
    from send2trash import send2trash as _send_to_recycle
except ImportError:  # pragma: no cover - dependency is present in release installs
    _send_to_recycle = None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _run_id() -> str:
    return "sort_" + datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_%f")


class PerFileSkipped(RuntimeError):
    pass


class SortExecutionControl:
    """Thread-safe pause/resume/cancel and per-file skip controls."""

    def __init__(self, cancellation: CancellationToken | None = None) -> None:
        self.cancellation = cancellation or CancellationToken()
        self._condition = threading.Condition()
        self._paused = False
        self._skip: set[str] = set()

    @property
    def paused(self) -> bool:
        with self._condition:
            return self._paused

    def pause(self) -> None:
        with self._condition:
            self._paused = True

    def resume(self) -> None:
        with self._condition:
            self._paused = False
            self._condition.notify_all()

    def cancel(self) -> None:
        self.cancellation.cancel()
        self.resume()

    def skip(self, source_path: str) -> None:
        with self._condition:
            self._skip.add(str(Path(source_path).resolve()))
            self._condition.notify_all()

    def checkpoint(self, source_path: str = "") -> bool:
        with self._condition:
            while self._paused and not self.cancellation.is_cancelled():
                self._condition.wait(0.2)
            self.cancellation.raise_if_cancelled()
            return bool(source_path and str(Path(source_path).resolve()) in self._skip)


class SortExecutor:
    def __init__(self, paths: RuntimePaths | None = None) -> None:
        self.paths = paths or get_runtime_paths()

    def execute(
        self,
        plan: SortPlan,
        *,
        approved_sources: Iterable[str] = (),
        confirmed: bool = False,
        control: SortExecutionControl | None = None,
        reporter: OperationReporter | None = None,
        retry_attempts: int = 1,
    ) -> SortExecutionResult:
        approved = {str(Path(value).resolve()) for value in approved_sources}
        # A live run executes only rows explicitly checked in Review. Rule
        # certainty controls the default checkbox state, never approval itself.
        selected = [
            item for item in plan.items
            if item.selected and (plan.dry_run or str(Path(item.metadata.path).resolve()) in approved)
        ]
        destructive = any(
            item.action in {SortAction.MOVE, SortAction.RENAME, SortAction.QUARANTINE, SortAction.RECYCLE}
            or item.conflict == "overwrite"
            for item in selected
        )
        if destructive and not plan.dry_run and not confirmed:
            raise ServiceError(ErrorCode.VALIDATION, "Confirm the reviewed sorting plan before changing files.")
        if not plan.dry_run:
            self._validate_space(selected)
        run_id = _run_id()
        run_root = self.paths.sorting / "runs" / run_id
        journal_path = run_root / "journal.json"
        control = control or SortExecutionControl()
        reporter = reporter or OperationReporter()
        records: list[dict] = []
        journal = {
            "schema_version": 1, "run_id": run_id, "profile_id": plan.profile_id,
            "created_at": _now(), "updated_at": _now(), "status": "dry_run" if plan.dry_run else "running",
            "dry_run": plan.dry_run, "sources": list(plan.sources),
            "planned_items": [self._serialize_item(item) for item in selected], "records": records,
        }
        self._write_journal(journal_path, journal)
        completed = skipped = failed = verified = bytes_processed = 0
        failures: list[str] = []
        started = time.monotonic()
        planned_total = sum(value.metadata.size for value in selected)
        planned_remaining = planned_total
        for index, item in enumerate(selected, 1):
            source = str(Path(item.metadata.path).resolve())
            try:
                if control.checkpoint(source):
                    skipped += 1
                    entry = self._record(item, "skipped", warning="Skipped during processing.")
                    records.append(entry)
                    self._append_record(journal_path, entry)
                    continue
            except OperationCancelled:
                journal["status"] = "cancelled"
                journal["updated_at"] = _now()
                self._write_journal(journal_path, journal)
                return self._result(run_id, journal_path, "cancelled", completed, skipped, failed, verified, bytes_processed, failures, records)
            reporter.progress_callback(index - 1, len(selected), f"Sorting {Path(source).name}", phase=OperationPhase.TRANSFER)
            if plan.dry_run or item.action == SortAction.IGNORE:
                status = "previewed" if plan.dry_run else "ignored"
                entry = self._record(item, status)
                records.append(entry)
                self._append_record(journal_path, entry)
                skipped += int(item.action == SortAction.IGNORE)
                continue
            error = ""
            record: dict = {}
            for attempt in range(max(0, retry_attempts) + 1):
                try:
                    record = self._execute_item(item, run_root, control)
                    break
                except PerFileSkipped:
                    record = self._record(item, "skipped", warning="Skipped during processing.")
                    break
                except OperationCancelled:
                    journal["status"] = "cancelled"
                    journal["updated_at"] = _now()
                    self._write_journal(journal_path, journal)
                    return self._result(run_id, journal_path, "cancelled", completed, skipped, failed, verified, bytes_processed, failures, records)
                except OSError as exc:
                    error = str(exc)
                    if attempt >= max(0, retry_attempts):
                        break
                    control.checkpoint(source)
            if error and not record:
                failed += 1
                failures.append(f"{source}: {error}")
                records.append(self._record(item, "failed", error=error, attempts=max(0, retry_attempts) + 1))
            else:
                records.append(record)
                if record.get("status") == "skipped":
                    skipped += 1
                else:
                    completed += 1
                    bytes_processed += item.metadata.size
                    verified += int(record.get("verified", False))
            self._append_record(journal_path, records[-1])
            elapsed = max(0.001, time.monotonic() - started)
            rate = bytes_processed / elapsed
            # Both totals used to be re-summed over the whole plan on every
            # item, which is O(n^2) arithmetic on top of the O(n^2) writes.
            planned_remaining -= item.metadata.size
            remaining = max(0, planned_remaining)
            reporter.emit(
                f"Sorted {index} of {len(selected)} files", phase=OperationPhase.VERIFICATION,
                current=index, total=len(selected), bytes_processed=bytes_processed,
                total_bytes=planned_total,
                details={"rate": rate, "eta_seconds": remaining / rate if rate else None},
            )
        status = "completed_with_errors" if failed else "completed"
        journal["status"] = status
        journal["completed_at"] = _now()
        journal["updated_at"] = _now()
        self._write_journal(journal_path, journal)
        return self._result(run_id, journal_path, status, completed, skipped, failed, verified, bytes_processed, failures, records)

    def undo(self, run_id: str, *, conflict_policy: ConflictPolicy = ConflictPolicy.RENAME, dry_run: bool = False) -> SortExecutionResult:
        journal_path = self.paths.sorting / "runs" / run_id / "journal.json"
        journal = self.load_run(run_id)
        failures: list[str] = []
        completed = skipped = 0
        transaction_backups: list[Path] = []
        run_root = journal_path.parent
        for record in reversed(journal.get("records", [])):
            if record.get("status") != "completed" or record.get("undone_at"):
                continue
            action = SortAction(record["action"])
            source = Path(record["source"])
            destination_text = str(record.get("destination", ""))
            destination = Path(destination_text) if destination_text else None
            try:
                if action == SortAction.COPY:
                    if not destination or not destination.exists():
                        raise FileNotFoundError(destination)
                    self._verify_fingerprint(destination, record.get("fingerprint", ""))
                    if not dry_run:
                        destination.unlink()
                elif action in {SortAction.MOVE, SortAction.RENAME, SortAction.QUARANTINE}:
                    if not destination or not destination.exists():
                        raise FileNotFoundError(destination)
                    self._verify_fingerprint(destination, record.get("fingerprint", ""))
                    target = self._undo_target(source, conflict_policy)
                    if target is None:
                        skipped += 1
                        continue
                    if not dry_run:
                        backup = self._restore_for_undo(
                            destination,
                            target,
                            str(record.get("fingerprint", "")),
                            run_root,
                        )
                        if backup is not None:
                            transaction_backups.append(backup)
                elif action == SortAction.RECYCLE:
                    skipped += 1
                    continue
                backup_text = str(record.get("replaced_backup", ""))
                if backup_text and not dry_run:
                    backup = Path(backup_text)
                    if backup.exists() and destination is not None:
                        destination.parent.mkdir(parents=True, exist_ok=True)
                        shutil.move(str(backup), str(destination))
                if not dry_run:
                    record["undone_at"] = _now()
                completed += 1
            except OSError as exc:
                failures.append(f"{record.get('source', '')}: {exc}")
        if not dry_run:
            journal["status"] = "undone_with_errors" if failures else "undone"
            journal["updated_at"] = _now()
            self._write_journal(journal_path, journal)
            if not failures:
                for backup in transaction_backups:
                    backup.unlink(missing_ok=True)
        return SortExecutionResult(run_id, "preview" if dry_run else journal.get("status", "undone"), str(journal_path), completed, skipped, len(failures), completed, failures=tuple(failures), undo_available=False)

    def list_runs(self) -> list[dict]:
        runs: list[dict] = []
        for path in sorted((self.paths.sorting / "runs").glob("*/journal.json"), reverse=True):
            try:
                runs.append(json.loads(path.read_text(encoding="utf-8")))
            except (OSError, json.JSONDecodeError):
                continue
        return runs

    def prune_runs(self, retention_days: int = 90) -> int:
        """Remove only expired app-owned journals/backups, never sorted files."""
        cutoff = datetime.now(timezone.utc) - timedelta(days=max(1, retention_days))
        removed = 0
        root = self.paths.sorting / "runs"
        for journal in root.glob("*/journal.json"):
            try:
                if datetime.fromtimestamp(journal.stat().st_mtime, timezone.utc) < cutoff:
                    shutil.rmtree(journal.parent)
                    removed += 1
            except OSError:
                continue
        return removed

    def retry_failed(self, run_id: str, *, confirmed: bool, retry_attempts: int = 1) -> SortExecutionResult:
        journal = self.load_run(run_id)
        failed_sources = {str(record.get("source", "")) for record in journal.get("records", []) if record.get("status") == "failed"}
        items = [self._deserialize_item(value) for value in journal.get("planned_items", []) if value.get("metadata", {}).get("path") in failed_sources]
        if not items:
            raise ServiceError(ErrorCode.VALIDATION, "This sorting run has no failed files available to retry.")
        plan = SortPlan(str(journal.get("profile_id", "")), tuple(journal.get("sources", [])), tuple(items), dry_run=False)
        return self.execute(plan, approved_sources=[item.metadata.path for item in items], confirmed=confirmed, retry_attempts=retry_attempts)

    def resume_run(self, run_id: str, *, confirmed: bool, retry_attempts: int = 1) -> SortExecutionResult:
        journal = self.load_run(run_id)
        terminal_sources = {
            str(record.get("source", "")) for record in journal.get("records", [])
            if record.get("status") in {"completed", "ignored", "skipped", "previewed"}
        }
        items = [self._deserialize_item(value) for value in journal.get("planned_items", []) if value.get("metadata", {}).get("path") not in terminal_sources]
        if not items:
            raise ServiceError(ErrorCode.VALIDATION, "This sorting run has no interrupted files available to resume.")
        plan = SortPlan(str(journal.get("profile_id", "")), tuple(journal.get("sources", [])), tuple(items), dry_run=bool(journal.get("dry_run", False)))
        return self.execute(plan, approved_sources=[item.metadata.path for item in items], confirmed=confirmed, retry_attempts=retry_attempts)

    def load_run(self, run_id: str) -> dict:
        if not run_id.startswith("sort_") or any(value in run_id for value in ("/", "\\", "..")):
            raise ServiceError(ErrorCode.VALIDATION, "Choose a valid sorting run.")
        path = self.paths.sorting / "runs" / run_id / "journal.json"
        try:
            journal = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ServiceError(ErrorCode.NOT_FOUND, "The sorting run journal is unavailable.", technical_detail=str(exc)) from exc
        # A run killed part way has a consolidated journal from before the
        # first item. The append log holds what actually happened, so undo and
        # resume see the real history rather than an empty record list.
        return self._merge_appended_records(path, journal)

    def export_run(self, run_id: str, destination: str | os.PathLike[str]) -> Path:
        journal = self.load_run(run_id)
        target = Path(destination).expanduser()
        target.parent.mkdir(parents=True, exist_ok=True)
        handle, temporary = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=target.parent)
        try:
            if target.suffix.casefold() == ".csv":
                fields = ("source", "destination", "action", "decision_source", "association", "confidence", "status", "size", "conflict", "error", "warning")
                with os.fdopen(handle, "w", encoding="utf-8", newline="") as stream:
                    writer = csv.DictWriter(stream, fieldnames=fields)
                    writer.writeheader()
                    for record in journal.get("records", []):
                        writer.writerow({field: record.get(field, "") for field in fields})
            else:
                with os.fdopen(handle, "w", encoding="utf-8") as stream:
                    json.dump(journal, stream, indent=2)
            os.replace(temporary, target)
            return target
        except Exception:
            try:
                os.remove(temporary)
            except OSError:
                pass
            raise

    def _execute_item(self, item: SortPlanItem, run_root: Path, control: SortExecutionControl) -> dict:
        source = Path(item.metadata.path)
        if not source.is_file():
            raise FileNotFoundError(source)
        current = source.stat()
        if current.st_size != item.metadata.size or current.st_mtime != item.metadata.modified:
            raise OSError("Source changed after preview; review the plan again.")
        fingerprint = self._fingerprint(source, control)
        if item.action == SortAction.RECYCLE:
            if _send_to_recycle is None:
                raise OSError("Recycle Bin support is unavailable in this installation.")
            _send_to_recycle(str(source))
            return self._record(item, "completed", fingerprint=fingerprint, verified=not source.exists(), warning="Recycle Bin actions cannot be undone from this app.")
        destination = Path(item.destination)
        if destination.resolve() == source.resolve():
            raise OSError("Source and destination are the same file.")
        destination.parent.mkdir(parents=True, exist_ok=True)
        backup = ""
        if destination.exists():
            if item.conflict != "overwrite":
                raise OSError("Destination changed after preview; review the plan again.")
            # Named from the source path rather than a directory count, which
            # re-listed the whole replaced/ folder for every overwrite.
            token = hashlib.sha256(str(destination).encode("utf-8", "replace")).hexdigest()[:12]
            backup_path = run_root / "replaced" / f"{token}_{destination.name}"
            backup_path.parent.mkdir(parents=True, exist_ok=True)
            journal_path = run_root / "journal.json"
            # Record the intent before touching the incumbent. run_root is on
            # the app data drive while destinations are commonly on another
            # volume, so displacing it is a copy plus a delete. A crash in
            # between previously left the incumbent gone, an orphan in
            # replaced/ that nothing referenced, and prune_runs eventually
            # deleting that orphan too.
            self._append_record(
                journal_path,
                {
                    "status": "replacing",
                    "source": item.metadata.path,
                    "destination": str(destination),
                    "replaced_backup": str(backup_path),
                    "created_at": _now(),
                },
            )
            # Verify the copy before removing the original, matching what undo
            # already does when it displaces a file.
            incumbent_fingerprint = self._fingerprint(destination)
            shutil.copy2(str(destination), str(backup_path))
            self._verify_fingerprint(backup_path, incumbent_fingerprint)
            destination.unlink()
            backup = str(backup_path)
        try:
            if item.action == SortAction.COPY:
                self._copy(source, destination, control)
            else:
                self._move(source, destination, control)
            self._verify_fingerprint(destination, fingerprint)
        except Exception:
            if item.action in {SortAction.MOVE, SortAction.RENAME, SortAction.QUARANTINE} and not source.exists() and destination.exists():
                source.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(destination), str(source))
            elif destination.exists():
                try:
                    destination.unlink()
                except OSError:
                    pass
            if backup and Path(backup).exists():
                shutil.move(backup, str(destination))
            raise
        return self._record(item, "completed", fingerprint=fingerprint, verified=True, replaced_backup=backup)

    @staticmethod
    def _validate_space(items: list[SortPlanItem]) -> None:
        required_by_root: dict[Path, int] = {}
        for item in items:
            if not item.destination or item.action in {SortAction.IGNORE, SortAction.RECYCLE}:
                continue
            source = Path(item.metadata.path)
            destination = Path(item.destination)
            requires_copy_space = item.action == SortAction.COPY or source.anchor.casefold() != destination.anchor.casefold()
            if not requires_copy_space:
                continue
            parent = destination.parent
            while not parent.exists() and parent != parent.parent:
                parent = parent.parent
            required_by_root[parent] = required_by_root.get(parent, 0) + item.metadata.size
        reserve = 1024 * 1024
        for root, required in required_by_root.items():
            try:
                free = shutil.disk_usage(root).free
            except OSError as exc:
                raise ServiceError(ErrorCode.IO_ERROR, "Available destination space could not be checked.", technical_detail=str(exc)) from exc
            if free < required + reserve:
                raise ServiceError(
                    ErrorCode.IO_ERROR,
                    "The destination does not have enough free space for the approved sorting plan.",
                    technical_detail=f"Required {required + reserve} bytes; available {free} bytes at {root}.",
                )

    def _copy(self, source: Path, destination: Path, control: SortExecutionControl) -> None:
        temporary = destination.with_name(f".{destination.name}.dtm-partial")
        try:
            with source.open("rb") as input_stream, temporary.open("xb") as output_stream:
                while True:
                    if control.checkpoint(str(source)):
                        raise PerFileSkipped("File was skipped during copying.")
                    chunk = input_stream.read(1024 * 1024)
                    if not chunk:
                        break
                    output_stream.write(chunk)
                output_stream.flush()
                os.fsync(output_stream.fileno())
            os.replace(temporary, destination)
            shutil.copystat(source, destination)
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass

    def _move(self, source: Path, destination: Path, control: SortExecutionControl) -> None:
        control.checkpoint(str(source))
        try:
            os.replace(source, destination)
        except OSError as exc:
            if exc.errno not in {errno.EXDEV, errno.EACCES, errno.EPERM}:
                raise
            self._copy(source, destination, control)
            self._verify_fingerprint(destination, self._fingerprint(source, control))
            source.unlink()

    @staticmethod
    def _fingerprint(path: Path, control: SortExecutionControl | None = None) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            while True:
                if control:
                    if control.checkpoint(str(path)):
                        raise PerFileSkipped("File was skipped during verification.")
                chunk = stream.read(1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
        return digest.hexdigest()

    def _verify_fingerprint(self, path: Path, expected: str) -> None:
        if expected and self._fingerprint(path) != expected:
            raise OSError("File verification failed after sorting.")

    def _restore_for_undo(
        self,
        destination: Path,
        target: Path,
        expected: str,
        run_root: Path,
    ) -> Path | None:
        """Restore without removing an incumbent until promotion can succeed."""

        target.parent.mkdir(parents=True, exist_ok=True)
        backup = None
        if target.exists():
            backup_dir = run_root / "undo_backups"
            backup_dir.mkdir(parents=True, exist_ok=True)
            backup = backup_dir / f"{len(list(backup_dir.glob('*'))):06d}_{target.name}"
            shutil.copy2(target, backup)
            self._verify_fingerprint(backup, self._fingerprint(target))

        handle, temporary_name = tempfile.mkstemp(
            prefix=f".{target.name}.",
            suffix=".dtm-undo",
            dir=target.parent,
        )
        os.close(handle)
        os.remove(temporary_name)
        temporary = Path(temporary_name)
        try:
            shutil.move(str(destination), str(temporary))
            self._verify_fingerprint(temporary, expected)
            os.replace(temporary, target)
            return backup
        except Exception:
            if temporary.exists() and not destination.exists():
                try:
                    shutil.move(str(temporary), str(destination))
                except OSError:
                    pass
            raise

    @staticmethod
    def _undo_target(source: Path, policy: ConflictPolicy) -> Path | None:
        if not source.exists():
            return source
        if policy == ConflictPolicy.SKIP or policy == ConflictPolicy.REVIEW:
            return None
        if policy == ConflictPolicy.OVERWRITE:
            return source
        index = 1
        candidate = source
        while candidate.exists():
            candidate = source.with_name(f"{source.stem} (restored {index}){source.suffix}")
            index += 1
        return candidate

    @staticmethod
    def _record(item: SortPlanItem, status: str, **extra) -> dict:
        return {
            "source": item.metadata.path, "destination": item.destination, "action": item.action.value,
            "decision_source": item.decision_source, "association": item.matched_association_name,
            "confidence": item.confidence, "explanation": item.explanation, "status": status,
            "size": item.metadata.size, "conflict": item.conflict,
            "extension": item.metadata.extension, "media_type": item.metadata.media_type,
            "conflict_policy": item.conflict_policy.value, "created_at": _now(), **extra,
        }

    @staticmethod
    def _serialize_item(item: SortPlanItem) -> dict:
        return {
            "metadata": {
                "path": item.metadata.path, "name": item.metadata.name, "extension": item.metadata.extension,
                "size": item.metadata.size, "created": item.metadata.created, "modified": item.metadata.modified,
                "media_type": item.metadata.media_type, "width": item.metadata.width, "height": item.metadata.height,
                "duration": item.metadata.duration, "captured": item.metadata.captured, "mime_type": item.metadata.mime_type,
                "attributes": item.metadata.attributes,
            },
            "decision_source": item.decision_source, "action": item.action.value, "destination": item.destination,
            "conflict_policy": item.conflict_policy.value, "confidence": item.confidence,
            "explanation": item.explanation, "matched_association_id": item.matched_association_id,
            "matched_association_name": item.matched_association_name, "category": item.category,
            "conflict": item.conflict, "warnings": list(item.warnings), "requires_review": item.requires_review,
            "approved": item.approved, "selected": item.selected,
        }

    @staticmethod
    def _deserialize_item(values: dict) -> SortPlanItem:
        metadata = FileMetadata(**values["metadata"])
        return SortPlanItem(
            metadata=metadata, decision_source=str(values.get("decision_source", "manual")),
            action=SortAction(str(values.get("action", SortAction.IGNORE.value))),
            destination=str(values.get("destination", "")),
            conflict_policy=ConflictPolicy(str(values.get("conflict_policy", ConflictPolicy.REVIEW.value))),
            confidence=float(values.get("confidence", 0)), explanation=str(values.get("explanation", "")),
            matched_association_id=str(values.get("matched_association_id", "")),
            matched_association_name=str(values.get("matched_association_name", "")),
            category=str(values.get("category", "")), conflict=str(values.get("conflict", "")),
            warnings=tuple(values.get("warnings", [])), requires_review=bool(values.get("requires_review", True)),
            approved=bool(values.get("approved", False)), selected=bool(values.get("selected", True)),
        )

    @staticmethod
    def _records_path(journal_path: Path) -> Path:
        return journal_path.with_name("records.jsonl")

    @classmethod
    def _append_record(cls, journal_path: Path, record: dict) -> None:
        """Append one record instead of rewriting the whole journal.

        The journal was serialized in full after every item, including
        planned_items, which holds a full metadata blob per selected file and
        never shrinks. That made the run cost O(n^2) bytes: a 20,000 file sort
        measured at roughly 360 GB written for a 24 MB final journal. Appending
        one line per item is O(1) and is still a durable crash record, which
        matters more than the consolidated file during the run.
        """

        try:
            path = cls._records_path(journal_path)
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(record) + "\n")
        except OSError:
            # The consolidated journal written at the end remains the fallback.
            pass

    @classmethod
    def _merge_appended_records(cls, journal_path: Path, journal: dict) -> dict:
        """Fill in records from the append log when the run did not finish.

        A run killed part way leaves a journal whose records list is whatever
        was last consolidated. The append log is the authoritative per-item
        history, so recovery reads it back.
        """

        path = cls._records_path(journal_path)
        if not path.exists():
            return journal
        appended: list[dict] = []
        try:
            with path.open(encoding="utf-8") as stream:
                for line in stream:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        appended.append(json.loads(line))
                    except json.JSONDecodeError:
                        # A torn final line from a hard kill; earlier ones stand.
                        continue
        except OSError:
            return journal
        if len(appended) > len(journal.get("records", [])):
            journal["records"] = appended
        return journal

    @staticmethod
    def _write_journal(path: Path, payload: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        handle, temporary = tempfile.mkstemp(prefix=".journal.", suffix=".tmp", dir=path.parent)
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

    @staticmethod
    def _result(run_id: str, journal: Path, status: str, completed: int, skipped: int, failed: int, verified: int, bytes_processed: int, failures: list[str], records: list[dict]) -> SortExecutionResult:
        undoable = any(record.get("status") == "completed" and record.get("action") != SortAction.RECYCLE.value for record in records)
        return SortExecutionResult(run_id, status, str(journal), completed, skipped, failed, verified, bytes_processed, tuple(failures), undoable)
