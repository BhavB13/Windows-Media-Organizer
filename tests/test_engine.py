import os
import hashlib
import tempfile
import threading
import unittest
from unittest import mock

import engine as engine_module
from adb_bridge import ADBBridge
from discovery import (
    _build_remote_find_script,
    _build_remote_walk_script,
    _canonical_adb_walk_root,
    _display_adb_path,
    discover_files,
)
from drive_cache import ADBHashCache, DriveHashCache, build_drive_cache
from engine import (
    build_compare_scan_settings,
    build_transfer_hash_settings,
    build_relative_path,
    build_target_path,
    execute_smart_transfer,
    iter_files,
    validate_transfer_paths,
    validate_scan_paths,
)
from models import FileInfo, Settings, TransferSettings
from utils import HashCache


class DummyLogger:
    def __init__(self):
        self.messages = []

    def log(self, msg):
        self.messages.append(msg)


class EngineTests(unittest.TestCase):
    def test_adb_cache_is_device_scoped_and_normalizes_storage_aliases(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            cache = ADBHashCache(os.path.join(temp_dir, "adb.json"), "phone-123")
            cache.set_file_hash(
                "/storage/emulated/0/DCIM/Camera/photo.jpg",
                4,
                123.0,
                "digest",
                "sha256",
                "full",
                root="/sdcard/DCIM",
            )

            self.assertEqual(
                cache.get_valid_hash("/sdcard/DCIM/Camera/photo.jpg", "sha256", "full", 4, 123.0),
                "digest",
            )
            self.assertEqual(cache.active_file_count_under_root("/sdcard/DCIM", "sha256", "full"), 1)

    def test_adb_path_shorthand_normalizes_to_sdcard_common_dirs(self):
        self.assertEqual(ADBBridge.normalize_remote_path("/sd/DCIM"), "/sdcard/DCIM")
        self.assertEqual(ADBBridge.normalize_remote_path("/sd/pictures"), "/sdcard/Pictures")
        self.assertEqual(ADBBridge.normalize_remote_path("storage/self/primary/downloads"), "/sdcard/Download")

    def test_adb_walk_root_uses_canonical_storage_without_changing_display_path(self):
        walk_root = _canonical_adb_walk_root("/sdcard/DCIM")

        self.assertEqual(walk_root, "/storage/emulated/0/DCIM")
        self.assertEqual(
            _display_adb_path("/storage/emulated/0/DCIM/Camera/photo.jpg", walk_root, "/sdcard/DCIM"),
            "/sdcard/DCIM/Camera/photo.jpg",
        )

    def test_build_target_path_avoids_name_collisions(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            source_root = os.path.join(temp_dir, "source")
            dest_root = os.path.join(temp_dir, "dest")
            os.makedirs(source_root)
            os.makedirs(dest_root)

            existing_path = os.path.join(dest_root, "photo.jpg")
            with open(existing_path, "wb") as handle:
                handle.write(b"existing")

            target = build_target_path(
                os.path.join(source_root, "photo.jpg"),
                source_root,
                dest_root,
                preserve_structure=False,
            )

            self.assertTrue(target.endswith("photo (1).jpg"))

    def test_build_relative_path_handles_adb_style_paths(self):
        relative = build_relative_path(
            "/sdcard/DCIM/Camera/2026/clip.mp4",
            "/sdcard/DCIM/Camera",
            is_adb=True,
        )
        self.assertEqual(relative, "2026/clip.mp4")

    def test_validate_transfer_paths_rejects_nested_destination(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            source_root = os.path.join(temp_dir, "source")
            dest_root = os.path.join(source_root, "dest")
            os.makedirs(dest_root)

            settings = TransferSettings(
                source_root=source_root,
                dest_root=dest_root,
                output_root="",
                criteria="hash",
                hash_algo="sha256",
                hash_mode="full",
                only_media=True,
                extensions=[".jpg"],
                min_size_kb=0,
                exclude_dirs=[],
                skip_hidden_system=True,
                dry_run=True,
                preserve_structure=False,
                max_hash_workers=1,
                transfer_mode="copy",
                duplicate_policy="skip",
                use_dest_cache=True,
            )

            self.assertEqual(validate_transfer_paths(settings), "Compare folder cannot be inside the source folder.")

    def test_validate_transfer_paths_handles_cross_drive_commonpath_errors(self):
        settings = TransferSettings(
            source_root="C:/source",
            dest_root="D:/compare",
            output_root="E:/output",
            criteria="hash",
            hash_algo="sha256",
            hash_mode="full",
            only_media=True,
            extensions=[".jpg"],
            min_size_kb=0,
            exclude_dirs=[],
            skip_hidden_system=True,
            dry_run=True,
            preserve_structure=False,
            max_hash_workers=1,
            transfer_mode="copy",
            duplicate_policy="skip",
            use_dest_cache=True,
        )

        with mock.patch.object(engine_module.os.path, "commonpath", side_effect=ValueError("different drives")), \
             mock.patch.object(engine_module.os.path, "isdir", return_value=True), \
             mock.patch.object(engine_module.os.path, "exists", return_value=True):
            self.assertEqual(validate_transfer_paths(settings), "")

    def test_validate_scan_paths_rejects_inaccessible_adb_folder_with_normalized_hint(self):
        settings = Settings(
            scan_root="/sd/DCIM",
            output_root="",
            criteria="hash",
            hash_algo="sha256",
            hash_mode="full",
            only_media=True,
            extensions=[".jpg"],
            min_size_kb=0,
            exclude_dirs=[],
            skip_hidden_system=True,
            dry_run=True,
            preserve_structure=True,
            max_hash_workers=1,
            use_adb=True,
            adb_serial="device-123",
        )

        with mock.patch.object(engine_module.ADBBridge, "get_device_state", return_value="device"), \
             mock.patch.object(engine_module.ADBBridge, "remote_path_status", return_value="missing"):
            error = validate_scan_paths(settings)

        self.assertIn("/sdcard/DCIM", error)
        self.assertIn("not accessible", error)

    def test_validate_transfer_paths_rejects_inaccessible_adb_source(self):
        settings = TransferSettings(
            source_root="/sd/DCIM",
            dest_root="C:/Pictures",
            output_root="",
            criteria="hash",
            hash_algo="sha256",
            hash_mode="full",
            only_media=True,
            extensions=[".jpg"],
            min_size_kb=0,
            exclude_dirs=[],
            skip_hidden_system=True,
            dry_run=True,
            preserve_structure=True,
            max_hash_workers=1,
            transfer_mode="copy",
            duplicate_policy="skip",
            use_dest_cache=True,
            source_is_adb=True,
            adb_serial="device-123",
        )

        with mock.patch.object(engine_module.os.path, "isdir", return_value=True), \
             mock.patch.object(engine_module.ADBBridge, "get_device_state", return_value="device"), \
             mock.patch.object(engine_module.ADBBridge, "remote_path_status", return_value="missing"):
            error = validate_transfer_paths(settings)

        self.assertIn("/sdcard/DCIM", error)
        self.assertIn("not accessible", error)

    def test_iter_files_normalizes_excluded_directories(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            android_dir = os.path.join(temp_dir, "Android")
            keep_dir = os.path.join(temp_dir, "Keep")
            os.makedirs(android_dir)
            os.makedirs(keep_dir)

            with open(os.path.join(android_dir, "skip.jpg"), "wb") as handle:
                handle.write(b"skip")
            with open(os.path.join(keep_dir, "keep.jpg"), "wb") as handle:
                handle.write(b"keep")

            settings = Settings(
                scan_root=temp_dir,
                output_root="",
                criteria="hash",
                hash_algo="sha256",
                hash_mode="full",
                only_media=True,
                extensions=[".jpg"],
                min_size_kb=0,
                exclude_dirs=["Android"],
                skip_hidden_system=False,
                dry_run=True,
                preserve_structure=True,
                max_hash_workers=1,
            )

            results = list(iter_files(temp_dir, settings, threading.Event()))
            self.assertEqual(len(results), 1)
            self.assertTrue(results[0].path.endswith("keep.jpg"))

    def test_discover_files_recurses_nested_local_tree(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            nested_dir = os.path.join(temp_dir, "A", "deep")
            excluded_dir = os.path.join(temp_dir, "Android")
            os.makedirs(nested_dir)
            os.makedirs(excluded_dir)

            with open(os.path.join(nested_dir, "keep.jpg"), "wb") as handle:
                handle.write(b"keep")
            with open(os.path.join(nested_dir, "ignore.txt"), "wb") as handle:
                handle.write(b"ignore")
            with open(os.path.join(excluded_dir, "skip.jpg"), "wb") as handle:
                handle.write(b"skip")

            settings = Settings(
                scan_root=temp_dir,
                output_root="",
                criteria="hash",
                hash_algo="sha256",
                hash_mode="full",
                only_media=True,
                extensions=[".jpg"],
                min_size_kb=0,
                exclude_dirs=["Android"],
                skip_hidden_system=False,
                dry_run=True,
                preserve_structure=True,
                max_hash_workers=1,
            )

            result = discover_files(temp_dir, settings, threading.Event())
            self.assertEqual(len(result.files), 1)
            self.assertTrue(result.files[0].path.endswith("keep.jpg"))
            self.assertGreaterEqual(result.folders_scanned, 3)
            self.assertEqual(result.errors, [])

    def test_discover_files_all_files_mode_includes_non_media(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            os.makedirs(os.path.join(temp_dir, "Docs"))
            with open(os.path.join(temp_dir, "Docs", "notes.txt"), "wb") as handle:
                handle.write(b"notes")

            settings = Settings(
                scan_root=temp_dir,
                output_root="",
                criteria="hash",
                hash_algo="sha256",
                hash_mode="full",
                only_media=False,
                extensions=[],
                min_size_kb=0,
                exclude_dirs=[],
                skip_hidden_system=False,
                dry_run=True,
                preserve_structure=True,
                max_hash_workers=1,
            )

            result = discover_files(temp_dir, settings, threading.Event())
            self.assertEqual(len(result.files), 1)
            self.assertTrue(result.files[0].path.endswith("notes.txt"))

    def test_remote_find_script_quotes_paths_and_prunes_excludes(self):
        settings = Settings(
            scan_root="/sdcard/My Photos",
            output_root="",
            criteria="hash",
            hash_algo="sha256",
            hash_mode="full",
            only_media=True,
            extensions=[".jpg"],
            min_size_kb=0,
            exclude_dirs=["Android", "System Volume Information"],
            skip_hidden_system=False,
            dry_run=True,
            preserve_structure=True,
            max_hash_workers=1,
        )

        script = _build_remote_find_script("/sdcard/My Photos", settings, "f")
        self.assertIn("find -L '/sdcard/My Photos'", script)
        self.assertIn("-name 'Android'", script)
        self.assertIn("-name 'System Volume Information'", script)

    def test_remote_walk_script_streams_folders_and_files(self):
        settings = Settings(
            scan_root="/sdcard/DCIM",
            output_root="",
            criteria="hash",
            hash_algo="sha256",
            hash_mode="full",
            only_media=True,
            extensions=[".jpg"],
            min_size_kb=0,
            exclude_dirs=["Android"],
            skip_hidden_system=False,
            dry_run=True,
            preserve_structure=True,
            max_hash_workers=1,
        )

        script = _build_remote_walk_script("/storage/emulated/0/DCIM", settings, follow_links=False)
        self.assertIn("find '/storage/emulated/0/DCIM'", script)
        self.assertNotIn("find -L", script)
        self.assertIn("-name 'Android'", script)
        self.assertIn("printf 'D|%s\\n'", script)
        self.assertIn("stat -c 'F|%s|%Y|%n'", script)

    def test_execute_smart_transfer_counts_skipped_duplicates_in_dry_run(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            source_root = os.path.join(temp_dir, "source")
            dest_root = os.path.join(temp_dir, "dest")
            os.makedirs(source_root)
            os.makedirs(dest_root)

            with open(os.path.join(source_root, "duplicate.jpg"), "wb") as handle:
                handle.write(b"same")
            with open(os.path.join(dest_root, "already-there.jpg"), "wb") as handle:
                handle.write(b"same")
            with open(os.path.join(source_root, "new.jpg"), "wb") as handle:
                handle.write(b"new")

            settings = TransferSettings(
                source_root=source_root,
                dest_root=dest_root,
                output_root="",
                criteria="hash",
                hash_algo="sha256",
                hash_mode="full",
                only_media=True,
                extensions=[".jpg"],
                min_size_kb=0,
                exclude_dirs=[],
                skip_hidden_system=False,
                dry_run=True,
                preserve_structure=False,
                max_hash_workers=1,
                transfer_mode="copy",
                duplicate_policy="skip",
                use_dest_cache=True,
            )

            result_logger = DummyLogger()
            result = execute_smart_transfer(
                settings,
                threading.Event(),
                HashCache(os.path.join(temp_dir, "hash_cache.json")),
                result_logger,
            )

            self.assertEqual(result["duplicates"], 1)
            self.assertEqual(result["skipped"], 1)
            self.assertEqual(result["transferred"], 1)
            self.assertEqual(sorted(os.listdir(dest_root)), ["already-there.jpg"])
            self.assertTrue(any("Would Transfer to Destination: 1" in msg for msg in result_logger.messages))

    def test_execute_smart_transfer_leaves_source_files_untouched(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            source_root = os.path.join(temp_dir, "source")
            dest_root = os.path.join(temp_dir, "dest")
            os.makedirs(source_root)
            os.makedirs(dest_root)

            source_file = os.path.join(source_root, "photo.jpg")
            with open(source_file, "wb") as handle:
                handle.write(b"original-source-data")

            before_mtime = os.path.getmtime(source_file)
            before_size = os.path.getsize(source_file)

            settings = TransferSettings(
                source_root=source_root,
                dest_root=dest_root,
                output_root="",
                criteria="hash",
                hash_algo="sha256",
                hash_mode="full",
                only_media=True,
                extensions=[".jpg"],
                min_size_kb=0,
                exclude_dirs=[],
                skip_hidden_system=False,
                dry_run=False,
                preserve_structure=False,
                max_hash_workers=1,
                transfer_mode="copy",
                duplicate_policy="skip",
                use_dest_cache=True,
            )

            result = execute_smart_transfer(
                settings,
                threading.Event(),
                HashCache(os.path.join(temp_dir, "hash_cache.json")),
                DummyLogger(),
            )

            self.assertEqual(result["transferred"], 1)
            self.assertTrue(os.path.exists(source_file))
            self.assertEqual(os.path.getsize(source_file), before_size)
            self.assertEqual(os.path.getmtime(source_file), before_mtime)
            with open(source_file, "rb") as handle:
                self.assertEqual(handle.read(), b"original-source-data")

    def test_execute_smart_transfer_can_compare_one_folder_and_copy_to_another(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            source_root = os.path.join(temp_dir, "source")
            compare_root = os.path.join(temp_dir, "compare")
            output_root = os.path.join(temp_dir, "output")
            os.makedirs(source_root)
            os.makedirs(compare_root)
            os.makedirs(output_root)

            with open(os.path.join(compare_root, "existing.jpg"), "wb") as handle:
                handle.write(b"same")
            with open(os.path.join(source_root, "duplicate.jpg"), "wb") as handle:
                handle.write(b"same")
            with open(os.path.join(source_root, "new.jpg"), "wb") as handle:
                handle.write(b"brand-new")

            settings = TransferSettings(
                source_root=source_root,
                dest_root=compare_root,
                output_root=output_root,
                criteria="hash",
                hash_algo="sha256",
                hash_mode="full",
                only_media=True,
                extensions=[".jpg"],
                min_size_kb=0,
                exclude_dirs=[],
                skip_hidden_system=False,
                dry_run=False,
                preserve_structure=False,
                max_hash_workers=1,
                transfer_mode="copy",
                duplicate_policy="skip",
                use_dest_cache=True,
            )

            result = execute_smart_transfer(
                settings,
                threading.Event(),
                HashCache(os.path.join(temp_dir, "hash_cache.json")),
                DummyLogger(),
            )

            self.assertEqual(result["duplicates"], 1)
            self.assertEqual(result["transferred"], 1)
            self.assertEqual(sorted(os.listdir(compare_root)), ["existing.jpg"])
            self.assertEqual(sorted(os.listdir(output_root)), ["new.jpg"])

    def test_execute_smart_transfer_compares_nested_subfolders_and_preserves_structure(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            source_root = os.path.join(temp_dir, "source")
            compare_root = os.path.join(temp_dir, "compare")
            output_root = os.path.join(temp_dir, "output")
            os.makedirs(os.path.join(source_root, "A", "deep"))
            os.makedirs(os.path.join(source_root, "B"))
            os.makedirs(os.path.join(compare_root, "archive"))
            os.makedirs(output_root)

            with open(os.path.join(compare_root, "archive", "existing.jpg"), "wb") as handle:
                handle.write(b"same")
            with open(os.path.join(source_root, "A", "deep", "duplicate.jpg"), "wb") as handle:
                handle.write(b"same")
            with open(os.path.join(source_root, "B", "unique.jpg"), "wb") as handle:
                handle.write(b"unique")

            settings = TransferSettings(
                source_root=source_root,
                dest_root=compare_root,
                output_root=output_root,
                criteria="hash",
                hash_algo="sha256",
                hash_mode="full",
                only_media=True,
                extensions=[".jpg"],
                min_size_kb=0,
                exclude_dirs=[],
                skip_hidden_system=False,
                dry_run=False,
                preserve_structure=True,
                max_hash_workers=1,
                transfer_mode="copy",
                duplicate_policy="skip",
                use_dest_cache=True,
            )

            result_logger = DummyLogger()
            result = execute_smart_transfer(
                settings,
                threading.Event(),
                HashCache(os.path.join(temp_dir, "hash_cache.json")),
                result_logger,
            )

            self.assertEqual(result["duplicates"], 1)
            self.assertEqual(result["transferred"], 1)
            self.assertTrue(os.path.exists(os.path.join(output_root, "B", "unique.jpg")))
            self.assertFalse(os.path.exists(os.path.join(output_root, "duplicate.jpg")))
            self.assertTrue(any("Transferred Breakdown by Source Folder:" in msg for msg in result_logger.messages))

    def test_adb_transfer_compare_folder_scans_local_nested_subfolders(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            compare_root = os.path.join(temp_dir, "Pictures")
            oldpics_root = os.path.join(compare_root, "oldpics")
            os.makedirs(oldpics_root)

            nested_file = os.path.join(oldpics_root, "already-transferred.jpg")
            with open(nested_file, "wb") as handle:
                handle.write(b"same")

            settings = TransferSettings(
                source_root="/sdcard/Pictures",
                dest_root=compare_root,
                output_root="",
                criteria="hash",
                hash_algo="sha256",
                hash_mode="full",
                only_media=True,
                extensions=[".jpg"],
                min_size_kb=0,
                exclude_dirs=[],
                skip_hidden_system=False,
                dry_run=True,
                preserve_structure=True,
                max_hash_workers=1,
                transfer_mode="copy",
                duplicate_policy="skip",
                use_dest_cache=True,
                source_is_adb=True,
                adb_serial="device-123",
            )

            compare_settings = build_compare_scan_settings(settings)
            results = list(iter_files(compare_root, compare_settings, threading.Event()))

            self.assertFalse(getattr(compare_settings, "source_is_adb", False))
            self.assertFalse(getattr(compare_settings, "use_adb", False))
            self.assertEqual(len(results), 1)
            self.assertFalse(results[0].is_adb)
            self.assertEqual(results[0].path, nested_file)

    def test_adb_transfer_skips_file_already_in_nested_compare_folder(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            compare_root = os.path.join(temp_dir, "Pictures")
            output_root = os.path.join(temp_dir, "output")
            os.makedirs(os.path.join(compare_root, "oldpics"))
            os.makedirs(output_root)

            nested_file = os.path.join(compare_root, "oldpics", "already-transferred.jpg")
            with open(nested_file, "wb") as handle:
                handle.write(b"same")

            source_info = FileInfo("/sdcard/Pictures/already-transferred.jpg", 4, 1, is_adb=True)

            settings = TransferSettings(
                source_root="/sdcard/Pictures",
                dest_root=compare_root,
                output_root=output_root,
                criteria="hash",
                hash_algo="sha256",
                hash_mode="full",
                only_media=True,
                extensions=[".jpg"],
                min_size_kb=0,
                exclude_dirs=[],
                skip_hidden_system=False,
                dry_run=True,
                preserve_structure=True,
                max_hash_workers=1,
                transfer_mode="copy",
                duplicate_policy="skip",
                use_dest_cache=True,
                source_is_adb=True,
                adb_serial="device-123",
            )

            def fake_iter_files(root, scan_settings, stop_event, *args, **kwargs):
                if root == settings.source_root:
                    self.assertTrue(getattr(scan_settings, "source_is_adb", False))
                    yield source_info
                    return
                if root == compare_root:
                    self.assertFalse(getattr(scan_settings, "source_is_adb", False))
                    yield FileInfo(nested_file, 4, os.path.getmtime(nested_file), is_adb=False)

            def fake_hash(path, settings, stop_event, hash_cache=None, is_adb=False, logger=None):
                return "same-digest"

            with mock.patch.object(engine_module.ADBBridge, "get_device_state", return_value="device"), \
                 mock.patch.object(engine_module.ADBBridge, "remote_path_status", return_value="dir"), \
                 mock.patch.object(engine_module, "iter_files", side_effect=fake_iter_files), \
                 mock.patch.object(engine_module, "compute_hash", side_effect=fake_hash):
                result = execute_smart_transfer(
                    settings,
                    threading.Event(),
                    HashCache(os.path.join(temp_dir, "hash_cache.json")),
                    DummyLogger(),
                )

            self.assertEqual(result["transferred"], 0)
            self.assertEqual(result["duplicates"], 1)
            self.assertEqual(result["skipped"], 1)
            self.assertEqual(os.listdir(output_root), [])

    def test_adb_transfer_fast_mode_uses_full_hash_for_large_nested_duplicates(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            compare_root = os.path.join(temp_dir, "Pictures")
            output_root = os.path.join(temp_dir, "output")
            os.makedirs(os.path.join(compare_root, "oldpics", "vacation"))
            os.makedirs(output_root)

            payload = (b"large-photo-data-" * 140000)
            nested_file = os.path.join(compare_root, "oldpics", "vacation", "already-transferred.jpg")
            with open(nested_file, "wb") as handle:
                handle.write(payload)

            full_digest = hashlib.sha256(payload).hexdigest()
            source_info = FileInfo(
                "/sdcard/Pictures/already-transferred.jpg",
                len(payload),
                1,
                is_adb=True,
            )

            settings = TransferSettings(
                source_root="/sdcard/Pictures",
                dest_root=compare_root,
                output_root=output_root,
                criteria="hash",
                hash_algo="sha256",
                hash_mode="fast",
                only_media=True,
                extensions=[".jpg"],
                min_size_kb=0,
                exclude_dirs=[],
                skip_hidden_system=False,
                dry_run=True,
                preserve_structure=True,
                max_hash_workers=1,
                transfer_mode="copy",
                duplicate_policy="skip",
                use_dest_cache=True,
                source_is_adb=True,
                adb_serial="device-123",
            )

            def fake_iter_files(root, scan_settings, stop_event, *args, **kwargs):
                if root == settings.source_root:
                    yield source_info
                    return
                if root == compare_root:
                    yield FileInfo(nested_file, len(payload), os.path.getmtime(nested_file), is_adb=False)

            with mock.patch.object(engine_module.ADBBridge, "get_device_state", return_value="device"), \
                 mock.patch.object(engine_module.ADBBridge, "remote_path_status", return_value="dir"), \
                 mock.patch.object(engine_module.ADBBridge, "remote_hash", return_value=full_digest), \
                 mock.patch.object(engine_module, "iter_files", side_effect=fake_iter_files):
                result = execute_smart_transfer(
                    settings,
                    threading.Event(),
                    HashCache(os.path.join(temp_dir, "hash_cache.json")),
                    DummyLogger(),
                )

            self.assertEqual(build_transfer_hash_settings(settings).hash_mode, "full")
            self.assertEqual(result["transferred"], 0)
            self.assertEqual(result["duplicates"], 1)
            self.assertEqual(result["skipped"], 1)
            self.assertEqual(os.listdir(output_root), [])

    def test_build_drive_cache_indexes_hashes_recursively(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = os.path.join(temp_dir, "Pictures")
            nested = os.path.join(root, "oldpics", "trip")
            os.makedirs(nested)
            photo = os.path.join(nested, "photo.jpg")
            with open(photo, "wb") as handle:
                handle.write(b"cached")

            settings = Settings(
                scan_root=root,
                output_root="",
                criteria="hash",
                hash_algo="sha256",
                hash_mode="full",
                only_media=True,
                extensions=[".jpg"],
                min_size_kb=0,
                exclude_dirs=[],
                skip_hidden_system=False,
                dry_run=True,
                preserve_structure=True,
                max_hash_workers=1,
            )

            cache_path = os.path.join(temp_dir, "drive_cache.json")
            cache = build_drive_cache(root, cache_path, settings, threading.Event())
            stats = cache.stats()

            self.assertEqual(stats.files, 1)
            self.assertEqual(stats.stale, 0)
            self.assertTrue(cache.has_hash(hashlib.sha256(b"cached").hexdigest()))

    def test_build_drive_cache_creates_missing_cache_directory(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = os.path.join(temp_dir, "Pictures")
            os.makedirs(root)
            photo = os.path.join(root, "photo.jpg")
            with open(photo, "wb") as handle:
                handle.write(b"cached")

            settings = Settings(
                scan_root=root,
                output_root="",
                criteria="hash",
                hash_algo="sha256",
                hash_mode="full",
                only_media=True,
                extensions=[".jpg"],
                min_size_kb=0,
                exclude_dirs=[],
                skip_hidden_system=False,
                dry_run=True,
                preserve_structure=True,
                max_hash_workers=1,
            )

            cache_path = os.path.join(temp_dir, "new-cache-dir", "drive_cache.json")
            cache = build_drive_cache(root, cache_path, settings, threading.Event())

            self.assertTrue(os.path.exists(cache_path))
            self.assertEqual(cache.stats().files, 1)

    def test_missing_drive_cache_file_falls_back_to_hashing_without_saving_dry_run(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            source_root = os.path.join(temp_dir, "source")
            compare_root = os.path.join(temp_dir, "Pictures")
            output_root = os.path.join(temp_dir, "output")
            os.makedirs(source_root)
            os.makedirs(compare_root)
            os.makedirs(output_root)

            with open(os.path.join(source_root, "photo.jpg"), "wb") as handle:
                handle.write(b"same")
            with open(os.path.join(compare_root, "already.jpg"), "wb") as handle:
                handle.write(b"same")

            cache_path = os.path.join(temp_dir, "missing-cache-dir", "drive_cache.json")
            settings = TransferSettings(
                source_root=source_root,
                dest_root=compare_root,
                output_root=output_root,
                criteria="hash",
                hash_algo="sha256",
                hash_mode="full",
                only_media=True,
                extensions=[".jpg"],
                min_size_kb=0,
                exclude_dirs=[],
                skip_hidden_system=False,
                dry_run=True,
                preserve_structure=True,
                max_hash_workers=1,
                transfer_mode="copy",
                duplicate_policy="skip",
                use_dest_cache=True,
                drive_cache_path=cache_path,
                update_drive_cache=True,
            )

            result = execute_smart_transfer(
                settings,
                threading.Event(),
                HashCache(os.path.join(temp_dir, "hash_cache.json")),
                DummyLogger(),
            )

            self.assertEqual(result["transferred"], 0)
            self.assertEqual(result["duplicates"], 1)
            self.assertFalse(os.path.exists(cache_path))

    def test_missing_runtime_hash_cache_file_loads_empty_and_saves(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            cache_path = os.path.join(temp_dir, "missing", "hash_cache.json")
            cache = HashCache(cache_path)

            cache.load()
            cache.set("photo.jpg", "sha256", "full", 4, 1.0, "digest")
            cache.save()

            self.assertTrue(os.path.exists(cache_path))

    def test_smart_transfer_uses_drive_cache_for_nested_compare_hash(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            source_root = os.path.join(temp_dir, "source")
            compare_root = os.path.join(temp_dir, "Pictures")
            oldpics = os.path.join(compare_root, "oldpics")
            output_root = os.path.join(temp_dir, "output")
            os.makedirs(source_root)
            os.makedirs(oldpics)
            os.makedirs(output_root)

            source_file = os.path.join(source_root, "photo.jpg")
            compare_file = os.path.join(oldpics, "photo.jpg")
            with open(source_file, "wb") as handle:
                handle.write(b"same")
            with open(compare_file, "wb") as handle:
                handle.write(b"same")

            digest = hashlib.sha256(b"same").hexdigest()
            cache_path = os.path.join(temp_dir, "drive_cache.json")
            drive_cache = DriveHashCache(cache_path)
            drive_cache.load()
            drive_cache.set_file_hash(
                compare_file,
                os.path.getsize(compare_file),
                os.path.getmtime(compare_file),
                digest,
                "sha256",
                "full",
                root=compare_root,
            )
            drive_cache.save()

            settings = TransferSettings(
                source_root=source_root,
                dest_root=compare_root,
                output_root=output_root,
                criteria="hash",
                hash_algo="sha256",
                hash_mode="full",
                only_media=True,
                extensions=[".jpg"],
                min_size_kb=0,
                exclude_dirs=[],
                skip_hidden_system=False,
                dry_run=True,
                preserve_structure=True,
                max_hash_workers=1,
                transfer_mode="copy",
                duplicate_policy="skip",
                use_dest_cache=True,
                drive_cache_path=cache_path,
                update_drive_cache=False,
            )

            original_compute_hash = engine_module.compute_hash

            def guarded_hash(path, settings, stop_event, hash_cache=None, is_adb=False, logger=None):
                self.assertNotEqual(path, compare_file)
                return original_compute_hash(path, settings, stop_event, hash_cache, is_adb, logger)

            with mock.patch.object(engine_module, "compute_hash", side_effect=guarded_hash):
                result = execute_smart_transfer(
                    settings,
                    threading.Event(),
                    HashCache(os.path.join(temp_dir, "hash_cache.json")),
                    DummyLogger(),
                )

            self.assertEqual(result["transferred"], 0)
            self.assertEqual(result["duplicates"], 1)
            self.assertEqual(result["skipped"], 1)

    def test_smart_transfer_skips_compare_hashing_when_cache_count_matches(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            source_root = os.path.join(temp_dir, "source")
            compare_root = os.path.join(temp_dir, "Pictures")
            os.makedirs(source_root)
            os.makedirs(compare_root)

            source_file = os.path.join(source_root, "photo.jpg")
            compare_file = os.path.join(compare_root, "already.jpg")
            with open(source_file, "wb") as handle:
                handle.write(b"same")
            with open(compare_file, "wb") as handle:
                handle.write(b"same")

            digest = hashlib.sha256(b"same").hexdigest()
            cache_path = os.path.join(temp_dir, "drive_cache.json")
            drive_cache = DriveHashCache(cache_path)
            drive_cache.load()
            drive_cache.set_file_hash(
                compare_file,
                os.path.getsize(compare_file),
                os.path.getmtime(compare_file),
                digest,
                "sha256",
                "full",
                root=compare_root,
            )
            drive_cache.save()

            settings = TransferSettings(
                source_root=source_root,
                dest_root=compare_root,
                output_root="",
                criteria="hash",
                hash_algo="sha256",
                hash_mode="full",
                only_media=True,
                extensions=[".jpg"],
                min_size_kb=0,
                exclude_dirs=[],
                skip_hidden_system=False,
                dry_run=True,
                preserve_structure=True,
                max_hash_workers=1,
                transfer_mode="copy",
                duplicate_policy="skip",
                use_dest_cache=True,
                drive_cache_path=cache_path,
                update_drive_cache=True,
            )

            original_compute_hash = engine_module.compute_hash

            def guarded_hash(path, settings, stop_event, hash_cache=None, is_adb=False, logger=None):
                self.assertNotEqual(path, compare_file)
                return original_compute_hash(path, settings, stop_event, hash_cache, is_adb, logger)

            logger = DummyLogger()
            with mock.patch.object(engine_module, "compute_hash", side_effect=guarded_hash):
                result = execute_smart_transfer(
                    settings,
                    threading.Event(),
                    HashCache(os.path.join(temp_dir, "hash_cache.json")),
                    logger,
                )

            self.assertEqual(result["transferred"], 0)
            self.assertEqual(result["duplicates"], 1)
            self.assertTrue(any("destination hashing skipped" in message for message in logger.messages))

    def test_smart_transfer_updates_drive_cache_after_copy(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            source_root = os.path.join(temp_dir, "source")
            compare_root = os.path.join(temp_dir, "compare")
            output_root = os.path.join(temp_dir, "output")
            os.makedirs(source_root)
            os.makedirs(compare_root)
            os.makedirs(output_root)

            source_file = os.path.join(source_root, "new.jpg")
            with open(source_file, "wb") as handle:
                handle.write(b"new-cache-entry")

            cache_path = os.path.join(temp_dir, "drive_cache.json")
            settings = TransferSettings(
                source_root=source_root,
                dest_root=compare_root,
                output_root=output_root,
                criteria="hash",
                hash_algo="sha256",
                hash_mode="full",
                only_media=True,
                extensions=[".jpg"],
                min_size_kb=0,
                exclude_dirs=[],
                skip_hidden_system=False,
                dry_run=False,
                preserve_structure=True,
                max_hash_workers=1,
                transfer_mode="copy",
                duplicate_policy="skip",
                use_dest_cache=True,
                drive_cache_path=cache_path,
                update_drive_cache=True,
            )

            result = execute_smart_transfer(
                settings,
                threading.Event(),
                HashCache(os.path.join(temp_dir, "hash_cache.json")),
                DummyLogger(),
            )

            cache = DriveHashCache(cache_path)
            cache.load()
            copied_file = os.path.join(output_root, "new.jpg")
            expected_digest = hashlib.sha256(b"new-cache-entry").hexdigest()

            self.assertEqual(result["transferred"], 1)
            self.assertTrue(os.path.exists(copied_file))
            self.assertTrue(cache.has_hash(expected_digest))

    def test_smart_transfer_uses_matching_adb_cache_without_remote_hashing(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            compare_root = os.path.join(temp_dir, "compare")
            os.makedirs(compare_root)
            compare_file = os.path.join(compare_root, "already.jpg")
            with open(compare_file, "wb") as handle:
                handle.write(b"same")

            remote_path = "/sdcard/DCIM/Camera/photo.jpg"
            remote_mtime = 123.0
            digest = hashlib.sha256(b"same").hexdigest()
            adb_cache_path = os.path.join(temp_dir, "adb_cache.json")
            adb_cache = ADBHashCache(adb_cache_path, "phone-123")
            adb_cache.set_file_hash(
                remote_path,
                4,
                remote_mtime,
                digest,
                "sha256",
                "full",
                root="/sdcard/DCIM",
            )
            adb_cache.save()

            settings = TransferSettings(
                source_root="/sdcard/DCIM",
                dest_root=compare_root,
                output_root="",
                criteria="hash",
                hash_algo="sha256",
                hash_mode="fast",
                only_media=True,
                extensions=[".jpg"],
                min_size_kb=0,
                exclude_dirs=[],
                skip_hidden_system=False,
                dry_run=True,
                preserve_structure=True,
                max_hash_workers=1,
                transfer_mode="copy",
                duplicate_policy="skip",
                use_dest_cache=False,
                source_is_adb=True,
                adb_serial="phone-123",
                use_adb_cache=True,
                adb_cache_path=adb_cache_path,
            )

            source_info = FileInfo(remote_path, 4, remote_mtime, is_adb=True)
            compare_info = FileInfo(
                compare_file,
                os.path.getsize(compare_file),
                os.path.getmtime(compare_file),
            )

            def fake_iter(root, *_args, **_kwargs):
                return iter([source_info] if root == settings.source_root else [compare_info])

            logger = DummyLogger()
            with (
                mock.patch.object(engine_module, "validate_transfer_paths", return_value=""),
                mock.patch.object(engine_module, "iter_files", side_effect=fake_iter),
                mock.patch.object(ADBBridge, "remote_hash") as remote_hash,
            ):
                result = execute_smart_transfer(
                    settings,
                    threading.Event(),
                    HashCache(os.path.join(temp_dir, "hash_cache.json")),
                    logger,
                )

            remote_hash.assert_not_called()
            self.assertEqual(result["duplicates"], 1)
            self.assertTrue(any("ADB cache count matches" in message for message in logger.messages))


if __name__ == "__main__":
    unittest.main()
