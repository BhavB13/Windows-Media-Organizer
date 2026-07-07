"""PySide6 user interface for Duplicate & Transfer Manager."""

from .app import create_application, run
from .shell import MainWindow

__all__ = ["MainWindow", "create_application", "run"]
