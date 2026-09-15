#!/usr/bin/env python3
"""
generate_icons.py - Genera le icone PNG della PWA (192/512/apple-touch 180)
============================================================================
Puro Python standard library (zlib + struct): nessuna dipendenza (PIL).

Design: fondo scuro arrotondato (#0e1117) + anello "readiness gauge"
con segmenti rosso/ambra/verde + pallino bianco al centro.

Uso:  python3 web/icons/generate_icons.py
"""

import math
import struct
import zlib
from pathlib import Path

OUT_DIR = Path(__file__).resolve().parent
BG = (14, 17, 23)          # #0e1117
RED = (239, 68, 68)        # #ef4444
AMBER = (245, 158, 11)     # #f59e0b
GREEN = (34, 197, 94)      # #22c55e
WHITE = (255, 255, 255)

# Suddivisione dell'anello (gradi): rosso 0-120, ambra 120-240, verde 240-360
SEGMENTS = [(0, 120, RED), (120, 240, AMBER), (240, 360, GREEN)]


def write_png(path: Path, size: int, pixels) -> None:
    """pixels: matrice size x size di tuple (r, g, b, a)."""
    raw = b"".join(
        b"\x00" + b"".join(struct.pack("4B", *px) for px in row) for row in pixels
    )

    def chunk(tag: bytes, data: bytes) -> bytes:
        c = struct.pack(">I", len(data)) + tag + data
        return c + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    png = b"\x89PNG\r\n\x1a\n"
    png += chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0))
    png += chunk(b"IDAT", zlib.compress(raw, 9))
    png += chunk(b"IEND", b"")
    path.write_bytes(png)
    print(f"  ✓ {path.name} ({size}x{size})")


def generate(size: int):
    cx = cy = size / 2
    radius = size * 0.40
    thickness = size * 0.085
    corner = size * 0.22
    dot_r = size * 0.085

    pixels = []
    for y in range(size):
        row = []
        for x in range(size):
            # fondo arrotondato: trasparente fuori dal rounded-rect
            dxc = max(corner - x, 0, x - (size - corner))
            dyc = max(corner - y, 0, y - (size - corner))
            if math.hypot(dxc, dyc) > corner:
                row.append((0, 0, 0, 0))
                continue

            col = BG
            d = math.hypot(x - cx, y - cy)
            if radius - thickness <= d <= radius + thickness:
                ang = math.degrees(math.atan2(y - cy, x - cx)) % 360
                for a0, a1, c in SEGMENTS:
                    if a0 <= ang < a1:
                        col = c
                        break
            elif d <= dot_r:
                col = WHITE
            row.append(col + (255,))
        pixels.append(row)
    return pixels


def main() -> None:
    print("🖼️  Generazione icone PWA...")
    for name, size in [("icon-192.png", 192), ("icon-512.png", 512),
                       ("apple-touch-icon.png", 180)]:
        write_png(OUT_DIR / name, size, generate(size))
    print("✅ Icone generate in", OUT_DIR)


if __name__ == "__main__":
    main()
