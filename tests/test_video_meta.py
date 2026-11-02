"""Tests for video metadata extraction from ISO-BMFF (MP4, MOV) files."""

from __future__ import annotations

import os
import struct
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from duplicate_transfer_manager.media.video_meta import (
    MP4_EPOCH_OFFSET,
    VideoMetadata,
    read_video_metadata,
)
from duplicate_transfer_manager.sorting.metadata import MetadataExtractor
from duplicate_transfer_manager.sorting.planner import camera_tokens


def box(kind: str, payload: bytes) -> bytes:
    """Standard 32-bit ISO-BMFF box."""
    return struct.pack(">I4s", 8 + len(payload), kind.encode("latin-1")) + payload


def box64(kind: str, payload: bytes) -> bytes:
    """64-bit large-size ISO-BMFF box."""
    return struct.pack(">I4sQ", 1, kind.encode("latin-1"), 16 + len(payload)) + payload


def full_box(kind: str, payload: bytes, version: int = 0, flags: int = 0) -> bytes:
    """FullBox with 1-byte version and 3-byte flags."""
    return box(kind, struct.pack(">I", (version << 24) | (flags & 0xFFFFFF)) + payload)


def mvhd_box(creation_time: int, duration: int, timescale: int = 1000, version: int = 0) -> bytes:
    """Movie Header Box (mvhd) in version 0 or version 1."""
    if version == 0:
        head = struct.pack(">IIII", creation_time, 0, timescale, duration)
    else:
        head = struct.pack(">QQIQ", creation_time, 0, timescale, duration)
    # Rate (1.0 = 0x10000), Volume (1.0 = 0x100), Reserved (10 bytes), Identity Matrix (36 bytes), Pre-defined (24 bytes), Next track ID (4 bytes)
    tail = struct.pack(
        ">ih10s9i24sI",
        0x10000,
        0x100,
        b"\x00" * 10,
        0x10000, 0, 0,
        0, 0x10000, 0,
        0, 0, 0x40000000,
        b"\x00" * 24,
        2,
    )
    return full_box("mvhd", head + tail, version=version)


def trak_box(
    *,
    handler: bytes = b"vide",
    width: int = 1920,
    height: int = 1080,
    matrix: tuple[int, ...] | None = None,
    version: int = 0,
) -> bytes:
    """Track Box (trak) with tkhd and mdia/hdlr."""
    if matrix is None:
        # Standard identity matrix
        matrix = (0x10000, 0, 0, 0, 0x10000, 0, 0, 0, 0x40000000)

    if version == 0:
        tkhd_head = struct.pack(">IIIII", 0, 0, 1, 0, 1000)
    else:
        tkhd_head = struct.pack(">QQIIQ", 0, 0, 1, 0, 1000)
    # reserved (8), layer (2), alt_group (2), volume (2), reserved (2)
    tkhd_mid = struct.pack(">8shhh2s", b"\x00" * 8, 0, 0, 0x100 if handler == b"soun" else 0, b"\x00" * 2)
    tkhd_matrix = struct.pack(">9i", *matrix)
    tkhd_dim = struct.pack(">II", width << 16, height << 16)
    tkhd = full_box("tkhd", tkhd_head + tkhd_mid + tkhd_matrix + tkhd_dim, version=version)

    mdhd = full_box("mdhd", struct.pack(">IIIIHH", 0, 0, 1000, 1000, 0, 0))
    hdlr = full_box("hdlr", b"\x00" * 4 + handler + b"\x00" * 12 + b"\x00")
    minf = box("minf", box("smhd", b"\x00" * 8) if handler == b"soun" else box("vmhd", b"\x00" * 12))
    mdia = box("mdia", mdhd + hdlr + minf)
    return box("trak", tkhd + mdia)


def apple_meta_box(items: dict[str, str]) -> bytes:
    """Apple QuickTime moov/meta box containing keys and ilst."""
    keys_entries = b""
    ilst_entries = b""

    for idx, (key_name, val_str) in enumerate(items.items(), start=1):
        key_raw = key_name.encode("utf-8")
        keys_entries += struct.pack(">I4s", 8 + len(key_raw), b"mdta") + key_raw

        val_raw = val_str.encode("utf-8")
        # data box: 4 bytes type indicator (1 = UTF-8 text), 4 bytes locale (0), payload
        data_box = box("data", struct.pack(">II", 1, 0) + val_raw)
        item_box = struct.pack(">I4s", 8 + len(data_box), struct.pack(">I", idx)) + data_box
        ilst_entries += item_box

    keys_box = full_box("keys", struct.pack(">I", len(items)) + keys_entries)
    ilst_box = box("ilst", ilst_entries)
    hdlr_box = full_box("hdlr", b"\x00" * 4 + b"mdir" + b"\x00" * 12 + b"\x00")
    return full_box("meta", hdlr_box + keys_box + ilst_box)


def make_mp4(
    *,
    mvhd: bytes | None = None,
    traks: list[bytes] | None = None,
    meta: bytes | None = None,
    moov_first: bool = True,
    mdat_64: bool = False,
    mdat_payload: bytes = b"sample_video_frames",
) -> bytes:
    """Assemble a complete MP4/MOV container."""
    ftyp = box("ftyp", b"mp42\x00\x00\x00\x00mp42isom")
    moov_children = b""
    if mvhd is not None:
        moov_children += mvhd
    if traks:
        for t in traks:
            moov_children += t
    if meta is not None:
        moov_children += meta
    moov = box("moov", moov_children)

    if mdat_64:
        mdat = box64("mdat", mdat_payload)
    else:
        mdat = box("mdat", mdat_payload)

    return (ftyp + moov + mdat) if moov_first else (ftyp + mdat + moov)


class VideoMetaTests(unittest.TestCase):
    def test_mvhd_v0_duration_and_utc_creation(self):
        # 2023-07-14 18:30:05 UTC
        dt = datetime(2023, 7, 14, 18, 30, 5, tzinfo=timezone.utc)
        epoch = dt.timestamp()
        mp4_sec = int(epoch + MP4_EPOCH_OFFSET)

        mvhd = mvhd_box(creation_time=mp4_sec, duration=15500, timescale=1000, version=0)
        trak = trak_box(handler=b"vide", width=1920, height=1080)
        data = make_mp4(mvhd=mvhd, traks=[trak])

        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "test.mp4"
            path.write_bytes(data)
            meta = read_video_metadata(path)

        self.assertEqual(meta.captured, epoch)
        self.assertEqual(meta.duration, 15.5)
        self.assertEqual(meta.width, 1920)
        self.assertEqual(meta.height, 1080)
        self.assertEqual(meta.make, "")
        self.assertEqual(meta.model, "")

    def test_mvhd_v1_duration_and_utc_creation(self):
        # 2024-02-20 12:00:00 UTC
        dt = datetime(2024, 2, 20, 12, 0, 0, tzinfo=timezone.utc)
        epoch = dt.timestamp()
        mp4_sec = int(epoch + MP4_EPOCH_OFFSET)

        mvhd = mvhd_box(creation_time=mp4_sec, duration=30000, timescale=1000, version=1)
        trak = trak_box(handler=b"vide", width=3840, height=2160, version=1)
        data = make_mp4(mvhd=mvhd, traks=[trak])

        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "test_v1.mp4"
            path.write_bytes(data)
            meta = read_video_metadata(path)

        self.assertEqual(meta.captured, epoch)
        self.assertEqual(meta.duration, 30.0)
        self.assertEqual(meta.width, 3840)
        self.assertEqual(meta.height, 2160)

    def test_creation_time_unknown_or_before_1990(self):
        # 0 creation time
        mvhd_zero = mvhd_box(creation_time=0, duration=5000, timescale=1000)
        trak = trak_box(handler=b"vide", width=640, height=480)
        data_zero = make_mp4(mvhd=mvhd_zero, traks=[trak])

        # 1980 date (before 1990)
        dt_old = datetime(1980, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
        mp4_old = int(dt_old.timestamp() + MP4_EPOCH_OFFSET)
        mvhd_old = mvhd_box(creation_time=mp4_old, duration=5000, timescale=1000)
        data_old = make_mp4(mvhd=mvhd_old, traks=[trak])

        with tempfile.TemporaryDirectory() as temp_dir:
            p_zero = Path(temp_dir) / "zero.mp4"
            p_zero.write_bytes(data_zero)
            p_old = Path(temp_dir) / "old.mp4"
            p_old.write_bytes(data_old)

            self.assertEqual(read_video_metadata(p_zero).captured, 0.0)
            self.assertEqual(read_video_metadata(p_old).captured, 0.0)

    def test_apple_creationdate_with_offset_wins(self):
        # mvhd has one date (e.g. 2020-01-01 UTC)
        mvhd_epoch = datetime(2020, 1, 1, 0, 0, 0, tzinfo=timezone.utc).timestamp()
        mvhd = mvhd_box(creation_time=int(mvhd_epoch + MP4_EPOCH_OFFSET), duration=10000, timescale=1000)
        trak = trak_box(handler=b"vide", width=1920, height=1080)

        # Apple QuickTime metadata with ISO 8601 offset: 2023-07-14T18:30:05-04:00
        apple_str = "2023-07-14T18:30:05-04:00"
        apple_epoch = datetime.fromisoformat(apple_str).timestamp()
        meta = apple_meta_box({
            "com.apple.quicktime.creationdate": apple_str,
            "com.apple.quicktime.make": "Apple",
            "com.apple.quicktime.model": "iPhone 15 Pro",
        })

        data = make_mp4(mvhd=mvhd, traks=[trak], meta=meta)

        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "iphone.mov"
            path.write_bytes(data)
            result = read_video_metadata(path)

        # Apple creationdate with offset must win over mvhd!
        self.assertEqual(result.captured, apple_epoch)
        self.assertNotEqual(result.captured, mvhd_epoch)
        self.assertEqual(result.make, "Apple")
        self.assertEqual(result.model, "iPhone 15 Pro")

    def test_rotated_portrait_swaps_dimensions(self):
        # 90 degrees clockwise rotation matrix:
        # a=0, b=1, c=-1, d=0
        matrix_90 = (0, 0x10000, 0, -0x10000, 0, 0, 0, 0, 0x40000000)
        trak_90 = trak_box(handler=b"vide", width=1920, height=1080, matrix=matrix_90)
        mvhd = mvhd_box(creation_time=0, duration=1000)
        data = make_mp4(mvhd=mvhd, traks=[trak_90])

        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "portrait_90.mp4"
            path.write_bytes(data)
            result = read_video_metadata(path)

        self.assertEqual(result.width, 1080)
        self.assertEqual(result.height, 1920)

    def test_rotated_270_degrees_swaps_dimensions(self):
        # 270 degrees rotation matrix:
        # a=0, b=-1, c=1, d=0
        matrix_270 = (0, -0x10000, 0, 0x10000, 0, 0, 0, 0, 0x40000000)
        trak_270 = trak_box(handler=b"vide", width=1920, height=1080, matrix=matrix_270)
        mvhd = mvhd_box(creation_time=0, duration=1000)
        data = make_mp4(mvhd=mvhd, traks=[trak_270])

        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "portrait_270.mp4"
            path.write_bytes(data)
            result = read_video_metadata(path)

        self.assertEqual(result.width, 1080)
        self.assertEqual(result.height, 1920)

    def test_audio_only_track_ignored(self):
        # Track with handler 'soun'
        trak_audio = trak_box(handler=b"soun", width=0, height=0)
        mvhd = mvhd_box(creation_time=0, duration=8000, timescale=1000)
        data = make_mp4(mvhd=mvhd, traks=[trak_audio])

        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "audio.m4a"
            path.write_bytes(data)
            result = read_video_metadata(path)

        self.assertEqual(result.duration, 8.0)
        self.assertEqual(result.width, 0)
        self.assertEqual(result.height, 0)

    def test_audio_first_then_video_track(self):
        trak_audio = trak_box(handler=b"soun", width=0, height=0)
        trak_video = trak_box(handler=b"vide", width=1280, height=720)
        mvhd = mvhd_box(creation_time=0, duration=12000, timescale=1000)
        data = make_mp4(mvhd=mvhd, traks=[trak_audio, trak_video])

        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "av.mp4"
            path.write_bytes(data)
            result = read_video_metadata(path)

        self.assertEqual(result.width, 1280)
        self.assertEqual(result.height, 720)

    def test_moov_after_mdat_and_64bit_mdat(self):
        # moov after mdat with 64-bit mdat size
        dt = datetime(2023, 10, 5, 14, 20, 0, tzinfo=timezone.utc)
        epoch = dt.timestamp()
        mvhd = mvhd_box(creation_time=int(epoch + MP4_EPOCH_OFFSET), duration=25000, timescale=1000)
        trak = trak_box(handler=b"vide", width=1920, height=1080)
        data = make_mp4(mvhd=mvhd, traks=[trak], moov_first=False, mdat_64=True, mdat_payload=b"A" * 500)

        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "moov_at_end.mp4"
            path.write_bytes(data)
            result = read_video_metadata(path)

        self.assertEqual(result.captured, epoch)
        self.assertEqual(result.duration, 25.0)
        self.assertEqual(result.width, 1920)
        self.assertEqual(result.height, 1080)

    def test_truncated_garbage_huge_count_input(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)

            empty_file = root / "empty.mp4"
            empty_file.write_bytes(b"")
            self.assertTrue(read_video_metadata(empty_file).empty)

            garbage_file = root / "garbage.mp4"
            garbage_file.write_bytes(b"Not an MP4 file at all, just random text.")
            self.assertTrue(read_video_metadata(garbage_file).empty)

            truncated_file = root / "truncated.mp4"
            full_data = make_mp4(
                mvhd=mvhd_box(creation_time=3000000000, duration=1000),
                traks=[trak_box()],
            )
            truncated_file.write_bytes(full_data[:20])
            self.assertTrue(read_video_metadata(truncated_file).empty)

            # Huge count in keys box (0xFFFFFFFF)
            bogus_keys = full_box("keys", struct.pack(">I", 0xFFFFFFFF) + b"\x00" * 32)
            bogus_meta = full_box("meta", bogus_keys)
            huge_count_file = root / "huge_count.mp4"
            huge_count_file.write_bytes(make_mp4(mvhd=mvhd_box(0, 1000), meta=bogus_meta))
            meta = read_video_metadata(huge_count_file)
            self.assertEqual(meta.make, "")
            self.assertEqual(meta.model, "")

            # Huge 64-bit box size pointing beyond EOF
            bogus_box = struct.pack(">I4sQ", 1, b"moov", 0xFFFFFFFFFFFFFFFF)
            bad_size_file = root / "bad_size.mp4"
            bad_size_file.write_bytes(bogus_box)
            self.assertTrue(read_video_metadata(bad_size_file).empty)

    def test_metadata_extractor_end_to_end_with_shutil_which_none(self):
        dt = datetime(2023, 7, 14, 18, 30, 5, tzinfo=timezone.utc)
        epoch = dt.timestamp()
        mvhd = mvhd_box(creation_time=int(epoch + MP4_EPOCH_OFFSET), duration=18200, timescale=1000)
        trak = trak_box(handler=b"vide", width=1920, height=1080)
        meta = apple_meta_box({
            "com.apple.quicktime.creationdate": "2023-07-14T18:30:05-04:00",
            "com.apple.quicktime.make": "Apple",
            "com.apple.quicktime.model": "iPhone 15 Pro",
        })
        data = make_mp4(mvhd=mvhd, traks=[trak], meta=meta)

        with tempfile.TemporaryDirectory() as temp_dir:
            video_path = Path(temp_dir) / "clip.mp4"
            video_path.write_bytes(data)

            with patch("duplicate_transfer_manager.sorting.metadata.shutil.which", return_value=None):
                item = MetadataExtractor().extract(video_path)

            apple_epoch = datetime.fromisoformat("2023-07-14T18:30:05-04:00").timestamp()
            self.assertEqual(item.captured, apple_epoch)
            self.assertEqual(item.width, 1920)
            self.assertEqual(item.height, 1080)
            self.assertEqual(item.duration, 18.2)
            self.assertEqual(item.attributes.get("camera_make"), "Apple")
            self.assertEqual(item.attributes.get("camera_model"), "iPhone 15 Pro")

            tokens = camera_tokens(item)
            self.assertEqual(tokens["camera"], "Apple iPhone 15 Pro")

    def test_file_bytes_and_mtime_unchanged(self):
        dt = datetime(2023, 8, 1, 10, 0, 0, tzinfo=timezone.utc)
        epoch = dt.timestamp()
        mvhd = mvhd_box(creation_time=int(epoch + MP4_EPOCH_OFFSET), duration=5000, timescale=1000)
        trak = trak_box(handler=b"vide", width=1280, height=720)
        data = make_mp4(mvhd=mvhd, traks=[trak])

        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "read_only.mp4"
            path.write_bytes(data)

            # Set a distinct mtime in the past
            os.utime(path, (1600000000, 1600000000))
            stat_before = path.stat()
            bytes_before = path.read_bytes()

            # Execute reader directly and via extractor
            _ = read_video_metadata(path)
            with patch("duplicate_transfer_manager.sorting.metadata.shutil.which", return_value=None):
                _ = MetadataExtractor().extract(path)

            stat_after = path.stat()
            bytes_after = path.read_bytes()

            self.assertEqual(bytes_before, bytes_after)
            self.assertEqual(stat_before.st_size, stat_after.st_size)
            self.assertEqual(stat_before.st_mtime_ns, stat_after.st_mtime_ns)


if __name__ == "__main__":
    unittest.main()


class FfprobePathTests(unittest.TestCase):
    def test_capture_date_and_camera_survive_when_ffprobe_is_installed(self):
        import json
        import subprocess
        import tempfile
        from pathlib import Path
        from unittest import mock

        from duplicate_transfer_manager.media import video_meta
        from duplicate_transfer_manager.sorting.metadata import MetadataExtractor

        fake = video_meta.VideoMetadata(captured=1_700_000_000.0, width=0, height=0, duration=0.0, make="Apple", model="iPhone 15 Pro")
        completed = subprocess.CompletedProcess([], 0, stdout=json.dumps({"streams": [{"width": 1920, "height": 1080, "duration": "12.5"}]}), stderr="")
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "clip.mov"
            path.write_bytes(b"\x00" * 64)
            with mock.patch("duplicate_transfer_manager.sorting.metadata.shutil.which", return_value="ffprobe"), \
                    mock.patch("duplicate_transfer_manager.sorting.metadata.subprocess.run", return_value=completed), \
                    mock.patch("duplicate_transfer_manager.sorting.metadata.read_video_metadata", return_value=fake):
                metadata = MetadataExtractor().extract(path)
        self.assertEqual((metadata.width, metadata.height), (1920, 1080))
        self.assertEqual(metadata.captured, 1_700_000_000.0)
        self.assertEqual(metadata.attributes.get("camera_model"), "iPhone 15 Pro")
