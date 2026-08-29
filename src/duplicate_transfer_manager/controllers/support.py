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

    def browse(self, root: str, serial: str) -> bool:
        """List one level of device folders without blocking the window.

        adb shells out to a subprocess. On a slow, locked or unauthorised phone
        that call takes seconds, and it used to run on the Qt main thread.
        """

        return self.run_loader(
            lambda: self.service.list_folders(root, serial),
            data_key="folders",
            message="Reading folders on the device…",
        )


class UpdateController(DataController):
    """Deliberately not wired into the UI yet.

    UpdateService can fetch and verify a signed manifest, but no release source
    is configured (UpdateService.RELEASE_MANIFEST_URL is empty), so there is
    nothing for this controller to check. It is kept, with its tests, so the
    verified-install path is ready the moment a release channel exists. Wire it
    in the same change that sets that URL -- not before, or the UI starts
    promising a check that cannot happen.
    """

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
