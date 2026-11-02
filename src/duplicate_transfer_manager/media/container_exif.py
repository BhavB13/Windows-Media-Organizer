"""Capture date and camera from HEIC/HEIF and camera RAW files, stdlib only.

Pillow cannot open HEIC or most RAW formats without plugins this app does not
ship, so for those files the sorter had no capture date - an iPhone photo was
filed by the day it was copied - and no camera. The metadata needed is only a
few fields of EXIF, and every format here stores ordinary TIFF-structured EXIF:

* TIFF-based RAW (DNG, CR2, NEF, NRW, ARW, SR2, SRF, PEF, ORF, RW2, 3FR, ERF,
  KDC, MOS, IIQ) begin with a TIFF header; Olympus and Panasonic use their own
  magic numbers on the same structure.
* HEIF, HEIC and AVIF keep an "Exif" item inside the ISO-BMFF ``meta`` box,
  located through ``iinf`` and ``iloc``; image size is in the ``ispe`` property.
* Fujifilm RAF embeds a JPEG whose APP1 segment carries the EXIF.
* Canon CR3 is ISO-BMFF and stores EXIF in CMT1 and CMT2 boxes inside a Canon
  ``uuid`` box in ``moov``.

Everything is read-only and bounded: only the headers and the EXIF block are
read, never the image data, and malformed input yields an empty result rather
than an exception.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

TIFF_RAW_EXTENSIONS = frozenset(
    {".tif", ".tiff", ".dng", ".cr2", ".nef", ".nrw", ".arw", ".sr2", ".srf", ".pef",
     ".orf", ".rw2", ".3fr", ".erf", ".kdc", ".mos", ".iiq"}
)
HEIF_EXTENSIONS = frozenset({".heic", ".heif", ".hif", ".avif"})
RAF_EXTENSIONS = frozenset({".raf"})
CR3_EXTENSIONS = frozenset({".cr3"})
SUPPORTED_EXTENSIONS = TIFF_RAW_EXTENSIONS | HEIF_EXTENSIONS | RAF_EXTENSIONS | CR3_EXTENSIONS

# Upper bounds on what is read, so a hostile or corrupt file cannot make the
# reader allocate or scan without limit.
MAX_METADATA_BYTES = 4 * 1024 * 1024
MAX_IFD_ENTRIES = 1024
MAX_BOXES = 4096

TAG_MAKE = 271
TAG_MODEL = 272
TAG_DATETIME = 306
TAG_EXIF_IFD = 34665
TAG_DATETIME_ORIGINAL = 36867
TAG_DATETIME_DIGITIZED = 36868
TAG_IMAGE_WIDTH = 256
TAG_IMAGE_LENGTH = 257

CANON_CR3_UUID = bytes.fromhex("85c0b687820f11e08111f4ce462b6a48")


@dataclass(frozen=True)
class ContainerMetadata:
    captured: float = 0.0
    make: str = ""
    model: str = ""
    width: int = 0
    height: int = 0

    @property
    def empty(self) -> bool:
        return not (self.captured or self.make or self.model or self.width or self.height)


# ----------------------------------------------------------------- TIFF / EXIF


def _parse_exif_datetime(value: str) -> float:
    text = value.replace("\x00", "").strip()
    try:
        # Zoneless wall clock, interpreted as local time to match the rest of
        # the sorter (see sorting/metadata.py).
        return datetime.strptime(text[:19], "%Y:%m:%d %H:%M:%S").timestamp()
    except ValueError:
        return 0.0


def parse_tiff(data: bytes) -> dict[int, object]:
    """Return the IFD0 and EXIF sub-IFD tags this module uses from a TIFF block."""

    if len(data) < 8:
        return {}
    order = data[:2]
    if order == b"II":
        endian = "<"
    elif order == b"MM":
        endian = ">"
    else:
        return {}
    magic = struct.unpack(endian + "H", data[2:4])[0]
    # 42 is TIFF; Olympus ORF uses 0x4F52/0x5352 and Panasonic RW2 uses 0x55.
    if magic not in (42, 0x4F52, 0x5352, 0x55):
        return {}
    first_ifd = struct.unpack(endian + "I", data[4:8])[0]
    wanted = {TAG_MAKE, TAG_MODEL, TAG_DATETIME, TAG_DATETIME_ORIGINAL, TAG_DATETIME_DIGITIZED,
              TAG_IMAGE_WIDTH, TAG_IMAGE_LENGTH}
    tags: dict[int, object] = {}

    def read_ifd(offset: int, depth: int) -> None:
        if depth > 2 or offset <= 0 or offset + 2 > len(data):
            return
        count = struct.unpack(endian + "H", data[offset:offset + 2])[0]
        if count > MAX_IFD_ENTRIES:
            return
        for index in range(count):
            entry = offset + 2 + index * 12
            if entry + 12 > len(data):
                return
            tag, kind, n, value = struct.unpack(endian + "HHI4s", data[entry:entry + 12])
            if tag == TAG_EXIF_IFD:
                read_ifd(struct.unpack(endian + "I", value)[0], depth + 1)
                continue
            if tag not in wanted or tag in tags:
                continue
            if kind == 2:  # ASCII
                if n <= 4:
                    raw = value[:n]
                else:
                    start = struct.unpack(endian + "I", value)[0]
                    if start + n > len(data):
                        continue
                    raw = data[start:start + n]
                tags[tag] = raw.split(b"\x00", 1)[0].decode("latin-1", errors="replace").strip()
            elif kind == 3 and n >= 1:  # SHORT
                tags[tag] = struct.unpack(endian + "H", value[:2])[0]
            elif kind == 4 and n >= 1:  # LONG
                tags[tag] = struct.unpack(endian + "I", value)[0]

    read_ifd(first_ifd, 0)
    return tags


def _from_tiff_tags(tags: dict[int, object], *, width: int = 0, height: int = 0) -> ContainerMetadata:
    captured = 0.0
    for tag in (TAG_DATETIME_ORIGINAL, TAG_DATETIME_DIGITIZED, TAG_DATETIME):
        if isinstance(tags.get(tag), str):
            captured = _parse_exif_datetime(tags[tag])
            if captured:
                break
    if not width and isinstance(tags.get(TAG_IMAGE_WIDTH), int):
        width = int(tags[TAG_IMAGE_WIDTH])
    if not height and isinstance(tags.get(TAG_IMAGE_LENGTH), int):
        height = int(tags[TAG_IMAGE_LENGTH])
    return ContainerMetadata(
        captured=captured,
        make=str(tags.get(TAG_MAKE, "") or ""),
        model=str(tags.get(TAG_MODEL, "") or ""),
        width=width,
        height=height,
    )


def _exif_from_jpeg(data: bytes) -> bytes:
    """The TIFF block inside a JPEG's APP1 "Exif" segment, or b""."""

    if data[:2] != b"\xff\xd8":
        return b""
    position = 2
    while position + 4 <= len(data):
        if data[position] != 0xFF:
            return b""
        marker = data[position + 1]
        if marker in (0xD9, 0xDA):  # end of image / start of scan: no more metadata
            return b""
        length = struct.unpack(">H", data[position + 2:position + 4])[0]
        segment = data[position + 4:position + 2 + length]
        if marker == 0xE1 and segment.startswith(b"Exif\x00\x00"):
            return segment[6:]
        position += 2 + length
    return b""


# ----------------------------------------------------------------- ISO-BMFF


def _boxes(data: bytes, start: int, end: int):
    """Yield (type, payload_start, payload_end) for boxes in data[start:end]."""

    position = start
    seen = 0
    while position + 8 <= end and seen < MAX_BOXES:
        seen += 1
        size, kind = struct.unpack(">I4s", data[position:position + 8])
        header = 8
        if size == 1:
            if position + 16 > end:
                return
            size = struct.unpack(">Q", data[position + 8:position + 16])[0]
            header = 16
        elif size == 0:
            size = end - position
        if size < header or position + size > end:
            return
        yield kind.decode("latin-1"), position + header, position + size
        position += size


def _find(data: bytes, start: int, end: int, kind: str):
    for box_kind, payload, box_end in _boxes(data, start, end):
        if box_kind == kind:
            return payload, box_end
    return None


def _heif_exif_and_size(path: Path) -> tuple[bytes, int, int]:
    with path.open("rb") as handle:
        head = handle.read(MAX_METADATA_BYTES)
        meta = _find(head, 0, len(head), "meta")
        if meta is None:
            return b"", 0, 0
        meta_start, meta_end = meta[0] + 4, meta[1]  # meta is a FullBox

        exif_ids: list[int] = []
        iinf = _find(head, meta_start, meta_end, "iinf")
        if iinf is not None:
            version = head[iinf[0]]
            start = iinf[0] + 4 + (4 if version else 2)
            for kind, payload, box_end in _boxes(head, start, iinf[1]):
                if kind != "infe" or payload + 4 > box_end:
                    continue
                infe_version = head[payload]
                if infe_version < 2:
                    continue
                cursor = payload + 4
                id_size = 4 if infe_version >= 3 else 2
                if cursor + id_size + 2 + 4 > box_end:
                    continue
                item_id = int.from_bytes(head[cursor:cursor + id_size], "big")
                item_type = head[cursor + id_size + 2:cursor + id_size + 6]
                if item_type == b"Exif":
                    exif_ids.append(item_id)

        width = height = 0
        iprp = _find(head, meta_start, meta_end, "iprp")
        if iprp is not None:
            ipco = _find(head, iprp[0], iprp[1], "ipco")
            if ipco is not None:
                for kind, payload, box_end in _boxes(head, ipco[0], ipco[1]):
                    if kind == "ispe" and payload + 12 <= box_end:
                        w, h = struct.unpack(">II", head[payload + 4:payload + 12])
                        if w * h > width * height:
                            width, height = w, h

        iloc = _find(head, meta_start, meta_end, "iloc")
        if not exif_ids or iloc is None:
            return b"", width, height
        exif = _read_iloc_item(handle, head, iloc[0], iloc[1], exif_ids[0])
        return exif, width, height


def _read_iloc_item(handle, head: bytes, start: int, end: int, wanted: int) -> bytes:
    version = head[start]
    cursor = start + 4
    if cursor + 2 > end:
        return b""
    sizes = struct.unpack(">H", head[cursor:cursor + 2])[0]
    offset_size, length_size = sizes >> 12, (sizes >> 8) & 0xF
    base_size, index_size = (sizes >> 4) & 0xF, sizes & 0xF
    cursor += 2
    id_size = 4 if version == 2 else 2
    count_size = 4 if version == 2 else 2
    item_count = int.from_bytes(head[cursor:cursor + count_size], "big")
    cursor += count_size

    def take(size: int) -> int:
        nonlocal cursor
        value = int.from_bytes(head[cursor:cursor + size], "big") if size else 0
        cursor += size
        return value

    for _ in range(min(item_count, MAX_BOXES)):
        if cursor > end:
            return b""
        item_id = take(id_size)
        if version in (1, 2):
            take(2)  # construction method
        take(2)  # data reference index
        base = take(base_size)
        extents = take(2)
        pieces = []
        for _extent in range(min(extents, 64)):
            if version in (1, 2) and index_size:
                take(index_size)
            offset = take(offset_size)
            length = take(length_size)
            pieces.append((base + offset, length))
        if item_id != wanted:
            continue
        data = b""
        for offset, length in pieces:
            if length <= 0 or length > MAX_METADATA_BYTES or len(data) + length > MAX_METADATA_BYTES:
                return b""
            handle.seek(offset)
            data += handle.read(length)
        if len(data) < 4:
            return b""
        # The item starts with a 4-byte offset to the TIFF header.
        skip = struct.unpack(">I", data[:4])[0]
        block = data[4 + skip:] if 4 + skip < len(data) else b""
        return block[6:] if block.startswith(b"Exif\x00\x00") else block
    return b""


def _cr3_metadata(path: Path) -> ContainerMetadata:
    try:
        with path.open("rb") as handle:
            head = handle.read(MAX_METADATA_BYTES)
        moov = _find(head, 0, len(head), "moov")
        if moov is None:
            return ContainerMetadata()
        moov_start, moov_end = moov
        cmt1_tags: dict[int, object] = {}
        cmt2_tags: dict[int, object] = {}
        for kind, payload, box_end in _boxes(head, moov_start, moov_end):
            if kind == "uuid" and box_end - payload >= 16 and head[payload:payload + 16] == CANON_CR3_UUID:
                for child_kind, child_payload, child_end in _boxes(head, payload + 16, box_end):
                    if child_kind == "CMT1":
                        cmt1_tags = parse_tiff(head[child_payload:child_end])
                    elif child_kind == "CMT2":
                        cmt2_tags = parse_tiff(head[child_payload:child_end])
                if cmt1_tags and cmt2_tags:
                    break
        captured = 0.0
        for tag_val in (
            cmt2_tags.get(TAG_DATETIME_ORIGINAL),
            cmt2_tags.get(TAG_DATETIME_DIGITIZED),
            cmt1_tags.get(TAG_DATETIME),
        ):
            if isinstance(tag_val, str):
                captured = _parse_exif_datetime(tag_val)
                if captured:
                    break
        make = str(cmt1_tags.get(TAG_MAKE, "") or "").strip()
        model = str(cmt1_tags.get(TAG_MODEL, "") or "").strip()
        return ContainerMetadata(captured=captured, make=make, model=model)
    except (OSError, ValueError, struct.error, IndexError, KeyError, TypeError):
        return ContainerMetadata()


# ----------------------------------------------------------------- entry point


def read_container_metadata(path: str | Path) -> ContainerMetadata:
    """Capture date, camera and size for a HEIF, CR3 or RAW file, or an empty result."""

    candidate = Path(path)
    suffix = candidate.suffix.lower()
    try:
        if suffix in HEIF_EXTENSIONS:
            exif, width, height = _heif_exif_and_size(candidate)
            return _from_tiff_tags(parse_tiff(exif), width=width, height=height)
        if suffix in RAF_EXTENSIONS:
            with candidate.open("rb") as handle:
                header = handle.read(100)
                if not header.startswith(b"FUJIFILMCCD-RAW"):
                    return ContainerMetadata()
                jpeg_offset, jpeg_length = struct.unpack(">II", header[84:92])
                handle.seek(jpeg_offset)
                jpeg = handle.read(min(jpeg_length, MAX_METADATA_BYTES))
            return _from_tiff_tags(parse_tiff(_exif_from_jpeg(jpeg)))
        if suffix in CR3_EXTENSIONS:
            return _cr3_metadata(candidate)
        if suffix in TIFF_RAW_EXTENSIONS:
            with candidate.open("rb") as handle:
                data = handle.read(MAX_METADATA_BYTES)
            result = _from_tiff_tags(parse_tiff(data))
            if suffix not in (".tif", ".tiff"):
                # A RAW file's first IFD usually describes its embedded preview,
                # not the sensor image, so its size would understate the photo.
                result = ContainerMetadata(captured=result.captured, make=result.make, model=result.model)
            return result
    except (OSError, ValueError, struct.error, IndexError):
        return ContainerMetadata()
    return ContainerMetadata()
