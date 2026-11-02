"""Tests for quarantine reason tracking: duplicate vs similar."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from duplicate_transfer_manager.core import QuarantineRecord
from duplicate_transfer_manager.runtime_paths import get_runtime_paths
from duplicate_transfer_manager.services.duplicate_workflow import (
    DuplicateQuarantineService,
    build_duplicate_review,
)
from duplicate_transfer_manager.services.similar_service import (
    SimilarPhotoGroup,
    SimilarPhotoReview,
    SimilarPhotoService,
)
from models import FileInfo


class QuarantineReasonTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.paths = get_runtime_paths(self.root / "data")

    def test_default_reason_is_duplicate(self) -> None:
        keep = self.root / "keep.bin"
        duplicate = self.root / "duplicate.bin"
        keep.write_bytes(b"payload-identical")
        duplicate.write_bytes(b"payload-identical")

        review = build_duplicate_review(
            [[FileInfo(str(keep), keep.stat().st_size, 100), FileInfo(str(duplicate), duplicate.stat().st_size, 200)]],
            thumbnail_root=self.root / "thumbs",
        )
        service = DuplicateQuarantineService(self.paths)
        group = review.groups[0]
        victim_id = next(item.id for item in group.items if item.id != group.keep_item_id)

        result = service.quarantine(review, [victim_id])
        self.assertEqual(len(result.records), 1)
        self.assertEqual(result.records[0].reason, "duplicate")

        manifest = json.loads(Path(result.manifest_path).read_text(encoding="utf-8"))
        self.assertEqual(manifest["records"][0]["reason"], "duplicate")

    def test_duplicate_quarantine_explicit_reason(self) -> None:
        keep = self.root / "keep.bin"
        victim = self.root / "victim.bin"
        keep.write_bytes(b"payload")
        victim.write_bytes(b"payload")

        review = build_duplicate_review(
            [[FileInfo(str(keep), 7, 100), FileInfo(str(victim), 7, 200)]],
            thumbnail_root=self.root / "thumbs",
        )
        service = DuplicateQuarantineService(self.paths)
        victim_id = next(item.id for item in review.groups[0].items if item.id != review.groups[0].keep_item_id)

        result = service.quarantine(
            review, [victim_id], operation_id="op-explicit-kw", reason="custom_reason"
        )
        self.assertEqual(result.records[0].reason, "custom_reason")
        manifest = json.loads(Path(result.manifest_path).read_text(encoding="utf-8"))
        self.assertEqual(manifest["records"][0]["reason"], "custom_reason")

        with self.assertRaises(TypeError):
            service.quarantine(review, [victim_id], "positional_reason")

    def test_similar_photo_quarantine_records_similar(self) -> None:
        original = self.root / "original.jpg"
        resized = self.root / "resized.jpg"
        original.write_bytes(b"large-original-image-content")
        resized.write_bytes(b"small-resized-image-content")

        dup_review = build_duplicate_review(
            [[FileInfo(str(original), original.stat().st_size, 100), FileInfo(str(resized), resized.stat().st_size, 200)]],
            thumbnail_root=self.root / "thumbs",
        )
        dup_group = dup_review.groups[0]
        victim = next(item for item in dup_group.items if Path(item.path).name == "resized.jpg")
        keeper = next(item for item in dup_group.items if Path(item.path).name == "original.jpg")

        similar_group = SimilarPhotoGroup(
            group=dup_group,
            kind="similar",
            keeper_id=keeper.id,
            reasons=("larger pixel dimensions",),
        )
        review = SimilarPhotoReview(
            groups=(similar_group,),
            scanned_images=2,
            duplicate_review=dup_review,
        )

        service = SimilarPhotoService(self.paths)
        result = service.quarantine(review, [victim.id])

        self.assertEqual(len(result.records), 1)
        self.assertEqual(result.records[0].reason, "similar")
        self.assertFalse(resized.exists())
        self.assertTrue(original.exists())
        self.assertTrue(Path(result.records[0].stored_path).exists())

        manifest = json.loads(Path(result.manifest_path).read_text(encoding="utf-8"))
        self.assertEqual(manifest["records"][0]["reason"], "similar")

    def test_similar_photo_quarantine_records_duplicate_for_identical_kind(self) -> None:
        photo1 = self.root / "photo1.jpg"
        photo2 = self.root / "photo2.jpg"
        photo1.write_bytes(b"same-bytes-photo")
        photo2.write_bytes(b"same-bytes-photo")

        dup_review = build_duplicate_review(
            [[FileInfo(str(photo1), photo1.stat().st_size, 100), FileInfo(str(photo2), photo2.stat().st_size, 200)]],
            thumbnail_root=self.root / "thumbs",
        )
        dup_group = dup_review.groups[0]
        victim = next(item for item in dup_group.items if Path(item.path).name == "photo2.jpg")
        keeper = next(item for item in dup_group.items if Path(item.path).name == "photo1.jpg")

        identical_group = SimilarPhotoGroup(
            group=dup_group,
            kind="identical",
            keeper_id=keeper.id,
            reasons=("byte identical",),
        )
        review = SimilarPhotoReview(
            groups=(identical_group,),
            scanned_images=2,
            duplicate_review=dup_review,
        )

        service = SimilarPhotoService(self.paths)
        result = service.quarantine(review, [victim.id])

        self.assertEqual(len(result.records), 1)
        self.assertEqual(result.records[0].reason, "duplicate")

        manifest = json.loads(Path(result.manifest_path).read_text(encoding="utf-8"))
        self.assertEqual(manifest["records"][0]["reason"], "duplicate")

    def test_mixed_call_records_per_item_reasons(self) -> None:
        # Group 1: identical photos
        id_keep = self.root / "identical_keep.jpg"
        id_victim = self.root / "identical_victim.jpg"
        id_keep.write_bytes(b"identical-bytes")
        id_victim.write_bytes(b"identical-bytes")

        # Group 2: similar photos
        sim_keep = self.root / "similar_keep.jpg"
        sim_victim = self.root / "similar_victim.jpg"
        sim_keep.write_bytes(b"high-res-bytes")
        sim_victim.write_bytes(b"low-res-bytes")

        dup_review = build_duplicate_review(
            [
                [FileInfo(str(id_keep), id_keep.stat().st_size, 100), FileInfo(str(id_victim), id_victim.stat().st_size, 110)],
                [FileInfo(str(sim_keep), sim_keep.stat().st_size, 200), FileInfo(str(sim_victim), sim_victim.stat().st_size, 210)],
            ],
            thumbnail_root=self.root / "thumbs",
        )

        group1 = dup_review.groups[0]
        id_victim_item = next(item for item in group1.items if Path(item.path).name == "identical_victim.jpg")
        id_keep_item = next(item for item in group1.items if Path(item.path).name == "identical_keep.jpg")

        group2 = dup_review.groups[1]
        sim_victim_item = next(item for item in group2.items if Path(item.path).name == "similar_victim.jpg")
        sim_keep_item = next(item for item in group2.items if Path(item.path).name == "similar_keep.jpg")

        sp_group1 = SimilarPhotoGroup(group=group1, kind="identical", keeper_id=id_keep_item.id, reasons=())
        sp_group2 = SimilarPhotoGroup(group=group2, kind="similar", keeper_id=sim_keep_item.id, reasons=())

        review = SimilarPhotoReview(
            groups=(sp_group1, sp_group2),
            scanned_images=4,
            duplicate_review=dup_review,
        )

        service = SimilarPhotoService(self.paths)
        result = service.quarantine(review, [id_victim_item.id, sim_victim_item.id])

        self.assertEqual(len(result.records), 2)
        records_by_path = {Path(record.original_path).name: record for record in result.records}

        self.assertEqual(records_by_path["identical_victim.jpg"].reason, "duplicate")
        self.assertEqual(records_by_path["similar_victim.jpg"].reason, "similar")

        manifest = json.loads(Path(result.manifest_path).read_text(encoding="utf-8"))
        manifest_by_path = {Path(r["original_path"]).name: r for r in manifest["records"]}
        self.assertEqual(manifest_by_path["identical_victim.jpg"]["reason"], "duplicate")
        self.assertEqual(manifest_by_path["similar_victim.jpg"]["reason"], "similar")

    def test_duplicate_quarantine_mapping_of_reasons(self) -> None:
        f1 = self.root / "file1.bin"
        f2 = self.root / "file2.bin"
        f3 = self.root / "file3.bin"
        f1.write_bytes(b"same")
        f2.write_bytes(b"same")
        f3.write_bytes(b"same")

        review = build_duplicate_review(
            [[FileInfo(str(f1), 4, 100), FileInfo(str(f2), 4, 200), FileInfo(str(f3), 4, 300)]],
            thumbnail_root=self.root / "thumbs",
        )
        group = review.groups[0]
        items_by_name = {Path(i.path).name: i for i in group.items}
        f2_item = items_by_name["file2.bin"]
        f3_item = items_by_name["file3.bin"]

        service = DuplicateQuarantineService(self.paths)
        reason_map = {f2_item.id: "duplicate", f3_item.id: "similar"}
        result = service.quarantine(review, [f2_item.id, f3_item.id], reason=reason_map)

        self.assertEqual(len(result.records), 2)
        records_by_name = {Path(r.original_path).name: r for r in result.records}
        self.assertEqual(records_by_name["file2.bin"].reason, "duplicate")
        self.assertEqual(records_by_name["file3.bin"].reason, "similar")

    def test_restore_similar_item_puts_it_back_byte_identical(self) -> None:
        original = self.root / "photo.jpg"
        keeper = self.root / "keeper.jpg"
        payload = b"magic-exif-and-jpeg-payload-data-\x00\xff\xfe\x01\x42"
        original.write_bytes(payload)
        keeper.write_bytes(payload)

        review = build_duplicate_review(
            [[FileInfo(str(keeper), len(payload), 100), FileInfo(str(original), len(payload), 200)]],
            thumbnail_root=self.root / "thumbs",
        )
        service = DuplicateQuarantineService(self.paths)
        victim_id = next(item.id for item in review.groups[0].items if item.id != review.groups[0].keep_item_id)

        result = service.quarantine(review, [victim_id], reason="similar")
        self.assertEqual(result.records[0].reason, "similar")
        self.assertFalse(original.exists())

        restore = service.restore_record(result.records[0])
        self.assertEqual(len(restore.restored), 1)
        self.assertEqual(restore.restored[0], str(original))
        self.assertTrue(original.exists())
        self.assertEqual(original.read_bytes(), payload)

    def test_old_manifest_without_reason_field_loads_and_restores(self) -> None:
        op_id = "op-legacy-2024"
        op_dir = self.paths.quarantine / op_id
        op_dir.mkdir(parents=True)

        stored_file = op_dir / "stored_img.bin"
        legacy_content = b"legacy-original-unmodified-content-12345"
        stored_file.write_bytes(legacy_content)

        restore_target = self.root / "restored" / "legacy_target.bin"

        # Construct a manifest strictly in the pre-change format (no "reason" key)
        legacy_manifest_payload = {
            "version": 1,
            "operation_id": op_id,
            "created_at": "2024-01-01T12:00:00+00:00",
            "dry_run": False,
            "records": [
                {
                    "original_path": str(restore_target),
                    "stored_path": str(stored_file),
                    "hash": "d41d8cd98f00b204e9800998ecf8427e",
                    "size": len(legacy_content),
                    "operation_id": op_id,
                    "status": "completed",
                }
            ],
            "failures": [],
            "safety": "Files are moved into app quarantine or copied from Android; no permanent deletion is performed.",
        }
        manifest_path = op_dir / "manifest.json"
        manifest_path.write_text(json.dumps(legacy_manifest_payload, indent=2), encoding="utf-8")

        service = DuplicateQuarantineService(self.paths)
        records = service.list_records()
        self.assertEqual(len(records), 1)
        record = records[0]
        self.assertEqual(record.operation_id, op_id)
        self.assertEqual(record.original_path, str(restore_target))
        self.assertEqual(record.stored_path, str(stored_file))
        # Missing reason field should not crash loading
        self.assertIsInstance(record.reason, str)

        restore_result = service.restore_operation(op_id)
        self.assertEqual(len(restore_result.restored), 1)
        self.assertTrue(restore_target.exists())
        self.assertEqual(restore_target.read_bytes(), legacy_content)

        updated_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        self.assertTrue(updated_manifest["records"][0]["restored_at"])
        self.assertEqual(updated_manifest["records"][0]["restored_path"], str(restore_target))


if __name__ == "__main__":
    unittest.main()
