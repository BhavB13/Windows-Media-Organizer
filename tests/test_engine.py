import os
import hashlib
import json
import tempfile
import threading
import unittest
from unittest import mock

import adb_bridge
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
    HashCancelled,
    build_compare_scan_settings,
    build_transfer_hash_settings,
    build_relative_path,
    build_target_path,
    execute_smart_transfer,
    compute_hash,
    group_duplicates,
    promote_transfer_file,
    iter_files,
    validate_transfer_paths,
    validate_scan_paths,
)
from models import FileInfo, Settings, TransferSettings
from utils import HashCache
from transfer_safety import TransferJournal


class DummyLogger:
    def __init__(self):
        self.messages = []

    def log(self, msg):
        self.messages.append(msg)


class EngineTests(unittest.TestCase):
    def test_transfer_journal_rejects_same_size_changed_target(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            target = os.path.join(temp_dir, "target.bin")
            with open(target, "wb") as handle:
                handle.write(b"original")
            journal = TransferJournal(os.path.join(temp_dir, "journal.json"))
            digest = hashlib.sha256(b"original").hexdigest()
            journal.complete("source", target, 8, digest, hash_algo="sha256", hash_mode="full")
            recorded = journal.data["completed"]["source"]["target_mtime"]
            self.assertTrue(journal.is_complete("source", 8))

            with open(target, "wb") as handle:
                handle.write(b"modified")
            # Set the timestamp explicitly rather than relying on the rewrite to
            # move it. The Windows clock only ticks about every 16 ms, so two
            # writes inside one tick can share an mtime and make this flaky.
            os.utime(target, (recorded + 60, recorded + 60))
            self.assertFalse(journal.is_complete("source", 8))

    def _run_adb_scan(self, stdout, returncode=0, stderr="", settings=None):
        """Drive scan_adb_tree against a replayed device listing."""
        import io
        import discovery as discovery_module

        class FakeProcess:
            def __init__(self):
                self.stdout = io.StringIO(stdout)
                self.stderr = io.StringIO(stderr)
                self.returncode = returncode

            def wait(self, timeout=None):
                return returncode

            def terminate(self):
                pass

            def kill(self):
                pass

        class DefaultSettings:
            exclude_dirs = []
            extensions = []
            only_media = False
            min_size_kb = 0
            skip_hidden_system = True
            adb_serial = "SERIAL"
            use_adb = True

        with mock.patch.object(discovery_module.subprocess, "Popen", return_value=FakeProcess()), mock.patch(
            "adb_bridge.ADBBridge.probe_device", return_value=(True, "")
        ), mock.patch("adb_bridge.ADBBridge.remote_path_status", return_value="dir"):
            return discovery_module.scan_adb_tree(
                "/sdcard/DCIM", settings or DefaultSettings(), threading.Event()
            )

    def test_adb_scan_walks_every_nested_subfolder(self):
        # The whole point of the recursive walk: a file several levels down,
        # in a folder whose name contains a space, must be found.
        listing = "\n".join(
            [
                "D|/sdcard/DCIM",
                "D|/sdcard/DCIM/Camera",
                "D|/sdcard/DCIM/Camera/2024/07",
                "D|/sdcard/DCIM/Camera/nested deep/level3/level4",
                "F|10|1700000000|/sdcard/DCIM/top.jpg",
                "F|11|1700000001|/sdcard/DCIM/Camera/2024/07/IMG_0001.JPG",
                "F|12|1700000002|/sdcard/DCIM/Camera/nested deep/level3/level4/deep.mp4",
                # A literal pipe in a file name must survive parsing.
                "F|13|1700000003|/sdcard/DCIM/Camera/pipe|name.jpg",
            ]
        ) + "\n"
        result = self._run_adb_scan(listing)

        self.assertEqual(len(result.files), 4)
        self.assertFalse(result.incomplete)
        self.assertIn("/sdcard/DCIM/Camera/nested deep/level3/level4/deep.mp4", [f.path for f in result.files])
        self.assertIn("/sdcard/DCIM/Camera/pipe|name.jpg", [f.path for f in result.files])

    def test_adb_scan_reports_a_truncated_listing_as_incomplete(self):
        # A phone unplugged mid-scan must not look like a folder that simply
        # held fewer files, or the import silently leaves the rest behind.
        result = self._run_adb_scan(
            "F|10|1700000000|/sdcard/DCIM/a.jpg\n",
            returncode=1,
            stderr="adb: error: device 'R58M12ABCDE' not found\n",
        )

        self.assertEqual(len(result.files), 1)
        self.assertTrue(result.incomplete)
        self.assertTrue(any("not found" in error for error in result.errors))

    def test_adb_scan_accounts_for_files_it_could_not_stat(self):
        # find listed the file but stat failed. Previously it vanished with no
        # error, no count, and no log line.
        result = self._run_adb_scan(
            "F|10|1700000000|/sdcard/DCIM/ok.jpg\nE|/sdcard/DCIM/locked.jpg\n"
        )

        self.assertEqual(len(result.files), 1)
        self.assertTrue(result.incomplete)
        self.assertEqual(result.unreadable, ("/sdcard/DCIM/locked.jpg",))

    def test_adb_scan_preserves_trailing_whitespace_in_names(self):
        # A trailing space is legal on Android. Trimming it produced a path
        # that does not exist on the device.
        result = self._run_adb_scan("F|10|1700000000|/sdcard/DCIM/photo.jpg \n")

        self.assertEqual(result.files[0].path, "/sdcard/DCIM/photo.jpg ")

    def test_remote_walk_follows_symlinks_for_every_root_spelling(self):
        import discovery as discovery_module

        class ExcludeSettings:
            exclude_dirs = ["android"]

        # A symlinked subfolder is neither -type d nor -type f, so without -L
        # its entire subtree is invisible. /sdcard/DCIM and its canonical
        # equivalent must not disagree about which files exist.
        for root in ("/sdcard/DCIM", "/storage/emulated/0/DCIM"):
            script = discovery_module._build_remote_walk_script(root, ExcludeSettings(), follow_links=False)
            self.assertIn("find -L ", script)
            # Exclusions are lowercased, so device matching must ignore case.
            self.assertIn("-iname", script)
            self.assertNotIn("-name '", script.replace("-iname '", ""))

    def test_remote_hash_many_groups_paths_into_few_adb_calls(self):
        paths = [f"/sdcard/DCIM/photo {index}'s.jpg" for index in range(300)]
        digests = {path: hashlib.sha256(path.encode()).hexdigest() for path in paths}
        calls = []

        def fake_run(cmd, **kwargs):
            script = cmd[-1]
            calls.append(script)
            # The device only reports the paths this invocation asked for.
            lines = [
                f"{digest}  {path}"
                for path, digest in digests.items()
                if ADBBridge._shell_quote(path) in script
            ]
            return mock.Mock(returncode=0, stdout="\n".join(lines), stderr="")

        with mock.patch.object(adb_bridge.subprocess, "run", side_effect=fake_run):
            result = ADBBridge.remote_hash_many(paths, "sha256", serial="serial")

        self.assertEqual(result, digests)
        # One adb spawn per file is what this replaces; a few hundred paths
        # must collapse into a handful of invocations.
        self.assertLess(len(calls), 10)
        self.assertTrue(all(call.startswith("sha256sum ") for call in calls))

    def test_remote_hash_many_reports_partial_results_and_rejects_bad_lines(self):
        paths = ["/sdcard/a.jpg", "/sdcard/missing.jpg", "/sdcard/c.jpg"]
        good = hashlib.sha256(b"a").hexdigest()
        stdout = "\n".join(
            [
                f"{good}  /sdcard/a.jpg",
                "not-a-digest  /sdcard/c.jpg",
                f"{good}  /sdcard/never-requested.jpg",
            ]
        )
        # The hash tools skip a missing file, report it on stderr, and exit
        # non-zero while still hashing everything else.
        with mock.patch.object(
            adb_bridge.subprocess,
            "run",
            return_value=mock.Mock(returncode=1, stdout=stdout, stderr="sha256sum: /sdcard/missing.jpg: No such file"),
        ):
            result = ADBBridge.remote_hash_many(paths, "sha256", serial="serial")

        self.assertEqual(result, {"/sdcard/a.jpg": good})

    def test_remote_hash_many_raises_when_the_device_is_unavailable(self):
        with mock.patch.object(
            adb_bridge.subprocess,
            "run",
            return_value=mock.Mock(returncode=1, stdout="", stderr="error: device unauthorized"),
        ):
            with self.assertRaises(adb_bridge.ADBOperationError) as caught:
                ADBBridge.remote_hash_many(["/sdcard/a.jpg"], "sha256", serial="serial")
        self.assertTrue(caught.exception.device_unavailable)

    def test_grouped_device_hashing_falls_back_when_unavailable(self):
        infos = [FileInfo("/sdcard/a.jpg", 10, 1.0, is_adb=True)]
        settings = mock.Mock(hash_algo="sha256", adb_serial="serial")
        with mock.patch.object(
            adb_bridge.ADBBridge,
            "remote_hash_many",
            side_effect=adb_bridge.ADBOperationError("Hash", "/sdcard/a.jpg", "device offline"),
        ):
            self.assertEqual(
                engine_module.batch_adb_hashes(infos, settings, threading.Event(), DummyLogger()),
                {},
            )

    def test_grouped_device_hashing_skips_local_only_scans(self):
        infos = [FileInfo("C:/photos/a.jpg", 10, 1.0, is_adb=False)]
        settings = mock.Mock(hash_algo="sha256", adb_serial="")
        with mock.patch.object(adb_bridge.ADBBridge, "remote_hash_many") as never:
            self.assertEqual(engine_module.batch_adb_hashes(infos, settings, threading.Event()), {})
        never.assert_not_called()

    def test_pull_notices_a_fast_transfer_without_waiting_for_the_progress_tick(self):
        # A phone photo finishes far inside one progress interval. The old
        # loop slept a full interval before noticing, which set the floor on
        # per-file wall clock for an import of thousands of small files.
        self.assertLess(adb_bridge.ADB_PULL_POLL_INTERVAL, adb_bridge.ADB_PULL_PROGRESS_INTERVAL / 4)

        class FakeProcess:
            def __init__(self, polls_until_exit):
                import io

                self.remaining = polls_until_exit
                self.returncode = None
                # adb writes progress here; the pull drains both pipes while
                # the transfer runs so a large file cannot block on a full one.
                self.stdout = io.StringIO("[ 50%] /sdcard/photo.jpg\n")
                self.stderr = io.StringIO("")

            def poll(self):
                if self.remaining > 0:
                    self.remaining -= 1
                    return None
                self.returncode = 0
                return 0

            def wait(self, timeout=None):
                self.returncode = 0
                return 0

            def kill(self):
                pass

        sleeps = []
        process = FakeProcess(polls_until_exit=2)
        with tempfile.TemporaryDirectory() as temp_dir:
            destination = os.path.join(temp_dir, "photo.jpg")
            with open(destination, "wb") as handle:
                handle.write(b"x" * 1024)
            with mock.patch.object(adb_bridge.subprocess, "Popen", return_value=process), mock.patch.object(
                adb_bridge.time, "sleep", side_effect=sleeps.append
            ):
                ADBBridge.pull("/sdcard/DCIM/photo.jpg", destination, serial="serial")

        self.assertTrue(sleeps)
        self.assertTrue(all(value == adb_bridge.ADB_PULL_POLL_INTERVAL for value in sleeps))
        self.assertLessEqual(sum(sleeps), adb_bridge.ADB_PULL_PROGRESS_INTERVAL)

    def test_pull_progress_reporting_stays_throttled_while_polling_quickly(self):
        # Shortening the completion check must not stat the destination or
        # repaint the UI on every poll.
        clock = {"now": 1000.0}
        reported = []

        class FakeProcess:
            def __init__(self):
                import io

                self.calls = 0
                self.returncode = None
                self.stdout = io.StringIO("")
                self.stderr = io.StringIO("")

            def poll(self):
                self.calls += 1
                if self.calls <= 40:
                    clock["now"] += adb_bridge.ADB_PULL_POLL_INTERVAL
                    return None
                self.returncode = 0
                return 0

            def wait(self, timeout=None):
                self.returncode = 0
                return 0

            def kill(self):
                pass

        with tempfile.TemporaryDirectory() as temp_dir:
            destination = os.path.join(temp_dir, "clip.mp4")
            with open(destination, "wb") as handle:
                handle.write(b"x" * 4096)
            with mock.patch.object(adb_bridge.subprocess, "Popen", return_value=FakeProcess()), mock.patch.object(
                adb_bridge.time, "sleep", lambda _seconds: None
            ), mock.patch.object(adb_bridge.time, "monotonic", lambda: clock["now"]):
                ADBBridge.pull(
                    "/sdcard/DCIM/clip.mp4",
                    destination,
                    serial="serial",
                    progress_callback=reported.append,
                )

        # 40 polls advance the clock by two seconds, so the half-second
        # cadence reports at 0.0, 0.5, 1.0, and 1.5 rather than on all 40.
        self.assertEqual(len(reported), 4)

    def test_transfer_journal_is_written_compactly(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            target = os.path.join(temp_dir, "target.bin")
            with open(target, "wb") as handle:
                handle.write(b"original")
            journal_path = os.path.join(temp_dir, "journal.json")
            journal = TransferJournal(journal_path)
            journal.complete("source", target, 8, hashlib.sha256(b"original").hexdigest())
            journal.save(force=True)

            with open(journal_path, encoding="utf-8") as handle:
                raw = handle.read()
            self.assertNotIn("\n", raw)
            # Still valid JSON that a later run can resume from.
            self.assertEqual(json.loads(raw)["completed"]["source"]["size"], 8)
            self.assertTrue(TransferJournal(journal_path).is_complete("source", 8))

    def test_resume_trusts_matching_timestamp_without_rereading_content(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            target = os.path.join(temp_dir, "target.bin")
            with open(target, "wb") as handle:
                handle.write(b"original")
            journal = TransferJournal(os.path.join(temp_dir, "journal.json"))
            digest = hashlib.sha256(b"original").hexdigest()
            journal.complete("source", target, 8, digest, hash_algo="sha256", hash_mode="full")

            # Re-reading a 200 GB destination on every resume is the cost this
            # fast path exists to avoid, so the hash must not be recomputed.
            with mock.patch(
                "transfer_safety._hash_local_file",
                side_effect=AssertionError("resume re-read a file it should have trusted"),
            ):
                self.assertTrue(journal.is_complete("source", 8))

    def test_resume_verifies_content_when_timestamp_changed(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            target = os.path.join(temp_dir, "target.bin")
            with open(target, "wb") as handle:
                handle.write(b"original")
            journal = TransferJournal(os.path.join(temp_dir, "journal.json"))
            digest = hashlib.sha256(b"original").hexdigest()
            journal.complete("source", target, 8, digest, hash_algo="sha256", hash_mode="full")
            recorded = journal.data["completed"]["source"]["target_mtime"]

            # Same size, same timestamp, different bytes: only a content read
            # can tell these apart, so the timestamp must be moved for the
            # fall-through path to be exercised.
            with open(target, "wb") as handle:
                handle.write(b"modified")
            os.utime(target, (recorded + 60, recorded + 60))
            self.assertFalse(journal.is_complete("source", 8))

            # A touched but unmodified file verifies by content and is kept,
            # rather than being needlessly transferred again.
            with open(target, "wb") as handle:
                handle.write(b"original")
            os.utime(target, (recorded + 120, recorded + 120))
            self.assertTrue(journal.is_complete("source", 8))

    def test_resume_full_verification_is_available_and_legacy_entries_still_hash(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            target = os.path.join(temp_dir, "target.bin")
            with open(target, "wb") as handle:
                handle.write(b"original")
            journal = TransferJournal(os.path.join(temp_dir, "journal.json"))
            digest = hashlib.sha256(b"original").hexdigest()
            journal.complete("source", target, 8, digest, hash_algo="sha256", hash_mode="full")

            with mock.patch(
                "transfer_safety._hash_local_file", return_value=digest
            ) as hashed:
                self.assertTrue(journal.is_complete("source", 8, verify_content=True))
            self.assertEqual(hashed.call_count, 1)

            # A journal written before timestamps were recorded must keep
            # verifying by content instead of trusting the entry.
            journal.data["completed"]["source"].pop("target_mtime")
            with mock.patch(
                "transfer_safety._hash_local_file", return_value=digest
            ) as legacy_hashed:
                self.assertTrue(journal.is_complete("source", 8))
            self.assertEqual(legacy_hashed.call_count, 1)

    def test_rename_promotion_does_not_clobber_target_created_after_resolution(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = os.path.abspath(temp_dir)
            staged = os.path.join(root, "staged.dtm-partial")
            target = os.path.join(root, "photo.jpg")
            with open(staged, "wb") as handle:
                handle.write(b"incoming")
            # This occupant appears after planning selected `target`.
            with open(target, "wb") as handle:
                handle.write(b"incumbent")

            promoted, backup = promote_transfer_file(staged, target, "rename", root)

            self.assertEqual(backup, "")
            with open(target, "rb") as handle:
                self.assertEqual(handle.read(), b"incumbent")
            self.assertNotEqual(promoted, target)
            with open(promoted, "rb") as handle:
                self.assertEqual(handle.read(), b"incoming")

    def test_fast_duplicate_candidates_are_confirmed_with_full_content(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = os.path.abspath(temp_dir)
            first = os.path.join(root, "first.bin")
            second = os.path.join(root, "second.bin")
            shared_start = b"A" * 1048576
            shared_end = b"Z" * 1048576
            with open(first, "wb") as handle:
                handle.write(shared_start + (b"B" * 1048576) + shared_end)
            with open(second, "wb") as handle:
                handle.write(shared_start + (b"C" * 1048576) + shared_end)
            settings = Settings(
                scan_root=root, output_root="", criteria="hash", hash_algo="sha256",
                hash_mode="fast", only_media=False, extensions=[], min_size_kb=0,
                exclude_dirs=[], skip_hidden_system=True, dry_run=True,
                preserve_structure=True, max_hash_workers=2,
            )
            infos = [
                FileInfo(first, os.path.getsize(first), os.path.getmtime(first)),
                FileInfo(second, os.path.getsize(second), os.path.getmtime(second)),
            ]

            groups = group_duplicates(
                infos, settings, threading.Event(), HashCache(os.path.join(root, "cache.json")), DummyLogger()
            )

            self.assertEqual(groups, [])

            with open(second, "wb") as handle:
                handle.write(shared_start + (b"B" * 1048576) + shared_end)
            infos[1] = FileInfo(second, os.path.getsize(second), os.path.getmtime(second))
            groups = group_duplicates(
                infos, settings, threading.Event(), HashCache(os.path.join(root, "cache2.json")), DummyLogger()
            )
            self.assertEqual(len(groups), 1)
            self.assertEqual({item.path for item in groups[0]}, {first, second})

    def test_cancelled_hash_is_not_cached_and_later_run_hashes_complete_file(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = os.path.join(temp_dir, "large.bin")
            payload = (b"0123456789abcdef" * 262144)
            with open(path, "wb") as handle:
                handle.write(payload)
            settings = Settings(
                scan_root=temp_dir, output_root="", criteria="hash", hash_algo="sha256",
                hash_mode="full", only_media=False, extensions=[], min_size_kb=0,
                exclude_dirs=[], skip_hidden_system=True, dry_run=True,
                preserve_structure=True, max_hash_workers=1,
            )
            cache = HashCache(os.path.join(temp_dir, "cache.json"))

            class CancelDuringRead:
                def __init__(self):
                    self.calls = 0

                def is_set(self):
                    self.calls += 1
                    return self.calls >= 5

            with self.assertRaises(HashCancelled):
                compute_hash(path, settings, CancelDuringRead(), cache)
            self.assertEqual(cache.data, {})

            digest = compute_hash(path, settings, threading.Event(), cache)
            self.assertEqual(digest, hashlib.sha256(payload).hexdigest())
            self.assertEqual(len(cache.data), 1)

    def test_adb_cache_is_device_scoped_and_normalizes_storage_aliases(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            cache = ADBHashCache(os.path.join(temp_dir, "adb.json"), "phone-123")
            cache.set_file_hash(
                "/storage/emulated/0/DCIM/Camera/photo.jpg",
                4,
                123.0,
                "a" * 64,
                "sha256",
                "full",
                root="/sdcard/DCIM",
            )

            self.assertEqual(
                cache.get_valid_hash("/sdcard/DCIM/Camera/photo.jpg", "sha256", "full", 4, 123.0),
                "a" * 64,
            )
            self.assertEqual(cache.active_file_count_under_root("/sdcard/DCIM", "sha256", "full"), 1)

    def test_unreadable_attributes_do_not_mark_a_file_hidden(self):
        # GetFileAttributesW returns the sentinel 0xFFFFFFFF on any failure,
        # and without an explicit restype that arrived as -1, whose bitwise AND
        # with HIDDEN|SYSTEM is truthy. Every long, locked, or permission-denied
        # path was therefore classified as hidden and dropped from local scans
        # with no error recorded, so the scan still called itself complete.
        from pathlib import Path
        from utils import is_hidden_or_system

        self.assertFalse(is_hidden_or_system(r"C:/definitely/does/not/exist/anywhere.jpg"))

        with tempfile.TemporaryDirectory() as temp_dir:
            deep = Path(temp_dir)
            for index in range(30):
                deep = deep / f"a_very_long_folder_name_{index:02d}_padding_padding"
            self.assertGreater(len(str(deep)), 260)
            self.assertFalse(is_hidden_or_system(str(deep)))

            if os.name == "nt":
                import ctypes

                visible = Path(temp_dir) / "visible.txt"
                visible.write_text("x", encoding="utf-8")
                self.assertFalse(is_hidden_or_system(str(visible)))
                # A genuinely hidden file must still be detected.
                ctypes.windll.kernel32.SetFileAttributesW(str(visible), 0x2)
                try:
                    self.assertTrue(is_hidden_or_system(str(visible)))
                finally:
                    ctypes.windll.kernel32.SetFileAttributesW(str(visible), 0x80)

    def test_fast_import_does_not_skip_a_file_that_only_matches_a_sampled_hash(self):
        # Fast hashing covers size + first and last 1 MiB. Two different large
        # files can share that digest, and skipping the import on that basis
        # leaves a file behind while reporting a clean run.
        with tempfile.TemporaryDirectory() as temp_dir:
            root = os.path.abspath(temp_dir)
            source_root = os.path.join(root, "source")
            library = os.path.join(root, "library")
            output = os.path.join(root, "output")
            for path in (source_root, library, output):
                os.makedirs(path)

            head = b"H" * (1024 * 1024)
            tail = b"T" * (1024 * 1024)
            middle = 1024 * 1024
            existing = os.path.join(library, "existing.bin")
            incoming = os.path.join(source_root, "incoming.bin")
            with open(existing, "wb") as handle:
                handle.write(head + (b"A" * middle) + tail)
            with open(incoming, "wb") as handle:
                handle.write(head + (b"B" * middle) + tail)

            settings = TransferSettings(
                source_root=source_root, dest_root=library, output_root=output,
                criteria="hash", hash_algo="sha256", hash_mode="fast", only_media=False,
                extensions=[], min_size_kb=0, exclude_dirs=[], skip_hidden_system=False,
                dry_run=False, preserve_structure=True, max_hash_workers=1,
                transfer_mode="copy", duplicate_policy="skip", use_dest_cache=False,
                source_is_adb=False, update_drive_cache=False, use_adb_cache=False,
            )

            # Same size, same first and last megabyte: the sampled digests match.
            fast = engine_module.compute_hash(existing, settings, threading.Event())
            self.assertEqual(fast, engine_module.compute_hash(incoming, settings, threading.Event()))

            result = engine_module.execute_smart_transfer(
                settings, threading.Event(), HashCache(os.path.join(root, "cache.json")), DummyLogger()
            )

            self.assertEqual(result["transferred"], 1, "a unique file must not be skipped as a duplicate")
            self.assertEqual(result["duplicates"], 0)
            self.assertTrue(os.path.exists(os.path.join(output, "incoming.bin")))

    def test_fast_import_still_skips_a_genuine_duplicate(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = os.path.abspath(temp_dir)
            source_root = os.path.join(root, "source")
            library = os.path.join(root, "library")
            output = os.path.join(root, "output")
            for path in (source_root, library, output):
                os.makedirs(path)

            payload = (b"H" * (1024 * 1024)) + (b"A" * (1024 * 1024)) + (b"T" * (1024 * 1024))
            with open(os.path.join(library, "existing.bin"), "wb") as handle:
                handle.write(payload)
            with open(os.path.join(source_root, "copy.bin"), "wb") as handle:
                handle.write(payload)

            settings = TransferSettings(
                source_root=source_root, dest_root=library, output_root=output,
                criteria="hash", hash_algo="sha256", hash_mode="fast", only_media=False,
                extensions=[], min_size_kb=0, exclude_dirs=[], skip_hidden_system=False,
                dry_run=False, preserve_structure=True, max_hash_workers=1,
                transfer_mode="copy", duplicate_policy="skip", use_dest_cache=False,
                source_is_adb=False, update_drive_cache=False, use_adb_cache=False,
            )
            result = engine_module.execute_smart_transfer(
                settings, threading.Event(), HashCache(os.path.join(root, "cache.json")), DummyLogger()
            )

            self.assertEqual(result["transferred"], 0)
            self.assertEqual(result["duplicates"], 1)

    def test_directory_listing_shows_symlinked_folders_and_reports_failures(self):
        listing = b"Camera/\nScreenshots/\nlinked_album/\nnote.txt\n"
        with mock.patch.object(
            adb_bridge.subprocess, "run", return_value=mock.Mock(returncode=0, stdout=listing, stderr=b"")
        ) as run:
            folders = ADBBridge.get_directory_structure("/sdcard/DCIM", serial="X")

        # -L dereferences symlinks, so a symlinked album is listed as a folder
        # instead of being invisible in the browser.
        self.assertIn("ls -pL", run.call_args[0][0][-1])
        self.assertEqual([f["name"] for f in folders], ["Camera", "linked_album", "Screenshots"])
        self.assertEqual(folders[0]["path"], "/sdcard/DCIM/Camera")

        # A device failure must not look like an empty folder.
        with mock.patch.object(
            adb_bridge.subprocess,
            "run",
            return_value=mock.Mock(returncode=1, stdout=b"", stderr=b"error: device offline"),
        ):
            with self.assertRaises(adb_bridge.ADBOperationError) as caught:
                ADBBridge.get_directory_structure("/sdcard/DCIM", serial="X")
        self.assertTrue(caught.exception.device_unavailable)

    def test_bundled_adb_is_preferred_over_whatever_is_on_path(self):
        # Packaging ships platform-tools beside the executable, but every call
        # site used the bare name "adb" and so depended on the user having the
        # Android SDK on PATH. An ordinary Windows PC does not, so a packaged
        # install found no phone and blamed the cable.
        with tempfile.TemporaryDirectory() as temp_dir:
            app_dir = os.path.join(temp_dir, "app")
            tools = os.path.join(app_dir, "platform-tools")
            os.makedirs(tools)
            name = "adb.exe" if os.name == "nt" else "adb"
            bundled = os.path.join(tools, name)
            with open(bundled, "wb") as handle:
                handle.write(b"")

            adb_bridge.set_adb_executable("")
            try:
                with mock.patch.object(adb_bridge.sys, "executable", os.path.join(app_dir, "app.exe")), mock.patch.object(
                    adb_bridge.shutil, "which", return_value="C:/somewhere/else/adb.exe"
                ):
                    adb_bridge.resolve_adb_executable.cache_clear()
                    self.assertEqual(adb_bridge.resolve_adb_executable(), bundled)
                    self.assertEqual(ADBBridge._adb_command("devices")[0], bundled)
            finally:
                adb_bridge.set_adb_executable("")

    def test_configured_adb_path_overrides_discovery(self):
        adb_bridge.set_adb_executable("D:/tools/adb.exe")
        try:
            self.assertEqual(adb_bridge.resolve_adb_executable(), "D:/tools/adb.exe")
            self.assertEqual(
                ADBBridge._adb_command("shell", "ls", serial="ABC"),
                ["D:/tools/adb.exe", "-s", "ABC", "shell", "ls"],
            )
        finally:
            adb_bridge.set_adb_executable("")

    def test_adb_path_shorthand_resolves_equivalent_mount_points_only(self):
        # Interchangeable spellings of primary storage collapse to one form.
        self.assertEqual(ADBBridge.normalize_remote_path("/sd/DCIM"), "/sdcard/DCIM")
        self.assertEqual(ADBBridge.normalize_remote_path("storage/self/primary/downloads"), "/sdcard/downloads")
        self.assertEqual(ADBBridge.normalize_remote_path("/storage/emulated/0/DCIM/Camera"), "/sdcard/DCIM/Camera")
        self.assertEqual(ADBBridge.normalize_remote_path("/sdcard/DCIM/"), "/sdcard/DCIM")
        self.assertEqual(ADBBridge.normalize_remote_path(r"\sdcard\DCIM"), "/sdcard/DCIM")

    def test_adb_path_normalization_never_renames_a_real_folder(self):
        # A phone can hold both /sdcard/Videos and /sdcard/Movies. Rewriting one
        # to the other scanned a folder the user did not choose and left their
        # actual videos unimported, so folder names must survive untouched.
        for path in (
            "/sdcard/Videos",
            "/sdcard/Videos/clip.mp4",
            "/sdcard/Downloads",
            "/sdcard/Audio/song.mp3",
            "/sdcard/Picture/a.jpg",
            "/sdcard/pictures",
        ):
            self.assertEqual(ADBBridge.normalize_remote_path(path), path)

        # Two distinct device files must never collapse onto one path, or
        # hashing one would stand in for the other.
        self.assertNotEqual(
            ADBBridge.normalize_remote_path("/sdcard/Videos/clip.mp4"),
            ADBBridge.normalize_remote_path("/sdcard/Movies/clip.mp4"),
        )

    def test_drive_cache_repairs_broken_hash_index_and_ignores_bad_digests(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            cache_path = os.path.join(temp_dir, "cache.json")
            file_path = os.path.join(temp_dir, "photo.jpg")
            with open(file_path, "wb") as handle:
                handle.write(b"same")
            with open(cache_path, "w", encoding="utf-8") as handle:
                handle.write(json.dumps({
                    "version": 1,
                    "roots": {},
                    "files": {
                        os.path.normcase(os.path.abspath(file_path)): {
                            "path": file_path,
                            "size": 4,
                            "mtime": 10,
                            "hash": "not-a-real-hash",
                            "algo": "sha256",
                            "mode": "full",
                        }
                    },
                    "hash_index": {"bad": ["missing"]},
                }))

            cache = DriveHashCache(cache_path)
            cache.load()

            self.assertIsNone(cache.get_valid_hash(file_path, "sha256", "full", 4, 10))
            self.assertEqual(cache.hashes(), set())

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

    def test_build_target_path_can_organize_by_source_date(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            source_root = os.path.join(temp_dir, "source")
            dest_root = os.path.join(temp_dir, "dest")
            os.makedirs(source_root)
            source = os.path.join(source_root, "photo.jpg")
            timestamp = 1767225600  # 2026-01-01 UTC

            target = build_target_path(
                source,
                source_root,
                dest_root,
                preserve_structure=True,
                destination_template="date",
                source_timestamp=timestamp,
            )

            self.assertEqual(target, os.path.join(dest_root, "2026", "01", "photo.jpg"))

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
        # Case-insensitive: normalize_excludes lowercases these names, so a
        # case-sensitive -name would never match the real folder on the device.
        self.assertIn("-iname 'Android'", script)
        self.assertIn("-iname 'System Volume Information'", script)

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
        # -L regardless of how the root was spelled: a symlinked subfolder is
        # neither -type d nor -type f, so without it the whole subtree is lost.
        self.assertIn("find -L '/storage/emulated/0/DCIM'", script)
        self.assertIn("-iname 'Android'", script)
        self.assertIn("printf 'D|%s\\n'", script)
        self.assertIn("stat -c 'F|%s|%Y|%n'", script)
        # A file find listed but stat could not read is reported, not dropped.
        self.assertIn("printf 'E|%s\\n'", script)

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

    def test_smart_transfer_handles_spaces_unicode_and_special_characters(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            source_root = os.path.join(temp_dir, "source folder")
            compare_root = os.path.join(temp_dir, "compare folder")
            output_root = os.path.join(temp_dir, "output folder")
            nested = os.path.join(source_root, "Trip 2026", "café & family")
            os.makedirs(nested)
            os.makedirs(compare_root)
            os.makedirs(output_root)

            source_file = os.path.join(nested, "photo (final) #1.jpg")
            with open(source_file, "wb") as handle:
                handle.write(b"unique-special")

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
                use_dest_cache=False,
            )

            result = execute_smart_transfer(
                settings,
                threading.Event(),
                HashCache(os.path.join(temp_dir, "hash_cache.json")),
                DummyLogger(),
            )

            expected = os.path.join(output_root, "Trip 2026", "café & family", "photo (final) #1.jpg")
            self.assertEqual(result["transferred"], 1)
            self.assertTrue(os.path.exists(expected))
            with open(expected, "rb") as handle:
                self.assertEqual(handle.read(), b"unique-special")

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
