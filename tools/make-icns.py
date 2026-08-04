#!/usr/bin/env python3
"""產生 macOS 的 static/gs-icon.icns，無第三方相依。

為什麼不是 README 裡那一行 sips
--------------------------------
原本文件寫的是把單一張 512x512 丟給 iconutil：

    sips -z 512 512 static/gs-icon.png --out gs-icon.iconset/icon_512x512.png
    iconutil -c icns gs-icon.iconset -o static/gs-icon.icns

那份 iconset 只有一個尺寸。macOS 需要 16 到 1024 共十個切片（含 @2x）；
少了就由系統即時縮放，Dock 與 Finder 在 Retina 下會糊掉，而且它同時要求
一張 static/gs-icon.png 當來源——那個檔在 .gitignore 的 static/ 裡，版控
拿不到，等於文件教的路徑在乾淨 checkout 上跑不起來。

這支改成直接把每個尺寸各自畫出來（小圖自己畫比從 1024 縮下去銳利），
不需要任何來源圖檔，也不需要 Pillow。

視覺與 Windows 版一致
--------------------
配色沿用 pack.config.ps1 的 $IconBg / $IconRing，跟 gs-app-pack 的
scripts/make_icon.py 畫的是同一個標記，兩個平台出貨的圖示才會是同一個。
差別只在外框：macOS 用 squircle 圓角並留白，方形滿版在 Dock 裡一眼就看得
出來不是原生 app。

用法：
    python3 tools/make-icns.py                 # → static/gs-icon.icns
    python3 tools/make-icns.py --out other.icns --ring "#d4af37"
"""
from __future__ import annotations

import argparse
import math
import shutil
import struct
import subprocess
import sys
import tempfile
import zlib
from pathlib import Path

# 與 pack.config.ps1 的 $IconBg / $IconRing 相同。
DEFAULT_BG = "#07060a"
DEFAULT_RING = "#d4af37"

#: iconutil 認得的十個切片：(檔名, 實際像素)。少一個 macOS 就自己縮放。
ICONSET = [
    ("icon_16x16.png", 16),
    ("icon_16x16@2x.png", 32),
    ("icon_32x32.png", 32),
    ("icon_32x32@2x.png", 64),
    ("icon_128x128.png", 128),
    ("icon_128x128@2x.png", 256),
    ("icon_256x256.png", 256),
    ("icon_256x256@2x.png", 512),
    ("icon_512x512.png", 512),
    ("icon_512x512@2x.png", 1024),
]

#: Big Sur 之後的圖示是超橢圓（squircle），不是圓角矩形。次方 5 很接近
#: 系統模板；4 太方、8 太圓。
SQUIRCLE_EXPONENT = 5.0
#: 內容佔畫布的比例。Apple 的模板留白約一成，不留的話 Dock 裡會比鄰居大一號。
CONTENT_SCALE = 0.90


def hex_to_rgb(value: str) -> tuple[int, int, int]:
    text = value.lstrip("#")
    if len(text) != 6:
        raise ValueError(f"顏色要是 #rrggbb，收到 {value!r}")
    return int(text[0:2], 16), int(text[2:4], 16), int(text[4:6], 16)


def _png_bytes(size: int, pixels: bytearray) -> bytes:
    """把 RGBA 位元組打包成 PNG。只用 zlib 與 struct。"""
    raw = bytearray()
    stride = size * 4
    for y in range(size):
        raw.append(0)  # filter type 0 (None)
        raw += pixels[y * stride:(y + 1) * stride]

    def chunk(tag: bytes, data: bytes) -> bytes:
        body = tag + data
        return (
            struct.pack(">I", len(data))
            + body
            + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)
        )

    ihdr = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)  # 8-bit RGBA
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(bytes(raw), 9))
        + chunk(b"IEND", b"")
    )


def render(size: int, bg: tuple[int, int, int], ring: tuple[int, int, int]) -> bytes:
    """畫一張圖示。超取樣做反鋸齒——16px 那張全靠它才不會是鋸齒毛邊。"""
    # 小圖每個像素的權重都很高，多取幾個樣本；大圖不需要，也不值得等。
    samples = 4 if size <= 128 else 2
    inv = 1.0 / samples
    weight = 1.0 / (samples * samples)

    centre = size / 2.0
    half = size * CONTENT_SCALE / 2.0
    # 環的半徑相對於內容框，比例沿用 make_icon.py 的 0.42 / 0.22。
    r_outer = half * 2 * 0.42
    r_inner = half * 2 * 0.22

    pixels = bytearray(size * size * 4)
    for y in range(size):
        for x in range(size):
            inside = 0.0   # 落在 squircle 內的樣本比例 → alpha
            on_ring = 0.0  # 其中又落在環上的比例 → 金色佔比
            for sy in range(samples):
                py = y + (sy + 0.5) * inv - centre
                for sx in range(samples):
                    px = x + (sx + 0.5) * inv - centre
                    # squircle：|x/a|^n + |y/a|^n <= 1
                    if (
                        (abs(px) / half) ** SQUIRCLE_EXPONENT
                        + (abs(py) / half) ** SQUIRCLE_EXPONENT
                    ) > 1.0:
                        continue
                    inside += weight
                    d = math.hypot(px, py)
                    if r_inner <= d <= r_outer:
                        on_ring += weight

            offset = (y * size + x) * 4
            if inside <= 0.0:
                continue  # 完全透明，bytearray 已經是 0
            # 環的顏色先跟底色混好，再乘上 squircle 的 alpha，邊緣才不會
            # 出現一圈深色（那是先乘 alpha 再混色的典型症狀）。
            t = on_ring / inside
            pixels[offset + 0] = round(bg[0] + (ring[0] - bg[0]) * t)
            pixels[offset + 1] = round(bg[1] + (ring[1] - bg[1]) * t)
            pixels[offset + 2] = round(bg[2] + (ring[2] - bg[2]) * t)
            pixels[offset + 3] = round(inside * 255)

    return _png_bytes(size, pixels)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--out",
        type=Path,
        default=Path(__file__).resolve().parent.parent / "static" / "gs-icon.icns",
    )
    parser.add_argument("--bg", default=DEFAULT_BG)
    parser.add_argument("--ring", default=DEFAULT_RING)
    parser.add_argument(
        "--keep-iconset",
        action="store_true",
        help="保留中間的 .iconset 資料夾（想用 Preview 逐張看時）",
    )
    args = parser.parse_args(argv)

    if sys.platform != "darwin":
        print("iconutil 只有 macOS 有，.icns 必須在 Mac 上產生。", file=sys.stderr)
        return 2
    if shutil.which("iconutil") is None:
        print("找不到 iconutil；請安裝 Xcode Command Line Tools。", file=sys.stderr)
        return 2

    bg = hex_to_rgb(args.bg)
    ring = hex_to_rgb(args.ring)

    scratch = Path(tempfile.mkdtemp(prefix="gs-icon."))
    iconset = scratch / "gs-icon.iconset"
    iconset.mkdir()
    try:
        for name, size in ICONSET:
            (iconset / name).write_bytes(render(size, bg, ring))
            print(f"  {name} ({size}x{size})")

        args.out.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            ["iconutil", "-c", "icns", str(iconset), "-o", str(args.out)],
            check=True,
        )
    finally:
        if args.keep_iconset:
            print(f"iconset 保留在：{iconset}")
        else:
            shutil.rmtree(scratch, ignore_errors=True)

    print(f"已寫出：{args.out}（{args.out.stat().st_size} bytes）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
