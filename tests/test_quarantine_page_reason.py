"""Tests for displaying quarantine reasons on the Quarantine page."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6.QtCore import QThreadPool
    from PySide6.QtWidgets import QApplication, QCheckBox
except ImportError as exc:  # pragma: no cover
    raise unittest.SkipTest(f"PySide6 is required for UI tests: {exc}")

from duplicate_transfer_manager.runtime_paths import get_runtime_paths
from duplicate_transfer_manager.services.duplicate_workflow import (
    DuplicateQuarantineService,
    build_duplicate_review,
)
from duplicate_transfer_manager.ui.pages import QuarantinePage
from models import FileInfo


class QuarantinePageReasonTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def test_quarantine_page_shows_reasons_and_restores(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            files_dir = root / "files"
            files_dir.mkdir(parents=True, exist_ok=True)
            paths = get_runtime_paths(root / "data")
            service = DuplicateQuarantineService(paths)

            # 1. Real duplicate with default reason ("duplicate")
            keep1 = files_dir / "keep1.txt"
            dup1 = files_dir / "dup1.txt"
            content1 = b"byte-identical-first-duplicate-pair"
            keep1.write_bytes(content1)
            dup1.write_bytes(content1)

            review1 = build_duplicate_review(
                [[FileInfo(str(keep1), len(content1), 100), FileInfo(str(dup1), len(content1), 200)]],
                thumbnail_root=root / "thumbs1",
            )
            victim1_id = next(item.id for item in review1.groups[0].items if item.id != review1.groups[0].keep_item_id)
            res1 = service.quarantine(review1, [victim1_id], operation_id="op1_default")
            self.assertEqual(res1.records[0].reason, "duplicate")

            # 2. Real duplicate with reason="similar"
            keep2 = files_dir / "keep2.jpg"
            dup2 = files_dir / "dup2.jpg"
            content2 = b"byte-identical-similar-photo-pair"
            keep2.write_bytes(content2)
            dup2.write_bytes(content2)

            review2 = build_duplicate_review(
                [[FileInfo(str(keep2), len(content2), 100), FileInfo(str(dup2), len(content2), 200)]],
                thumbnail_root=root / "thumbs2",
            )
            victim2_id = next(item.id for item in review2.groups[0].items if item.id != review2.groups[0].keep_item_id)
            res2 = service.quarantine(review2, [victim2_id], operation_id="op2_similar", reason="similar")
            self.assertEqual(res2.records[0].reason, "similar")

            # 3. Hand-written old-format manifest without "reason" key
            op3_dir = paths.quarantine / "op3_legacy"
            op3_dir.mkdir(parents=True, exist_ok=True)
            legacy_stored = op3_dir / "legacy_stored.bin"
            legacy_stored.write_bytes(b"legacy-stored-bytes")
            legacy_manifest = {
                "operation_id": "op3_legacy",
                "created_at": "2024-01-01T00:00:00Z",
                "records": [
                    {
                        "original_path": str(files_dir / "legacy_orig.bin"),
                        "stored_path": str(legacy_stored),
                        "hash": "0123456789abcdef" * 4,
                        "size": len(b"legacy-stored-bytes"),
                        # Explicitly no "reason" key
                        "operation_id": "op3_legacy",
                        "timestamp": "2024-01-01T00:00:00Z",
                        "source_is_adb": False,
                        "device_serial": "",
                        "restored_path": "",
                        "restored_at": "",
                        "status": "completed",
                    }
                ],
            }
            (op3_dir / "manifest.json").write_text(json.dumps(legacy_manifest), encoding="utf-8")

            # Verify dup2 was moved to quarantine before restore
            self.assertFalse(dup2.exists(), "quarantined similar file should have been moved")

            # Load page
            page = QuarantinePage(service)
            self.app.processEvents()

            # Verify table header: Reason column is placed after Operation (index 2)
            self.assertEqual(page.table.horizontalHeaderItem(0).text(), "Restore")
            self.assertEqual(page.table.horizontalHeaderItem(1).text(), "Operation")
            self.assertEqual(page.table.horizontalHeaderItem(2).text(), "Reason")
            self.assertEqual(page.table.horizontalHeaderItem(3).text(), "Original path")

            # Assert Reason cells read "Duplicate", "Similar photo", "Duplicate"
            self.assertEqual(page.table.rowCount(), 3)
            self.assertEqual(page.table.item(0, 2).text(), "Duplicate")
            self.assertEqual(page.table.item(1, 2).text(), "Similar photo")
            self.assertEqual(page.table.item(2, 2).text(), "Duplicate")

            # Select the similar row and assert the preview mentions "Similar photo"
            page.table.setCurrentCell(1, 2)
            page._update_preview()
            self.assertIn("Similar photo", page.preview_detail.text())

            # Also check the default row preview mentions "Duplicate"
            page.table.setCurrentCell(0, 2)
            page._update_preview()
            self.assertIn("Duplicate", page.preview_detail.text())

            # Reselect similar row and check its checkbox to restore it
            check_box = page.table.cellWidget(1, 0)
            self.assertIsInstance(check_box, QCheckBox)
            check_box.setChecked(True)
            self.app.processEvents()

            # Restore it through the page's restore path
            page.restore_selected()
            self.assertTrue(QThreadPool.globalInstance().waitForDone(15_000))
            self.app.processEvents()

            # Assert the file is back byte-identical
            self.assertTrue(dup2.exists(), "restored file should exist at original path")
            self.assertEqual(dup2.read_bytes(), content2, "restored file must be byte-identical")

            # Assert dup1 (not selected for restore) remains quarantined
            self.assertFalse(dup1.exists(), "unselected duplicate should remain quarantined")

            page.deleteLater()
            self.app.processEvents()

    def test_format_reason_formatting(self) -> None:
        self.assertEqual(QuarantinePage._format_reason("duplicate"), "Duplicate")
        self.assertEqual(QuarantinePage._format_reason("Duplicate"), "Duplicate")
        self.assertEqual(QuarantinePage._format_reason("DUPLICATE"), "Duplicate")
        self.assertEqual(QuarantinePage._format_reason("similar"), "Similar photo")
        self.assertEqual(QuarantinePage._format_reason("Similar"), "Similar photo")
        self.assertEqual(QuarantinePage._format_reason("SIMILAR"), "Similar photo")
        self.assertEqual(QuarantinePage._format_reason(""), "Duplicate")
        self.assertEqual(QuarantinePage._format_reason(None), "Duplicate")
        self.assertEqual(QuarantinePage._format_reason("   "), "Duplicate")
        self.assertEqual(QuarantinePage._format_reason("custom"), "Custom")
        self.assertEqual(QuarantinePage._format_reason("other reason"), "Other Reason")


if __name__ == "__main__":
    unittest.main()
