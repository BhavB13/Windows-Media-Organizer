"""Generate the Windows application icon used by release packaging."""

from __future__ import annotations

import struct
from pathlib import Path


def _inside_rounded_rect(x: int, y: int, size: int, radius: int) -> bool:
    left = radius
    right = size - radius - 1
    top = radius
    bottom = size - radius - 1
    if left <= x <= right or top <= y <= bottom:
        return True
    cx = left if x < left else right
    cy = top if y < top else bottom
    return (x - cx) ** 2 + (y - cy) ** 2 <= radius**2


def _pixel(size: int, x: int, y: int) -> tuple[int, int, int, int]:
    radius = max(4, size // 5)
    if not _inside_rounded_rect(x, y, size, radius):
        return (0, 0, 0, 0)

    blue = (37, 99, 235, 255)
    white = (255, 255, 255, 255)
    soft = (255, 255, 255, 210)
    inset = max(3, size // 8)
    border = max(1, size // 16)
    inner_radius = max(2, radius // 2)
    in_inner = _inside_rounded_rect(
        x - inset,
        y - inset,
        size - (inset * 2),
        inner_radius,
    )
    in_inner_shift = _inside_rounded_rect(
        x - inset - border,
        y - inset - border,
        size - (inset * 2) + (border * 2),
        inner_radius + border,
    )
    if in_inner_shift and not in_inner:
        return soft

    line_y = size // 2
    line_height = max(2, size // 10)
    if size * 0.28 <= x <= size * 0.68 and abs(y - line_y) <= line_height // 2:
        return white

    ax = size * 0.72
    ay = size * 0.50
    arrow_width = size * 0.18
    arrow_height = size * 0.18
    if x >= ax - arrow_width and abs(y - ay) <= arrow_height:
        left_edge = ax - arrow_width
        slope_limit = arrow_height - abs(y - ay)
        if x >= left_edge + (arrow_width - slope_limit):
            return white

    return blue


def _ico_image(size: int) -> bytes:
    pixels = bytearray()
    for y in range(size - 1, -1, -1):
        for x in range(size):
            red, green, blue, alpha = _pixel(size, x, y)
            pixels.extend((blue, green, red, alpha))
    mask_stride = ((size + 31) // 32) * 4
    and_mask = b"\x00" * (mask_stride * size)
    header = struct.pack(
        "<IIIHHIIIIII",
        40,
        size,
        size * 2,
        1,
        32,
        0,
        len(pixels),
        0,
        0,
        0,
        0,
    )
    return header + bytes(pixels) + and_mask


def main() -> int:
    assets = Path("assets")
    assets.mkdir(exist_ok=True)
    sizes = [16, 24, 32, 48, 64, 128, 256]
    images = [_ico_image(size) for size in sizes]
    offset = 6 + (16 * len(sizes))
    directory = bytearray()
    for size, image in zip(sizes, images):
        directory.extend(
            struct.pack(
                "<BBBBHHII",
                0 if size == 256 else size,
                0 if size == 256 else size,
                0,
                0,
                1,
                32,
                len(image),
                offset,
            )
        )
        offset += len(image)
    payload = struct.pack("<HHH", 0, 1, len(sizes)) + bytes(directory) + b"".join(images)
    (assets / "app.ico").write_bytes(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
