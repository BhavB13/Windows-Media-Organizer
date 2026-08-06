"""Automated tests for Duplicate & Transfer Manager."""
"""Make source-layout package imports deterministic for isolated unittest runs."""

from pathlib import Path
import sys


SOURCE_ROOT = Path(__file__).resolve().parents[1] / "src"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))
