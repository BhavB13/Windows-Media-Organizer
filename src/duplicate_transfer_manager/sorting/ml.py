"""Optional local classifier boundary and correction feedback persistence."""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable

from ..runtime_paths import RuntimePaths, get_runtime_paths
from .models import FileMetadata, MLSuggestion, SortingProfile


@dataclass(frozen=True)
class MLFeedback:
    file_name: str
    extension: str
    source_parent: str
    previous_category: str
    corrected_category: str
    corrected_destination: str
    created_at: str


Classifier = Callable[[FileMetadata, SortingProfile, tuple[dict, ...]], MLSuggestion | None]


class LocalMLService:
    """Local-only classification with an optional provider and inert feedback.

    Feedback is deliberately not used to silently train a model. Exact local
    corrections and prior destinations can influence later suggestions, while
    model training remains an explicit future/user-controlled operation.
    """

    _tokens = {
        "screenshot": "Screenshots", "screen shot": "Screenshots",
        "receipt": "Receipts", "invoice": "Receipts",
        "dog": "Pets", "cat": "Pets", "pet": "Pets",
        "vacation": "Travel", "travel": "Travel", "trip": "Travel",
        "food": "Food", "meal": "Food", "recipe": "Food",
        "art": "Artwork", "drawing": "Artwork",
    }

    def __init__(self, paths: RuntimePaths | None = None, classifier: Classifier | None = None) -> None:
        self.paths = paths or get_runtime_paths()
        self.classifier = classifier
        self.feedback_path = self.paths.sorting / "ml_feedback.json"

    @property
    def available(self) -> bool:
        return self.classifier is not None

    def predict(
        self,
        metadata: FileMetadata,
        profile: SortingProfile,
        *,
        default_destination: str = "",
        prior_destinations: Iterable[dict] = (),
    ) -> MLSuggestion | None:
        if not profile.ml_enabled:
            return None
        prior = tuple(prior_destinations)
        if self.classifier is not None:
            suggestion = self.classifier(metadata, profile, prior)
            if suggestion is not None:
                return suggestion
        correction = self._matching_feedback(metadata)
        if correction:
            return MLSuggestion(
                correction.corrected_category, correction.corrected_destination, 1.0,
                "Applied a previous local correction. No model was retrained.", "local_feedback", False,
            )
        name = metadata.name.casefold()
        for token, category in self._tokens.items():
            if token in name:
                destination = str(Path(default_destination) / category) if default_destination else ""
                return MLSuggestion(category, destination, 0.78, f"Local filename signal matched “{token}”.", "local_heuristic", False)
        previous = self._prior_destination(metadata, prior)
        if previous:
            return MLSuggestion(
                Path(previous).name, previous, 0.70,
                "Suggested from prior local destinations for this extension and media type.", "local_history", False,
            )
        return MLSuggestion(
            metadata.media_type.title() or "Other",
            str(Path(default_destination) / (metadata.media_type.title() or "Other")) if default_destination else "",
            0.40,
            "No trained local classifier is installed; this low-confidence media-type fallback requires review.",
            "local_fallback", False,
        )

    def record_correction(self, metadata: FileMetadata, suggestion: MLSuggestion | None, category: str, destination: str) -> MLFeedback:
        feedback = self.list_feedback()
        record = MLFeedback(
            file_name=metadata.name, extension=metadata.extension, source_parent=metadata.source_path,
            previous_category=suggestion.category if suggestion else "", corrected_category=category,
            corrected_destination=destination, created_at=datetime.now(timezone.utc).isoformat(),
        )
        feedback.append(record)
        self._write_feedback(feedback)
        return record

    def list_feedback(self) -> list[MLFeedback]:
        try:
            payload = json.loads(self.feedback_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
        return [MLFeedback(**item) for item in payload.get("feedback", []) if isinstance(item, dict)]

    def _matching_feedback(self, metadata: FileMetadata) -> MLFeedback | None:
        candidates = [item for item in self.list_feedback() if item.extension == metadata.extension and item.source_parent == metadata.source_path]
        return candidates[-1] if candidates else None

    @staticmethod
    def _prior_destination(metadata: FileMetadata, prior: tuple[dict, ...]) -> str:
        matches = [str(item.get("destination", "")) for item in prior if item.get("extension") == metadata.extension and item.get("media_type") == metadata.media_type]
        return matches[-1] if matches else ""

    def _write_feedback(self, values: list[MLFeedback]) -> None:
        self.feedback_path.parent.mkdir(parents=True, exist_ok=True)
        handle, temporary = tempfile.mkstemp(prefix=".ml-feedback.", suffix=".tmp", dir=self.feedback_path.parent)
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as stream:
                json.dump({"schema_version": 1, "feedback": [asdict(value) for value in values]}, stream, indent=2)
            os.replace(temporary, self.feedback_path)
        except Exception:
            try:
                os.remove(temporary)
            except OSError:
                pass
            raise
