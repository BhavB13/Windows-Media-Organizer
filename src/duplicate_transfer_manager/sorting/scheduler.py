"""Windows Task Scheduler integration for monitored sorting profiles."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys

from ..core import ErrorCode, ServiceError
from ..scheduled_tasks import build_task_command
from .models import MonitoredFolder, SortingProfile


class SortScheduleService:
    task_prefix = "DuplicateTransferManager-Sort-"

    def task_name(self, profile: SortingProfile, monitor: MonitoredFolder) -> str:
        safe = "".join(value for value in f"{profile.id}-{monitor.id}" if value.isalnum() or value in "_-")
        return f"{self.task_prefix}{safe}"[:220]

    def command(self, profile: SortingProfile, monitor: MonitoredFolder, *, data_root: str = "") -> str:
        arguments = ["--profile-id", profile.id, "--monitor-id", monitor.id]
        if data_root:
            arguments.extend(["--data-root", data_root])
        return subprocess.list2cmdline(build_task_command("sort", arguments))

    def configure(self, profile: SortingProfile, monitor: MonitoredFolder, *, data_root: str = "") -> None:
        if os.name != "nt":
            raise ServiceError(ErrorCode.VALIDATION, "Scheduled sorting is available on Windows only.")
        task_name = self.task_name(profile, monitor)
        if monitor.scan_mode != "scheduled" or monitor.schedule == "off" or not monitor.enabled:
            subprocess.run(["schtasks", "/Delete", "/F", "/TN", task_name], capture_output=True, text=True, check=False)
            return
        if monitor.schedule not in {"hourly", "daily", "weekly"}:
            raise ServiceError(ErrorCode.VALIDATION, "Choose hourly, daily, weekly, or off for scheduled sorting.")
        if not monitor.dry_run and not monitor.live_approved:
            raise ServiceError(ErrorCode.VALIDATION, "Live scheduled sorting requires explicit approval in the monitored-folder settings.")
        completed = subprocess.run(
            ["schtasks", "/Create", "/F", "/TN", task_name, "/SC", monitor.schedule.upper(), "/TR", self.command(profile, monitor, data_root=data_root)],
            capture_output=True, text=True, check=False,
        )
        if completed.returncode != 0:
            raise ServiceError(
                ErrorCode.IO_ERROR, "Windows could not save the monitored sorting task.",
                technical_detail=(completed.stderr or completed.stdout or "schtasks failed").strip(),
            )
