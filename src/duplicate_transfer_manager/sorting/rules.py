"""Deterministic, priority-ordered association evaluation."""

from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from ..core import ErrorCode, ServiceError
from .models import Association, ConditionField, ConditionOperator, FileMetadata, MatchMode


@dataclass(frozen=True)
class AssociationMatch:
    association: Association | None
    ambiguous: tuple[Association, ...] = ()
    explanation: str = "No deterministic association matched."


class RuleEvaluator:
    """Evaluate user rules before any ML classifier is consulted."""

    def evaluate(self, metadata: FileMetadata, associations: tuple[Association, ...] | list[Association]) -> AssociationMatch:
        matches = [association for association in associations if association.enabled and self.matches(metadata, association)]
        if not matches:
            return AssociationMatch(None)
        matches.sort(key=lambda association: (-association.priority, association.name.casefold(), association.id))
        top_priority = matches[0].priority
        top = tuple(association for association in matches if association.priority == top_priority)
        if len(top) > 1:
            names = ", ".join(association.name for association in top)
            return AssociationMatch(None, top, f"Multiple priority-{top_priority} associations matched: {names}.")
        return AssociationMatch(top[0], (), f"Matched user association “{top[0].name}” at priority {top_priority}.")

    def matches(self, metadata: FileMetadata, association: Association) -> bool:
        include = [condition for condition in association.conditions if not condition.exclude]
        exclusions = [condition for condition in association.conditions if condition.exclude]
        if any(self._condition(metadata, condition) for condition in exclusions):
            return False
        if not include:
            return True
        results = [self._condition(metadata, condition) for condition in include]
        return all(results) if association.match_mode == MatchMode.ALL else any(results)

    def _condition(self, metadata: FileMetadata, condition) -> bool:
        actual = self._field_value(metadata, condition.field)
        expected = condition.value
        operator = condition.operator
        if isinstance(actual, str):
            actual_text = actual if condition.case_sensitive else actual.casefold()
            if isinstance(expected, str):
                expected_value: Any = expected if condition.case_sensitive else expected.casefold()
            else:
                expected_value = expected
            if operator == ConditionOperator.EQUALS:
                return actual_text == expected_value
            if operator == ConditionOperator.NOT_EQUALS:
                return actual_text != expected_value
            if operator == ConditionOperator.CONTAINS:
                return str(expected_value) in actual_text
            if operator == ConditionOperator.STARTS_WITH:
                return actual_text.startswith(str(expected_value))
            if operator == ConditionOperator.ENDS_WITH:
                return actual_text.endswith(str(expected_value))
            if operator == ConditionOperator.GLOB:
                return fnmatch.fnmatchcase(actual_text, str(expected_value))
            if operator == ConditionOperator.REGEX:
                try:
                    flags = 0 if condition.case_sensitive else re.IGNORECASE
                    return re.search(str(expected), actual, flags) is not None
                except re.error as exc:
                    raise ServiceError(ErrorCode.VALIDATION, "An association contains an invalid regular expression.", technical_detail=str(exc)) from exc
            if operator == ConditionOperator.IN:
                values = expected if isinstance(expected, (list, tuple, set)) else str(expected).split(",")
                normalized = [str(value) if condition.case_sensitive else str(value).casefold().strip() for value in values]
                return actual_text in normalized
        if operator in {ConditionOperator.BEFORE, ConditionOperator.AFTER, ConditionOperator.BETWEEN}:
            return self._date_compare(float(actual or 0), operator, expected)
        try:
            actual_number = float(actual)
            if operator == ConditionOperator.GREATER_THAN:
                return actual_number > float(expected)
            if operator == ConditionOperator.GREATER_OR_EQUAL:
                return actual_number >= float(expected)
            if operator == ConditionOperator.LESS_THAN:
                return actual_number < float(expected)
            if operator == ConditionOperator.LESS_OR_EQUAL:
                return actual_number <= float(expected)
            if operator == ConditionOperator.BETWEEN:
                low, high = expected
                return float(low) <= actual_number <= float(high)
            if operator == ConditionOperator.EQUALS:
                return actual_number == float(expected)
            if operator == ConditionOperator.NOT_EQUALS:
                return actual_number != float(expected)
        except (TypeError, ValueError):
            return False
        return False

    @staticmethod
    def _field_value(metadata: FileMetadata, field: ConditionField) -> Any:
        values = {
            ConditionField.EXTENSION: metadata.extension,
            ConditionField.FILENAME: metadata.name,
            ConditionField.SOURCE_PATH: metadata.source_path,
            ConditionField.SIZE: metadata.size,
            ConditionField.CREATED: metadata.created,
            ConditionField.MODIFIED: metadata.modified,
            ConditionField.MEDIA_TYPE: metadata.media_type,
            ConditionField.WIDTH: metadata.width,
            ConditionField.HEIGHT: metadata.height,
            ConditionField.DURATION: metadata.duration,
            ConditionField.CAPTURED: metadata.captured,
        }
        return values[field]

    @staticmethod
    def _date_compare(actual: float, operator: ConditionOperator, expected: Any) -> bool:
        def timestamp(value: Any) -> float:
            if isinstance(value, (int, float)):
                return float(value)
            return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()

        try:
            if operator == ConditionOperator.BEFORE:
                return actual < timestamp(expected)
            if operator == ConditionOperator.AFTER:
                return actual > timestamp(expected)
            low, high = expected
            return timestamp(low) <= actual <= timestamp(high)
        except (TypeError, ValueError):
            return False
