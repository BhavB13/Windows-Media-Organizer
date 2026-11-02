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
RAW_EXTENSIONS = frozenset(
    (TIFF_RAW_EXTENSIONS - {".tif", ".tiff"}) | RAF_EXTENSIONS | CR3_EXTENSIONS | {".raw"}
)
DEFAULT_MAX_EMBEDDED_JPEG_BYTES = 16 * 1024 * 1024  # 16 MiB

# Upper bounds on what is read, so a hostile or corrupt file cannot make the
# reader allocate or scan without limit.
MAX_METADATA_BYTES = 4 * 1024 * 1024
MAX_IFD_ENTRIES = 1024
MAX_BOXES = 4096

TAG_MAKE = 271
TAG_MODEL = 272
TAG_ORIENTATION = 274
TAG_COMPRESSION = 259
TAG_DATETIME = 306
TAG_SUBIFDS = 330
TAG_JPEG_INTERCHANGE_FORMAT = 513
TAG_JPEG_INTERCHANGE_FORMAT_LENGTH = 514
TAG_STRIP_OFFSETS = 273
TAG_STRIP_BYTE_COUNTS = 279
TAG_TILE_OFFSETS = 324
TAG_TILE_BYTE_COUNTS = 325
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
              TAG_IMAGE_WIDTH, TAG_IMAGE_LENGTH, TAG_ORIENTATION}
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


# ----------------------------------------------------------------- Embedded JPEG Extraction


def _read_ifd_uint(endian: str, kind: int, data: bytes) -> int:
    if kind == 3:  # SHORT
        return struct.unpack(endian + "H", data[:2])[0]
    if kind in (4, 13):  # LONG or IFD
        return struct.unpack(endian + "I", data[:4])[0]
    return 0


def _iter_stream_boxes(handle, start: int, end: int):
    """Yield (kind_str, payload_start, payload_end) by seeking directly through ISO-BMFF boxes."""
    pos = start
    seen = 0
    while pos + 8 <= end and seen < MAX_BOXES:
        seen += 1
        handle.seek(pos)
        header_bytes = handle.read(8)
        if len(header_bytes) < 8:
            return
        size, kind = struct.unpack(">I4s", header_bytes)
        hdr_len = 8
        if size == 1:
            ext_bytes = handle.read(8)
            if len(ext_bytes) < 8:
                return
            size = struct.unpack(">Q", ext_bytes)[0]
            hdr_len = 16
        elif size == 0:
            size = end - pos
        if size < hdr_len or pos + size > end:
            return
        payload_start = pos + hdr_len
        payload_end = pos + size
        try:
            kind_str = kind.decode("latin-1")
        except Exception:
            return
        yield kind_str, payload_start, payload_end
        pos += size


def _extract_raf_jpeg(candidate: Path, file_size: int, max_bytes: int) -> bytes:
    with candidate.open("rb") as handle:
        header = handle.read(100)
        if not header.startswith(b"FUJIFILMCCD-RAW") or len(header) < 92:
            return b""
        jpeg_offset, jpeg_length = struct.unpack(">II", header[84:92])
        if jpeg_offset <= 0 or jpeg_length <= 0 or jpeg_length > max_bytes:
            return b""
        if jpeg_offset + jpeg_length > file_size:
            return b""
        handle.seek(jpeg_offset)
        data = handle.read(jpeg_length)
        if len(data) == jpeg_length and data.startswith(b"\xff\xd8"):
            return data
    return b""


def _extract_cr3_jpeg(candidate: Path, file_size: int, max_bytes: int) -> bytes:
    candidates: list[bytes] = []

    def check_box(handle, start: int, end: int, kind: str) -> None:
        payload_len = end - start
        if payload_len < 4:
            return
        handle.seek(start)
        head = handle.read(min(64, payload_len))
        if head.startswith(b"\xff\xd8"):
            jpeg_off = start
            jpeg_len = payload_len
        elif len(head) >= 20 and head[16:18] == b"\xff\xd8":
            if kind == "PRVW":
                _, _, _, _, _, jpeg_size = struct.unpack(">LHHHHL", head[:16])
            else:
                _, _, _, jpeg_size, _, _ = struct.unpack(">LHHLHH", head[:16])
            jpeg_off = start + 16
            jpeg_len = jpeg_size if 0 < jpeg_size <= payload_len - 16 else (payload_len - 16)
        elif b"\xff\xd8" in head:
            soi = head.find(b"\xff\xd8")
            jpeg_off = start + soi
            jpeg_len = payload_len - soi
        else:
            return

        if 0 < jpeg_len <= max_bytes and jpeg_off + jpeg_len <= file_size:
            handle.seek(jpeg_off)
            jpeg_bytes = handle.read(jpeg_len)
            if len(jpeg_bytes) == jpeg_len and jpeg_bytes.startswith(b"\xff\xd8"):
                candidates.append(jpeg_bytes)

    with candidate.open("rb") as handle:
        for kind, start, end in _iter_stream_boxes(handle, 0, file_size):
            if kind == "moov":
                for child_kind, child_start, child_end in _iter_stream_boxes(handle, start, end):
                    if child_kind == "uuid" and child_end - child_start >= 16:
                        handle.seek(child_start)
                        uuid_bytes = handle.read(16)
                        if uuid_bytes == CANON_CR3_UUID:
                            for gchild, gstart, gend in _iter_stream_boxes(handle, child_start + 16, child_end):
                                if gchild in ("PRVW", "THMB"):
                                    check_box(handle, gstart, gend, gchild)
                    elif child_kind in ("PRVW", "THMB"):
                        check_box(handle, child_start, child_end, child_kind)
            elif kind in ("PRVW", "THMB"):
                check_box(handle, start, end, kind)

    if not candidates:
        return b""
    candidates.sort(key=len, reverse=True)
    return candidates[0]


def _extract_tiff_jpeg(candidate: Path, file_size: int, max_bytes: int) -> bytes:
    with candidate.open("rb") as handle:
        header = handle.read(8)
        if len(header) < 8:
            return b""
        order = header[:2]
        if order == b"II":
            endian = "<"
        elif order == b"MM":
            endian = ">"
        else:
            return b""
        magic = struct.unpack(endian + "H", header[2:4])[0]
        if magic not in (42, 0x4F52, 0x5352, 0x55):
            return b""
        first_ifd = struct.unpack(endian + "I", header[4:8])[0]

        candidates: list[tuple[int, int]] = []
        visited_ifds: set[int] = set()

        def scan_ifd(offset: int, depth: int) -> None:
            if offset <= 0 or offset + 2 > file_size or offset in visited_ifds or len(visited_ifds) > 64:
                return
            visited_ifds.add(offset)
            handle.seek(offset)
            cnt_bytes = handle.read(2)
            if len(cnt_bytes) < 2:
                return
            count = struct.unpack(endian + "H", cnt_bytes)[0]
            if count > MAX_IFD_ENTRIES:
                return
            raw_entries = handle.read(count * 12)
            if len(raw_entries) < count * 12:
                return
            next_bytes = handle.read(4)
            next_ifd = struct.unpack(endian + "I", next_bytes)[0] if len(next_bytes) == 4 else 0

            subifd_offsets: list[int] = []
            exif_offset = 0
            jpeg_offset = 0
            jpeg_length = 0
            strip_offset = 0
            strip_length = 0
            tile_offset = 0
            tile_length = 0
            compression = 0

            for i in range(count):
                entry = raw_entries[i * 12 : (i + 1) * 12]
                tag, kind, n, val_bytes = struct.unpack(endian + "HHI4s", entry)
                if tag == TAG_JPEG_INTERCHANGE_FORMAT:
                    jpeg_offset = _read_ifd_uint(endian, kind, val_bytes)
                elif tag == TAG_JPEG_INTERCHANGE_FORMAT_LENGTH:
                    jpeg_length = _read_ifd_uint(endian, kind, val_bytes)
                elif tag == TAG_SUBIFDS:
                    if n == 1:
                        subifd_offsets.append(struct.unpack(endian + "I", val_bytes)[0])
                    elif n > 1:
                        val_offset = struct.unpack(endian + "I", val_bytes)[0]
                        item_sz = 4 if kind in (4, 13) else 2
                        if 0 < val_offset and val_offset + n * item_sz <= file_size:
                            cur_pos = handle.tell()
                            handle.seek(val_offset)
                            raw_arr = handle.read(n * item_sz)
                            handle.seek(cur_pos)
                            fmt = endian + (f"{n}I" if item_sz == 4 else f"{n}H")
                            if len(raw_arr) == n * item_sz:
                                subifd_offsets.extend(struct.unpack(fmt, raw_arr))
                elif tag == TAG_EXIF_IFD and depth < 1:
                    exif_offset = struct.unpack(endian + "I", val_bytes)[0]
                elif tag == TAG_COMPRESSION:
                    compression = _read_ifd_uint(endian, kind, val_bytes)
                elif tag == TAG_STRIP_OFFSETS:
                    strip_offset = _read_ifd_uint(endian, kind, val_bytes)
                elif tag == TAG_STRIP_BYTE_COUNTS:
                    strip_length = _read_ifd_uint(endian, kind, val_bytes)
                elif tag == TAG_TILE_OFFSETS:
                    tile_offset = _read_ifd_uint(endian, kind, val_bytes)
                elif tag == TAG_TILE_BYTE_COUNTS:
                    tile_length = _read_ifd_uint(endian, kind, val_bytes)

            if jpeg_offset > 0:
                candidates.append((jpeg_offset, jpeg_length))
            if strip_offset > 0 and (compression in (6, 7) or not compression):
                candidates.append((strip_offset, strip_length))
            if tile_offset > 0 and (compression in (6, 7) or not compression):
                candidates.append((tile_offset, tile_length))

            if depth < 1:
                for sub_off in subifd_offsets:
                    scan_ifd(sub_off, depth + 1)
                if exif_offset > 0:
                    scan_ifd(exif_offset, depth + 1)

            if next_ifd > 0 and len(visited_ifds) < 16:
                scan_ifd(next_ifd, depth)

        scan_ifd(first_ifd, 0)

        if not candidates:
            return b""

        seen_offsets: set[int] = set()
        unique_candidates: list[tuple[int, int]] = []
        for off, l in candidates:
            if off in seen_offsets or off <= 0 or off >= file_size:
                continue
            seen_offsets.add(off)
            unique_candidates.append((off, l))

        unique_candidates.sort(key=lambda c: c[1], reverse=True)

        for off, exp_len in unique_candidates:
            if exp_len > max_bytes:
                continue
            read_len = exp_len if exp_len > 0 else min(max_bytes, file_size - off)
            read_len = min(read_len, max_bytes, file_size - off)
            if read_len < 4:
                continue
            handle.seek(off)
            data = handle.read(read_len)
            if not data.startswith(b"\xff\xd8"):
                continue
            if exp_len > 0 and len(data) == exp_len:
                return data
            if exp_len == 0:
                eoi = data.find(b"\xff\xd9")
                if eoi != -1:
                    return data[: eoi + 2]
                return data

    return b""


def read_raw_orientation(path: str | Path) -> int:
    """Read EXIF orientation (1..8) from the RAW file's own metadata (IFD0 tag 274)."""
    candidate = Path(path)
    suffix = candidate.suffix.lower()
    try:
        if suffix in CR3_EXTENSIONS:
            with candidate.open("rb") as handle:
                head = handle.read(MAX_METADATA_BYTES)
            moov = _find(head, 0, len(head), "moov")
            if moov is not None:
                moov_start, moov_end = moov
                for kind, payload, box_end in _boxes(head, moov_start, moov_end):
                    if kind == "uuid" and box_end - payload >= 16 and head[payload:payload + 16] == CANON_CR3_UUID:
                        for child_kind, child_payload, child_end in _boxes(head, payload + 16, box_end):
                            if child_kind == "CMT1":
                                cmt1_tags = parse_tiff(head[child_payload:child_end])
                                orient = cmt1_tags.get(TAG_ORIENTATION)
                                if isinstance(orient, int) and 1 <= orient <= 8:
                                    return orient
            return 1

        if suffix in RAF_EXTENSIONS:
            with candidate.open("rb") as handle:
                header = handle.read(100)
                if not header.startswith(b"FUJIFILMCCD-RAW"):
                    return 1
                jpeg_offset, jpeg_length = struct.unpack(">II", header[84:92])
                handle.seek(jpeg_offset)
                jpeg = handle.read(min(jpeg_length, MAX_METADATA_BYTES))
            tags = parse_tiff(_exif_from_jpeg(jpeg))
            orient = tags.get(TAG_ORIENTATION)
            if isinstance(orient, int) and 1 <= orient <= 8:
                return orient
            return 1

        # TIFF-based RAW (CR2, NEF, NRW, ARW, SR2, SRF, PEF, ORF, RW2, DNG, etc.)
        with candidate.open("rb") as handle:
            data = handle.read(MAX_METADATA_BYTES)
        tags = parse_tiff(data)
        orient = tags.get(TAG_ORIENTATION)
        if isinstance(orient, int) and 1 <= orient <= 8:
            return orient
    except (OSError, ValueError, struct.error, IndexError, KeyError, TypeError):
        return 1
    return 1


def extract_embedded_jpeg(
    path: str | Path,
    *,
    max_bytes: int = DEFAULT_MAX_EMBEDDED_JPEG_BYTES,
) -> bytes:
    """Return the largest valid embedded JPEG from a RAW or container file, or b''.

    Reads only the headers/IFD entries and the winning JPEG bytes themselves.
    Follows SubIFD pointers one level. Never raises on corrupt or unsupported input.
    """
    if max_bytes <= 0:
        return b""

    candidate = Path(path)
    try:
        if not candidate.is_file():
            return b""
        file_size = candidate.stat().st_size
        if file_size < 16:
            return b""
    except OSError:
        return b""

    try:
        suffix = candidate.suffix.lower()
        if suffix in RAF_EXTENSIONS:
            return _extract_raf_jpeg(candidate, file_size, max_bytes)
        if suffix in CR3_EXTENSIONS:
            return _extract_cr3_jpeg(candidate, file_size, max_bytes)

        # Check file header for RAF, CR3 or TIFF before defaulting to TIFF
        with candidate.open("rb") as handle:
            magic_head = handle.read(16)
        if magic_head.startswith(b"FUJIFILMCCD-RAW"):
            return _extract_raf_jpeg(candidate, file_size, max_bytes)
        if len(magic_head) >= 8 and magic_head[4:8] == b"ftyp":
            res = _extract_cr3_jpeg(candidate, file_size, max_bytes)
            if res:
                return res

        return _extract_tiff_jpeg(candidate, file_size, max_bytes)
    except (OSError, ValueError, struct.error, IndexError, KeyError, TypeError):
        return b""

