"""Controllers for duplicate discovery and smart transfers."""

from __future__ import annotations

from typing import Any

from ..services import DuplicateScanService, TransferService
from .base import BaseOperationController
from .qt_compat import QObject, QThreadPool


class DuplicateScanController(BaseOperationController):
    def __init__(
        self,
        hash_cache: Any,
        thread_pool: QThreadPool | None = None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(thread_pool, parent)
        self.service = DuplicateScanService(hash_cache)

    def start(self, settings: Any) -> bool:
        return self.start_task(
            lambda cancellation, reporter: self.service.run(
                settings,
                cancellation,
                reporter,
            )
        )


class TransferController(BaseOperationController):
    def __init__(
        self,
        hash_cache: Any,
        thread_pool: QThreadPool | None = None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(thread_pool, parent)
        self.service = TransferService(hash_cache)

    def start(self, settings: Any) -> bool:
        return self.start_task(
            lambda cancellation, reporter: self.service.run(
                settings,
                cancellation,
                reporter,
            )
        )
