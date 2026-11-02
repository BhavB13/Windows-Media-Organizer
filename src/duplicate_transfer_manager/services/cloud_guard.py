"""Warn before a scan or import downloads files that live only in the cloud.

OneDrive, Dropbox, Google Drive and iCloud present cloud-only files at their
full size while the bytes stay on the provider's servers, and reading one
downloads it. This app identifies files by hashing their contents, so pointing
a duplicate scan or an import at such a folder would quietly download the whole
library over the user's connection and fill their disk with it.

The check reads file attributes only, through ``core.storage_class``. It never
opens a file, because opening is what triggers the download.

It is advisory and bounded. A failure inside the check never blocks the scan it
guards, and the walk stops after a time budget so it cannot hold up a scan on a
very large tree; a check that stopped early says "at least".
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

from ..core import storage_class

DEFAULT_TIME_BUDGET_SECONDS = 10.0

CHOICE_SKIP = "skip"
CHOICE_DOWNLOAD = "download"
CHOICE_CANCEL = "cancel"


@dataclass(frozen=True)
class CloudDownloadAssessment:
    would_download: bool = False
    files: int = 0
    bytes: int = 0
    providers: tuple[str, ...] = ()
    complete: bool = True
    cancelled: bool = False
    error: str = ""
    message: str = ""
    roots: tuple[str, ...] = field(default_factory=tuple)


class _BudgetedStop:
    """A stop event that also trips when a time budget runs out."""

    def __init__(self, outer: Any, budget_seconds: float, clock: Callable[[], float]) -> None:
        self._outer = outer
        self._clock = clock
        self._deadline = clock() + max(0.0, budget_seconds)
        self.expired = False

    def is_set(self) -> bool:
        if self._outer is not None and self._outer.is_set():
            return True
        if self._clock() >= self._deadline:
            self.expired = True
            return True
        return False


def format_bytes(value: int) -> str:
    size = float(max(0, int(value)))
    for unit in ("bytes", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{int(size)} bytes" if unit == "bytes" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


def _provider_phrase(providers: tuple[str, ...]) -> str:
    known = [name for name in providers if name and name != storage_class.UNKNOWN_PROVIDER]
    if not known:
        return "a cloud storage service"
    if len(known) == 1:
        return known[0]
    return ", ".join(known[:-1]) + f" and {known[-1]}"


def _message(files: int, total_bytes: int, providers: tuple[str, ...], complete: bool) -> str:
    count = f"{'At least ' if not complete else ''}{files:,} file{'s' if files != 1 else ''}"
    verb = "is" if files == 1 else "are"
    return (
        f"{count} ({format_bytes(total_bytes)}) in this location {verb} stored in "
        f"{_provider_phrase(providers)} and not on this PC. Reading them will download them, "
        "which uses your internet connection and fills this drive."
    )


def assess_cloud_downloads(
    roots: Iterable[str],
    *,
    is_adb: bool = False,
    stop_event: Any = None,
    time_budget_seconds: float = DEFAULT_TIME_BUDGET_SECONDS,
    survey: Callable[..., Any] | None = None,
    clock: Callable[[], float] = time.monotonic,
    platform_is_windows: bool | None = None,
) -> CloudDownloadAssessment:
    """Report whether reading ``roots`` would download cloud-only files."""

    root_list = tuple(str(root) for root in roots if str(root or "").strip())
    windows = (os.name == "nt") if platform_is_windows is None else platform_is_windows
    # Android sources are read over ADB, and placeholders are a Windows feature.
    if is_adb or not windows or not root_list:
        return CloudDownloadAssessment(roots=root_list)

    survey = survey or storage_class.survey_tree
    files = total = 0
    providers: list[str] = []
    complete = True
    try:
        for root in root_list:
            stop = _BudgetedStop(stop_event, time_budget_seconds, clock)
            result = survey(root, stop_event=stop)
            files += int(getattr(result, "recall_files", 0) or 0)
            total += int(getattr(result, "recall_bytes", 0) or 0)
            for name in getattr(result, "providers", ()) or ():
                if name not in providers:
                    providers.append(name)
            if getattr(result, "cancelled", False):
                if stop_event is not None and stop_event.is_set():
                    return CloudDownloadAssessment(cancelled=True, roots=root_list)
                complete = False
                break
    except Exception as exc:  # noqa: BLE001 - the guard must never block the scan it guards
        return CloudDownloadAssessment(error=str(exc), roots=root_list)

    provider_tuple = tuple(providers)
    if files <= 0:
        return CloudDownloadAssessment(complete=complete, providers=provider_tuple, roots=root_list)
    return CloudDownloadAssessment(
        would_download=True,
        files=files,
        bytes=total,
        providers=provider_tuple,
        complete=complete,
        message=_message(files, total, provider_tuple, complete),
        roots=root_list,
    )
