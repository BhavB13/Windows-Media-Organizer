import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import drive_cache
from drive_cache import DriveHashCache, default_cache_path
import volume_identity
from volume_identity import (
    FAT_MAX_FILE_BYTES,
    IDENTITY_TIER_GUID,
    IDENTITY_TIER_PATH,
    IDENTITY_TIER_SERIAL,
    VolumeIdentity,
    clear_probe_cache,
    has_4gib_file_limit,
    has_fat_4gib_limit,
    is_volume_online,
    preserves_subsecond_mtime,
    probe_volume,
    relative_path_from_mount,
    resolve_mount_point,
    supports_subsecond_mtime,
)
from duplicate_transfer_manager.runtime_paths import get_runtime_paths


class FakeKernel32:
    """Deterministic in-memory stand-in for Win32 kernel32 volume APIs."""

    def __init__(self, volumes=None, *, raise_error=None):
        # Map normalised mount point (e.g. "E:\\") -> dict with guid, serial, fs, label
        self.volumes = {
            os.path.normcase(root): dict(info)
            for root, info in (volumes or {}).items()
        }
        self.raise_error = raise_error
        self.info_calls = []
        self.guid_calls = []

    def GetVolumeInformationW(
        self,
        root,
        name_buffer,
        name_len,
        serial_ptr,
        max_component_ptr,
        flags_ptr,
        fs_buffer,
        fs_len,
    ):
        self.info_calls.append(root)
        if self.raise_error is not None:
            raise self.raise_error
        entry = self.volumes.get(os.path.normcase(root))
        if not entry or not entry.get("info_ok", True):
            return 0
        name_buffer.value = str(entry.get("label", ""))[: max(0, name_len - 1)]
        fs_buffer.value = str(entry.get("filesystem", "NTFS"))[: max(0, fs_len - 1)]
        serial_val = int(entry.get("serial", 0))
        if hasattr(serial_ptr, "_obj"):
            serial_ptr._obj.value = serial_val
        elif hasattr(serial_ptr, "contents"):
            serial_ptr.contents.value = serial_val
        return 1

    def GetVolumeNameForVolumeMountPointW(self, root, guid_buffer, guid_len):
        self.guid_calls.append(root)
        if self.raise_error is not None:
            raise self.raise_error
        entry = self.volumes.get(os.path.normcase(root))
        if not entry or not entry.get("guid_ok", True):
            return 0
        guid = str(entry.get("guid", ""))
        if not guid:
            return 0
        guid_buffer.value = guid[: max(0, guid_len - 1)]
        return 1


class VolumeIdentityTests(unittest.TestCase):
    def setUp(self):
        clear_probe_cache()

    def tearDown(self):
        clear_probe_cache()

    def test_stable_key_tier_preference_and_flags(self):
        with_all = VolumeIdentity(
            guid=r"\\?\Volume{12345678-1234-1234-1234-1234567890AB}\\",
            serial="CAFEBABE",
            filesystem="NTFS",
            label="MediaSSD",
            mount_point="E:\\",
        )
        with_same_guid_diff_serial_and_letter = VolumeIdentity(
            guid=r"\\?\Volume{12345678-1234-1234-1234-1234567890ab}\\",
            serial="DEADBEEF",
            filesystem="NTFS",
            label="Renamed",
            mount_point="F:\\",
        )
        self.assertEqual(with_all.tier, IDENTITY_TIER_GUID)
        self.assertFalse(with_all.is_fallback)
        self.assertFalse(with_all.is_path_fallback)
        self.assertTrue(with_all.is_strong)
        self.assertEqual(with_all.stable_key, with_same_guid_diff_serial_and_letter.stable_key)
        self.assertIn("12345678-1234-1234-1234-1234567890ab", with_all.stable_key)

        serial_only_e = VolumeIdentity(
            guid="",
            serial="1A2B3C4D",
            filesystem="exFAT",
            label="CameraCard",
            mount_point="E:\\",
        )
        serial_only_f = VolumeIdentity(
            guid="",
            serial="1a2b3c4d",
            filesystem="exFAT",
            label="CameraCard",
            mount_point="F:\\",
        )
        self.assertEqual(serial_only_e.tier, IDENTITY_TIER_SERIAL)
        self.assertFalse(serial_only_e.is_fallback)
        self.assertFalse(serial_only_e.is_path_fallback)
        self.assertTrue(serial_only_e.is_strong)
        self.assertEqual(serial_only_e.stable_key, serial_only_f.stable_key)
        self.assertIn("1a2b3c4d", serial_only_e.stable_key)

        fallback = VolumeIdentity(
            guid="",
            serial="",
            filesystem="",
            label="",
            mount_point="E:\\",
        )
        self.assertEqual(fallback.tier, IDENTITY_TIER_PATH)
        self.assertTrue(fallback.is_fallback)
        self.assertTrue(fallback.is_path_fallback)
        self.assertFalse(fallback.is_strong)
        self.assertEqual(fallback.stable_key, "E")

    def test_nested_path_resolves_to_volume_identity_and_caches_by_mount_point(self):
        fake_k32 = FakeKernel32(
            {
                "E:\\": {
                    "guid": r"\\?\Volume{aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee}\\",
                    "serial": 0x11223344,
                    "filesystem": "NTFS",
                    "label": "ExternalVault",
                }
            }
        )
        fake_windll = mock.Mock(kernel32=fake_k32)
        with (
            mock.patch.object(volume_identity.os, "name", "nt"),
            mock.patch.object(volume_identity.ctypes, "windll", fake_windll, create=True),
        ):
            root_id = probe_volume("E:\\")
            nested_id = probe_volume(r"E:\Photos\2026\Vacation\RAW\IMG_0001.CR3")

        self.assertEqual(root_id, nested_id)
        self.assertEqual(nested_id.mount_point, "E:\\")
        self.assertEqual(nested_id.tier, IDENTITY_TIER_GUID)
        self.assertEqual(nested_id.serial, "11223344")
        self.assertEqual(nested_id.filesystem, "NTFS")
        self.assertEqual(nested_id.label, "ExternalVault")
        # Both paths share mount point E:\, so the OS probe ran only once.
        self.assertEqual(len(fake_k32.info_calls), 1)
        self.assertEqual(len(fake_k32.guid_calls), 1)

        clear_probe_cache()
        with (
            mock.patch.object(volume_identity.os, "name", "nt"),
            mock.patch.object(volume_identity.ctypes, "windll", fake_windll, create=True),
        ):
            probe_volume("E:\\Photos")
        self.assertEqual(len(fake_k32.info_calls), 2)

    def test_probe_failure_degrades_to_path_fallback_without_raising(self):
        with mock.patch.object(volume_identity.os, "name", "posix"):
            posix_identity = probe_volume("E:\\Photos\\2026")
        self.assertTrue(posix_identity.is_fallback)
        self.assertEqual(posix_identity.tier, IDENTITY_TIER_PATH)
        self.assertEqual(posix_identity.mount_point, "E:\\")

        for error in (OSError("device not ready"), AttributeError("no windll"), RuntimeError("ctypes boom")):
            clear_probe_cache()
            failing_k32 = FakeKernel32(raise_error=error)
            with (
                mock.patch.object(volume_identity.os, "name", "nt"),
                mock.patch.object(
                    volume_identity.ctypes,
                    "windll",
                    mock.Mock(kernel32=failing_k32),
                    create=True,
                ),
            ):
                fallback = probe_volume("E:\\Photos\\2026")
            self.assertTrue(fallback.is_fallback)
            self.assertEqual(fallback.tier, IDENTITY_TIER_PATH)
            self.assertEqual(fallback.stable_key, "E")

        # When GUID fails but GetVolumeInformationW succeeds, serial tier is used.
        clear_probe_cache()
        serial_k32 = FakeKernel32(
            {
                "E:\\": {
                    "guid": "",
                    "guid_ok": False,
                    "serial": 0xABCDEF01,
                    "filesystem": "exFAT",
                    "label": "SDCard",
                }
            }
        )
        with (
            mock.patch.object(volume_identity.os, "name", "nt"),
            mock.patch.object(
                volume_identity.ctypes,
                "windll",
                mock.Mock(kernel32=serial_k32),
                create=True,
            ),
        ):
            serial_id = probe_volume("E:\\DCIM\\100MEDIA")
        self.assertEqual(serial_id.tier, IDENTITY_TIER_SERIAL)
        self.assertFalse(serial_id.is_fallback)
        self.assertEqual(serial_id.serial, "ABCDEF01")
        self.assertEqual(serial_id.filesystem, "exFAT")

    def test_simulated_letter_change_e_to_f_with_same_guid_yields_same_cache_path(self):
        shared_guid = r"\\?\Volume{99999999-8888-7777-6666-555555555555}\\"
        fake_k32 = FakeKernel32(
            {
                "E:\\": {
                    "guid": shared_guid,
                    "serial": 0x12345678,
                    "filesystem": "NTFS",
                    "label": "PortableSSD",
                },
                "F:\\": {
                    "guid": shared_guid,
                    "serial": 0x12345678,
                    "filesystem": "NTFS",
                    "label": "PortableSSD",
                },
            }
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            with (
                mock.patch.dict(os.environ, {"DTM_DATA_DIR": temp_dir}, clear=False),
                mock.patch.object(volume_identity.os, "name", "nt"),
                mock.patch.object(
                    volume_identity.ctypes,
                    "windll",
                    mock.Mock(kernel32=fake_k32),
                    create=True,
                ),
            ):
                path_on_e = default_cache_path(r"E:\Photos\Family\2025")
                path_on_f = default_cache_path(r"F:\Photos\Family\2025")
                root_on_e = default_cache_path("E:\\")
                root_on_f = default_cache_path("F:\\")

        self.assertEqual(path_on_e, path_on_f)
        self.assertEqual(root_on_e, root_on_f)
        self.assertNotEqual(path_on_e, root_on_e)
        self.assertIn("Photos_Family_2025", os.path.basename(path_on_e))

    def test_two_different_guids_on_same_letter_yield_different_cache_paths(self):
        drive_one = FakeKernel32(
            {
                "E:\\": {
                    "guid": r"\\?\Volume{11111111-1111-1111-1111-111111111111}\\",
                    "serial": 0x11111111,
                    "filesystem": "NTFS",
                    "label": "DriveOne",
                }
            }
        )
        drive_two = FakeKernel32(
            {
                "E:\\": {
                    "guid": r"\\?\Volume{22222222-2222-2222-2222-222222222222}\\",
                    "serial": 0x22222222,
                    "filesystem": "NTFS",
                    "label": "DriveTwo",
                }
            }
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            with mock.patch.dict(os.environ, {"DTM_DATA_DIR": temp_dir}, clear=False):
                with (
                    mock.patch.object(volume_identity.os, "name", "nt"),
                    mock.patch.object(
                        volume_identity.ctypes,
                        "windll",
                        mock.Mock(kernel32=drive_one),
                        create=True,
                    ),
                ):
                    first_cache_path = default_cache_path(r"E:\Photos\Archive")

                clear_probe_cache()

                with (
                    mock.patch.object(volume_identity.os, "name", "nt"),
                    mock.patch.object(
                        volume_identity.ctypes,
                        "windll",
                        mock.Mock(kernel32=drive_two),
                        create=True,
                    ),
                ):
                    second_cache_path = default_cache_path(r"E:\Photos\Archive")

        self.assertNotEqual(first_cache_path, second_cache_path)

    def test_path_fallback_matches_legacy_default_cache_path_behaviour_exactly(self):
        failing_k32 = FakeKernel32(raise_error=OSError("no volume info"))
        with tempfile.TemporaryDirectory() as temp_dir:
            with (
                mock.patch.dict(os.environ, {"DTM_DATA_DIR": temp_dir}, clear=False),
                mock.patch.object(volume_identity.os, "name", "nt"),
                mock.patch.object(
                    volume_identity.ctypes,
                    "windll",
                    mock.Mock(kernel32=failing_k32),
                    create=True,
                ),
            ):
                cache_path = default_cache_path(r"E:\Photos\2025")
                expected = get_runtime_paths().drive_caches / "E_Photos_2025.json"

        self.assertEqual(Path(cache_path), expected)

    def test_legacy_letter_keyed_cache_is_migrated_and_original_left_on_disk(self):
        fake_k32 = FakeKernel32(
            {
                "E:\\": {
                    "guid": r"\\?\Volume{abcdef01-2345-6789-abcd-ef0123456789}\\",
                    "serial": 0xAABBCCDD,
                    "filesystem": "NTFS",
                    "label": "BackupDrive",
                }
            }
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            with mock.patch.dict(os.environ, {"DTM_DATA_DIR": temp_dir}, clear=False):
                caches_dir = get_runtime_paths().drive_caches
                legacy_path = caches_dir / "E_Photos_Library.json"
                digest = hashlib.sha256(b"important-media").hexdigest()

                legacy_cache = DriveHashCache(str(legacy_path))
                legacy_cache.set_file_hash(
                    r"E:\Photos\Library\img001.jpg",
                    size=1024,
                    mtime=1700000000.0,
                    digest=digest,
                    algo="sha256",
                    mode="full",
                    root=r"E:\Photos\Library",
                )
                legacy_cache.save()
                original_legacy_bytes = legacy_path.read_bytes()

                with (
                    mock.patch.object(volume_identity.os, "name", "nt"),
                    mock.patch.object(
                        volume_identity.ctypes,
                        "windll",
                        mock.Mock(kernel32=fake_k32),
                        create=True,
                    ),
                ):
                    migrated_path = Path(default_cache_path(r"E:\Photos\Library"))

                self.assertNotEqual(migrated_path, legacy_path)
                self.assertTrue(migrated_path.is_file(), "new volume-keyed cache file must be created")
                self.assertTrue(legacy_path.is_file(), "legacy cache file must remain in place for downgrades")
                self.assertEqual(legacy_path.read_bytes(), original_legacy_bytes)

                raw_migrated = json.loads(migrated_path.read_text(encoding="utf-8"))
                self.assertEqual(raw_migrated.get("migrated_from"), str(legacy_path))
                self.assertIn("migration", raw_migrated)
                self.assertEqual(raw_migrated["migration"]["legacy_path"], str(legacy_path))

                reloaded = DriveHashCache(str(migrated_path))
                reloaded.load()
                self.assertEqual(
                    reloaded.get_valid_hash(
                        r"E:\Photos\Library\img001.jpg",
                        "sha256",
                        "full",
                        1024,
                        1700000000.0,
                    ),
                    digest,
                )

    def test_existing_new_style_cache_is_not_overwritten_by_migration(self):
        fake_k32 = FakeKernel32(
            {
                "E:\\": {
                    "guid": r"\\?\Volume{abcdef01-2345-6789-abcd-ef0123456789}\\",
                    "serial": 0xAABBCCDD,
                    "filesystem": "NTFS",
                    "label": "BackupDrive",
                }
            }
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            with mock.patch.dict(os.environ, {"DTM_DATA_DIR": temp_dir}, clear=False):
                caches_dir = get_runtime_paths().drive_caches
                legacy_path = caches_dir / "E_Photos_Library.json"
                old_digest = "a" * 64
                new_digest = "b" * 64

                with (
                    mock.patch.object(volume_identity.os, "name", "nt"),
                    mock.patch.object(
                        volume_identity.ctypes,
                        "windll",
                        mock.Mock(kernel32=fake_k32),
                        create=True,
                    ),
                ):
                    target_new_path = Path(default_cache_path(r"E:\Photos\Library"))

                    # Populate the new-style cache first.
                    new_cache = DriveHashCache(str(target_new_path))
                    new_cache.set_file_hash(
                        r"E:\Photos\Library\current.jpg",
                        size=2048,
                        mtime=1700000100.0,
                        digest=new_digest,
                        algo="sha256",
                        mode="full",
                        root=r"E:\Photos\Library",
                    )
                    new_cache.save()

                    # Now also place a legacy cache file on disk.
                    legacy_cache = DriveHashCache(str(legacy_path))
                    legacy_cache.set_file_hash(
                        r"E:\Photos\Library\stale_legacy.jpg",
                        size=512,
                        mtime=1600000000.0,
                        digest=old_digest,
                        algo="sha256",
                        mode="full",
                        root=r"E:\Photos\Library",
                    )
                    legacy_cache.save()

                    resolved_again = Path(default_cache_path(r"E:\Photos\Library"))

                self.assertEqual(resolved_again, target_new_path)
                check_cache = DriveHashCache(str(target_new_path))
                check_cache.load()
                self.assertTrue(check_cache.has_hash(new_digest))
                self.assertFalse(check_cache.has_hash(old_digest))

    def test_mark_missing_under_root_refuses_to_mark_entries_stale_while_volume_is_offline(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            cache_file = os.path.join(temp_dir, "drive_cache.json")
            cache = DriveHashCache(cache_file)
            digest_a = hashlib.sha256(b"photo-a").hexdigest()
            digest_b = hashlib.sha256(b"photo-b").hexdigest()
            file_a = r"E:\Photos\2025\a.jpg"
            file_b = r"E:\Photos\2025\b.jpg"

            cache.set_file_hash(file_a, 100, 1700000000.0, digest_a, "sha256", "full", root=r"E:\Photos")
            cache.set_file_hash(file_b, 200, 1700000001.0, digest_b, "sha256", "full", root=r"E:\Photos")

            # Simulate E:\ being unplugged / offline (kernel32 fails for E:\).
            offline_k32 = FakeKernel32({})
            with (
                mock.patch.object(volume_identity.os, "name", "nt"),
                mock.patch.object(
                    volume_identity.ctypes,
                    "windll",
                    mock.Mock(kernel32=offline_k32),
                    create=True,
                ),
            ):
                self.assertFalse(is_volume_online(r"E:\Photos"))
                stale_marked = cache.mark_missing_under_root(r"E:\Photos", [])

            self.assertEqual(stale_marked, 0, "disconnected drive must never mark entries stale")
            self.assertEqual(cache.stats().stale, 0)
            self.assertEqual(
                cache.get_valid_hash(file_a, "sha256", "full", 100, 1700000000.0),
                digest_a,
            )
            self.assertEqual(
                cache.get_valid_hash(file_b, "sha256", "full", 200, 1700000001.0),
                digest_b,
            )

            # Now simulate E:\ being online; file_a is present and file_b was genuinely deleted.
            online_k32 = FakeKernel32(
                {
                    "E:\\": {
                        "guid": r"\\?\Volume{12345678-1234-1234-1234-1234567890ab}\\",
                        "serial": 0x12345678,
                        "filesystem": "NTFS",
                        "label": "OnlineDrive",
                    }
                }
            )
            with (
                mock.patch.object(volume_identity.os, "name", "nt"),
                mock.patch.object(
                    volume_identity.ctypes,
                    "windll",
                    mock.Mock(kernel32=online_k32),
                    create=True,
                ),
            ):
                self.assertTrue(is_volume_online(file_b), "volume is online even when individual file was deleted")
                stale_marked = cache.mark_missing_under_root(r"E:\Photos", [file_a])

            self.assertEqual(stale_marked, 1)
            self.assertEqual(cache.stats().stale, 1)
            self.assertIsNotNone(cache.get_valid_hash(file_a, "sha256", "full", 100, 1700000000.0))
            self.assertIsNone(cache.get_valid_hash(file_b, "sha256", "full", 200, 1700000001.0))

    def test_is_volume_online_detects_different_volume_mounted_on_same_letter(self):
        expected_identity = VolumeIdentity(
            guid=r"\\?\Volume{11111111-1111-1111-1111-111111111111}\\",
            serial="11111111",
            filesystem="NTFS",
            label="ExpectedDrive",
            mount_point="E:\\",
        )
        other_drive_k32 = FakeKernel32(
            {
                "E:\\": {
                    "guid": r"\\?\Volume{22222222-2222-2222-2222-222222222222}\\",
                    "serial": 0x22222222,
                    "filesystem": "NTFS",
                    "label": "DifferentDrive",
                }
            }
        )
        with (
            mock.patch.object(volume_identity.os, "name", "nt"),
            mock.patch.object(
                volume_identity.ctypes,
                "windll",
                mock.Mock(kernel32=other_drive_k32),
                create=True,
            ),
        ):
            self.assertFalse(is_volume_online(expected_identity))

    def test_fat32_and_exfat_constraint_helpers(self):
        # FAT32: coarse 2s mtime (no sub-second precision) AND 4 GiB per-file limit
        self.assertFalse(preserves_subsecond_mtime("FAT32"))
        self.assertFalse(supports_subsecond_mtime("fat32"))
        self.assertTrue(has_fat_4gib_limit("FAT32"))
        self.assertTrue(has_4gib_file_limit("fat32"))

        # exFAT: coarse mtime (no reliable sub-second precision), NO 4 GiB limit
        self.assertFalse(preserves_subsecond_mtime("exFAT"))
        self.assertFalse(supports_subsecond_mtime("exfat"))
        self.assertFalse(has_fat_4gib_limit("exFAT"))
        self.assertFalse(has_4gib_file_limit("exfat"))

        # NTFS: sub-second mtime precision (100ns) AND no 4 GiB limit
        self.assertTrue(preserves_subsecond_mtime("NTFS"))
        self.assertTrue(supports_subsecond_mtime("ntfs"))
        self.assertFalse(has_fat_4gib_limit("NTFS"))
        self.assertFalse(has_4gib_file_limit("ntfs"))

        fat32_vol = VolumeIdentity(guid="{1}", serial="1", filesystem="FAT32", label="USB", mount_point="E:\\")
        exfat_vol = VolumeIdentity(guid="{2}", serial="2", filesystem="exFAT", label="SD", mount_point="F:\\")
        ntfs_vol = VolumeIdentity(guid="{3}", serial="3", filesystem="NTFS", label="SSD", mount_point="G:\\")

        self.assertFalse(fat32_vol.preserves_subsecond_mtime)
        self.assertTrue(fat32_vol.has_fat_4gib_limit)

        self.assertFalse(exfat_vol.preserves_subsecond_mtime)
        self.assertFalse(exfat_vol.has_fat_4gib_limit)

        self.assertTrue(ntfs_vol.preserves_subsecond_mtime)
        self.assertFalse(ntfs_vol.has_fat_4gib_limit)
        self.assertEqual(FAT_MAX_FILE_BYTES, (1 << 32) - 1)


if __name__ == "__main__":
    unittest.main()
