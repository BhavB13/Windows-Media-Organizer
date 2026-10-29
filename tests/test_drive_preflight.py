"""External-drive behaviour of the transfer path.

Covers what happens when the destination is a drive rather than a folder: the
FAT32 4 GiB per-file limit, a destination that is unplugged part-way through,
and source and destination folders that overlap - including through a link.

No test needs a real drive or a multi-gigabyte file. The filesystem is faked
through ``probe_volume`` and the size limit is lowered for the test, so the
real code paths run against a few bytes of data.
"""

from __future__ import annotations

import os
import shutil
import tempfile
import threading
import unittest
from types import SimpleNamespace
from unittest import mock

import engine as engine_module
import transfer_safety
from models import TransferSettings
from utils import HashCache
from volume_identity import FAT_MAX_FILE_BYTES, VolumeIdentity

from tests.test_engine import DummyLogger


def _identity(filesystem: str, *, fallback: bool = False) -> VolumeIdentity:
    return VolumeIdentity(
        guid="" if fallback else "{00000000-0000-0000-0000-000000000001}",
        filesystem=filesystem,
        mount_point="",
        tier="fallback" if fallback else "guid",
        is_fallback=fallback,
    )


def _settings(source, library, output, root, **overrides) -> TransferSettings:
    settings = TransferSettings(
        source_root=source, dest_root=library, output_root=output,
        criteria="hash", hash_algo="sha256", hash_mode="full", only_media=False,
        extensions=[], min_size_kb=0, exclude_dirs=[], skip_hidden_system=False,
        dry_run=False, preserve_structure=True, max_hash_workers=1,
        transfer_mode="copy", duplicate_policy="skip", use_dest_cache=False,
        source_is_adb=False, update_drive_cache=False, use_adb_cache=False,
        conflict_policy="rename",
    )
    # Keep the resume journal inside the test's own directory.
    settings.journal_path = os.path.join(root, "journal.json")
    for key, value in overrides.items():
        setattr(settings, key, value)
    return settings


def _write(path: str, data: bytes) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as handle:
        handle.write(data)


def _snapshot(root: str) -> dict[str, tuple[bytes, int]]:
    out = {}
    for folder, _dirs, files in os.walk(root):
        for name in files:
            path = os.path.join(folder, name)
            with open(path, "rb") as handle:
                out[path] = (handle.read(), os.stat(path).st_mtime_ns)
    return out


class FatSizeLimitTests(unittest.TestCase):
    def test_boundary_is_four_gib_minus_one_byte(self):
        files = [
            SimpleNamespace(path="fits", size=FAT_MAX_FILE_BYTES),
            SimpleNamespace(path="too-big", size=FAT_MAX_FILE_BYTES + 1),
        ]
        with mock.patch.object(transfer_safety, "probe_volume", return_value=_identity("FAT32")):
            too_large, filesystem = transfer_safety.files_too_large_for_destination("X:\\", files)
        self.assertEqual([f.path for f in too_large], ["too-big"])
        self.assertEqual(filesystem, "FAT32")

    def test_exfat_ntfs_and_unidentified_drives_limit_nothing(self):
        huge = [SimpleNamespace(path="video.mp4", size=FAT_MAX_FILE_BYTES * 3)]
        for identity in (_identity("exFAT"), _identity("NTFS"), _identity("", fallback=True)):
            with self.subTest(filesystem=identity.filesystem or "fallback"):
                with mock.patch.object(transfer_safety, "probe_volume", return_value=identity):
                    too_large, _ = transfer_safety.files_too_large_for_destination("X:\\", huge)
                self.assertEqual(too_large, [])

    def test_preflight_warns_before_anything_is_written(self):
        with tempfile.TemporaryDirectory() as out:
            files = [SimpleNamespace(path="a", size=FAT_MAX_FILE_BYTES + 1)]
            with mock.patch.object(transfer_safety, "probe_volume", return_value=_identity("FAT32")):
                errors, warnings = transfer_safety.preflight_transfer(out, files, verify_write=False)
        self.assertEqual(errors, [], "an oversize file must not block the files that do fit")
        self.assertTrue(any("FAT32" in w and "4 GB" in w for w in warnings), warnings)

    def test_oversize_files_are_skipped_and_reported_while_the_rest_copy(self):
        with tempfile.TemporaryDirectory() as root:
            source = os.path.join(root, "source")
            library = os.path.join(root, "library")
            output = os.path.join(root, "usb")
            os.makedirs(library)
            os.makedirs(output)
            _write(os.path.join(source, "small.jpg"), b"a small photo")
            _write(os.path.join(source, "long-video.mp4"), b"a video far too long for FAT32" * 4)
            before = _snapshot(source)

            # Lower the limit so a few bytes stand in for 4 GiB.
            with mock.patch.object(transfer_safety, "probe_volume", return_value=_identity("FAT32")), \
                    mock.patch.object(transfer_safety, "FAT_MAX_FILE_BYTES", 40):
                result = engine_module.execute_smart_transfer(
                    _settings(source, library, output, root),
                    threading.Event(), HashCache(os.path.join(root, "cache.json")), DummyLogger(),
                )

            self.assertEqual(result["too_large_skipped"], 1)
            self.assertEqual(result["transferred"], 1)
            self.assertTrue(os.path.exists(os.path.join(output, "small.jpg")))
            self.assertFalse(os.path.exists(os.path.join(output, "long-video.mp4")))
            # No partial copy of the oversize file was started anywhere.
            leftovers = [name for _d, _s, names in os.walk(output) for name in names if "long-video" in name]
            self.assertEqual(leftovers, [])
            self.assertEqual(_snapshot(source), before, "the source must be untouched")


class DestinationDisconnectTests(unittest.TestCase):
    def test_unplugging_the_destination_stops_cleanly_and_resumes_later(self):
        with tempfile.TemporaryDirectory() as root:
            source = os.path.join(root, "source")
            library = os.path.join(root, "library")
            drive = os.path.join(root, "drive")
            unplugged = os.path.join(root, "drive-unplugged")
            output = os.path.join(drive, "photos")
            os.makedirs(library)
            os.makedirs(output)
            for index in range(4):
                _write(os.path.join(source, f"photo{index}.jpg"), f"photo number {index}".encode())
            before = _snapshot(source)

            real_copy = shutil.copy2
            calls = {"n": 0}

            def copy_then_unplug(src, dst, *args, **kwargs):
                calls["n"] += 1
                if calls["n"] == 2:
                    # The drive drops off the bus during the second copy.
                    os.rename(drive, unplugged)
                    raise OSError(2, "The system cannot find the path specified", dst)
                return real_copy(src, dst, *args, **kwargs)

            settings = _settings(source, library, output, root)
            with mock.patch.object(engine_module.shutil, "copy2", side_effect=copy_then_unplug):
                first = engine_module.execute_smart_transfer(
                    settings, threading.Event(), HashCache(os.path.join(root, "cache.json")), DummyLogger(),
                )

            self.assertTrue(first["destination_disconnected"])
            self.assertEqual(first["transferred"], 1)
            # It stopped at the disconnect instead of failing every remaining file.
            self.assertEqual(calls["n"], 2)
            self.assertEqual(_snapshot(source), before)

            # Reconnect and run again: the completed file is resumed, the rest copy.
            os.rename(unplugged, drive)
            second = engine_module.execute_smart_transfer(
                _settings(source, library, output, root),
                threading.Event(), HashCache(os.path.join(root, "cache.json")), DummyLogger(),
            )
            self.assertFalse(second["destination_disconnected"])
            self.assertEqual(second["resumed"], 1)
            self.assertEqual(second["transferred"], 3)
            self.assertEqual(
                sorted(name for name in os.listdir(output) if name.endswith(".jpg")),
                [f"photo{index}.jpg" for index in range(4)],
            )
            self.assertEqual(_snapshot(source), before)

    def test_an_ordinary_file_failure_does_not_stop_the_run(self):
        with tempfile.TemporaryDirectory() as root:
            source = os.path.join(root, "source")
            library = os.path.join(root, "library")
            output = os.path.join(root, "out")
            os.makedirs(library)
            os.makedirs(output)
            for index in range(3):
                _write(os.path.join(source, f"p{index}.jpg"), f"p{index}".encode())

            real_copy = shutil.copy2
            calls = {"n": 0}

            def fail_once(src, dst, *args, **kwargs):
                calls["n"] += 1
                if calls["n"] == 1:
                    raise PermissionError(13, "Access is denied", dst)
                return real_copy(src, dst, *args, **kwargs)

            with mock.patch.object(engine_module.shutil, "copy2", side_effect=fail_once):
                result = engine_module.execute_smart_transfer(
                    _settings(source, library, output, root),
                    threading.Event(), HashCache(os.path.join(root, "cache.json")), DummyLogger(),
                )
            self.assertFalse(result["destination_disconnected"])
            self.assertEqual(result["transferred"], 2)
            self.assertEqual(result["errors"], 1)


class OverlapTests(unittest.TestCase):
    def _validate(self, source, library, output, root):
        return engine_module.validate_transfer_paths(_settings(source, library, output, root))

    def test_source_inside_the_compare_folder_is_refused(self):
        # Previously accepted: every new photo matched itself during comparison
        # and the run reported them all as already in the library.
        with tempfile.TemporaryDirectory() as root:
            library = os.path.join(root, "library")
            inbox = os.path.join(library, "inbox")
            output = os.path.join(root, "output")
            os.makedirs(inbox)
            os.makedirs(output)
            message = self._validate(inbox, library, output, root)
        self.assertIn("inside the compare folder", message)

    def test_source_inside_the_output_folder_is_refused(self):
        with tempfile.TemporaryDirectory() as root:
            library = os.path.join(root, "library")
            output = os.path.join(root, "output")
            source = os.path.join(output, "incoming")
            os.makedirs(library)
            os.makedirs(source)
            message = self._validate(source, library, output, root)
        self.assertIn("inside the output folder", message)

    def test_existing_checks_are_unchanged(self):
        with tempfile.TemporaryDirectory() as root:
            source = os.path.join(root, "source")
            library = os.path.join(source, "library")
            output = os.path.join(root, "output")
            os.makedirs(library)
            os.makedirs(output)
            self.assertEqual(
                self._validate(source, library, output, root),
                "Compare folder cannot be inside the source folder.",
            )

    def test_separate_folders_still_validate(self):
        with tempfile.TemporaryDirectory() as root:
            paths = [os.path.join(root, name) for name in ("source", "library", "output")]
            for path in paths:
                os.makedirs(path)
            self.assertEqual(self._validate(*paths, root), "")

    def test_overlap_through_a_junction_is_refused(self):
        try:
            import _winapi
        except ImportError:
            self.skipTest("directory junctions are Windows-only")
        with tempfile.TemporaryDirectory() as root:
            source = os.path.join(root, "source")
            library = os.path.join(root, "library")
            os.makedirs(source)
            os.makedirs(library)
            # A junction makes "output" look unrelated while it is the source.
            output = os.path.join(root, "looks-different")
            try:
                _winapi.CreateJunction(source, output)
            except OSError as exc:
                self.skipTest(f"cannot create a junction here: {exc}")
            try:
                message = self._validate(source, library, output, root)
            finally:
                os.rmdir(output)  # removes the junction only, never its target
        self.assertIn("same folder", message)


if __name__ == "__main__":
    unittest.main()
