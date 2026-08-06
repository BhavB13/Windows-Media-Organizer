import tempfile
import unittest
from pathlib import Path
import struct

from duplicate_transfer_manager.core import OrganizerSettings, OperationState
from duplicate_transfer_manager.runtime_paths import get_runtime_paths
from duplicate_transfer_manager.services import FileOrganizerService


class FileOrganizerServiceTests(unittest.TestCase):
    def test_flatten_moves_selected_folder_and_rolls_back(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "library"
            nested = source / "camera" / "day-one"
            nested.mkdir(parents=True)
            photo = nested / "photo.jpg"
            photo.write_bytes(b"image")
            service = FileOrganizerService(get_runtime_paths(root / "data"))
            settings = OrganizerSettings(str(source), str(source), (str(source / "camera"),))

            result = service.organize(settings)
            moved = source / "photo.jpg"

            self.assertEqual(result.status, OperationState.COMPLETED)
            self.assertTrue(moved.exists())
            self.assertFalse(photo.exists())
            self.assertEqual(service.search_catalog("photo")[0]["category"], "Pictures")
            rollback = service.rollback(result.data["organization"].operation_id)
            self.assertEqual(rollback.failures, ())
            self.assertTrue(photo.exists())
            self.assertFalse(moved.exists())

    def test_dry_run_creates_recovery_manifest_without_moving_files(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "source"
            destination = root / "organized"
            (source / "downloads").mkdir(parents=True)
            destination.mkdir()
            original = source / "downloads" / "file.txt"
            original.write_text("safe", encoding="utf-8")
            service = FileOrganizerService(get_runtime_paths(root / "data"))

            result = service.organize(
                OrganizerSettings(str(source), str(destination), dry_run=True)
            )

            self.assertTrue(original.exists())
            self.assertEqual(result.counts["moved"], 0)
            self.assertTrue(Path(result.report_path).exists())

    def test_type_date_ml_modes_and_collision_policy_are_deterministic(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "source"
            destination = root / "destination"
            (source / "one").mkdir(parents=True)
            (source / "two").mkdir(parents=True)
            destination.mkdir()
            first = source / "one" / "receipt.jpg"
            second = source / "two" / "receipt.jpg"
            first.write_bytes(b"first")
            second.write_bytes(b"second")
            service = FileOrganizerService(get_runtime_paths(root / "data"))

            type_plan = service.build_plan(OrganizerSettings(str(source), str(destination), mode="type"))
            date_plan = service.build_plan(OrganizerSettings(str(source), str(destination), mode="date"))
            ml_plan = service.build_plan(
                OrganizerSettings(str(source), str(destination), mode="ml", ml_auto_organize=True)
            )

            self.assertTrue(all("Pictures" in item.destination_path for item in type_plan))
            self.assertTrue(all("Pictures" in item.destination_path for item in date_plan))
            self.assertTrue(any(item.collision == "rename" for item in type_plan))
            self.assertTrue(all(item.category == "Receipts" for item in ml_plan))
            self.assertTrue(all(item.selected for item in ml_plan))

    def test_low_confidence_local_ml_suggestion_requires_review(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "source"
            destination = root / "destination"
            (source / "folder").mkdir(parents=True)
            destination.mkdir()
            (source / "folder" / "unlabeled.jpg").write_bytes(b"image")
            service = FileOrganizerService(get_runtime_paths(root / "data"))

            plan = service.build_plan(
                OrganizerSettings(str(source), str(destination), mode="ml", ml_auto_organize=True)
            )

            self.assertEqual(plan[0].confidence, 0.60)
            self.assertFalse(plan[0].selected)

    def test_identical_destination_file_is_not_moved_or_overwritten(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "source"
            destination = root / "destination"
            (source / "nested").mkdir(parents=True)
            destination.mkdir()
            (source / "nested" / "same.jpg").write_bytes(b"same")
            (destination / "same.jpg").write_bytes(b"same")
            service = FileOrganizerService(get_runtime_paths(root / "data"))

            plan = service.build_plan(OrganizerSettings(str(source), str(destination)))

            self.assertEqual(plan[0].collision, "duplicate")
            self.assertFalse(plan[0].selected)
            self.assertEqual(plan[0].destination_path, "")

    def test_prune_removes_only_expired_organization_manifests(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            paths = get_runtime_paths(root / "data")
            operation = paths.organization / "old" 
            operation.mkdir()
            manifest = operation / "manifest.json"
            manifest.write_text("{}", encoding="utf-8")
            old = 1
            import os
            os.utime(manifest, (old, old))

            removed = FileOrganizerService(paths).prune_manifests(90)

            self.assertEqual(removed, 1)
            self.assertFalse(operation.exists())

    def test_live_run_uses_the_reviewed_mapping_and_rejects_stale_sources(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "source"
            destination = root / "destination"
            (source / "nested").mkdir(parents=True)
            destination.mkdir()
            photo = source / "nested" / "photo.jpg"
            photo.write_bytes(b"before review")
            service = FileOrganizerService(get_runtime_paths(root / "data"))
            settings = OrganizerSettings(str(source), str(destination))
            reviewed = service.build_plan(settings)
            # The mapping was reviewed, then the source changed.  It must not
            # be moved just because a fresh plan would now happen to work.
            photo.write_bytes(b"changed after review")

            result = service.organize(settings, reviewed_plan=reviewed)

            self.assertTrue(photo.exists())
            self.assertEqual(result.counts["moved"], 0)
            self.assertEqual(result.counts["errors"], 1)

    def test_replace_conflict_is_backed_up_and_restored_with_rollback(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "source"
            destination = root / "destination"
            (source / "nested").mkdir(parents=True)
            destination.mkdir()
            incoming = source / "nested" / "photo.jpg"
            occupied = destination / "photo.jpg"
            incoming.write_bytes(b"new image")
            occupied.write_bytes(b"existing image")
            service = FileOrganizerService(get_runtime_paths(root / "data"))
            settings = OrganizerSettings(str(source), str(destination), conflict_policy="replace")
            reviewed = service.build_plan(settings)

            result = service.organize(settings, reviewed_plan=reviewed)

            self.assertEqual(occupied.read_bytes(), b"new image")
            rollback = service.rollback(result.data["organization"].operation_id)
            self.assertEqual(rollback.failures, ())
            self.assertEqual(incoming.read_bytes(), b"new image")
            self.assertEqual(occupied.read_bytes(), b"existing image")

    def test_date_mode_prefers_embedded_jpeg_capture_time(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "source"
            destination = root / "destination"
            (source / "camera").mkdir(parents=True)
            destination.mkdir()
            # Minimal JPEG EXIF containing DateTimeOriginal 2020-02-03.
            tiff = bytearray(b"II") + struct.pack("<H", 42) + struct.pack("<I", 8)
            tiff += struct.pack("<H", 1)
            tiff += struct.pack("<HHII", 0x8769, 4, 1, 26)
            tiff += struct.pack("<I", 0)
            tiff += struct.pack("<H", 1)
            tiff += struct.pack("<HHII", 0x9003, 2, 20, 44)
            tiff += struct.pack("<I", 0)
            tiff += b"2020:02:03 04:05:06\x00"
            (source / "camera" / "photo.jpg").write_bytes(b"\xff\xd8\xff\xe1" + b"Exif\x00\x00" + tiff + b"\xff\xd9")
            service = FileOrganizerService(get_runtime_paths(root / "data"))

            plan = service.build_plan(OrganizerSettings(str(source), str(destination), mode="date"))

            self.assertIn("Pictures/2020/02/photo.jpg", plan[0].destination_path.replace("\\", "/"))
            self.assertIn("Embedded capture time", plan[0].reason)

    def test_local_ml_corrections_remain_local_and_change_future_plans(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "source"
            destination = root / "destination"
            (source / "camera").mkdir(parents=True)
            (source / "private").mkdir(parents=True)
            destination.mkdir()
            (source / "camera" / "one.jpg").write_bytes(b"one")
            (source / "private" / "two.jpg").write_bytes(b"two")
            service = FileOrganizerService(get_runtime_paths(root / "data"))
            settings = OrganizerSettings(str(source), str(destination), mode="ml", ml_auto_organize=True)

            service.set_ml_extension_rule("jpg", "Artwork")
            service.exclude_ml_folder(str(source / "private"))
            plan = {Path(item.source_path).name: item for item in service.build_plan(settings)}

            self.assertEqual(plan["one.jpg"].category, "Artwork")
            self.assertEqual(plan["one.jpg"].confidence, 1.0)
            self.assertIn("Local rule", plan["one.jpg"].reason)
            self.assertFalse(plan["two.jpg"].selected)
            self.assertIn("excluded", plan["two.jpg"].reason)
            self.assertTrue((service.paths.organization / "ml_overrides.json").exists())


if __name__ == "__main__":
    unittest.main()
