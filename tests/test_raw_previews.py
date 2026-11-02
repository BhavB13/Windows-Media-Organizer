"""Unit tests for camera RAW embedded JPEG extraction and thumbnail generation."""

from __future__ import annotations

import io
import os
from pathlib import Path
import struct
import sys
import tempfile
import unittest
import warnings

_SOURCE_ROOT = Path(__file__).resolve().parents[1] / "src"
if str(_SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SOURCE_ROOT))

from PIL import Image

from duplicate_transfer_manager.media.container_exif import (
    CANON_CR3_UUID,
    extract_embedded_jpeg,
    read_raw_orientation,
)
from duplicate_transfer_manager.previews.thumbnails import (
    ThumbnailCache,
    build_cache_key,
)


def _make_jpeg(
    size: tuple[int, int] = (160, 120),
    color: tuple[int, int, int] = (200, 100, 50),
    *,
    split_colors: tuple[tuple[int, int, int], tuple[int, int, int]] | None = None,
) -> bytes:
    """Create a valid in-memory JPEG using Pillow."""
    image = Image.new("RGB", size, color)
    if split_colors is not None:
        top_color, bottom_color = split_colors
        half_h = size[1] // 2
        image.paste(top_color, (0, 0, size[0], half_h))
        image.paste(bottom_color, (0, half_h, size[0], size[1]))
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=90)
    return buffer.getvalue()


def _box(kind: str, payload: bytes) -> bytes:
    return struct.pack(">I4s", 8 + len(payload), kind.encode("latin-1")) + payload


def _build_tiff_raw(
    jpeg_ifd0: bytes | None = None,
    jpeg_subifd: bytes | None = None,
    *,
    orientation: int = 1,
    endian: str = "<",
    magic: int = 42,
    make: str = "Canon",
    model: str = "Canon EOS R5",
    subifd_compression: int | None = None,
    use_strip_tags: bool = False,
) -> bytes:
    """Construct a byte-built TIFF RAW fixture with IFD0 and optional SubIFD."""
    e = endian
    make_bytes = make.encode("latin-1") + b"\x00"
    model_bytes = model.encode("latin-1") + b"\x00"

    # Pre-calculate entry counts and layout
    count_ifd0 = 3  # Make, Model, Orientation
    if jpeg_ifd0 is not None:
        count_ifd0 += 2  # JPEGInterchangeFormat, JPEGInterchangeFormatLength
    if jpeg_subifd is not None:
        count_ifd0 += 1  # SubIFDs

    len_ifd0 = 2 + count_ifd0 * 12 + 4
    subifd_offset = 8 + len_ifd0
    count_subifd = (3 if use_strip_tags else 2) if jpeg_subifd is not None else 0
    len_subifd = (2 + count_subifd * 12 + 4) if jpeg_subifd is not None else 0
    data_offset = 8 + len_ifd0 + len_subifd

    make_offset = data_offset
    model_offset = make_offset + len(make_bytes)
    jpeg1_offset = model_offset + len(model_bytes)
    jpeg2_offset = jpeg1_offset + (len(jpeg_ifd0) if jpeg_ifd0 else 0)

    entries = [
        struct.pack(e + "HHII", 271, 2, len(make_bytes), make_offset),
        struct.pack(e + "HHII", 272, 2, len(model_bytes), model_offset),
        struct.pack(e + "HHII", 274, 3, 1, orientation),
    ]
    if jpeg_ifd0 is not None:
        entries.append(struct.pack(e + "HHII", 513, 4, 1, jpeg1_offset))
        entries.append(struct.pack(e + "HHII", 514, 4, 1, len(jpeg_ifd0)))
    if jpeg_subifd is not None:
        entries.append(struct.pack(e + "HHII", 330, 4, 1, subifd_offset))
    entries.sort(key=lambda b: struct.unpack(e + "H", b[:2])[0])

    order = b"II" if e == "<" else b"MM"
    out = order + struct.pack(e + "HI", magic, 8)
    out += struct.pack(e + "H", len(entries)) + b"".join(entries) + struct.pack(e + "I", 0)

    if jpeg_subifd is not None:
        if use_strip_tags:
            sub_entries = [
                struct.pack(e + "HHII", 259, 3, 1, subifd_compression or 6),
                struct.pack(e + "HHII", 273, 4, 1, jpeg2_offset),
                struct.pack(e + "HHII", 279, 4, 1, len(jpeg_subifd)),
            ]
        else:
            sub_entries = [
                struct.pack(e + "HHII", 513, 4, 1, jpeg2_offset),
                struct.pack(e + "HHII", 514, 4, 1, len(jpeg_subifd)),
            ]
        sub_entries.sort(key=lambda b: struct.unpack(e + "H", b[:2])[0])
        out += struct.pack(e + "H", len(sub_entries)) + b"".join(sub_entries) + struct.pack(e + "I", 0)

    out += make_bytes + model_bytes + (jpeg_ifd0 or b"") + (jpeg_subifd or b"")
    return out


def _build_raf(jpeg_bytes: bytes) -> bytes:
    """Build a Fujifilm RAF container with embedded JPEG at header offset 84..92."""
    header = b"FUJIFILMCCD-RAW 0201FF383501".ljust(84, b"\x00")
    jpeg_at = 128
    header += struct.pack(">II", jpeg_at, len(jpeg_bytes))
    return header.ljust(jpeg_at, b"\x00") + jpeg_bytes


def _build_cr3(
    prvw_jpeg: bytes,
    thmb_jpeg: bytes | None = None,
    *,
    with_box_header: bool = False,
    include_uuid: bool = True,
) -> bytes:
    """Build a Canon CR3 ISOBMFF container with PRVW and optional THMB."""
    ftyp = _box("ftyp", b"crx " + struct.pack(">I", 1) + b"crx " + b"isom")

    def wrap_prvw(raw: bytes) -> bytes:
        if not with_box_header:
            return _box("PRVW", raw)
        hdr = struct.pack(">LHHHHL", 0, 0, 1620, 1080, 0, len(raw))
        return _box("PRVW", hdr + raw)

    def wrap_thmb(raw: bytes) -> bytes:
        if not with_box_header:
            return _box("THMB", raw)
        hdr = struct.pack(">LHHLHH", 0, 160, 120, len(raw), 0, 0)
        return _box("THMB", hdr + raw)

    children = b""
    if thmb_jpeg is not None:
        children += wrap_thmb(thmb_jpeg)
    children += wrap_prvw(prvw_jpeg)

    if include_uuid:
        uuid_payload = CANON_CR3_UUID + children
        moov_children = _box("uuid", uuid_payload)
    else:
        moov_children = children

    moov = _box("moov", moov_children)
    return ftyp + moov


def _build_valid_ifd0_tiff(
    w: int = 32,
    h: int = 24,
    jpeg_bytes: bytes | None = None,
) -> bytes:
    """Build a TIFF file where IFD0 is a valid w x h RGB raster, plus optional tag 513/514 JPEG."""
    endian = "<"
    raw_pixels = b"\x80\x80\x80" * (w * h)
    entries = [
        struct.pack(endian + "HHII", 256, 4, 1, w),
        struct.pack(endian + "HHII", 257, 4, 1, h),
        struct.pack(endian + "HHII", 259, 3, 1, 1),
        struct.pack(endian + "HHII", 262, 3, 1, 2),
        struct.pack(endian + "HHII", 277, 3, 1, 3),
        struct.pack(endian + "HHII", 278, 4, 1, h),
        struct.pack(endian + "HHII", 279, 4, 1, len(raw_pixels)),
    ]
    count = len(entries) + 2 + (2 if jpeg_bytes else 0)
    ifd0_len = 2 + count * 12 + 4
    data_off = 8 + ifd0_len
    bps_off = data_off
    bps_bytes = struct.pack(endian + "HHH", 8, 8, 8)
    strip_off = bps_off + len(bps_bytes)
    jpeg_off = strip_off + len(raw_pixels)
    entries.append(struct.pack(endian + "HHII", 258, 3, 3, bps_off))
    entries.append(struct.pack(endian + "HHII", 273, 4, 1, strip_off))
    if jpeg_bytes:
        entries.append(struct.pack(endian + "HHII", 513, 4, 1, jpeg_off))
        entries.append(struct.pack(endian + "HHII", 514, 4, 1, len(jpeg_bytes)))
    entries.sort(key=lambda b: struct.unpack(endian + "H", b[:2])[0])
    out = b"II*\x00\x08\x00\x00\x00" + struct.pack(endian + "H", len(entries)) + b"".join(entries) + struct.pack(endian + "I", 0)
    out += bps_bytes + raw_pixels + (jpeg_bytes or b"")
    return out


class RawPreviewsTests(unittest.TestCase):
    """Test embedded JPEG extraction and thumbnail generation for camera RAW formats."""

    def setUp(self) -> None:
        self._temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self._temp_dir.name)
        self.sources_dir = self.root / "sources"
        self.cache_dir = self.root / "cache" / "thumbnails"
        self.sources_dir.mkdir(parents=True, exist_ok=True)
        self.cache = ThumbnailCache(cache_dir=self.cache_dir, default_size=(128, 128))

    def tearDown(self) -> None:
        self._temp_dir.cleanup()

    def _write_file(self, name: str, data: bytes) -> Path:
        target = self.sources_dir / name
        target.write_bytes(data)
        return target

    # ------------------------------------------------ Embedded JPEG Extraction

    def test_extract_tiff_raw_returns_valid_jpeg(self) -> None:
        expected_jpeg = _make_jpeg(size=(120, 90), color=(10, 80, 200))
        raw_data = _build_tiff_raw(jpeg_ifd0=expected_jpeg)
        path = self._write_file("photo.cr2", raw_data)

        extracted = extract_embedded_jpeg(path)
        self.assertEqual(extracted, expected_jpeg)
        self.assertTrue(extracted.startswith(b"\xff\xd8"))

    def test_extract_largest_wins_subifd_over_ifd0(self) -> None:
        small_jpeg = _make_jpeg(size=(40, 30), color=(50, 50, 50))
        large_jpeg = _make_jpeg(size=(320, 240), color=(255, 100, 0))
        self.assertGreater(len(large_jpeg), len(small_jpeg))

        raw_data = _build_tiff_raw(jpeg_ifd0=small_jpeg, jpeg_subifd=large_jpeg)
        path = self._write_file("sample.dng", raw_data)

        extracted = extract_embedded_jpeg(path)
        self.assertEqual(extracted, large_jpeg)

    def test_extract_subifd_with_compression_strip_tags(self) -> None:
        small_jpeg = _make_jpeg(size=(50, 40), color=(10, 10, 10))
        large_jpeg = _make_jpeg(size=(400, 300), color=(0, 200, 100))

        raw_data = _build_tiff_raw(
            jpeg_ifd0=small_jpeg,
            jpeg_subifd=large_jpeg,
            use_strip_tags=True,
            subifd_compression=6,
        )
        path = self._write_file("strip.nef", raw_data)

        extracted = extract_embedded_jpeg(path)
        self.assertEqual(extracted, large_jpeg)

    def test_extract_raf_header_offset(self) -> None:
        expected_jpeg = _make_jpeg(size=(200, 150), color=(30, 140, 90))
        raf_data = _build_raf(expected_jpeg)
        path = self._write_file("fuji.raf", raf_data)

        extracted = extract_embedded_jpeg(path)
        self.assertEqual(extracted, expected_jpeg)

    def test_extract_cr3_prvw_inside_uuid(self) -> None:
        thmb = _make_jpeg(size=(60, 45), color=(20, 20, 20))
        prvw = _make_jpeg(size=(500, 375), color=(180, 40, 40))
        self.assertGreater(len(prvw), len(thmb))

        cr3_data = _build_cr3(prvw, thmb_jpeg=thmb, with_box_header=True)
        path = self._write_file("canon.cr3", cr3_data)

        extracted = extract_embedded_jpeg(path)
        self.assertEqual(extracted, prvw)

    def test_extract_cr3_raw_box_payload(self) -> None:
        prvw = _make_jpeg(size=(300, 200), color=(80, 120, 220))
        cr3_data = _build_cr3(prvw, with_box_header=False)
        path = self._write_file("canon_raw.cr3", cr3_data)

        extracted = extract_embedded_jpeg(path)
        self.assertEqual(extracted, prvw)

    def test_extract_caps_at_max_bytes(self) -> None:
        jpeg = _make_jpeg(size=(300, 200))
        raw_data = _build_tiff_raw(jpeg_ifd0=jpeg)
        path = self._write_file("capped.arw", raw_data)

        extracted = extract_embedded_jpeg(path, max_bytes=len(jpeg) - 10)
        self.assertEqual(extracted, b"")

        extracted_ok = extract_embedded_jpeg(path, max_bytes=len(jpeg) + 100)
        self.assertEqual(extracted_ok, jpeg)

    def test_extract_malformed_returns_empty_and_never_raises(self) -> None:
        cases = {
            "empty.cr2": b"",
            "garbage.nef": os.urandom(256),
            "truncated_hdr.dng": b"II*\x00\x08",
            "truncated_raf.raf": b"FUJIFILMCCD-RAW 0201",
            "truncated_cr3.cr3": _box("ftyp", b"crx ")[:10],
            "bad_magic.rw2": b"II\x99\x99\x08\x00\x00\x00",
            "ifd_oob.orf": b"II*\x00\xff\xff\x00\x00",
            "ifd_huge_count.pef": b"II*\x00\x08\x00\x00\x00" + struct.pack("<H", 5000),
        }
        for name, data in cases.items():
            with self.subTest(case=name):
                path = self._write_file(name, data)
                res = extract_embedded_jpeg(path)
                self.assertEqual(res, b"")

        # Non-existent path returns empty without raising
        self.assertEqual(extract_embedded_jpeg(self.root / "does_not_exist.cr2"), b"")

    # ------------------------------------------------ Orientation & Thumbnails

    def test_orientation_6_on_raw_produces_portrait_thumbnail(self) -> None:
        # Build an upright portrait image (100x200): top half RED, bottom half BLUE.
        upright = Image.new("RGB", (100, 200), (0, 0, 255))
        upright.paste((255, 0, 0), (0, 0, 100, 100))

        # Camera sensor stores pixels sideways (200x100, rotated 90 CCW) with EXIF Orientation=6 (Rotate 90 CW).
        sideways_raw = upright.transpose(Image.Transpose.ROTATE_90)
        self.assertEqual(sideways_raw.size, (200, 100))

        buffer = io.BytesIO()
        sideways_raw.save(buffer, format="JPEG", quality=95)
        sideways_jpeg = buffer.getvalue()

        # Set RAW file's IFD0 tag 274 (Orientation) to 6 (Rotate 90 CW).
        raw_data = _build_tiff_raw(jpeg_ifd0=sideways_jpeg, orientation=6)
        path = self._write_file("portrait_test.cr2", raw_data)

        self.assertEqual(read_raw_orientation(path), 6)

        result = self.cache.get_or_create(path, size=(100, 100))
        self.assertTrue(result.ok)
        self.assertEqual(result.exif_orientation, 6)
        # Orientation 6 rotates 200x100 to 100x200; scaling to 100x100 box yields 50x100 (portrait).
        self.assertEqual(result.size, (50, 100))
        self.assertIsNotNone(result.thumbnail_path)
        assert result.thumbnail_path is not None

        with Image.open(result.thumbnail_path) as thumb:
            self.assertEqual(thumb.size, (50, 100))
            top_pixel = thumb.getpixel((25, 10))
            bottom_pixel = thumb.getpixel((25, 90))
            # After 90 CW rotation: top must be predominantly red; bottom must be predominantly blue.
            self.assertGreater(top_pixel[0], 200)
            self.assertLess(top_pixel[2], 50)
            self.assertGreater(bottom_pixel[2], 200)
            self.assertLess(bottom_pixel[0], 50)

    def test_thumbnail_cache_hit_on_second_request(self) -> None:
        jpeg = _make_jpeg(size=(300, 200), color=(70, 130, 180))
        path = self._write_file("cached_sample.nef", _build_tiff_raw(jpeg_ifd0=jpeg))

        first = self.cache.get_or_create(path, size=(128, 128))
        self.assertTrue(first.ok)
        self.assertFalse(first.cache_hit)
        self.assertTrue(first.generated)
        self.assertIsNotNone(first.thumbnail_path)

        second = self.cache.get_or_create(path, size=(128, 128))
        self.assertTrue(second.ok)
        self.assertTrue(second.cache_hit)
        self.assertFalse(second.generated)
        self.assertEqual(second.thumbnail_path, first.thumbnail_path)
        self.assertEqual(second.cache_key, first.cache_key)

        # Fresh instance (cold in-memory cache) must also hit on-disk cache
        cold_cache = ThumbnailCache(cache_dir=self.cache_dir, default_size=(128, 128))
        third = cold_cache.get_or_create(path, size=(128, 128))
        self.assertTrue(third.ok)
        self.assertTrue(third.cache_hit)
        self.assertFalse(third.generated)
        self.assertEqual(third.thumbnail_path, first.thumbnail_path)

    def test_raw_bytes_and_mtime_unchanged_after_generation(self) -> None:
        jpeg = _make_jpeg(size=(240, 180), color=(120, 60, 200))
        path = self._write_file("sacred_user_file.arw", _build_tiff_raw(jpeg_ifd0=jpeg, orientation=3))

        bytes_before = path.read_bytes()
        mtime_before = path.stat().st_mtime_ns

        result = self.cache.get_or_create(path, size=(128, 128))
        self.assertTrue(result.ok)

        self.assertEqual(path.read_bytes(), bytes_before)
        self.assertEqual(path.stat().st_mtime_ns, mtime_before)

    def test_modifying_raw_file_invalidates_cache_entry(self) -> None:
        jpeg1 = _make_jpeg(size=(200, 150), color=(10, 20, 30))
        path = self._write_file("editable.dng", _build_tiff_raw(jpeg_ifd0=jpeg1))

        initial = self.cache.get_or_create(path, size=(128, 128))
        self.assertTrue(initial.ok)

        # Modify mtime -> cache invalidated
        stat_before = path.stat()
        os.utime(path, ns=(stat_before.st_atime_ns, stat_before.st_mtime_ns + 10_000_000_000))

        after_mtime = self.cache.get_or_create(path, size=(128, 128))
        self.assertTrue(after_mtime.ok)
        self.assertFalse(after_mtime.cache_hit)
        self.assertTrue(after_mtime.generated)
        self.assertNotEqual(after_mtime.cache_key, initial.cache_key)

    def test_malformed_raw_reports_error_and_never_raises(self) -> None:
        path = self._write_file("corrupt.cr2", b"II*\x00\x08\x00\x00\x00garbage_data_here")

        with warnings.catch_warnings():
            warnings.simplefilter("ignore", UserWarning)
            result = self.cache.get_or_create(path, size=(128, 128))
        self.assertFalse(result.ok)
        self.assertIsNotNone(result.error)
        self.assertIsNone(result.thumbnail_path)
        self.assertEqual(result.status, "error")

    def test_thumbnail_tiny_ifd0_replaced_by_embedded_jpeg(self) -> None:
        large_jpeg = _make_jpeg(size=(200, 150), color=(100, 200, 50))
        raw_data = _build_valid_ifd0_tiff(w=32, h=24, jpeg_bytes=large_jpeg)
        path = self._write_file("tiny_ifd0.dng", raw_data)

        # IFD0 is 32x24, but requested size is 100x100.
        # The larger 200x150 embedded JPEG should be decoded instead of the tiny IFD0.
        result = self.cache.get_or_create(path, size=(100, 100))
        self.assertTrue(result.ok)
        self.assertEqual(result.source_dimensions, (200, 150))
        self.assertEqual(result.size, (100, 75))

    def test_raf_and_cr3_thumbnail_generation(self) -> None:
        raf_jpeg = _make_jpeg(size=(320, 240), color=(40, 180, 90))
        raf_path = self._write_file("camera.raf", _build_raf(raf_jpeg))

        raf_res = self.cache.get_or_create(raf_path, size=(100, 100))
        self.assertTrue(raf_res.ok)
        self.assertEqual(raf_res.format, "JPEG")
        self.assertIsNotNone(raf_res.thumbnail_path)

        cr3_jpeg = _make_jpeg(size=(400, 250), color=(220, 60, 40))
        cr3_path = self._write_file("camera.cr3", _build_cr3(cr3_jpeg, with_box_header=True))

        cr3_res = self.cache.get_or_create(cr3_path, size=(100, 100))
        self.assertTrue(cr3_res.ok)
        self.assertEqual(cr3_res.format, "JPEG")
        self.assertIsNotNone(cr3_res.thumbnail_path)


if __name__ == "__main__":
    unittest.main()
