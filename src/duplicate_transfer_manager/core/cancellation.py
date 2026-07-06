"""Cooperative cancellation primitives shared by every operation."""

from __future__ import annotations

import threading


class OperationCancelled(RuntimeError):
    """Raised when an operation reaches a cooperative cancellation point."""


class CancellationToken:
    """Thread-safe cancellation token compatible with ``threading.Event`` APIs."""

    def __init__(self) -> None:
        self._event = threading.Event()

    def cancel(self) -> None:
        self._event.set()

    def is_cancelled(self) -> bool:
        return self._event.is_set()

    def is_set(self) -> bool:
        """Compatibility method for the existing engine."""

        return self._event.is_set()

    def wait(self, timeout: float | None = None) -> bool:
        return self._event.wait(timeout)

    def raise_if_cancelled(self) -> None:
        if self.is_cancelled():
            raise OperationCancelled("The operation was cancelled.")
