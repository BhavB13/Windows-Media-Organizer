"""UI-independent application services."""

from .duplicate_service import DuplicateScanService
from .duplicate_workflow import (
    DuplicateGroup,
    DuplicateItem,
    DuplicateQuarantineService,
    DuplicateReview,
    QuarantineResult,
    RestoreResult,
    build_duplicate_review,
    duplicate_review_to_dict,
    format_duplicate_size,
)
from .import_workflow import (
    STAGE_LABELS,
    TRANSFER_PROFILES,
    ImportReview,
    build_import_review,
    build_import_settings,
    classify_transfer_stage,
    format_transfer_bytes,
    selected_extensions,
    summarize_transfer_result,
)
from .ios_transfer import IOSDevice, IOSDeviceProvider, IOSImportAdapter, IOSTransferService
from .support_services import (
    CrashReportService,
    DashboardService,
    DeviceService,
    DiagnosticsService,
    OperationRecordService,
    QuarantineService,
    ReportService,
    SettingsService,
    ScheduledScanService,
    UpdateService,
)
from .transfer_service import TransferService
from .organizer_service import FileOrganizerService

__all__ = [
    "DeviceService",
    "CrashReportService",
    "DashboardService",
    "DiagnosticsService",
    "DuplicateScanService",
    "DuplicateGroup",
    "DuplicateItem",
    "DuplicateQuarantineService",
    "DuplicateReview",
    "ImportReview",
    "IOSDevice",
    "IOSDeviceProvider",
    "IOSImportAdapter",
    "IOSTransferService",
    "QuarantineService",
    "OperationRecordService",
    "QuarantineResult",
    "ReportService",
    "RestoreResult",
    "SettingsService",
    "ScheduledScanService",
    "TransferService",
    "FileOrganizerService",
    "STAGE_LABELS",
    "TRANSFER_PROFILES",
    "UpdateService",
    "build_duplicate_review",
    "build_import_review",
    "build_import_settings",
    "classify_transfer_stage",
    "duplicate_review_to_dict",
    "format_duplicate_size",
    "format_transfer_bytes",
    "selected_extensions",
    "summarize_transfer_result",
]
