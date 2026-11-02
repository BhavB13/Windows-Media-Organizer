"""Video metadata extraction (capture date, size, duration, camera) from ISO-BMFF files."""

from __future__ import annotations

import struct
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

# Bounded reads to prevent high memory or infinite scanning on corrupt/hostile files.
MAX_METADATA_BYTES = 8 * 1024 * 1024  # 8 MiB
MAX_BOXES = 4096
MP4_EPOCH_OFFSET = 2082844800  # Seconds between 1904-01-01 and 1970-01-01 UTC
MIN_VALID_EPOCH = 631152000.0   # 1990-01-01 00:00:00 UTC


@dataclass(frozen=True)
class VideoMetadata:
    captured: float = 0.0
    width: int = 0
    height: int = 0
    duration: float = 0.0
    make: str = ""
    model: str = ""

    @property
    def empty(self) -> bool:
        return not (self.captured or self.width or self.height or self.duration or self.make or self.model)


def _boxes(data: bytes, start: int = 0, end: int | None = None):
    """Yield (kind_bytes, payload_start, payload_end) for boxes in data[start:end]."""
    if end is None:
        end = len(data)
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
        yield kind, position + header, position + size
        position += size


def _parse_iso_datetime(value: str) -> float:
    text = value.replace("\x00", "").strip()
    if not text:
        return 0.0
    try:
        dt = datetime.fromisoformat(text)
        epoch = dt.timestamp()
        return epoch if epoch >= MIN_VALID_EPOCH else 0.0
    except (ValueError, TypeError, OverflowError):
        return 0.0


def _parse_mvhd(payload: bytes) -> tuple[float, float]:
    if len(payload) < 20:
        return 0.0, 0.0
    version = payload[0]
    if version == 0:
        creation_raw, _mod_raw, timescale, duration_raw = struct.unpack(">IIII", payload[4:20])
    elif version == 1:
        if len(payload) < 32:
            return 0.0, 0.0
        creation_raw, _mod_raw, timescale, duration_raw = struct.unpack(">QQIQ", payload[4:32])
    else:
        return 0.0, 0.0

    duration = float(duration_raw / timescale) if timescale > 0 else 0.0
    if creation_raw <= 0:
        captured = 0.0
    else:
        epoch = float(creation_raw - MP4_EPOCH_OFFSET)
        captured = epoch if epoch >= MIN_VALID_EPOCH else 0.0
    return captured, duration


def _parse_tkhd(payload: bytes) -> tuple[int, int, bool]:
    if len(payload) < 4:
        return 0, 0, False
    version = payload[0]
    if version == 0:
        if len(payload) < 84:
            return 0, 0, False
        matrix = struct.unpack(">9i", payload[40:76])
        raw_width, raw_height = struct.unpack(">II", payload[76:84])
    elif version == 1:
        if len(payload) < 96:
            return 0, 0, False
        matrix = struct.unpack(">9i", payload[52:88])
        raw_width, raw_height = struct.unpack(">II", payload[88:96])
    else:
        return 0, 0, False

    a, b, _u, c, d, _v, _x, _y, _w = matrix
    is_rotated = (a == 0 and d == 0 and (b != 0 or c != 0)) or (abs(b) > abs(a) and abs(c) > abs(d))
    w = raw_width >> 16
    h = raw_height >> 16
    if w == 0 and h == 0 and (raw_width > 0 or raw_height > 0):
        w = raw_width
        h = raw_height
    return w, h, is_rotated


def _parse_trak(payload: bytes) -> tuple[bool, int, int]:
    is_video = False
    track_w = 0
    track_h = 0
    is_rotated = False

    for kind, start, end in _boxes(payload, 0, len(payload)):
        if kind == b"tkhd":
            track_w, track_h, is_rotated = _parse_tkhd(payload[start:end])
        elif kind == b"mdia":
            mdia_payload = payload[start:end]
            for m_kind, m_start, m_end in _boxes(mdia_payload, 0, len(mdia_payload)):
                if m_kind == b"hdlr":
                    hdlr_payload = mdia_payload[m_start:m_end]
                    if len(hdlr_payload) >= 12:
                        handler_type = hdlr_payload[8:12]
                        if handler_type == b"vide":
                            is_video = True
    if is_video:
        if is_rotated:
            return True, track_h, track_w
        return True, track_w, track_h
    return False, 0, 0


def _parse_meta(payload: bytes) -> tuple[float, str, str]:
    sub_start = 4 if len(payload) >= 4 else 0
    found_known = False
    if len(payload) >= 8:
        for k, _, _ in _boxes(payload, 4, len(payload)):
            if k in (b"hdlr", b"keys", b"ilst"):
                found_known = True
                break
    if not found_known:
        for k, _, _ in _boxes(payload, 0, len(payload)):
            if k in (b"hdlr", b"keys", b"ilst"):
                sub_start = 0
                break

    keys_payload: bytes | None = None
    ilst_payload: bytes | None = None
    for kind, start, end in _boxes(payload, sub_start, len(payload)):
        if kind == b"keys":
            keys_payload = payload[start:end]
        elif kind == b"ilst":
            ilst_payload = payload[start:end]

    if not keys_payload or not ilst_payload or len(keys_payload) < 8:
        return 0.0, "", ""

    entry_count = struct.unpack(">I", keys_payload[4:8])[0]
    cursor = 8
    key_map: dict[int, str] = {}
    for idx in range(1, min(entry_count + 1, MAX_BOXES)):
        if cursor + 8 > len(keys_payload):
            break
        key_size, _namespace = struct.unpack(">I4s", keys_payload[cursor:cursor + 8])
        if key_size < 8 or cursor + key_size > len(keys_payload):
            break
        key_name = keys_payload[cursor + 8:cursor + key_size].decode("utf-8", errors="replace")
        key_map[idx] = key_name
        cursor += key_size

    captured = 0.0
    make = ""
    model = ""

    for item_kind, item_start, item_end in _boxes(ilst_payload, 0, len(ilst_payload)):
        if len(item_kind) != 4:
            continue
        key_idx = struct.unpack(">I", item_kind)[0]
        key_name = key_map.get(key_idx)
        if not key_name:
            continue

        item_data = ilst_payload[item_start:item_end]
        raw_val = b""
        for sub_kind, s_start, s_end in _boxes(item_data, 0, len(item_data)):
            if sub_kind == b"data":
                data_box = item_data[s_start:s_end]
                if len(data_box) >= 8:
                    raw_val = data_box[8:]
                break
        if not raw_val:
            raw_val = item_data

        val_str = raw_val.decode("utf-8", errors="replace").replace("\x00", "").strip()
        if key_name == "com.apple.quicktime.creationdate":
            parsed = _parse_iso_datetime(val_str)
            if parsed > 0:
                captured = parsed
        elif key_name == "com.apple.quicktime.make":
            make = val_str
        elif key_name == "com.apple.quicktime.model":
            model = val_str

    return captured, make, model


def _parse_video(handle, file_size: int) -> VideoMetadata:
    position = 0
    bytes_budget = MAX_METADATA_BYTES
    moov_data: bytes | None = None
    seen_boxes = 0

    while position + 8 <= file_size and bytes_budget > 0 and seen_boxes < MAX_BOXES:
        seen_boxes += 1
        handle.seek(position)
        header = handle.read(min(16, bytes_budget))
        bytes_budget -= len(header)
        if len(header) < 8:
            break
        size, kind = struct.unpack(">I4s", header[:8])
        box_header_size = 8
        if size == 1:
            if len(header) < 16:
                break
            size = struct.unpack(">Q", header[8:16])[0]
            box_header_size = 16
        elif size == 0:
            size = file_size - position
        if size < box_header_size:
            break

        box_payload_size = size - box_header_size
        box_payload_offset = position + box_header_size

        if kind == b"moov":
            read_len = min(box_payload_size, bytes_budget)
            handle.seek(box_payload_offset)
            moov_data = handle.read(read_len)
            bytes_budget -= len(moov_data)
            break
        else:
            position += size
            if size == 0:
                break

    if not moov_data:
        return VideoMetadata()

    mvhd_captured = 0.0
    duration = 0.0
    video_width = 0
    video_height = 0
    apple_captured = 0.0
    make = ""
    model = ""

    for kind, start, end in _boxes(moov_data, 0, len(moov_data)):
        if kind == b"mvhd":
            mvhd_captured, duration = _parse_mvhd(moov_data[start:end])
        elif kind == b"trak":
            is_vid, w, h = _parse_trak(moov_data[start:end])
            if is_vid and not (video_width or video_height):
                video_width = w
                video_height = h
        elif kind == b"meta":
            apple_captured, make, model = _parse_meta(moov_data[start:end])

    captured = apple_captured if apple_captured > 0 else mvhd_captured
    if captured < MIN_VALID_EPOCH:
        captured = 0.0

    return VideoMetadata(
        captured=captured,
        width=video_width,
        height=video_height,
        duration=duration,
        make=make,
        model=model,
    )


def read_video_metadata(path: str | Path) -> VideoMetadata:
    """Read video capture date, dimensions, duration and camera from an ISO-BMFF container."""
    candidate = Path(path)
    try:
        if not candidate.is_file():
            return VideoMetadata()
        file_size = candidate.stat().st_size
        with candidate.open("rb") as handle:
            return _parse_video(handle, file_size)
    except (OSError, ValueError, struct.error):
        return VideoMetadata()
