"""Dibuja el icono del acceso directo (un globo con el centro de mando) y lo guarda en dashboard/radar.ico."""
import math
import struct
from pathlib import Path

OUT = Path(__file__).resolve().parent.parent / "dashboard" / "radar.ico"


def pixel(x: float, y: float) -> tuple[int, int, int, int]:
    """Color RGBA de un punto en coordenadas de -1 a 1."""
    r = math.hypot(x, y)
    if r > 0.98:
        return (0, 0, 0, 0)
    if r > 0.86:                                   # halo de atmósfera
        return (59, 227, 160, int(255 * (0.98 - r) / 0.12))
    # océano con luz desde arriba a la izquierda
    shade = max(0.0, min(1.0, 0.55 - 0.45 * (x + y) / 1.2))
    col = (int(7 + 30 * shade), int(23 + 50 * shade), int(49 + 70 * shade), 255)
    # meridianos y ecuador
    lines = [abs(y) < 0.035, abs(math.hypot(x / 0.45, y) - 0.86) < 0.045, abs(x) < 0.03]
    if any(lines) and r < 0.84:
        col = (110, 168, 255, 255)
    # centro de mando: rombo dorado
    if abs(x + 0.18) + abs(y - 0.22) < 0.2:
        col = (255, 209, 102, 255)
    return col


def image(size: int) -> bytes:
    ss = 4   # suavizado por supermuestreo
    rows = []
    for py in range(size - 1, -1, -1):             # BMP va de abajo arriba
        row = bytearray()
        for px in range(size):
            acc = [0, 0, 0, 0]
            for sy in range(ss):
                for sx in range(ss):
                    x = ((px + (sx + 0.5) / ss) / size) * 2 - 1
                    y = ((py + (sy + 0.5) / ss) / size) * 2 - 1
                    c = pixel(x, y)
                    for i in range(4):
                        acc[i] += c[i]
            r, g, b, a = (v // (ss * ss) for v in acc)
            row += bytes((b, g, r, a))
        rows.append(bytes(row))
    pixels = b"".join(rows)
    mask = b"\x00" * (((size + 31) // 32) * 4 * size)
    header = struct.pack("<IiiHHIIiiII", 40, size, size * 2, 1, 32, 0, len(pixels) + len(mask), 0, 0, 0, 0)
    return header + pixels + mask


def main() -> None:
    sizes = [16, 24, 32, 48, 64, 128]
    images = [image(s) for s in sizes]
    head = struct.pack("<HHH", 0, 1, len(sizes))
    offset = 6 + 16 * len(sizes)
    entries = b""
    for s, data in zip(sizes, images):
        entries += struct.pack("<BBBBHHII", s % 256, s % 256, 0, 0, 1, 32, len(data), offset)
        offset += len(data)
    OUT.write_bytes(head + entries + b"".join(images))
    print("Icono guardado en", OUT)


if __name__ == "__main__":
    main()
