"""QtCore imports with a minimal asynchronous fallback for headless tests."""

from __future__ import annotations

import threading
from concurrent.futures import Future, ThreadPoolExecutor, wait
from typing import Any


try:
    from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal, Slot

    PYSIDE_AVAILABLE = True
except ImportError:  # pragma: no cover - production installs PySide6
    PYSIDE_AVAILABLE = False

    class _BoundSignal:
        def __init__(self) -> None:
            self._callbacks: list[Any] = []
            self._lock = threading.Lock()

        def connect(self, callback: Any) -> None:
            with self._lock:
                self._callbacks.append(callback)

        def emit(self, *args: Any) -> None:
            with self._lock:
                callbacks = tuple(self._callbacks)
            for callback in callbacks:
                callback(*args)

    class Signal:
        def __init__(self, *_types: Any) -> None:
            self._name = ""

        def __set_name__(self, _owner: type, name: str) -> None:
            self._name = f"__signal_{name}"

        def __get__(self, instance: Any, _owner: type | None = None) -> Any:
            if instance is None:
                return self
            signal = instance.__dict__.get(self._name)
            if signal is None:
                signal = _BoundSignal()
                instance.__dict__[self._name] = signal
            return signal

    class QObject:
        def __init__(self, _parent: Any = None) -> None:
            self._parent = _parent

    class QRunnable:
        def run(self) -> None:
            raise NotImplementedError

    class QThreadPool:
        _global: "QThreadPool | None" = None

        def __init__(self) -> None:
            self._executor = ThreadPoolExecutor(thread_name_prefix="dtm-qt-fallback")
            self._futures: set[Future[Any]] = set()
            self._lock = threading.Lock()

        @classmethod
        def globalInstance(cls) -> "QThreadPool":
            if cls._global is None:
                cls._global = cls()
            return cls._global

        def start(self, runnable: QRunnable) -> None:
            future = self._executor.submit(runnable.run)
            with self._lock:
                self._futures.add(future)
            future.add_done_callback(self._discard)

        def _discard(self, future: Future[Any]) -> None:
            with self._lock:
                self._futures.discard(future)

        def waitForDone(self, milliseconds: int = -1) -> bool:
            with self._lock:
                futures = tuple(self._futures)
            timeout = None if milliseconds < 0 else milliseconds / 1000
            _done, pending = wait(futures, timeout=timeout)
            return not pending

    def Slot(*_types: Any, **_kwargs: Any):
        def decorator(function: Any) -> Any:
            return function

        return decorator
