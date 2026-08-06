"""Framework-neutral hybrid file sorting engine."""

from .metadata import MetadataExtractor, SortScanner
from .executor import SortExecutionControl, SortExecutor
from .ml import LocalMLService, MLFeedback
from .monitor import SortMonitorService
from .migration import SortingMigrationService
from .models import (
    Association,
    ConflictPolicy,
    ConditionField,
    ConditionOperator,
    FileMetadata,
    MatchMode,
    MonitoredFolder,
    MLSuggestion,
    SortAction,
    SortCondition,
    SortExecutionResult,
    SortPlan,
    SortPlanItem,
    SortingProfile,
)
from .persistence import SortingProfileStore
from .planner import SortPlanner
from .presets import CATEGORY_BY_KEY, DEFAULT_SELECTED_CATEGORIES, DEFAULT_SORT_CATEGORIES, QuickSortOptions, SortCategory, build_quick_profile, parse_extensions
from .rules import AssociationMatch, RuleEvaluator
from .scheduler import SortScheduleService
from .session import SortSetup, SortWorkflowSession, SortWorkflowStage
from .workflow import HybridSortService

__all__ = [
    "Association",
    "AssociationMatch",
    "ConflictPolicy",
    "ConditionField",
    "ConditionOperator",
    "FileMetadata",
    "LocalMLService",
    "HybridSortService",
    "MatchMode",
    "MetadataExtractor",
    "MLFeedback",
    "MLSuggestion",
    "MonitoredFolder",
    "RuleEvaluator",
    "SortAction",
    "SortCondition",
    "SortExecutionControl",
    "SortExecutionResult",
    "SortExecutor",
    "SortPlan",
    "SortPlanItem",
    "SortPlanner",
    "SortCategory",
    "QuickSortOptions",
    "DEFAULT_SORT_CATEGORIES",
    "DEFAULT_SELECTED_CATEGORIES",
    "CATEGORY_BY_KEY",
    "build_quick_profile",
    "parse_extensions",
    "SortScanner",
    "SortScheduleService",
    "SortSetup",
    "SortWorkflowSession",
    "SortWorkflowStage",
    "SortMonitorService",
    "SortingMigrationService",
    "SortingProfile",
    "SortingProfileStore",
]
