"""Qt controllers for devices, reports, settings, diagnostics, and updates."""

from __future__ import annotations

import time
from typing import Any

from ..core import (
    AppSettings,
    OperationPhase,
    OperationResult,
    OperationState,
)
from ..services import (
    DeviceService,
    DiagnosticsService,
    QuarantineService,
    ReportService,
    SettingsService,
    UpdateService,
)
from .base import BaseOperationController
from .qt_compat import QObject, QThreadPool


class DataController(BaseOperationController):
    def run_loader(
        self,
        loader: Any,
        *,
        data_key: str,
        message: str,
    ) -> bool:
        def task(cancellation, reporter):
            started = time.monotonic()
            cancellation.raise_if_cancelled()
            reporter.set_state(
                OperationState.SCANNING,
                phase=OperationPhase.MAINTENANCE,
                message=message,
            )
            data = loader()
            cancellation.raise_if_cancelled()
            reporter.set_state(
                OperationState.COMPLETED,
                phase=OperationPhase.FINALIZATION,
                message="Ready.",
            )
            return OperationResult(
                status=OperationState.COMPLETED,
                duration_seconds=time.monotonic() - started,
                data={data_key: data},
            )

        return self.start_task(task)


class DeviceController(DataController):
    def __init__(
        self,
        thread_pool: QThreadPool | None = None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(thread_pool, parent)
        self.service = DeviceService()

    def refresh(self) -> bool:
        return self.run_loader(
            self.service.list_devices,
            data_key="devices",
            message="Checking connected Android devices…",
        )

    def inspect(self, serial: str) -> bool:
        return self.run_loader(
            lambda: self.service.inspect(serial),
            data_key="device",
            message="Reading Android device information…",
        )


class ReportsController(DataController):
    def __init__(
        self,
        service: ReportService | None = None,
        thread_pool: QThreadPool | None = None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(thread_pool, parent)
        self.service = service or ReportService()

    def refresh(self) -> bool:
        return self.run_loader(
            self.service.list_reports,
            data_key="reports",
            message="Loading transfer reports…",
        )

    def load(self, path: str) -> bool:
        return self.run_loader(
            lambda: self.service.load_report(path),
            data_key="report",
            message="Loading transfer report…",
        )


class QuarantineController(DataController):
    def __init__(
        self,
        service: QuarantineService | None = None,
        thread_pool: QThreadPool | None = None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(thread_pool, parent)
        self.service = service or QuarantineService()

    def refresh(self) -> bool:
        return self.run_loader(
            self.service.list_records,
            data_key="records",
            message="Loading quarantine records…",
        )


class SettingsController(DataController):
    def __init__(
        self,
        service: SettingsService | None = None,
        thread_pool: QThreadPool | None = None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(thread_pool, parent)
        self.service = service or SettingsService()

    def load(self) -> bool:
        return self.run_loader(
            self.service.load,
            data_key="settings",
            message="Loading settings…",
        )

    def save(self, settings: AppSettings) -> bool:
        return self.run_loader(
            lambda: self.service.save(settings),
            data_key="settings",
            message="Saving settings…",
        )


class DiagnosticsController(DataController):
    def __init__(
        self,
        service: DiagnosticsService | None = None,
        thread_pool: QThreadPool | None = None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(thread_pool, parent)
        self.service = service or DiagnosticsService()

    def collect(self, include_devices: bool = True) -> bool:
        return self.run_loader(
            lambda: self.service.collect(include_devices),
            data_key="diagnostics",
            message="Collecting local diagnostics…",
        )


class UpdateController(DataController):
    def __init__(
        self,
        service: UpdateService | None = None,
        thread_pool: QThreadPool | None = None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(thread_pool, parent)
        self.service = service or UpdateService()

    def check(self) -> bool:
        return self.run_loader(
            self.service.status,
            data_key="update",
            message="Checking update configuration…",
        )
