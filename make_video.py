#!/usr/bin/env python3
"""terrainify：任意视频 → 像素翻转等高线地形视频。

思路：统计每个像素截至当前帧的黑白翻转累计次数，按次数映射
等高线地形配色（次数=海拔），随播放推进地形逐渐隆起。

子命令：
  analyze  第一遍：全片扫描，统计最终累计翻转矩阵与色标切点
  render   第二遍：按配置渲染输出视频（可指定帧数出小样）

用法示例见 README.md。
"""
import argparse
from itertools import product
import json
from pathlib import Path
import subprocess
import sys

import cv2
import numpy as np

COUNTS_SUFFIX = "_counts.npy"
STATS_SUFFIX = "_stats.json"

OUT_W, OUT_H = 1920, 1080          # 成片分辨率
TERRAIN_W, TERRAIN_H = 1440, 1080  # 地形图区域（4:3）
SIDE_W = 240                       # 左右信息栏宽度
BIN_THR = 127                      # 二值化阈值

# 12 级地形调色板（RGB）：深渊→深海→浅海→沙滩→低地→草地→森林→丘陵→山地棕→高山→岩灰→雪
PALETTE = np.array([
    (8, 10, 36), (16, 38, 92), (28, 78, 132), (58, 124, 165),
    (202, 195, 148), (122, 185, 104), (66, 152, 74), (34, 121, 52),
    (186, 184, 92), (158, 128, 72), (146, 116, 100), (232, 232, 236),
], dtype=np.uint8)
PALETTE_BGR = np.ascontiguousarray(PALETTE[:, ::-1])  # OpenCV 帧用


def derived_paths(input_path):
    """analyze 产物路径按输入名派生：video.mp4 → video_counts.npy / video_stats.json。"""
    stem = Path(input_path).stem
    return Path(f"{stem}{COUNTS_SUFFIX}"), Path(f"{stem}{STATS_SUFFIX}")


def parse_crop(text):
    """'left,top,right,bottom' → 裁剪框；空值表示不裁剪。"""
    if not text:
        return None
    try:
        parts = [int(v) for v in text.split(",")]
    except ValueError:
        raise argparse.ArgumentTypeError("crop 需为逗号分隔的四个整数")
    if len(parts) != 4 or not (parts[0] < parts[2] and parts[1] < parts[3]):
        raise argparse.ArgumentTypeError("crop 需为 left,top,right,bottom 且右>左、下>上")
    return tuple(parts)


def palette_interp(n_levels):
    """12 锚点色线性插值出 n_levels 级渐变调色板（BGR）。"""
    if n_levels <= 12:
        return PALETTE_BGR
    anchors = np.arange(12) / 11.0
    pos = np.linspace(0, 1, n_levels)
    out = np.stack([np.interp(pos, anchors, PALETTE_BGR[:, c]) for c in range(3)], 1)
    rounded = np.clip(out.round(), 0, 255).astype(np.uint8)
    # 8 位通道取整会使密集渐变的相邻档位重色；在邻近色中选误差最小的空位。
    occupied = {tuple(c) for c in rounded.tolist()}
    seen = set()
    offsets = np.array(list(product((-1, 0, 1), repeat=3)))
    for i, color in enumerate(rounded):
        key = tuple(color.tolist())
        if key in seen:
            candidates = np.clip(color.astype(int) + offsets, 0, 255)
            order = np.argsort(np.square(candidates - out[i]).sum(axis=1), kind="stable")
            for j in order:
                alternative = tuple(candidates[j].tolist())
                if alternative not in occupied:
                    rounded[i] = candidates[j]
                    key = alternative
                    occupied.add(key)
                    break
            else:
                raise ValueError("颜色级数过高，无法在邻近色中区分全部档位")
        seen.add(key)
    return rounded


def quant_points(n_levels):
    """n 级分位切点位置（0..1 之间 n-1 个）。"""
    return np.linspace(0, 1, n_levels + 1)[1:-1]

# 分位数版切点：低分位铺开，避免大批像素挤进同一级
QUANT_POINTS = [0.02, 0.08, 0.18, 0.32, 0.48, 0.62, 0.74, 0.84, 0.91, 0.96, 1.0]


def iter_binary_frames(path, crop=None):
    """解码视频，逐帧产出二值布尔矩阵；crop=(left,top,right,bottom) 可选裁剪。"""
    cap = cv2.VideoCapture(path)
    if not cap.isOpened():
        sys.exit(f"无法打开视频: {path}")
    while True:
        ok, frame = cap.read()
        if not ok:
            break
        if crop:
            x0, y0, x1, y1 = crop
            frame = frame[y0:y1, x0:x1]
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        yield gray > BIN_THR
    cap.release()


def source_fps(path):
    """读取源视频帧率（读不到时回退 30）。"""
    cap = cv2.VideoCapture(path)
    fps = cap.get(cv2.CAP_PROP_FPS)
    cap.release()
    return fps if fps and fps > 0 else 30.0


def cmd_analyze(args):
    """第一遍：全片统计最终累计翻转矩阵，算两种色标的切点。"""
    h = w = None
    counts = None
    prev = None
    total_flips = 0
    for i, b in enumerate(iter_binary_frames(args.input, args.crop)):
        if counts is None:
            h, w = b.shape
            counts = np.zeros((h, w), dtype=np.uint16)
        if prev is not None:
            flips = b != prev
            counts += flips
            total_flips += int(flips.sum())
        prev = b
        if (i + 1) % 1000 == 0:
            print(f"  已扫描 {i + 1} 帧, 累计翻转事件 {total_flips:,}")

    max_count = int(counts.max())
    # 线性：min..max 均分 12 级（11 个内部切点）
    linear_bounds = np.unique(np.linspace(counts.min(), max_count, 12)[1:].round().astype(int))
    # 分位数：全像素按分位切 11 刀
    quant_bounds = np.unique(np.quantile(counts.astype(np.float32), QUANT_POINTS).round().astype(int))

    counts_path, stats_path = derived_paths(args.input)
    stats = {
        "frames": i + 1, "width": w, "height": h,
        "fps": round(source_fps(args.input), 3),
        "min_count": int(counts.min()),
        "max_count": max_count, "total_flips": total_flips,
        "pixels": int(counts.size),
        "linear_bounds": linear_bounds.tolist(),
        "quantile_bounds": quant_bounds.tolist(),
    }
    np.save(counts_path, counts)
    with open(stats_path, "w") as f:
        json.dump(stats, f, ensure_ascii=False, indent=2)
    print(json.dumps(stats, ensure_ascii=False, indent=2))


def level_map(counts_f, bounds):
    """把计数字段映射为 0..len(bounds) 的级别索引。"""
    return np.searchsorted(bounds, counts_f)


def shade_levels(lvl, palette, major_every=0, mask=None, fine_contours=True):
    """绘制单像素等高线，保留色带亮度和色相；可限制在前景内。"""
    img = palette[lvl].copy()
    edge = np.zeros(lvl.shape, dtype=bool)
    major = np.zeros(lvl.shape, dtype=bool)
    for first, second, target in (
        (lvl[:, :-1], lvl[:, 1:], (slice(None), slice(None, -1))),
        (lvl[:-1], lvl[1:], (slice(None, -1), slice(None))),
    ):
        changed = first != second
        edge[target] |= changed
        if major_every > 0:
            # 跨越正整数计曲线才加深，不把 0 级边界视作计曲线。
            low, high = np.minimum(first, second), np.maximum(first, second)
            major[target] |= changed & (low // major_every < high // major_every)
    if mask is not None:
        edge &= mask
        major &= mask
    # 不膨胀线条，避免细碎色带的描边合并成大片暗块。
    img[major] = np.rint(img[major].astype(np.float32) * 0.78).astype(np.uint8)
    fine = edge & ~major if fine_contours else np.zeros(lvl.shape, dtype=bool)
    img[fine] = np.rint(img[fine].astype(np.float32) * 0.90).astype(np.uint8)
    return img


def shade_terrain(counts_now, bounds, smooth, palette=None, major_every=0,
                  fine_contours=True):
    """渲染与计数矩阵同尺寸的地形图（BGR 通道顺序）。

    counts_now: 当前累计翻转矩阵; bounds: 色标切点; smooth: 是否 3x3 高斯轻平滑;
    palette: 渐变调色板（默认 12 锚点）; major_every>0 时每 N 级一条略深的计曲线。
    """
    field = counts_now.astype(np.float32)
    if smooth:
        field = cv2.GaussianBlur(field, (3, 3), 0)
    lvl = level_map(field, bounds)
    return shade_levels(lvl, palette if palette is not None else PALETTE_BGR,
                        major_every, fine_contours=fine_contours)


def main():
    ap = argparse.ArgumentParser(prog="terrainify",
                                 description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("analyze", help="第一遍：全片统计")
    a.add_argument("--input", required=True, help="源视频路径")
    a.add_argument("--crop", type=parse_crop, default=None,
                   help="可选边缘裁剪 left,top,right,bottom（默认不裁剪）")

    r = sub.add_parser("render", help="第二遍：渲染输出")
    r.add_argument("--input", required=True, help="源视频路径（须先跑过 analyze）")
    r.add_argument("--crop", type=parse_crop, default=None,
                   help="边缘裁剪，须与 analyze 一致")
    r.add_argument("--scale", choices=["linear", "quantile"], required=True)
    r.add_argument("--smooth", type=int, choices=[0, 1], default=0)
    r.add_argument("--frames", type=int, default=0, help="只渲染前 N 帧（0=全片），用于小样")
    r.add_argument("--start", type=int, default=0, help="从第 N 帧开始渲染（计数仍从第 0 帧累积）")
    r.add_argument("--live", type=int, choices=[0, 1], default=0,
                   help="动态分位色标：每帧按当前分布重切色带（浮点切点，连续演化无跳变）")
    r.add_argument("--levels", type=int, default=24, help="色带级数（12 锚点插值）")
    r.add_argument("--major-every", type=int, default=4, help="每 N 级一条计曲线，0 表示关闭")
    r.add_argument("--fine-contours", type=int, choices=[0, 1], default=1,
                   help="是否描出每一级的细线；密集渐变可关闭，仅保留计曲线")
    r.add_argument("--title", default="Flip Terrain", help="画面左栏主标题")
    r.add_argument("--out", required=True)
    r.add_argument("--tag", default="", help="版本标签，显示在画面左栏")
    args = ap.parse_args()

    if args.cmd == "analyze":
        cmd_analyze(args)
        return

    _, stats_path = derived_paths(args.input)
    with open(stats_path) as f:
        stats = json.load(f)

    from render_kit import render_frame  # 渲染布局/文字逻辑独立模块
    render_frame(args, stats)  # 色标切点由 render 按级数/模式自算


if __name__ == "__main__":
    main()
