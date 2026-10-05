#!/usr/bin/env python3
"""封面生成：最终累计地形 + 标题 + 统计数字（1920x1080，最近邻放大保持锐利轮廓）。"""
import argparse
import json
import os
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont

from make_video import derived_paths, palette_interp, shade_terrain

COVER_W, COVER_H = 1920, 1080
N_LEVELS = 24

FONT_BOLD_CANDIDATES = [
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Bold.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansSC-Bold.otf",
]


def load_bold_font(size):
    for p in FONT_BOLD_CANDIDATES:
        if os.path.exists(p):
            return ImageFont.truetype(p, size)
    return ImageFont.load_default()


def final_terrain(counts, n_levels=N_LEVELS):
    """按成片默认配置（固定线性刻度 1~max、平滑、计曲线）绘制最终地形。"""
    bounds = np.linspace(1, counts.max(), n_levels + 1)[1:-1].astype(np.float32)
    return shade_terrain(counts, bounds, True, palette_interp(n_levels), n_levels // 6)


def render_cover(counts, stats, title, n_levels=N_LEVELS):
    terrain = cv2.resize(final_terrain(counts, n_levels), (COVER_W, COVER_H),
                         interpolation=cv2.INTER_NEAREST)
    img = Image.fromarray(cv2.cvtColor(terrain, cv2.COLOR_BGR2RGB))
    d = ImageDraw.Draw(img)
    d.text((60, 64), title, font=load_bold_font(92),
           fill=(250, 248, 244), stroke_width=4, stroke_fill=(20, 18, 16))
    d.text((66, 188), "像素翻转地形图", font=load_bold_font(40),
           fill=(240, 205, 130), stroke_width=3, stroke_fill=(20, 18, 16))
    d.text((66, 986),
           f"海拔 = 累计翻转次数 · {stats['total_flips']:,} 次翻转 · {stats['frames']} 帧",
           font=load_bold_font(26), fill=(225, 222, 216),
           stroke_width=2, stroke_fill=(20, 18, 16))
    return cv2.cvtColor(np.array(img), cv2.COLOR_RGB2BGR)


def main():
    ap = argparse.ArgumentParser(prog="terrainify-cover",
                                 description="最终地形封面生成（须先跑过 analyze）")
    ap.add_argument("--input", required=True, help="源视频路径")
    ap.add_argument("--title", required=True, help="封面主标题")
    ap.add_argument("--out", required=True, help="输出 jpg 路径")
    ap.add_argument("--levels", type=int, default=N_LEVELS, help="色带级数")
    args = ap.parse_args()

    counts_path, stats_path = derived_paths(args.input)
    counts = np.load(counts_path)
    stats = json.loads(Path(stats_path).read_text())
    cover = render_cover(counts, stats, args.title, args.levels)
    cv2.imwrite(args.out, cover, [cv2.IMWRITE_JPEG_QUALITY, 95])
    print(f"完成: {args.out}")


if __name__ == "__main__":
    main()
