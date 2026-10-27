"""用标准库生成原创黑白声波图标；不下载图标，不引入第三方位图素材。"""

from pathlib import Path
import struct
import zlib


def chunk(kind: bytes, payload: bytes) -> bytes:
    """生成带校验和的 PNG 数据块。"""
    return struct.pack(">I", len(payload)) + kind + payload + struct.pack(">I", zlib.crc32(kind + payload))


def make_png(size: int) -> bytes:
    """采用四倍采样生成圆角方形和五段等宽声波，保证小尺寸边缘平滑。"""
    raw = bytearray()
    bars = [(66, 101, 155), (97, 70, 186), (128, 44, 212), (159, 82, 174), (190, 104, 152)]
    for y in range(size):
        raw.append(0)
        for x in range(size):
            samples = []
            for sy in range(4):
                for sx in range(4):
                    px = (x + (sx + 0.5) / 4) * 256 / size
                    py = (y + (sy + 0.5) / 4) * 256 / size
                    dx, dy = max(40 - px, 0, px - 216), max(40 - py, 0, py - 216)
                    inside = dx * dx + dy * dy <= 32 * 32
                    wave = any((px - bx) ** 2 + (py - min(max(py, top + 9), bottom - 9)) ** 2 <= 81 for bx, top, bottom in bars)
                    samples.append((242 if wave else 28, 255 if inside else 0))
            alpha = sum(s[1] for s in samples) // 16
            value = sum(s[0] for s in samples) // 16
            raw.extend((value, value, value, alpha))
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)) + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b"")


def main() -> None:
    """ICO 中每个分辨率存储独立 PNG，生成文件固定放入 Windows runner 资源。"""
    sizes = [16, 32, 48, 64, 128, 256]
    images = [make_png(size) for size in sizes]
    offset = 6 + 16 * len(images)
    header = bytearray(struct.pack("<HHH", 0, 1, len(images)))
    for size, image in zip(sizes, images):
        header.extend(struct.pack("<BBBBHHII", size % 256, size % 256, 0, 0, 1, 32, len(image), offset))
        offset += len(image)
    target = Path(__file__).resolve().parents[1] / "desktop/windows/runner/resources/app_icon.ico"
    target.write_bytes(header + b"".join(images))


if __name__ == "__main__":
    main()
