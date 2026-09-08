"""Generate deterministic Cowork PNG icons using only the Python standard library."""

from __future__ import annotations

import struct
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parent
GLYPHS = {
    "N": ["10001", "11001", "11001", "10101", "10011", "10011", "10001"],
    "O": ["01110", "10001", "10001", "10001", "10001", "10001", "01110"],
    "C": ["01111", "10000", "10000", "10000", "10000", "10000", "01111"],
}


def _png(width: int, height: int, pixels: bytes) -> bytes:
    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

    rows = b"".join(b"\x00" + pixels[y * width * 4 : (y + 1) * width * 4] for y in range(height))
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(rows, 9))
        + chunk(b"IEND", b"")
    )


def _icon(size: int, background: tuple[int, int, int, int]) -> bytes:
    pixels = bytearray(background * (size * size))
    scale = max(2, size // 24)
    glyph_width = 5 * scale
    gap = scale
    total_width = glyph_width * 3 + gap * 2
    x_origin = (size - total_width) // 2
    y_origin = (size - 7 * scale) // 2
    for glyph_index, letter in enumerate("NOC"):
        for y, row in enumerate(GLYPHS[letter]):
            for x, enabled in enumerate(row):
                if enabled != "1":
                    continue
                for dy in range(scale):
                    for dx in range(scale):
                        px = x_origin + glyph_index * (glyph_width + gap) + x * scale + dx
                        py = y_origin + y * scale + dy
                        offset = (py * size + px) * 4
                        pixels[offset : offset + 4] = bytes((255, 255, 255, 255))
    return _png(size, size, bytes(pixels))


def main() -> None:
    (ROOT / "color.png").write_bytes(_icon(192, (15, 108, 189, 255)))
    (ROOT / "outline.png").write_bytes(_icon(32, (0, 0, 0, 0)))


if __name__ == "__main__":
    main()
