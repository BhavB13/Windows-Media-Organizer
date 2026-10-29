"""Capture date and camera from HEIC and RAW files without Pillow plugins.

The fixtures are built byte by byte from the format specifications, so the
tests need no sample photos and no third-party decoder.
"""

from __future__ import annotations

import os
import struct
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

from duplicate_transfer_manager.media.container_exif import parse_tiff, read_container_metadata
from duplicate_transfer_manager.sorting.metadata import MetadataExtractor
from duplicate_transfer_manager.sorting.planner import camera_tokens

WHEN = "2023:07:14 18:30:05"
WHEN_TS = datetime(2023, 7, 14, 18, 30, 5).timestamp()


def tiff_block(*, endian="<", magic=42, make="Canon", model="Canon EOS R5", date=WHEN, size=None) -> bytes:
    """A TIFF block with Make, Model, optional size, and an EXIF IFD holding the date."""

    e = endian
    ascii_values = [(271, make), (272, model)]
    ifd0_count = len(ascii_values) + 1 + (2 if size else 0)
    ifd0_offset = 8
    exif_offset = ifd0_offset + 2 + ifd0_count * 12 + 4
    data_offset = exif_offset + 2 + 12 + 4

    payload = b""
    entries = []
    for tag, text in ascii_values:
        raw = text.encode() + b"\x00"
        entries.append(struct.pack(e + "HHII", tag, 2, len(raw), data_offset + len(payload)))
        payload += raw
    if size:
        entries.append(struct.pack(e + "HHI", 256, 4, 1) + struct.pack(e + "I", size[0]))
        entries.append(struct.pack(e + "HHI", 257, 4, 1) + struct.pack(e + "I", size[1]))
    entries.append(struct.pack(e + "HHII", 34665, 4, 1, exif_offset))
    entries.sort(key=lambda raw: struct.unpack(e + "H", raw[:2])[0])
    date_raw = date.encode() + b"\x00"
    date_at = data_offset + len(payload)
    payload += date_raw

    order = b"II" if e == "<" else b"MM"
    out = order + struct.pack(e + "HI", magic, ifd0_offset)
    out += struct.pack(e + "H", ifd0_count) + b"".join(entries) + struct.pack(e + "I", 0)
    out += struct.pack(e + "H", 1) + struct.pack(e + "HHII", 36867, 2, len(date_raw), date_at) + struct.pack(e + "I", 0)
    return out + payload


def box(kind: str, payload: bytes) -> bytes:
    return struct.pack(">I4s", 8 + len(payload), kind.encode()) + payload


def full_box(kind: str, payload: bytes, version: int = 0) -> bytes:
    return box(kind, bytes([version, 0, 0, 0]) + payload)


def heic_file(exif_tiff: bytes, *, width=4032, height=3024, offset_prefix=6) -> bytes:
    ftyp = box("ftyp", b"heic" + b"\x00\x00\x00\x00" + b"mif1heic")
    infe_image = full_box("infe", struct.pack(">HH", 1, 0) + b"hvc1" + b"\x00", version=2)
    infe_exif = full_box("infe", struct.pack(">HH", 2, 0) + b"Exif" + b"\x00", version=2)
    iinf = full_box("iinf", struct.pack(">H", 2) + infe_image + infe_exif)
    ispe_small = full_box("ispe", struct.pack(">II", 512, 384))
    ispe = full_box("ispe", struct.pack(">II", width, height))
    iprp = box("iprp", box("ipco", ispe_small + ispe))
    hdlr = full_box("hdlr", b"\x00" * 4 + b"pict" + b"\x00" * 12 + b"\x00")

    if offset_prefix == 6:
        exif_item = struct.pack(">I", 6) + b"Exif\x00\x00" + exif_tiff
    else:
        exif_item = struct.pack(">I", 0) + b"Exif\x00\x00" + exif_tiff

    def build(exif_at: int) -> bytes:
        sizes = (4 << 12) | (4 << 8)
        entries = struct.pack(">HH", 1, 0) + struct.pack(">H", 1) + struct.pack(">II", 0, 0)
        entries += struct.pack(">HH", 2, 0) + struct.pack(">H", 1) + struct.pack(">II", exif_at, len(exif_item))
        iloc = full_box("iloc", struct.pack(">HH", sizes, 2) + entries)
        return full_box("meta", hdlr + iinf + iprp + iloc)

    meta = build(0)
    exif_at = len(ftyp) + len(meta) + 8
    meta = build(exif_at)
    return ftyp + meta + box("mdat", exif_item)


def raf_file(exif_tiff: bytes) -> bytes:
    app1 = b"Exif\x00\x00" + exif_tiff
    jpeg = b"\xff\xd8" + b"\xff\xe1" + struct.pack(">H", len(app1) + 2) + app1 + b"\xff\xd9"
    header = b"FUJIFILMCCD-RAW 0201FF383501".ljust(84, b"\x00")
    jpeg_at = 128
    header += struct.pack(">II", jpeg_at, len(jpeg))
    return header.ljust(jpeg_at, b"\x00") + jpeg


class ContainerExifTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def _write(self, name: str, data: bytes) -> Path:
        path = self.root / name
        path.write_bytes(data)
        return path

    def test_dng_gives_date_and_camera_but_not_the_preview_size(self):
        path = self._write("IMG_1.dng", tiff_block(size=(256, 171)))
        meta = read_container_metadata(path)
        self.assertEqual(meta.captured, WHEN_TS)
        self.assertEqual((meta.make, meta.model), ("Canon", "Canon EOS R5"))
        self.assertEqual((meta.width, meta.height), (0, 0), "a RAW file's first IFD is usually its preview")

    def test_big_endian_tiff_reports_its_size(self):
        path = self._write("scan.tif", tiff_block(endian=">", size=(2400, 1800), make="EPSON", model="Perfection V600"))
        meta = read_container_metadata(path)
        self.assertEqual((meta.width, meta.height), (2400, 1800))
        self.assertEqual(meta.make, "EPSON")
        self.assertEqual(meta.captured, WHEN_TS)

    def test_olympus_and_panasonic_magic_numbers(self):
        for name, magic in (("P1.orf", 0x4F52), ("P2.rw2", 0x55)):
            with self.subTest(name=name):
                meta = read_container_metadata(self._write(name, tiff_block(magic=magic, make="OLYMPUS", model="E-M1")))
                self.assertEqual(meta.make, "OLYMPUS")
                self.assertEqual(meta.captured, WHEN_TS)

    def test_heic_from_an_iphone(self):
        path = self._write("IMG_0001.HEIC", heic_file(tiff_block(endian=">", make="Apple", model="iPhone 15 Pro")))
        meta = read_container_metadata(path)
        self.assertEqual(meta.captured, WHEN_TS)
        self.assertEqual((meta.make, meta.model), ("Apple", "iPhone 15 Pro"))
        self.assertEqual((meta.width, meta.height), (4032, 3024), "the largest ispe is the image")

    def test_heic_with_zero_offset_exif_item(self):
        path = self._write("b.heic", heic_file(tiff_block(), offset_prefix=0))
        self.assertEqual(read_container_metadata(path).model, "Canon EOS R5")

    def test_fujifilm_raf_through_its_embedded_jpeg(self):
        path = self._write("DSCF0001.RAF", raf_file(tiff_block(make="FUJIFILM", model="X-T5")))
        meta = read_container_metadata(path)
        self.assertEqual((meta.make, meta.model), ("FUJIFILM", "X-T5"))
        self.assertEqual(meta.captured, WHEN_TS)

    def test_malformed_input_yields_nothing_and_never_raises(self):
        cases = {
            "empty.heic": b"",
            "garbage.dng": os.urandom(4096),
            "truncated.heic": heic_file(tiff_block())[:60],
            "bad-raf.raf": b"FUJIFILMCCD-RAW".ljust(100, b"\xff"),
            "huge-count.dng": b"II*\x00\x08\x00\x00\x00" + b"\xff\xff" + b"\x00" * 20,
            "loop.cr2": b"II*\x00\x08\x00\x00\x00" + struct.pack("<H", 1) + struct.pack("<HHII", 34665, 4, 1, 8),
        }
        for name, data in cases.items():
            with self.subTest(name=name):
                meta = read_container_metadata(self._write(name, data))
                self.assertEqual(meta.captured, 0.0)

    def test_parse_tiff_ignores_unknown_byte_orders(self):
        self.assertEqual(parse_tiff(b"XX*\x00\x08\x00\x00\x00"), {})

    def test_reading_does_not_change_the_file(self):
        path = self._write("IMG.HEIC", heic_file(tiff_block()))
        before = (path.read_bytes(), path.stat().st_mtime_ns)
        read_container_metadata(path)
        self.assertEqual((path.read_bytes(), path.stat().st_mtime_ns), before)


class ExtractorIntegrationTests(unittest.TestCase):
    def test_heic_is_dated_named_and_filed_as_an_image_without_codecs(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "IMG_0042.HEIC"
            path.write_bytes(heic_file(tiff_block(endian=">", make="Apple", model="iPhone 15 Pro")))
            # As on a Windows install without the HEIF codec: no MIME type.
            with mock.patch("duplicate_transfer_manager.sorting.metadata.mimetypes.guess_type", return_value=(None, None)):
                metadata = MetadataExtractor().extract(path)
        self.assertEqual(metadata.media_type, "image")
        self.assertEqual(metadata.captured, WHEN_TS)
        self.assertEqual((metadata.width, metadata.height), (4032, 3024))
        self.assertEqual(camera_tokens(metadata)["camera"], "Apple iPhone 15 Pro")

    def test_a_raw_file_is_dated_from_exif_not_from_when_it_was_copied(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "_MG_0001.CR2"
            path.write_bytes(tiff_block())
            os.utime(path, (1_700_000_000, 1_700_000_000))
            metadata = MetadataExtractor().extract(path)
        self.assertEqual(metadata.captured, WHEN_TS)
        self.assertEqual(metadata.attributes.get("camera_model"), "Canon EOS R5")


if __name__ == "__main__":
    unittest.main()


class IndependentWriterTests(unittest.TestCase):
    """The fixtures above and the parser were written together; check the
    parser against EXIF produced by a different implementation."""

    def test_parser_reads_exif_written_by_pillow(self):
        try:
            from PIL import Image
        except Exception:  # pragma: no cover
            self.skipTest("Pillow is not installed")
        import io

        from duplicate_transfer_manager.media.container_exif import _exif_from_jpeg, _from_tiff_tags

        exif = Image.Exif()
        exif[271] = "NIKON CORPORATION"
        exif[272] = "NIKON Z 6"
        exif.get_ifd(0x8769)[36867] = WHEN
        buffer = io.BytesIO()
        Image.new("RGB", (8, 8)).save(buffer, "JPEG", exif=exif.tobytes())

        meta = _from_tiff_tags(parse_tiff(_exif_from_jpeg(buffer.getvalue())))
        self.assertEqual((meta.make, meta.model), ("NIKON CORPORATION", "NIKON Z 6"))
        self.assertEqual(meta.captured, WHEN_TS)
