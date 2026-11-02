"""Unit tests for the SQLite media catalog (Task W3)."""

from __future__ import annotations

from datetime import UTC, date, datetime
import hashlib
import os
from pathlib import Path
import shutil
import sqlite3
import struct
import sys
import tempfile
import threading
import unittest

_SOURCE_ROOT = Path(__file__).resolve().parents[1] / "src"
if str(_SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SOURCE_ROOT))

from duplicate_transfer_manager.index import (  # noqa: E402
    INDEX_DOCUMENTATION,
    INITIAL_SCHEMA_VERSION,
    SCHEMA_VERSION,
    CatalogFileInput,
    MediaCatalog,
    MediaMetadata,
    SchemaMigrationError,
    SearchFilter,
    apply_migrations,
    compute_file_sha256,
    extract_file_metadata,
    get_user_version,
    initialize_schema,
)


def _build_minimal_tiff_with_exif(
    *,
    make: str = "Canon",
    model: str = "Canon EOS R5",
    capture_time: str = "2023:08:15 10:20:30",
    width: int = 4000,
    height: int = 3000,
) -> bytes:
    """Construct a minimal little-endian TIFF header with IFD0 and ExifIFD tags."""
    make_bytes = make.encode("ascii") + b"\x00"
    model_bytes = model.encode("ascii") + b"\x00"
    dt_bytes = capture_time.encode("ascii") + b"\x00"

    ifd0_start = 8
    ifd0_count = 5
    exif_ifd_start = ifd0_start + 2 + ifd0_count * 12 + 4  # 74
    exif_ifd_count = 3
    data_start = exif_ifd_start + 2 + exif_ifd_count * 12 + 4  # 116

    make_offset = data_start
    model_offset = make_offset + len(make_bytes)
    dt_offset = model_offset + len(model_bytes)

    parts: list[bytes] = [
        b"II",
        struct.pack("<HI", 42, ifd0_start),
        struct.pack("<H", ifd0_count),
        struct.pack("<HHII", 0x0100, 4, 1, width),
        struct.pack("<HHII", 0x0101, 4, 1, height),
        struct.pack("<HHII", 0x010F, 2, len(make_bytes), make_offset),
        struct.pack("<HHII", 0x0110, 2, len(model_bytes), model_offset),
        struct.pack("<HHII", 0x8769, 4, 1, exif_ifd_start),
        struct.pack("<I", 0),
        struct.pack("<H", exif_ifd_count),
        struct.pack("<HHII", 0x9003, 2, len(dt_bytes), dt_offset),
        struct.pack("<HHII", 0xA002, 4, 1, width),
        struct.pack("<HHII", 0xA003, 4, 1, height),
        struct.pack("<I", 0),
        make_bytes,
        model_bytes,
        dt_bytes,
    ]
    return b"".join(parts)


def _build_minimal_jpeg_with_exif(
    *,
    make: str = "Fujifilm",
    model: str = "X-T5",
    capture_time: str = "2024:02:14 16:45:00",
    width: int = 7728,
    height: int = 5152,
) -> bytes:
    """Construct a minimal JPEG with APP1 Exif and SOF0 segments."""
    tiff_payload = _build_minimal_tiff_with_exif(
        make=make,
        model=model,
        capture_time=capture_time,
        width=width,
        height=height,
    )
    app1_body = b"Exif\x00\x00" + tiff_payload
    app1_seg = b"\xff\xe1" + struct.pack(">H", len(app1_body) + 2) + app1_body
    sof0_body = struct.pack(">BHHB", 8, height, width, 3)
    sof0_seg = b"\xff\xc0" + struct.pack(">H", len(sof0_body) + 2) + sof0_body
    return b"\xff\xd8" + app1_seg + sof0_seg + b"\xff\xd9"


def _build_minimal_png(width: int = 1920, height: int = 1080) -> bytes:
    """Construct a minimal PNG header with an IHDR chunk for dimension parsing."""
    signature = b"\x89PNG\r\n\x1a\n"
    ihdr_data = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    ihdr_len = struct.pack(">I", len(ihdr_data))
    return signature + ihdr_len + b"IHDR" + ihdr_data + b"\x00\x00\x00\x00"


class MediaCatalogTests(unittest.TestCase):
    def test_schema_creation_wal_foreign_keys_and_user_version(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            db_path = Path(tmp_dir) / "catalog.sqlite3"
            with MediaCatalog(db_path) as catalog:
                self.assertEqual(catalog.user_version, SCHEMA_VERSION)
                conn = catalog.connection

                journal_mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
                self.assertEqual(str(journal_mode).lower(), "wal")

                fk_enabled = conn.execute("PRAGMA foreign_keys").fetchone()[0]
                self.assertEqual(int(fk_enabled), 1)

                busy_timeout = conn.execute("PRAGMA busy_timeout").fetchone()[0]
                self.assertEqual(int(busy_timeout), 5000)

                tables = {
                    str(row[0])
                    for row in conn.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'table'"
                    ).fetchall()
                }
                self.assertTrue({"volumes", "files", "metadata"}.issubset(tables))

                indexes = {
                    str(row[0])
                    for row in conn.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'index'"
                    ).fetchall()
                }
                for index_name in INDEX_DOCUMENTATION:
                    self.assertIn(index_name, indexes)

            with MediaCatalog(db_path) as reopened:
                self.assertEqual(reopened.user_version, SCHEMA_VERSION)

    def test_migration_runner_upgrades_older_version_without_data_loss(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            db_path = Path(tmp_dir) / "legacy_v1.sqlite3"

            raw_conn = sqlite3.connect(str(db_path), isolation_level=None)
            try:
                version = initialize_schema(raw_conn, target_version=INITIAL_SCHEMA_VERSION)
                self.assertEqual(version, 1)
                raw_conn.execute("BEGIN IMMEDIATE")
                raw_conn.execute(
                    "INSERT INTO volumes (volume_key, label, filesystem, last_seen) VALUES (?, ?, ?, ?)",
                    ("vol-v1", "PhotosDrive", "NTFS", 1700000000.0),
                )
                cursor = raw_conn.execute(
                    """
                    INSERT INTO files (
                        volume_key, relative_path, size, mtime_ns,
                        content_hash, media_kind, indexed_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        "vol-v1",
                        "2022/trip/IMG_001.JPG",
                        4096,
                        123456789,
                        "deadbeef" * 8,
                        "image",
                        1700000001.0,
                    ),
                )
                file_id = int(cursor.lastrowid)
                raw_conn.execute(
                    """
                    INSERT INTO metadata (
                        file_id, capture_timestamp, capture_date,
                        camera_make, camera_model, width, height
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        file_id,
                        1655296200.0,
                        "2022-06-15T12:30:00+00:00",
                        "Sony",
                        "A7IV",
                        7008,
                        4672,
                    ),
                )
                raw_conn.execute("COMMIT")
            finally:
                raw_conn.close()

            with MediaCatalog(db_path) as catalog:
                self.assertEqual(catalog.user_version, SCHEMA_VERSION)
                vol = catalog.get_volume("vol-v1")
                self.assertIsNotNone(vol)
                assert vol is not None
                self.assertEqual(vol.label, "PhotosDrive")
                self.assertEqual(vol.filesystem, "NTFS")
                self.assertEqual(vol.status, "online")

                rec = catalog.get_file("vol-v1", "2022/trip/IMG_001.JPG")
                self.assertIsNotNone(rec)
                assert rec is not None
                self.assertEqual(rec.filename, "IMG_001.JPG")
                self.assertEqual(rec.extension, ".jpg")
                self.assertEqual(rec.size, 4096)
                self.assertEqual(rec.camera_make, "Sony")
                self.assertEqual(rec.camera_model, "A7IV")
                self.assertEqual(rec.capture_year, 2022)
                self.assertEqual(rec.width, 7008)
                self.assertEqual(rec.height, 4672)

                new_version = apply_migrations(
                    catalog.connection,
                    target_version=SCHEMA_VERSION + 1,
                    migrations={
                        SCHEMA_VERSION + 1: (
                            "ALTER TABLE files ADD COLUMN rating INTEGER NOT NULL DEFAULT 5",
                        )
                    },
                )
                self.assertEqual(new_version, SCHEMA_VERSION + 1)
                self.assertEqual(get_user_version(catalog.connection), SCHEMA_VERSION + 1)
                rating_row = catalog.connection.execute(
                    "SELECT rating FROM files WHERE volume_key = ?",
                    ("vol-v1",),
                ).fetchone()
                self.assertEqual(int(rating_row[0]), 5)

                with self.assertRaises(SchemaMigrationError):
                    apply_migrations(catalog.connection, target_version=SCHEMA_VERSION)

    def test_first_index_adds_rows_and_extracts_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            db_path = root / "index" / "catalog.db"
            vol_root = root / "media_drive"
            (vol_root / "dcim").mkdir(parents=True)

            jpg_file = vol_root / "dcim" / "photo.jpg"
            jpg_file.write_bytes(
                _build_minimal_jpeg_with_exif(
                    make="Fujifilm",
                    model="X-T5",
                    capture_time="2024:02:14 16:45:00",
                    width=7728,
                    height=5152,
                )
            )
            png_file = vol_root / "dcim" / "screen.png"
            png_file.write_bytes(_build_minimal_png(2560, 1440))

            video_file = vol_root / "clip.mp4"
            video_file.write_bytes(b"fake-mp4-video-payload" * 50)

            with MediaCatalog(db_path) as catalog:
                result = catalog.scan_volume(
                    "VOL_MAIN",
                    vol_root,
                    label="MediaDrive",
                    filesystem="exFAT",
                )
                self.assertEqual(result.added, 3)
                self.assertEqual(result.updated, 0)
                self.assertEqual(result.unchanged, 0)
                self.assertEqual(result.removed, 0)

                photo_rec = catalog.get_file("VOL_MAIN", "dcim/photo.jpg")
                self.assertIsNotNone(photo_rec)
                assert photo_rec is not None
                self.assertEqual(photo_rec.camera_make, "Fujifilm")
                self.assertEqual(photo_rec.camera_model, "X-T5")
                self.assertEqual(photo_rec.capture_year, 2024)
                self.assertEqual(photo_rec.width, 7728)
                self.assertEqual(photo_rec.height, 5152)
                self.assertEqual(photo_rec.media_kind, "image")
                self.assertEqual(
                    photo_rec.content_hash,
                    hashlib.sha256(jpg_file.read_bytes()).hexdigest(),
                )

                png_rec = catalog.get_file("VOL_MAIN", "dcim/screen.png")
                self.assertIsNotNone(png_rec)
                assert png_rec is not None
                self.assertEqual(png_rec.width, 2560)
                self.assertEqual(png_rec.height, 1440)

    def test_rescan_unchanged_tree_performs_no_writes_and_reports_all_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            db_path = root / "catalog.db"
            vol_root = root / "library"
            vol_root.mkdir()

            (vol_root / "one.jpg").write_bytes(_build_minimal_tiff_with_exif())
            (vol_root / "two.png").write_bytes(_build_minimal_png(800, 600))
            (vol_root / "three.mp4").write_bytes(b"video-bytes")

            hash_calls = 0
            meta_calls = 0

            def spy_hash(path: Path) -> str | None:
                nonlocal hash_calls
                hash_calls += 1
                return compute_file_sha256(path)

            def spy_meta(path: Path) -> MediaMetadata:
                nonlocal meta_calls
                meta_calls += 1
                return extract_file_metadata(path)

            with MediaCatalog(db_path) as catalog:
                first = catalog.scan_volume(
                    "VOL_LIB",
                    vol_root,
                    label="Lib",
                    filesystem="NTFS",
                    hash_fn=spy_hash,
                    metadata_fn=spy_meta,
                )
                self.assertEqual(first.added, 3)
                self.assertEqual(hash_calls, 3)
                self.assertEqual(meta_calls, 3)

                hash_calls = 0
                meta_calls = 0
                changes_before = catalog.connection.total_changes
                data_version_before = catalog.connection.execute("PRAGMA data_version").fetchone()[0]

                second = catalog.scan_volume(
                    "VOL_LIB",
                    vol_root,
                    label="Lib",
                    filesystem="NTFS",
                    hash_fn=spy_hash,
                    metadata_fn=spy_meta,
                )
                changes_after = catalog.connection.total_changes
                data_version_after = catalog.connection.execute("PRAGMA data_version").fetchone()[0]

                self.assertEqual(second.added, 0)
                self.assertEqual(second.updated, 0)
                self.assertEqual(second.unchanged, 3)
                self.assertEqual(second.removed, 0)
                self.assertEqual(hash_calls, 0)
                self.assertEqual(meta_calls, 0)
                self.assertEqual(changes_after, changes_before)
                self.assertEqual(data_version_after, data_version_before)

    def test_changed_size_or_mtime_triggers_selective_update(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            db_path = root / "catalog.db"
            vol_root = root / "drive"
            vol_root.mkdir()

            file_a = vol_root / "a.jpg"
            file_b = vol_root / "b.jpg"
            file_c = vol_root / "c.jpg"
            file_a.write_bytes(b"content-a")
            file_b.write_bytes(b"content-b")
            file_c.write_bytes(b"content-c")

            hashed_files: list[str] = []

            def track_hash(path: Path) -> str | None:
                hashed_files.append(path.name)
                return compute_file_sha256(path)

            with MediaCatalog(db_path) as catalog:
                initial = catalog.scan_volume("VOL_1", vol_root, hash_fn=track_hash)
                self.assertEqual(initial.added, 3)
                hashed_files.clear()

                # Change ONLY mtime_ns on a.jpg (same size)
                st_a = file_a.stat()
                os.utime(file_a, ns=(st_a.st_atime_ns, st_a.st_mtime_ns + 5_000_000_000))

                res_mtime = catalog.scan_volume("VOL_1", vol_root, hash_fn=track_hash)
                self.assertEqual(res_mtime.added, 0)
                self.assertEqual(res_mtime.updated, 1)
                self.assertEqual(res_mtime.unchanged, 2)
                self.assertEqual(res_mtime.removed, 0)
                self.assertEqual(hashed_files, ["a.jpg"])
                hashed_files.clear()

                # Change ONLY size/content on b.jpg (preserving mtime_ns)
                st_b = file_b.stat()
                new_b_bytes = b"content-b-updated-longer"
                file_b.write_bytes(new_b_bytes)
                os.utime(file_b, ns=(st_b.st_atime_ns, st_b.st_mtime_ns))

                res_size = catalog.scan_volume("VOL_1", vol_root, hash_fn=track_hash)
                self.assertEqual(res_size.added, 0)
                self.assertEqual(res_size.updated, 1)
                self.assertEqual(res_size.unchanged, 2)
                self.assertEqual(res_size.removed, 0)
                self.assertEqual(hashed_files, ["b.jpg"])

                rec_b = catalog.get_file("VOL_1", "b.jpg")
                assert rec_b is not None
                self.assertEqual(rec_b.size, len(new_b_bytes))
                self.assertEqual(rec_b.content_hash, hashlib.sha256(new_b_bytes).hexdigest())

    def test_offline_volume_rows_survive_and_are_reported_offline(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            db_path = root / "catalog.db"
            internal_root = root / "internal"
            external_root = root / "external_usb"
            internal_root.mkdir()
            external_root.mkdir()

            (internal_root / "keep.jpg").write_bytes(b"internal-photo")
            (external_root / "usb1.jpg").write_bytes(b"external-photo-1")
            (external_root / "usb2.mov").write_bytes(b"external-video-2")

            with MediaCatalog(db_path) as catalog:
                catalog.scan_volume("VOL_INT", internal_root, label="Internal")
                catalog.scan_volume("VOL_EXT", external_root, label="ExternalUSB")

                # Disconnect external_usb by removing its mount directory.
                shutil.rmtree(external_root)

                offline_scan = catalog.scan_volume("VOL_EXT", external_root)
                self.assertTrue(offline_scan.offline)
                self.assertEqual(offline_scan.volume_status, "offline")
                self.assertEqual(offline_scan.added, 0)
                self.assertEqual(offline_scan.updated, 0)
                self.assertEqual(offline_scan.unchanged, 0)
                self.assertEqual(offline_scan.removed, 0)

                vol_ext = catalog.get_volume("VOL_EXT")
                self.assertIsNotNone(vol_ext)
                assert vol_ext is not None
                self.assertFalse(vol_ext.is_online)
                self.assertEqual(vol_ext.status, "offline")

                ext_files = catalog.search(volume_key="VOL_EXT")
                self.assertEqual(len(ext_files), 2)
                for record in ext_files:
                    self.assertTrue(record.is_offline)
                    self.assertEqual(record.volume_status, "offline")

                statuses = catalog.refresh_volume_statuses(present_volume_keys=["VOL_INT"])
                self.assertEqual(statuses, {"VOL_INT": "online", "VOL_EXT": "offline"})

    def test_explicit_volume_removal_works(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            db_path = Path(tmp_dir) / "catalog.db"
            with MediaCatalog(db_path) as catalog:
                catalog.upsert_files(
                    "VOL_KEEP",
                    [CatalogFileInput(relative_path="stay.jpg", size=100, mtime_ns=1)],
                )
                catalog.upsert_files(
                    "VOL_DROP",
                    [
                        CatalogFileInput(relative_path="a.jpg", size=200, mtime_ns=2),
                        CatalogFileInput(relative_path="b.jpg", size=300, mtime_ns=3),
                    ],
                )
                catalog.mark_volume_offline("VOL_DROP")

                # Remove a single file explicitly by volume_key + relative_path
                removed_file = catalog.remove_volume_files("VOL_DROP", ["a.jpg"])
                self.assertEqual(removed_file, 1)
                self.assertEqual(len(catalog.search(volume_key="VOL_DROP")), 1)

                # Remove the entire volume explicitly
                removed_vol = catalog.remove_volume("VOL_DROP")
                self.assertEqual(removed_vol, 1)
                self.assertIsNone(catalog.get_volume("VOL_DROP"))
                self.assertEqual(catalog.search(volume_key="VOL_DROP"), [])

                # Other volume remains intact
                self.assertEqual(len(catalog.search(volume_key="VOL_KEEP")), 1)

    def test_every_search_filter_including_duplicate_filter(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            db_path = Path(tmp_dir) / "catalog.db"
            dup_hash = "a" * 64
            unique_hash_1 = "b" * 64
            unique_hash_2 = "c" * 64

            with MediaCatalog(db_path) as catalog:
                catalog.upsert_files(
                    "VOL_A",
                    [
                        CatalogFileInput(
                            relative_path="2022/IMG_0010.JPG",
                            size=1_000,
                            mtime_ns=100,
                            content_hash=dup_hash,
                            media_kind="image",
                            capture_time=datetime(2022, 3, 15, 10, 0, 0, tzinfo=UTC),
                            camera_make="Canon",
                            camera_model="EOS R5",
                            width=4000,
                            height=3000,
                        ),
                        CatalogFileInput(
                            relative_path="2022/IMG_0020_copy.JPG",
                            size=1_000,
                            mtime_ns=200,
                            content_hash=dup_hash,
                            media_kind="image",
                            capture_time=datetime(2022, 3, 15, 10, 0, 0, tzinfo=UTC),
                            camera_make="Canon",
                            camera_model="EOS R5",
                            width=4000,
                            height=3000,
                        ),
                        CatalogFileInput(
                            relative_path="2023/VID_9000.MP4",
                            size=50_000,
                            mtime_ns=300,
                            content_hash=unique_hash_1,
                            media_kind="video",
                            capture_time=datetime(2023, 7, 4, 18, 30, 0, tzinfo=UTC),
                            camera_make="Sony",
                            camera_model="FX3",
                            width=3840,
                            height=2160,
                        ),
                    ],
                )
                catalog.upsert_files(
                    "VOL_B",
                    [
                        CatalogFileInput(
                            relative_path="iphone/IMG_7777.HEIC",
                            size=5_000,
                            mtime_ns=400,
                            content_hash=unique_hash_2,
                            media_kind="image",
                            capture_time=datetime(2024, 1, 20, 9, 0, 0, tzinfo=UTC),
                            camera_make="Apple",
                            camera_model="iPhone 15 Pro",
                            width=1179,
                            height=2556,
                        ),
                        CatalogFileInput(
                            relative_path="music/track.flac",
                            size=12_000,
                            mtime_ns=500,
                            content_hash=None,
                            media_kind="audio",
                        ),
                    ],
                )

                self.assertEqual([r.filename for r in catalog.search(media_kind="video")], ["VID_9000.MP4"])
                self.assertEqual(len(catalog.search(media_kind=["image", "audio"])), 4)
                self.assertEqual(len(catalog.search(filename="img_00")), 2)
                self.assertEqual(
                    [r.size for r in catalog.search(min_size=2_000, max_size=20_000, order_by="size")],
                    [5_000, 12_000],
                )
                self.assertEqual(
                    [r.mtime_ns for r in catalog.search(min_mtime_ns=150, max_mtime_ns=350, order_by="mtime_ns")],
                    [200, 300],
                )
                self.assertEqual(
                    [r.filename for r in catalog.search(capture_start=date(2023, 1, 1), capture_end=date(2023, 12, 31))],
                    ["VID_9000.MP4"],
                )
                self.assertEqual(len(catalog.search(camera_make="canon", camera_model="eos r5")), 2)
                self.assertEqual(len(catalog.search(volume_key="VOL_B")), 2)

                only_dups = catalog.search(has_duplicates=True)
                self.assertEqual(len(only_dups), 2)
                self.assertTrue(all(r.content_hash == dup_hash for r in only_dups))

                non_dups = catalog.search(has_duplicates=False)
                self.assertEqual(len(non_dups), 3)
                self.assertTrue(all(r.content_hash != dup_hash for r in non_dups))

    def test_paging_and_deterministic_ordering(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            db_path = Path(tmp_dir) / "catalog.db"
            with MediaCatalog(db_path) as catalog:
                catalog.upsert_files(
                    "VOL_1",
                    [
                        CatalogFileInput(relative_path="b_item.jpg", size=1_000, mtime_ns=200),
                        CatalogFileInput(relative_path="a_item.jpg", size=1_000, mtime_ns=100),
                        CatalogFileInput(relative_path="c_item.jpg", size=2_000, mtime_ns=300),
                        CatalogFileInput(relative_path="d_item.jpg", size=3_000, mtime_ns=400),
                        CatalogFileInput(relative_path="e_item.jpg", size=4_000, mtime_ns=500),
                    ],
                )

                # Tie on size=1_000 breaks deterministically by (volume_key, relative_path)
                by_size = catalog.search(order_by="size", descending=False)
                self.assertEqual(
                    [r.relative_path for r in by_size],
                    ["a_item.jpg", "b_item.jpg", "c_item.jpg", "d_item.jpg", "e_item.jpg"],
                )

                by_size_desc = catalog.search(order_by="size", descending=True)
                self.assertEqual(
                    [r.relative_path for r in by_size_desc],
                    ["e_item.jpg", "d_item.jpg", "c_item.jpg", "a_item.jpg", "b_item.jpg"],
                )

                p1 = catalog.search(SearchFilter(order_by="size", limit=2, offset=0))
                p2 = catalog.search(SearchFilter(order_by="size", limit=2, offset=2))
                p3 = catalog.search(SearchFilter(order_by="size", limit=2, offset=4))
                self.assertEqual([len(p1), len(p2), len(p3)], [2, 2, 1])
                self.assertEqual([r.id for r in p1 + p2 + p3], [r.id for r in by_size])

    def test_sql_metacharacters_in_filenames_handled_safely_as_data(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            db_path = Path(tmp_dir) / "catalog.db"
            with MediaCatalog(db_path) as catalog:
                catalog.upsert_files(
                    "VOL_SAFE",
                    [
                        CatalogFileInput(
                            relative_path="sub/O'Brien_100%_raw'; DROP TABLE files; --.jpg",
                            size=1_024,
                            mtime_ns=100,
                            media_kind="image",
                        ),
                        CatalogFileInput(
                            relative_path="sub/1000_normal_file.jpg",
                            size=2_048,
                            mtime_ns=200,
                            media_kind="image",
                        ),
                        CatalogFileInput(
                            relative_path="sub/under_score_test.jpg",
                            size=512,
                            mtime_ns=300,
                            media_kind="image",
                        ),
                    ],
                )

                # '%' must be treated as literal character, NOT a SQL wildcard matching "1000_normal_file.jpg"
                pct_hits = catalog.search(filename="100%")
                self.assertEqual(len(pct_hits), 1)
                self.assertIn("100%", pct_hits[0].filename)

                quote_hits = catalog.search(filename="O'Brien")
                self.assertEqual(len(quote_hits), 1)

                inj_hits = catalog.search(filename="'; DROP TABLE files; --")
                self.assertEqual(len(inj_hits), 1)

                # '_' must not match arbitrary single characters
                underscore_hits = catalog.search(filename="under_score")
                self.assertEqual(len(underscore_hits), 1)

                # Confirm table still has all 3 rows
                self.assertEqual(len(catalog.search()), 3)

    def test_storage_analytics_and_recoverable_duplicate_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            db_path = Path(tmp_dir) / "catalog.db"
            hash_dup_3x = "1" * 64
            hash_dup_2x = "2" * 64
            hash_unique = "3" * 64

            with MediaCatalog(db_path) as catalog:
                self.assertEqual(catalog.recoverable_duplicate_bytes(), 0)

                catalog.upsert_files(
                    "VOL_1",
                    [
                        CatalogFileInput(
                            relative_path="a/dup1.jpg",
                            size=1_000,
                            mtime_ns=10,
                            content_hash=hash_dup_3x,
                            media_kind="image",
                            capture_time="2021:05:01 12:00:00",
                        ),
                        CatalogFileInput(
                            relative_path="a/dup2.jpg",
                            size=1_000,
                            mtime_ns=20,
                            content_hash=hash_dup_3x,
                            media_kind="image",
                            capture_time="2021:06:01 12:00:00",
                        ),
                        CatalogFileInput(
                            relative_path="a/dup3.jpg",
                            size=1_000,
                            mtime_ns=30,
                            content_hash=hash_dup_3x,
                            media_kind="image",
                            capture_time="2021:07:01 12:00:00",
                        ),
                        CatalogFileInput(
                            relative_path="a/unique.png",
                            size=4_000,
                            mtime_ns=40,
                            content_hash=hash_unique,
                            media_kind="image",
                            capture_time="2022:01:10 09:00:00",
                        ),
                    ],
                )
                catalog.upsert_files(
                    "VOL_2",
                    [
                        CatalogFileInput(
                            relative_path="v/movie1.mp4",
                            size=5_000,
                            mtime_ns=50,
                            content_hash=hash_dup_2x,
                            media_kind="video",
                            capture_time="2022:08:20 15:00:00",
                        ),
                        CatalogFileInput(
                            relative_path="v/movie2.mp4",
                            size=5_000,
                            mtime_ns=60,
                            content_hash=hash_dup_2x,
                            media_kind="video",
                            capture_time="2022:08:20 15:00:00",
                        ),
                        CatalogFileInput(
                            relative_path="docs/notes.pdf",
                            size=500,
                            mtime_ns=70,
                            content_hash=None,
                            media_kind="document",
                        ),
                    ],
                )

                by_kind = {b.key: (b.file_count, b.total_bytes) for b in catalog.analytics_by_media_kind()}
                self.assertEqual(
                    by_kind,
                    {
                        "video": (2, 10_000),
                        "image": (4, 7_000),
                        "document": (1, 500),
                    },
                )

                by_vol = {b.key: (b.file_count, b.total_bytes) for b in catalog.analytics_by_volume()}
                self.assertEqual(
                    by_vol,
                    {
                        "VOL_2": (3, 10_500),
                        "VOL_1": (4, 7_000),
                    },
                )

                by_year = {b.key: (b.file_count, b.total_bytes) for b in catalog.analytics_by_capture_year()}
                self.assertEqual(
                    by_year,
                    {
                        2021: (3, 3_000),
                        2022: (3, 14_000),
                    },
                )

                by_ext = {b.key: (b.file_count, b.total_bytes) for b in catalog.analytics_by_extension()}
                self.assertEqual(
                    by_ext,
                    {
                        ".mp4": (2, 10_000),
                        ".png": (1, 4_000),
                        ".jpg": (3, 3_000),
                        ".pdf": (1, 500),
                    },
                )

                # (3 - 1) * 1000 + (2 - 1) * 5000 = 2000 + 5000 = 7000
                self.assertEqual(catalog.recoverable_duplicate_bytes(), 7_000)

                summary = catalog.get_storage_analytics()
                self.assertEqual(summary.total_files, 7)
                self.assertEqual(summary.total_bytes, 17_500)
                self.assertEqual(summary.recoverable_duplicate_bytes, 7_000)

    def test_cancellation_mid_scan_leaves_database_consistent_and_reopenable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            db_path = root / "catalog.db"
            vol_root = root / "photos"
            vol_root.mkdir()

            for idx in range(6):
                (vol_root / f"img_{idx:02d}.jpg").write_bytes(f"payload-{idx}".encode("ascii"))

            with MediaCatalog(db_path) as catalog:
                baseline = catalog.upsert_files(
                    "VOL_BASE",
                    [
                        CatalogFileInput(
                            relative_path="committed.jpg",
                            size=123,
                            mtime_ns=999,
                            content_hash="f" * 64,
                            media_kind="image",
                        )
                    ],
                )
                self.assertEqual(baseline.added, 1)

                stop_event = threading.Event()
                calls = 0

                def cancelling_hash(path: Path) -> str:
                    nonlocal calls
                    calls += 1
                    if calls >= 3:
                        stop_event.set()
                    return hashlib.sha256(path.read_bytes()).hexdigest()

                cancelled_res = catalog.scan_volume(
                    "VOL_CANCELLED",
                    vol_root,
                    hash_fn=cancelling_hash,
                    stop_event=stop_event,
                )
                self.assertTrue(cancelled_res.cancelled)
                self.assertEqual(cancelled_res.volume_status, "cancelled")
                self.assertFalse(catalog.connection.in_transaction)

                # Interrupted scan must not leave a half-written batch behind.
                self.assertEqual(catalog.search(volume_key="VOL_CANCELLED"), [])
                self.assertIsNone(catalog.get_volume("VOL_CANCELLED"))

                integrity = catalog.connection.execute("PRAGMA integrity_check").fetchone()[0]
                self.assertEqual(str(integrity).lower(), "ok")
                fk_violations = catalog.connection.execute("PRAGMA foreign_key_check").fetchall()
                self.assertEqual(fk_violations, [])

            with MediaCatalog(db_path) as reopened:
                committed = reopened.search(volume_key="VOL_BASE")
                self.assertEqual(len(committed), 1)
                self.assertEqual(committed[0].relative_path, "committed.jpg")

                completed = reopened.scan_volume("VOL_CANCELLED", vol_root)
                self.assertFalse(completed.cancelled)
                self.assertEqual(completed.added, 6)
                self.assertEqual(len(reopened.search(volume_key="VOL_CANCELLED")), 6)


if __name__ == "__main__":
    unittest.main()
