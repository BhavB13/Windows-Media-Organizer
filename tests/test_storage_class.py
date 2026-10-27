"""Deterministic unit tests for duplicate_transfer_manager.core.storage_class."""

from __future__ import annotations

import builtins
import ctypes
import importlib.util
import io
import os
import pathlib
import sys
import tempfile
import threading
import types
import unittest
from unittest.mock import patch

import duplicate_transfer_manager.core as _core_pkg
from duplicate_transfer_manager.core.cancellation import CancellationToken

try:
    from duplicate_transfer_manager.core import storage_class
except ImportError:
    _storage_class_file = (
        pathlib.Path(__file__).resolve().parents[1]
        / "src"
        / "duplicate_transfer_manager"
        / "core"
        / "storage_class.py"
    )
    _spec = importlib.util.spec_from_file_location(
        "duplicate_transfer_manager.core.storage_class",
        _storage_class_file,
    )
    assert _spec is not None and _spec.loader is not None
    storage_class = importlib.util.module_from_spec(_spec)
    sys.modules["duplicate_transfer_manager.core.storage_class"] = storage_class
    setattr(_core_pkg, "storage_class", storage_class)
    _spec.loader.exec_module(storage_class)
from duplicate_transfer_manager.core.storage_class import (
    DRIVE_CDROM,
    DRIVE_FIXED,
    DRIVE_NO_ROOT_DIR,
    DRIVE_RAMDISK,
    DRIVE_REMOTE,
    DRIVE_REMOVABLE,
    DRIVE_UNKNOWN,
    DriveType,
    FILE_ATTRIBUTE_ARCHIVE,
    FILE_ATTRIBUTE_DIRECTORY,
    FILE_ATTRIBUTE_NORMAL,
    FILE_ATTRIBUTE_OFFLINE,
    FILE_ATTRIBUTE_PINNED,
    FILE_ATTRIBUTE_READONLY,
    FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS,
    FILE_ATTRIBUTE_RECALL_ON_OPEN,
    FILE_ATTRIBUTE_REPARSE_POINT,
    FILE_ATTRIBUTE_UNPINNED,
    INVALID_FILE_ATTRIBUTES,
    IO_REPARSE_TAG_CLOUD,
    IO_REPARSE_TAG_CLOUD_3,
    IO_REPARSE_TAG_DEDUP,
    IO_REPARSE_TAG_FILE_PLACEHOLDER,
    IO_REPARSE_TAG_HSM,
    IO_REPARSE_TAG_MOUNT_POINT,
    IO_REPARSE_TAG_ONEDRIVE,
    IO_REPARSE_TAG_STORAGE_SYNC,
    IO_REPARSE_TAG_SYMLINK,
    IO_REPARSE_TAG_WOF,
    MAX_PATH,
    PROVIDER_DROPBOX,
    PROVIDER_GOOGLE_DRIVE,
    PROVIDER_ICLOUD,
    PROVIDER_ONEDRIVE,
    Residency,
    UNKNOWN_PROVIDER,
    classify_attributes,
    classify_drive_type,
    classify_file,
    classify_path_storage,
    identify_sync_provider,
    is_cloud_reparse_tag,
    survey_directory,
)


class _FakeStorageBackend:
    """Deterministic fake Win32 metadata backend that fails if open() is ever called."""

    def __init__(
        self,
        *,
        drive_types: dict[str, int] | None = None,
        default_drive_type: int = DRIVE_FIXED,
        attributes: dict[str, int | Exception] | None = None,
        reparse_tags: dict[str, int | Exception] | None = None,
        sizes: dict[str, int | Exception] | None = None,
    ) -> None:
        self.drive_types = {
            os.path.normcase(k): v for k, v in (drive_types or {}).items()
        }
        self.default_drive_type = default_drive_type
        self.attributes = {
            os.path.normcase(os.path.abspath(k)): v
            for k, v in (attributes or {}).items()
        }
        self.reparse_tags = {
            os.path.normcase(os.path.abspath(k)): v
            for k, v in (reparse_tags or {}).items()
        }
        self.sizes = {
            os.path.normcase(os.path.abspath(k)): v
            for k, v in (sizes or {}).items()
        }
        self.open_calls: list[str] = []

    def open(self, path: str, *args: object, **kwargs: object) -> None:
        self.open_calls.append(str(path))
        raise AssertionError(f"Forbidden file open attempted on {path!r}")

    def get_drive_type(self, root_path: str) -> int:
        key = os.path.normcase(root_path)
        return self.drive_types.get(key, self.default_drive_type)

    def get_file_attributes(self, path: str) -> int:
        key = os.path.normcase(os.path.abspath(path))
        if key in self.attributes:
            val = self.attributes[key]
            if isinstance(val, Exception):
                raise val
            return int(val)
        if os.path.isdir(path):
            return FILE_ATTRIBUTE_DIRECTORY
        if os.path.exists(path):
            return FILE_ATTRIBUTE_ARCHIVE
        return INVALID_FILE_ATTRIBUTES

    def get_reparse_tag(self, path: str) -> int:
        key = os.path.normcase(os.path.abspath(path))
        if key in self.reparse_tags:
            val = self.reparse_tags[key]
            if isinstance(val, Exception):
                raise val
            return int(val)
        return 0

    def get_file_size(self, path: str) -> int:
        key = os.path.normcase(os.path.abspath(path))
        if key in self.sizes:
            val = self.sizes[key]
            if isinstance(val, Exception):
                raise val
            return int(val)
        return int(os.lstat(path).st_size)


class StorageClassTests(unittest.TestCase):
    """Verify storage classification, cloud placeholder detection, and batch surveys."""

    def test_each_drive_type_maps_to_expected_enum(self) -> None:
        cases = [
            (DRIVE_FIXED, DriveType.FIXED, ""),
            (DRIVE_REMOVABLE, DriveType.REMOVABLE, ""),
            (DRIVE_REMOTE, DriveType.REMOTE, ""),
            (DRIVE_CDROM, DriveType.CDROM, ""),
            (DRIVE_RAMDISK, DriveType.RAMDISK, ""),
            (DRIVE_UNKNOWN, DriveType.UNKNOWN, "DRIVE_UNKNOWN"),
            (DRIVE_NO_ROOT_DIR, DriveType.UNKNOWN, "DRIVE_NO_ROOT_DIR"),
            (999, DriveType.UNKNOWN, "unrecognized"),
        ]
        for raw_code, expected_enum, expected_reason_part in cases:
            with self.subTest(raw_code=raw_code, expected=expected_enum):
                seen_roots: list[str] = []

                def fake_drive_type(root: str, code: int = raw_code) -> int:
                    seen_roots.append(root)
                    return code

                info = classify_path_storage(
                    r"D:\Photos\2026\trip.jpg",
                    get_drive_type=fake_drive_type,
                    platform_is_windows=True,
                )
                self.assertEqual(info.drive_type, expected_enum)
                self.assertEqual(info.root, "D:\\")
                self.assertFalse(info.is_unc)
                self.assertEqual(seen_roots, ["D:\\"])
                if expected_enum is DriveType.UNKNOWN:
                    self.assertTrue(info.reason)
                    self.assertIn(expected_reason_part, info.reason)
                else:
                    self.assertEqual(info.reason, "")
                self.assertEqual(
                    classify_drive_type(
                        r"D:\Photos\2026\trip.jpg",
                        get_drive_type=fake_drive_type,
                        platform_is_windows=True,
                    ),
                    expected_enum,
                )

    def test_unc_paths_are_recognised_as_remote_without_drive_letter(self) -> None:
        unc_paths = [
            (r"\\nas-server\media\Photos\IMG_0001.jpg", r"\\nas-server\media\\"[0:-1]),
            ("//fileserver/shared/archive/video.mp4", r"\\fileserver\shared\\"[0:-1]),
            (r"\\?\UNC\corp-nas\backups\2026", r"\\corp-nas\backups\\"[0:-1]),
        ]
        for raw_path, expected_root in unc_paths:
            with self.subTest(path=raw_path):
                def fail_if_called(root: str) -> int:
                    raise AssertionError(f"GetDriveTypeW should not be needed for UNC {root}")

                info = classify_path_storage(
                    raw_path,
                    get_drive_type=fail_if_called,
                    platform_is_windows=True,
                )
                self.assertEqual(info.drive_type, DriveType.REMOTE)
                self.assertEqual(info.drive_type, DriveType.NETWORK)
                self.assertTrue(info.is_unc)
                self.assertTrue(info.is_remote)
                self.assertFalse(info.is_fixed)
                self.assertFalse(info.is_removable)
                self.assertEqual(info.root, expected_root)

        incomplete_unc = classify_path_storage(r"\\onlyserver", platform_is_windows=True)
        self.assertEqual(incomplete_unc.drive_type, DriveType.UNKNOWN)
        self.assertTrue(incomplete_unc.reason)

    def test_plain_local_file_is_local_and_would_not_download_on_read(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            file_path = os.path.join(tmp_dir, "local_photo.jpg")
            with open(file_path, "wb") as handle:
                handle.write(b"1234")

            for attr in (0, FILE_ATTRIBUTE_NORMAL, FILE_ATTRIBUTE_ARCHIVE, FILE_ATTRIBUTE_READONLY):
                with self.subTest(attr=hex(attr)):
                    info = classify_file(
                        file_path,
                        get_file_attributes=lambda _p, a=attr: a,
                        get_drive_type=lambda _r: DRIVE_FIXED,
                        platform_is_windows=True,
                    )
                    self.assertEqual(info.residency, Residency.LOCAL)
                    self.assertFalse(info.would_download_on_read)
                    self.assertEqual(info.size_bytes, 4)
                    self.assertEqual(info.drive_type, DriveType.FIXED)
                    self.assertFalse(info.is_directory)
                    self.assertFalse(info.is_reparse_point)
                    self.assertFalse(info.is_reparse_directory)
                    self.assertEqual(info.reason, "")

    def test_individual_attribute_bits_produce_expected_residency_and_download_gate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            sample = os.path.join(tmp_dir, "item.bin")
            with open(sample, "wb") as handle:
                handle.write(b"abc")

            # 1. FILE_ATTRIBUTE_OFFLINE (0x1000)
            offline_info = classify_file(
                sample,
                get_file_attributes=lambda _p: FILE_ATTRIBUTE_OFFLINE,
                get_drive_type=lambda _r: DRIVE_FIXED,
                platform_is_windows=True,
            )
            self.assertEqual(offline_info.residency, Residency.OFFLINE)
            self.assertTrue(offline_info.would_download_on_read)

            # 2. FILE_ATTRIBUTE_RECALL_ON_OPEN (0x40000)
            recall_open_info = classify_file(
                sample,
                get_file_attributes=lambda _p: FILE_ATTRIBUTE_RECALL_ON_OPEN,
                get_drive_type=lambda _r: DRIVE_FIXED,
                platform_is_windows=True,
            )
            self.assertEqual(recall_open_info.residency, Residency.PLACEHOLDER_RECALL_ON_ACCESS)
            self.assertTrue(recall_open_info.would_download_on_read)

            # 3. FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS (0x400000)
            recall_access_info = classify_file(
                sample,
                get_file_attributes=lambda _p: FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS,
                get_drive_type=lambda _r: DRIVE_FIXED,
                platform_is_windows=True,
            )
            self.assertEqual(recall_access_info.residency, Residency.PLACEHOLDER_RECALL_ON_ACCESS)
            self.assertTrue(recall_access_info.would_download_on_read)

            # 4. FILE_ATTRIBUTE_REPARSE_POINT (0x400) + cloud reparse tags
            for cloud_tag in (
                IO_REPARSE_TAG_CLOUD,
                IO_REPARSE_TAG_CLOUD_3,
                IO_REPARSE_TAG_ONEDRIVE,
                IO_REPARSE_TAG_FILE_PLACEHOLDER,
                IO_REPARSE_TAG_STORAGE_SYNC,
            ):
                with self.subTest(cloud_tag=hex(cloud_tag)):
                    self.assertTrue(is_cloud_reparse_tag(cloud_tag))
                    cloud_reparse_info = classify_file(
                        sample,
                        get_file_attributes=lambda _p: FILE_ATTRIBUTE_REPARSE_POINT,
                        get_reparse_tag=lambda _p, t=cloud_tag: t,
                        get_drive_type=lambda _r: DRIVE_FIXED,
                        platform_is_windows=True,
                    )
                    self.assertEqual(
                        cloud_reparse_info.residency,
                        Residency.PLACEHOLDER_RECALL_ON_ACCESS,
                    )
                    self.assertTrue(cloud_reparse_info.would_download_on_read)
                    self.assertTrue(cloud_reparse_info.is_reparse_point)
                    self.assertEqual(cloud_reparse_info.reparse_tag, cloud_tag)

            # 5. FILE_ATTRIBUTE_REPARSE_POINT (0x400) + non-cloud reparse tags
            for other_tag in (0, IO_REPARSE_TAG_SYMLINK, IO_REPARSE_TAG_DEDUP, IO_REPARSE_TAG_WOF):
                with self.subTest(other_tag=hex(other_tag)):
                    self.assertFalse(is_cloud_reparse_tag(other_tag))
                    other_reparse_info = classify_file(
                        sample,
                        get_file_attributes=lambda _p: FILE_ATTRIBUTE_REPARSE_POINT,
                        get_reparse_tag=lambda _p, t=other_tag: t,
                        get_drive_type=lambda _r: DRIVE_FIXED,
                        platform_is_windows=True,
                    )
                    self.assertEqual(other_reparse_info.residency, Residency.REPARSE_OTHER)
                    self.assertFalse(other_reparse_info.would_download_on_read)
                    self.assertTrue(other_reparse_info.is_reparse_point)

    def test_combined_attribute_bits_resolve_sensibly(self) -> None:
        # Typical OneDrive dehydrated placeholder:ARCHIVE | REPARSE_POINT | UNPINNED | RECALL_ON_DATA_ACCESS
        onedrive_combo = classify_attributes(
            FILE_ATTRIBUTE_ARCHIVE
            | FILE_ATTRIBUTE_REPARSE_POINT
            | FILE_ATTRIBUTE_UNPINNED
            | FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS,
            IO_REPARSE_TAG_CLOUD,
            path=r"C:\Users\Demo\OneDrive\Pictures\vacation.heic",
            size_bytes=5_000_000,
            drive_type=DriveType.FIXED,
        )
        self.assertEqual(onedrive_combo.residency, Residency.PLACEHOLDER_RECALL_ON_ACCESS)
        self.assertTrue(onedrive_combo.would_download_on_read)
        self.assertEqual(onedrive_combo.provider, PROVIDER_ONEDRIVE)

        # OFFLINE + RECALL_ON_DATA_ACCESS -> PLACEHOLDER_RECALL_ON_ACCESS, would_download_on_read=True
        offline_and_recall = classify_attributes(
            FILE_ATTRIBUTE_OFFLINE | FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS,
            path=r"C:\Data\tiered.dat",
        )
        self.assertEqual(offline_and_recall.residency, Residency.PLACEHOLDER_RECALL_ON_ACCESS)
        self.assertTrue(offline_and_recall.would_download_on_read)

        # OFFLINE + REPARSE_POINT with HSM (non-cloud) tag -> OFFLINE, would_download_on_read=True
        offline_hsm = classify_attributes(
            FILE_ATTRIBUTE_OFFLINE | FILE_ATTRIBUTE_REPARSE_POINT,
            IO_REPARSE_TAG_HSM,
            path=r"C:\Archive\tape_backed.tar",
        )
        self.assertEqual(offline_hsm.residency, Residency.OFFLINE)
        self.assertTrue(offline_hsm.would_download_on_read)

        # Hydrated/pinned local file with ARCHIVE | PINNED | READONLY -> LOCAL, would_download_on_read=False
        pinned_local = classify_attributes(
            FILE_ATTRIBUTE_ARCHIVE | FILE_ATTRIBUTE_PINNED | FILE_ATTRIBUTE_READONLY,
            0,
            path=r"C:\Users\Demo\OneDrive\Pinned\keep.jpg",
        )
        self.assertEqual(pinned_local.residency, Residency.LOCAL)
        self.assertFalse(pinned_local.would_download_on_read)

        # Directory + Reparse point -> REPARSE_OTHER, is_reparse_directory=True, would_download_on_read=False
        junction_dir = classify_attributes(
            FILE_ATTRIBUTE_DIRECTORY | FILE_ATTRIBUTE_REPARSE_POINT,
            IO_REPARSE_TAG_MOUNT_POINT,
            path=r"C:\Data\LoopJunction",
        )
        self.assertEqual(junction_dir.residency, Residency.REPARSE_OTHER)
        self.assertTrue(junction_dir.is_directory)
        self.assertTrue(junction_dir.is_reparse_point)
        self.assertTrue(junction_dir.is_reparse_directory)
        self.assertFalse(junction_dir.would_download_on_read)

    def test_provider_identification_and_unknown_provider_fallback(self) -> None:
        provider_cases = [
            (r"C:\Users\Alex\OneDrive\Photos\img.jpg", 0, PROVIDER_ONEDRIVE),
            (r"C:\Users\Alex\OneDrive - Contoso\Docs\spec.docx", 0, PROVIDER_ONEDRIVE),
            (r"C:\Users\Alex\Dropbox\Camera Uploads\2026.mp4", 0, PROVIDER_DROPBOX),
            (r"C:\Users\Alex\Dropbox (Personal)\notes.txt", 0, PROVIDER_DROPBOX),
            (r"C:\Users\Alex\Google Drive\My Drive\budget.xlsx", 0, PROVIDER_GOOGLE_DRIVE),
            (r"C:\Users\Alex\AppData\Local\Google\DriveFS\item.db", 0, PROVIDER_GOOGLE_DRIVE),
            (r"C:\Users\Alex\iCloudDrive\Documents\scan.pdf", 0, PROVIDER_ICLOUD),
            (r"C:\Users\Alex\Pictures\iCloud Photos\Photos\IMG_1.HEIC", 0, PROVIDER_ICLOUD),
            # Reparse tag identifies OneDrive even when folder path has a custom name
            (r"D:\CustomSyncFolder\video.mov", IO_REPARSE_TAG_ONEDRIVE, PROVIDER_ONEDRIVE),
            (r"D:\CustomSyncFolder\legacy.mov", IO_REPARSE_TAG_FILE_PLACEHOLDER, PROVIDER_ONEDRIVE),
            # Generic cloud reparse tag on an unlabelled path must NOT guess
            (r"D:\CustomCloudMount\mystery.raw", IO_REPARSE_TAG_CLOUD, UNKNOWN_PROVIDER),
            (r"C:\LocalFolder\regular.jpg", 0, UNKNOWN_PROVIDER),
        ]
        for path, tag, expected_provider in provider_cases:
            with self.subTest(path=path, tag=hex(tag)):
                self.assertEqual(identify_sync_provider(path, tag), expected_provider)
                classified = classify_attributes(
                    FILE_ATTRIBUTE_REPARSE_POINT | FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS,
                    tag,
                    path=path,
                )
                self.assertEqual(classified.provider, expected_provider)
                self.assertEqual(classified.sync_provider, expected_provider)

    def test_directory_reparse_point_is_reported_and_not_followed_by_walk(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            real_sub = os.path.join(tmp_dir, "real_folder")
            junction_dir = os.path.join(tmp_dir, "junction_escape")
            os.makedirs(real_sub)
            os.makedirs(junction_dir)

            real_file = os.path.join(real_sub, "kept.jpg")
            with open(real_file, "wb"):
                pass

            # File inside the directory junction that must NEVER be visited
            trapped_file = os.path.join(junction_dir, "should_not_be_scanned.jpg")
            with open(trapped_file, "wb"):
                pass

            backend = _FakeStorageBackend(
                attributes={
                    real_file: FILE_ATTRIBUTE_ARCHIVE,
                    junction_dir: FILE_ATTRIBUTE_DIRECTORY | FILE_ATTRIBUTE_REPARSE_POINT,
                    trapped_file: FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS,
                },
                reparse_tags={
                    junction_dir: IO_REPARSE_TAG_MOUNT_POINT,
                },
                sizes={
                    real_file: 512,
                    trapped_file: 999_999,
                },
            )

            survey = survey_directory(
                tmp_dir,
                backend=backend,
                platform_is_windows=True,
            )

            self.assertEqual(survey.local_files, 1)
            self.assertEqual(survey.local_bytes, 512)
            self.assertEqual(survey.recall_files, 0)
            self.assertEqual(survey.recall_bytes, 0)
            self.assertEqual(survey.reparse_directories, (junction_dir,))
            self.assertEqual(survey.skipped_reparse_dirs, (junction_dir,))

            # Also verify when root itself is a directory reparse point
            root_junction_survey = survey_directory(
                junction_dir,
                backend=backend,
                platform_is_windows=True,
            )
            self.assertEqual(root_junction_survey.reparse_directories, (junction_dir,))
            self.assertEqual(root_junction_survey.total_files, 0)
            self.assertEqual(root_junction_survey.directories_scanned, 0)

    def test_batch_survey_counts_and_recall_bytes_total(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            onedrive_dir = os.path.join(tmp_dir, "OneDrive", "Camera Roll")
            icloud_dir = os.path.join(tmp_dir, "iCloud Photos")
            local_dir = os.path.join(tmp_dir, "LocalLibrary")
            archive_dir = os.path.join(local_dir, "Archive")
            os.makedirs(onedrive_dir)
            os.makedirs(icloud_dir)
            os.makedirs(archive_dir)

            local_a = os.path.join(local_dir, "a.jpg")
            local_b = os.path.join(local_dir, "b.jpg")
            cloud_1 = os.path.join(onedrive_dir, "placeholder1.mp4")
            cloud_2 = os.path.join(icloud_dir, "placeholder2.mov")
            offline_1 = os.path.join(archive_dir, "archived_offline.mkv")
            dedup_file = os.path.join(archive_dir, "dedup_local.bin")

            for path, _byte_len in (
                (local_a, 1_000),
                (local_b, 2_500),
                (cloud_1, 10_000_000),
                (cloud_2, 25_000_000),
                (offline_1, 5_000_000),
                (dedup_file, 4_000),
            ):
                with open(path, "wb"):
                    pass

            backend = _FakeStorageBackend(
                attributes={
                    local_a: FILE_ATTRIBUTE_ARCHIVE,
                    local_b: FILE_ATTRIBUTE_NORMAL,
                    cloud_1: FILE_ATTRIBUTE_REPARSE_POINT | FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS,
                    cloud_2: FILE_ATTRIBUTE_REPARSE_POINT,
                    offline_1: FILE_ATTRIBUTE_OFFLINE,
                    dedup_file: FILE_ATTRIBUTE_REPARSE_POINT,
                },
                reparse_tags={
                    cloud_1: IO_REPARSE_TAG_CLOUD,
                    cloud_2: IO_REPARSE_TAG_CLOUD,
                    dedup_file: IO_REPARSE_TAG_DEDUP,
                },
                sizes={
                    local_a: 1_000,
                    local_b: 2_500,
                    cloud_1: 10_000_000,
                    cloud_2: 25_000_000,
                    offline_1: 5_000_000,
                    dedup_file: 4_000,
                },
            )

            survey = survey_directory(
                tmp_dir,
                backend=backend,
                platform_is_windows=True,
            )

            self.assertFalse(survey.cancelled)
            self.assertEqual(survey.drive_type, DriveType.FIXED)
            self.assertEqual(survey.local_files, 2)
            self.assertEqual(survey.local_count, 2)
            self.assertEqual(survey.local_bytes, 3_500)
            self.assertEqual(survey.recall_files, 3)
            self.assertEqual(survey.would_download_files, 3)
            self.assertEqual(survey.recall_bytes, 40_000_000)
            self.assertEqual(survey.would_download_bytes, 40_000_000)
            self.assertEqual(survey.offline_files, 1)
            self.assertEqual(survey.reparse_other_files, 1)
            self.assertEqual(survey.unknown_files, 0)
            self.assertEqual(survey.total_files, 6)
            self.assertIn(PROVIDER_ONEDRIVE, survey.providers)
            self.assertIn(PROVIDER_ICLOUD, survey.providers)

    def test_vanished_file_permission_error_and_ctypes_failure_yield_unknown_without_raising(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            ok_file = os.path.join(tmp_dir, "ok.jpg")
            vanished_file = os.path.join(tmp_dir, "vanished.jpg")
            denied_file = os.path.join(tmp_dir, "denied.jpg")
            ctypes_err_file = os.path.join(tmp_dir, "ctypes_fail.jpg")

            for p in (ok_file, vanished_file, denied_file, ctypes_err_file):
                with open(p, "wb") as handle:
                    handle.write(b"data")

            backend = _FakeStorageBackend(
                attributes={
                    ok_file: FILE_ATTRIBUTE_ARCHIVE,
                    # Simulate vanished file returning INVALID_FILE_ATTRIBUTES
                    vanished_file: INVALID_FILE_ATTRIBUTES,
                    # Simulate permission error raising PermissionError
                    denied_file: PermissionError(13, "Access is denied"),
                    # Simulate missing ctypes symbol raising AttributeError
                    ctypes_err_file: AttributeError("symbol GetFileAttributesW missing"),
                },
            )

            # Remove vanished_file before walk inspects it
            os.remove(vanished_file)
            # Re-create right before scandir and delete during attribute read to test mid-walk disappearance
            with open(vanished_file, "wb") as handle:
                handle.write(b"temp")

            def attr_with_midwalk_vanish(path: str) -> int:
                if os.path.normcase(os.path.abspath(path)) == os.path.normcase(os.path.abspath(vanished_file)):
                    try:
                        os.remove(vanished_file)
                    except OSError:
                        pass
                    return INVALID_FILE_ATTRIBUTES
                return backend.get_file_attributes(path)

            survey = survey_directory(
                tmp_dir,
                get_file_attributes=attr_with_midwalk_vanish,
                get_reparse_tag=backend.get_reparse_tag,
                get_file_size=backend.get_file_size,
                get_drive_type=backend.get_drive_type,
                platform_is_windows=True,
            )

            self.assertEqual(survey.local_files, 1)
            self.assertEqual(survey.unknown_files, 3)
            self.assertEqual(len(survey.unknown_entries), 3)
            for entry in survey.unknown_entries:
                self.assertEqual(entry.residency, Residency.UNKNOWN)
                self.assertFalse(entry.would_download_on_read)
                self.assertTrue(entry.reason, f"Expected non-empty reason for {entry.path}")

    def test_max_path_non_windows_and_missing_ctypes_symbols_return_unknown_with_reason(self) -> None:
        long_path = "C:\\" + (" very_long_segment" * 20).replace(" ", "_")
        self.assertGreaterEqual(len(long_path), MAX_PATH)

        long_storage = classify_path_storage(
            long_path,
            get_drive_type=lambda _r: DRIVE_FIXED,
            platform_is_windows=True,
        )
        self.assertEqual(long_storage.drive_type, DriveType.UNKNOWN)
        self.assertIn("MAX_PATH", long_storage.reason)

        long_file = classify_file(
            long_path,
            get_file_attributes=lambda _p: FILE_ATTRIBUTE_ARCHIVE,
            platform_is_windows=True,
        )
        self.assertEqual(long_file.residency, Residency.UNKNOWN)
        self.assertFalse(long_file.would_download_on_read)
        self.assertIn("MAX_PATH", long_file.reason)

        # Non-Windows platform via parameter and via monkeypatching _is_windows
        non_win_storage = classify_path_storage(r"C:\Users\Test", platform_is_windows=False)
        self.assertEqual(non_win_storage.drive_type, DriveType.UNKNOWN)
        self.assertIn("Non-Windows", non_win_storage.reason)

        with patch.object(storage_class, "_is_windows", return_value=False):
            non_win_file = classify_file(r"C:\Users\Test\file.txt")
            self.assertEqual(non_win_file.residency, Residency.UNKNOWN)
            self.assertFalse(non_win_file.would_download_on_read)
            self.assertIn("Non-Windows", non_win_file.reason)

        # Missing ctypes symbol on kernel32
        empty_kernel32 = types.SimpleNamespace()
        fake_windll = types.SimpleNamespace(kernel32=empty_kernel32)
        with (
            patch.object(storage_class, "_is_windows", return_value=True),
            patch.object(ctypes, "windll", fake_windll, create=True),
        ):
            missing_sym_storage = classify_path_storage(r"C:\Users\Test")
            self.assertEqual(missing_sym_storage.drive_type, DriveType.UNKNOWN)
            self.assertIn("GetDriveTypeW", missing_sym_storage.reason)

            missing_sym_file = classify_file(r"C:\Users\Test\file.txt")
            self.assertEqual(missing_sym_file.residency, Residency.UNKNOWN)
            self.assertFalse(missing_sym_file.would_download_on_read)
            self.assertIn("GetFileAttributesW", missing_sym_file.reason)

    def test_cancellation_stops_walk_promptly(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            for index in range(4):
                p = os.path.join(tmp_dir, f"f{index}.dat")
                with open(p, "wb"):
                    pass

            # 1. Pre-cancelled token returns immediately without scanning any directory
            pre_cancelled = CancellationToken()
            pre_cancelled.cancel()
            res_pre = survey_directory(
                tmp_dir,
                stop_event=pre_cancelled,
                get_file_attributes=lambda _p: FILE_ATTRIBUTE_ARCHIVE,
                get_drive_type=lambda _r: DRIVE_FIXED,
                platform_is_windows=True,
            )
            self.assertTrue(res_pre.cancelled)
            self.assertEqual(res_pre.total_files, 0)
            self.assertEqual(res_pre.directories_scanned, 0)

            # 2. Cancellation triggered mid-walk after inspecting 2 files
            stop_event = threading.Event()
            inspected_files = 0

            def attr_cancels_after_two(path: str) -> int:
                nonlocal inspected_files
                if os.path.isdir(path):
                    return FILE_ATTRIBUTE_DIRECTORY
                inspected_files += 1
                if inspected_files >= 2:
                    stop_event.set()
                return FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS

            res_mid = survey_directory(
                tmp_dir,
                stop_event=stop_event,
                get_file_attributes=attr_cancels_after_two,
                get_drive_type=lambda _r: DRIVE_FIXED,
                platform_is_windows=True,
            )
            self.assertTrue(res_mid.cancelled)
            self.assertEqual(res_mid.recall_files, 2)
            self.assertLess(res_mid.total_files, 4)

    def test_no_code_path_ever_opens_a_file_for_reading(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            sub = os.path.join(tmp_dir, "OneDrive")
            os.makedirs(sub)
            f_local = os.path.join(tmp_dir, "local.jpg")
            f_cloud = os.path.join(sub, "cloud.mp4")
            f_offline = os.path.join(sub, "offline.mov")
            for p in (f_local, f_cloud, f_offline):
                with open(p, "wb") as handle:
                    handle.write(b"payload")

            backend = _FakeStorageBackend(
                attributes={
                    f_local: FILE_ATTRIBUTE_ARCHIVE,
                    f_cloud: FILE_ATTRIBUTE_REPARSE_POINT | FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS,
                    f_offline: FILE_ATTRIBUTE_OFFLINE,
                },
                reparse_tags={
                    f_cloud: IO_REPARSE_TAG_CLOUD,
                },
            )

            def forbidden_open(*args: object, **kwargs: object) -> None:
                raise AssertionError(f"File open must never be called: args={args!r}")

            with (
                patch.object(builtins, "open", side_effect=forbidden_open),
                patch.object(io, "open", side_effect=forbidden_open),
                patch.object(os, "open", side_effect=forbidden_open),
                patch.object(pathlib.Path, "open", side_effect=forbidden_open),
            ):
                storage_res = classify_path_storage(
                    tmp_dir,
                    backend=backend,
                    open_hook=forbidden_open,
                    platform_is_windows=True,
                )
                self.assertEqual(storage_res.drive_type, DriveType.FIXED)

                file_res = classify_file(
                    f_cloud,
                    backend=backend,
                    open_hook=forbidden_open,
                    platform_is_windows=True,
                )
                self.assertEqual(file_res.residency, Residency.PLACEHOLDER_RECALL_ON_ACCESS)
                self.assertTrue(file_res.would_download_on_read)

                survey_res = survey_directory(
                    tmp_dir,
                    backend=backend,
                    open_hook=forbidden_open,
                    platform_is_windows=True,
                )
                self.assertEqual(survey_res.local_files, 1)
                self.assertEqual(survey_res.recall_files, 2)
                self.assertEqual(backend.open_calls, [])

    def test_monkeypatched_ctypes_kernel32_and_find_first_file_reparse_tag(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            sample = os.path.join(tmp_dir, "OneDrive", "doc.pdf")
            os.makedirs(os.path.dirname(sample))
            with open(sample, "wb") as handle:
                handle.write(b"12345678")

            closed_handles: list[int] = []

            def fake_find_first_file_w(path: str, data_ptr: object) -> int:
                # Populate WIN32_FIND_DATAW via ctypes pointer
                struct_obj = ctypes.cast(
                    data_ptr, ctypes.POINTER(storage_class._WIN32_FIND_DATAW)
                ).contents
                struct_obj.dwFileAttributes = (
                    FILE_ATTRIBUTE_REPARSE_POINT | FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS
                )
                struct_obj.dwReserved0 = IO_REPARSE_TAG_CLOUD
                return 0x1234ABCD

            def fake_find_close(handle: object) -> int:
                val = getattr(handle, "value", handle)
                closed_handles.append(int(val or 0))
                return 1

            fake_kernel32 = types.SimpleNamespace(
                GetDriveTypeW=lambda _root: DRIVE_REMOVABLE,
                GetFileAttributesW=lambda _path: (
                    FILE_ATTRIBUTE_REPARSE_POINT | FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS
                ),
                FindFirstFileW=fake_find_first_file_w,
                FindClose=fake_find_close,
            )
            fake_windll = types.SimpleNamespace(kernel32=fake_kernel32)

            with (
                patch.object(storage_class, "_is_windows", return_value=True),
                patch.object(ctypes, "windll", fake_windll, create=True),
            ):
                info = classify_file(sample)
                self.assertEqual(info.drive_type, DriveType.REMOVABLE)
                self.assertEqual(info.residency, Residency.PLACEHOLDER_RECALL_ON_ACCESS)
                self.assertTrue(info.would_download_on_read)
                self.assertEqual(info.reparse_tag, IO_REPARSE_TAG_CLOUD)
                self.assertEqual(info.provider, PROVIDER_ONEDRIVE)
                self.assertEqual(info.size_bytes, 8)
                self.assertEqual(closed_handles, [0x1234ABCD])

    def test_signed_minus_one_attributes_and_invalid_inputs_never_raise(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            sample = os.path.join(tmp_dir, "broken.bin")
            with open(sample, "wb") as handle:
                handle.write(b"x")

            # Signed -1 from unconfigured ctypes restype must be treated as INVALID_FILE_ATTRIBUTES
            neg_one = classify_file(
                sample,
                get_file_attributes=lambda _p: -1,
                get_drive_type=lambda _r: DRIVE_FIXED,
                platform_is_windows=True,
            )
            self.assertEqual(neg_one.residency, Residency.UNKNOWN)
            self.assertFalse(neg_one.would_download_on_read)
            self.assertTrue(neg_one.reason)

        for bad_input in ("", "   ", "C:\\bad\x00path", None):
            with self.subTest(bad_input=bad_input):
                st_res = classify_path_storage(bad_input)  # type: ignore[arg-type]
                self.assertEqual(st_res.drive_type, DriveType.UNKNOWN)
                self.assertTrue(st_res.reason)

                f_res = classify_file(bad_input)  # type: ignore[arg-type]
                self.assertEqual(f_res.residency, Residency.UNKNOWN)
                self.assertFalse(f_res.would_download_on_read)
                self.assertTrue(f_res.reason)

                sv_res = survey_directory(bad_input)  # type: ignore[arg-type]
                self.assertEqual(sv_res.unknown_files, 1)
                self.assertTrue(sv_res.reason)

    def test_subdirectory_permission_error_during_scandir_is_recorded_as_unknown(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            accessible_dir = os.path.join(tmp_dir, "accessible")
            locked_dir = os.path.join(tmp_dir, "locked")
            os.makedirs(accessible_dir)
            os.makedirs(locked_dir)

            good_file = os.path.join(accessible_dir, "good.jpg")
            with open(good_file, "wb") as handle:
                handle.write(b"1234")

            real_scandir = os.scandir

            def selective_scandir(path: str):
                if os.path.normcase(os.path.abspath(path)) == os.path.normcase(os.path.abspath(locked_dir)):
                    raise PermissionError(13, f"Access denied: {path}")
                return real_scandir(path)

            survey = survey_directory(
                tmp_dir,
                get_file_attributes=lambda p: (
                    FILE_ATTRIBUTE_DIRECTORY if os.path.isdir(p) else FILE_ATTRIBUTE_ARCHIVE
                ),
                get_drive_type=lambda _r: DRIVE_FIXED,
                scandir_fn=selective_scandir,
                platform_is_windows=True,
            )

            self.assertEqual(survey.local_files, 1)
            self.assertEqual(survey.local_bytes, 4)
            self.assertEqual(survey.unknown_files, 1)
            self.assertEqual(len(survey.unknown_entries), 1)
            self.assertEqual(survey.unknown_entries[0].residency, Residency.UNKNOWN)
            self.assertIn("PermissionError", survey.unknown_entries[0].reason)


if __name__ == "__main__":
    unittest.main()
