"""Structured error conversion for service and controller boundaries."""

from __future__ import annotations

import traceback
from dataclasses import dataclass
from enum import Enum

from .cancellation import OperationCancelled


class ErrorCode(str, Enum):
    VALIDATION = "validation_error"
    CANCELLED = "cancelled"
    DEVICE_UNAVAILABLE = "device_unavailable"
    PERMISSION_DENIED = "permission_denied"
    NOT_FOUND = "not_found"
    IO_ERROR = "io_error"
    INVALID_DATA = "invalid_data"
    OPERATION_IN_PROGRESS = "operation_in_progress"
    INTERNAL = "internal_error"


@dataclass(frozen=True)
class StructuredError:
    code: ErrorCode
    title: str
    message: str
    technical_detail: str
    recoverable: bool = False
    suggested_action: str = ""


class ServiceError(RuntimeError):
    """Service exception carrying a safe user-facing message."""

    def __init__(
        self,
        code: ErrorCode,
        message: str,
        *,
        title: str = "Operation could not continue",
        recoverable: bool = True,
        suggested_action: str = "",
        technical_detail: str = "",
    ) -> None:
        super().__init__(message)
        self.error = StructuredError(
            code=code,
            title=title,
            message=message,
            technical_detail=technical_detail or message,
            recoverable=recoverable,
            suggested_action=suggested_action,
        )


def map_exception(exc: BaseException) -> StructuredError:
    if isinstance(exc, ServiceError):
        return exc.error
    if isinstance(exc, OperationCancelled):
        return StructuredError(
            ErrorCode.CANCELLED,
            "Operation cancelled",
            "The operation was cancelled safely.",
            str(exc),
            recoverable=True,
        )
    if getattr(exc, "device_unavailable", False):
        return StructuredError(
            ErrorCode.DEVICE_UNAVAILABLE,
            "Device unavailable",
            "The Android device disconnected or is no longer authorized.",
            "".join(traceback.format_exception(type(exc), exc, exc.__traceback__)),
            recoverable=True,
            suggested_action="Unlock the device, reconnect it, and authorize USB debugging.",
        )
    if isinstance(exc, PermissionError):
        return StructuredError(
            ErrorCode.PERMISSION_DENIED,
            "Access denied",
            "Windows denied access to a required file or folder.",
            "".join(traceback.format_exception(type(exc), exc, exc.__traceback__)),
            recoverable=True,
            suggested_action="Choose a writable location or adjust its permissions.",
        )
    if isinstance(exc, FileNotFoundError):
        return StructuredError(
            ErrorCode.NOT_FOUND,
            "Location unavailable",
            "A required file, folder, or device could not be found.",
            "".join(traceback.format_exception(type(exc), exc, exc.__traceback__)),
            recoverable=True,
            suggested_action="Reconnect the drive or choose the location again.",
        )
    if isinstance(exc, OSError):
        return StructuredError(
            ErrorCode.IO_ERROR,
            "Storage operation failed",
            "A file or storage operation could not be completed.",
            "".join(traceback.format_exception(type(exc), exc, exc.__traceback__)),
            recoverable=True,
            suggested_action="Check the destination, available space, and permissions.",
        )
    return StructuredError(
        ErrorCode.INTERNAL,
        "Unexpected error",
        "Duplicate & Transfer Manager encountered an unexpected error.",
        "".join(traceback.format_exception(type(exc), exc, exc.__traceback__)),
        recoverable=False,
        suggested_action="Review the activity log and try again.",
    )
