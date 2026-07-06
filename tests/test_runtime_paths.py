import json
import os
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest.mock import patch

from runtime_paths import APP_DIRECTORY_NAME, get_runtime_paths, migrate_legacy_data
from duplicate_transfer_manager import __version__
from drive_cache import default_adb_cache_path, default_cache_path
from transfer_safety import default_journal_path, write_transfer_report
from utils import SessionLogger


class RuntimePathTests(unittest.TestCase):
    def test_package_and_project_versions_match(self):
        project_file = Path(__file__).resolve().parents[1] / "pyproject.toml"
        metadata = tomllib.loads(project_file.read_text(encoding="utf-8"))
        self.assertEqual(metadata["project"]["version"], __version__)

    def test_windows_default_uses_local_app_data(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            from duplicate_transfer_manager.runtime_paths import _default_data_root

            with patch.dict(
                os.environ,
                {"LOCALAPPDATA": temp_dir, "DTM_DATA_DIR": ""},
                clear=False,
            ):
                root = _default_data_root("nt")

            self.assertEqual(root, Path(temp_dir) / APP_DIRECTORY_NAME)

    def test_override_creates_all_runtime_directories(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "custom"
            with patch.dict(os.environ, {"DTM_DATA_DIR": str(root)}, clear=False):
                paths = get_runtime_paths()

            for directory in (
                paths.cache,
                paths.drive_caches,
                paths.reports,
                paths.journals,
                paths.quarantine,
                paths.logs,
                paths.updates,
            ):
                self.assertTrue(directory.is_dir())

    def test_legacy_migration_copies_once_without_removing_sources(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            legacy = base / "legacy"
            data = base / "data"
            (legacy / "drive_caches").mkdir(parents=True)
            (legacy / "transfer_reports").mkdir()
            (legacy / "transfer_state").mkdir()
            (legacy / "hash_cache.json").write_text('{"cached": true}', encoding="utf-8")
            (legacy / "drive_caches" / "drive.json").write_text("{}", encoding="utf-8")
            (legacy / "transfer_reports" / "report.json").write_text("{}", encoding="utf-8")
            (legacy / "transfer_state" / "journal.json").write_text("{}", encoding="utf-8")

            paths = get_runtime_paths(data)
            result = migrate_legacy_data(legacy, paths)

            self.assertFalse(result.already_completed)
            self.assertEqual(len(result.copied), 4)
            self.assertTrue((legacy / "hash_cache.json").exists())
            self.assertEqual(paths.hash_cache.read_text(encoding="utf-8"), '{"cached": true}')
            self.assertTrue((paths.drive_caches / "drive.json").exists())
            self.assertTrue((paths.reports / "report.json").exists())
            self.assertTrue((paths.journals / "journal.json").exists())

            marker = json.loads(paths.migration_marker.read_text(encoding="utf-8"))
            self.assertEqual(marker["version"], 1)
            second_result = migrate_legacy_data(legacy, paths)
            self.assertTrue(second_result.already_completed)

    def test_existing_destination_is_not_overwritten(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            legacy = base / "legacy"
            legacy.mkdir()
            (legacy / "hash_cache.json").write_text('{"legacy": true}', encoding="utf-8")
            paths = get_runtime_paths(base / "data")
            paths.hash_cache.write_text('{"current": true}', encoding="utf-8")

            result = migrate_legacy_data(legacy, paths)

            self.assertEqual(paths.hash_cache.read_text(encoding="utf-8"), '{"current": true}')
            self.assertEqual(result.skipped, (str(legacy / "hash_cache.json"),))

    def test_runtime_writers_use_the_application_data_root(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "data"
            with patch.dict(os.environ, {"DTM_DATA_DIR": str(root)}, clear=False):
                paths = get_runtime_paths()
                drive_cache = Path(default_cache_path("C:\\Pictures"))
                adb_cache = Path(default_adb_cache_path("device-123"))
                journal = Path(default_journal_path("C:\\Library"))
                report = Path(write_transfer_report("C:\\Library", {"transferred": 0}, []))
                logger = SessionLogger(None)
                logger.log("runtime path test")

            self.assertEqual(drive_cache.parent, paths.drive_caches)
            self.assertEqual(adb_cache.parent, paths.drive_caches)
            self.assertEqual(journal.parent, paths.journals)
            self.assertEqual(report.parent, paths.reports)
            self.assertEqual(Path(logger.log_file).parent, paths.logs)
            self.assertTrue(Path(logger.log_file).exists())


if __name__ == "__main__":
    unittest.main()
