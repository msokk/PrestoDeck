#!/usr/bin/env python3
"""Generate the 80x80 white cog icon used by the Settings button.

Pure stdlib (zlib + struct), so it runs anywhere without Pillow/cairosvg. It
rasterizes a gear — a white annulus (body) with N rectangular teeth — onto a
transparent RGBA canvas, 3x supersampled for smooth edges, and writes the PNG
straight to the icons dir. Re-run after tweaking the geometry constants.

    python3 adhoc/make_settings_icon.py
"""

import math
import os
import struct
import zlib

SIZE = 40          # output is SIZE x SIZE (matches the other corner glyphs)
SS = 3             # supersampling factor (anti-aliasing)
TEETH = 8
R_TOOTH = 0.44 * SIZE  # outer radius of the teeth
R_BODY = 0.34 * SIZE   # outer radius of the solid ring
R_HOLE = 0.16 * SIZE   # inner hole radius
TOOTH_HALF = 0.20      # half-width of each tooth, in radians
# Match the existing white glyphs (base.Colors.WHITE = 255, 250, 240).
INK = (255, 250, 240)

OUT = os.path.join(
    os.path.dirname(__file__), "..", "src", "applications", "spotify", "icons", "settings.png"
)


def _coverage(cx, cy):
    """Returns 0..1 ink coverage for the output pixel at (cx, cy)."""
    center = SIZE / 2.0
    hits = 0
    for sy in range(SS):
        for sx in range(SS):
            # Sample at the sub-pixel center, in coordinates relative to the gear center.
            x = cx + (sx + 0.5) / SS - center
            y = cy + (sy + 0.5) / SS - center
            d = math.hypot(x, y)
            if d <= R_HOLE:
                continue  # inside the hole
            if d <= R_BODY:
                hits += 1  # solid ring
                continue
            if d <= R_TOOTH:
                # In a tooth if the angle lands within TOOTH_HALF of a tooth center.
                a = math.atan2(y, x) % (2 * math.pi)
                step = 2 * math.pi / TEETH
                nearest = round(a / step) * step
                if abs((a - nearest + math.pi) % (2 * math.pi) - math.pi) <= TOOTH_HALF:
                    hits += 1
    return hits / (SS * SS)


def _png(rgba_rows):
    """Encodes a list of bytes rows (one per scanline) into a PNG byte string."""
    def chunk(tag, data):
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    raw = b"".join(b"\x00" + row for row in rgba_rows)  # filter byte 0 per scanline
    ihdr = struct.pack(">IIBBBBB", SIZE, SIZE, 8, 6, 0, 0, 0)  # 8-bit RGBA
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(raw, 9))
            + chunk(b"IEND", b""))


def main():
    rows = []
    for y in range(SIZE):
        row = bytearray()
        for x in range(SIZE):
            a = _coverage(x, y)
            row += bytes((INK[0], INK[1], INK[2], int(round(a * 255))))
        rows.append(bytes(row))

    with open(OUT, "wb") as f:
        f.write(_png(rows))
    print("wrote", os.path.normpath(OUT))


if __name__ == "__main__":
    main()
