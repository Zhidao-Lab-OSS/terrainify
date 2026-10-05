#!/usr/bin/env python3
"""渲染模块：画面布局、中文文字、ffmpeg 管道。

布局（1920x1080）：
  左栏 240px：标题 + 垂直等高线图例 + 版本标签
  中央 1440x1080：地形图（随播放隆起）
  右栏 240px：时间 + 累计翻转总数 + 小进度条
  右下角：原片小窗（画中画）  底部：全宽进度条
"""
import os
import subprocess

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont

from make_video import (OUT_W, OUT_H, TERRAIN_W, TERRAIN_H, SIDE_W,
                        derived_paths, quant_points, palette_interp,
                        iter_binary_frames, shade_terrain)

PIP_W, PIP_H = 320, 240
SUBTITLE = "像素翻转地形图"

FONT_CANDIDATES = [
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansSC-Regular.otf",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
]


def load_font(size):
    for p in FONT_CANDIDATES:
        if os.path.exists(p):
            return ImageFont.truetype(p, size)
    return ImageFont.load_default()


def has_audio_track(path):
    """ffprobe 检测源视频是否带音轨；探测失败按无音轨处理（输出纯视频）。"""
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "a",
             "-show_entries", "stream=codec_type", "-of", "csv=p=0", str(path)],
            capture_output=True, text=True, check=True).stdout
        return bool(out.strip())
    except (subprocess.CalledProcessError, FileNotFoundError):
        return False


def build_left_panel(tag, bounds, n_levels=24, scale_range=(1, 400),
                     title="Flip Terrain"):
    """左栏保留全部色带，仅显示稀疏的整数刻度。"""
    canvas = np.zeros((OUT_H, SIDE_W, 3), dtype=np.uint8)
    canvas[:] = (18, 16, 14)
    img = Image.fromarray(cv2.cvtColor(canvas, cv2.COLOR_BGR2RGB))
    d = ImageDraw.Draw(img)
    f_title = load_font(30)
    f_sub = load_font(19)
    f_tick = load_font(20)

    d.text((18, 46), title, font=f_title, fill=(240, 235, 225))
    d.text((18, 90), SUBTITLE, font=f_sub, fill=(150, 160, 170))

    x0, y0, bw, legend_height = 24, 170, 34, 696
    palette = palette_interp(n_levels)
    band_height = legend_height / n_levels
    for k in range(n_levels):
        top = y0 + (n_levels - 1 - k) * band_height
        rgb = tuple(int(c) for c in palette[k][::-1])
        d.rectangle((x0, top, x0 + bw, top + band_height), fill=rgb)

    lower, upper = scale_range
    endpoints = np.concatenate(([lower], bounds, [upper]))
    # 1-2-5 取整生成约 9 个整数刻度，刻度位置依实际色标插值。
    if upper > lower:
        raw_step = (upper - lower) / 8
        magnitude = 10 ** np.floor(np.log10(raw_step))
        step = next(m * magnitude for m in (1, 2, 5, 10)
                    if m * magnitude >= raw_step)
        interior = np.arange(np.ceil(lower / step) * step, upper, step)
        interior = interior[(interior - lower >= step * 0.4)
                            & (upper - interior >= step * 0.4)]
        ticks = np.concatenate(([lower], interior, [upper]))
    else:
        ticks = [lower]
    for value in ticks:
        fraction = np.interp(value, endpoints, np.linspace(0, 1, n_levels + 1))
        y = y0 + (1 - fraction) * legend_height
        d.line((x0 + bw, y, x0 + bw + 7, y), fill=(180, 180, 180), width=1)
        d.text((x0 + bw + 16, y), f"{value:.0f}", anchor="lm",
               font=f_tick, fill=(225, 220, 210))

    d.text((18, OUT_H - 60), tag, font=f_sub, fill=(200, 170, 90))
    return cv2.cvtColor(np.array(img), cv2.COLOR_RGB2BGR)


def live_bounds(counts, n_levels):
    """动态分位切点（浮点、长度恒定），每帧重算实现连续演化。"""
    return np.quantile(counts.astype(np.float32), quant_points(n_levels))


def render_frame(args, stats):
    """第二遍渲染主循环（色标切点由级数/模式内部决定）。"""
    fps = float(stats.get("fps", 30))
    smooth = bool(args.smooth)
    live = bool(getattr(args, "live", 0))
    n_levels = getattr(args, "levels", 24)
    major_every = getattr(args, "major_every", 4)
    fine_contours = bool(getattr(args, "fine_contours", 1))
    n_frames = args.frames or stats["frames"]
    counts = np.zeros((stats["height"], stats["width"]), dtype=np.uint16)
    pal = palette_interp(n_levels)
    counts_path, _ = derived_paths(args.input)

    if live:
        bounds = live_bounds(counts, n_levels)
        scale_range = (float(counts.min()), float(counts.max()))
    else:  # 静态模式：刻度全程固定
        counts_final = np.load(counts_path).astype(np.float32)
        if getattr(args, "scale", "quantile") == "linear":
            # 线性等距：1 ~ 最高翻转数，开播即锁定、全程不刷新
            bounds = np.linspace(1.0, counts_final.max(),
                                 n_levels + 1)[1:-1].astype(np.float32)
            scale_range = (1, float(counts_final.max()))
        else:
            bounds = np.quantile(counts_final, quant_points(n_levels))
            scale_range = (float(counts_final.min()), float(counts_final.max()))
    left = build_left_panel(args.tag, bounds, n_levels, scale_range,
                            getattr(args, "title", "Flip Terrain"))

    # 源带音轨则映射原声（随 --start 偏移），否则输出纯视频。
    if has_audio_track(args.input):
        audio_args = ["-ss", f"{args.start / fps:.3f}", "-i", args.input,
                      "-map", "0:v", "-map", "1:a:0",
                      "-c:a", "aac", "-b:a", "192k"]
    else:
        audio_args = []
    ff = subprocess.Popen(
        ["ffmpeg", "-y", "-loglevel", "error",
         "-f", "rawvideo", "-pix_fmt", "bgr24", "-s", f"{OUT_W}x{OUT_H}",
         "-r", f"{fps:.3f}", "-i", "-",
         *audio_args,
         "-c:v", "libx264", "-preset", "medium", "-crf", "18", "-pix_fmt", "yuv420p",
         "-shortest", "-t", str(n_frames / fps),
         args.out],
        stdin=subprocess.PIPE)

    prev = None
    for idx, b in enumerate(iter_binary_frames(args.input, args.crop)):
        if prev is not None:
            counts += b != prev
        prev = b

        if idx < args.start:
            continue

        if live:  # 每帧重切浮点切点，画面连续演化；图例数字每秒刷新
            bounds = live_bounds(counts, n_levels)
            if idx % 30 == 0:
                left = build_left_panel(args.tag, bounds, n_levels,
                                        (float(counts.min()), float(counts.max())),
                                        getattr(args, "title", "Flip Terrain"))

        frame = np.zeros((OUT_H, OUT_W, 3), dtype=np.uint8)
        frame[:] = (12, 11, 10)
        frame[:, SIDE_W:SIDE_W + TERRAIN_W] = cv2.resize(
            shade_terrain(counts, bounds, smooth, palette=pal, major_every=major_every,
                          fine_contours=fine_contours),
            (TERRAIN_W, TERRAIN_H), interpolation=cv2.INTER_NEAREST)
        frame[:, :SIDE_W] = left

        # 右栏：时间 + 累计翻转总数（ASCII 数字，cv2 直接画，快）
        rx = SIDE_W + TERRAIN_W
        t_cur, t_tot = idx / fps, stats["frames"] / fps
        cv2.putText(frame, "TIME", (rx + 28, 90), 1, 0.7, (110, 110, 115), 1, cv2.LINE_AA)
        cv2.putText(frame, f"{int(t_cur // 60):02d}:{int(t_cur % 60):02d} / {int(t_tot // 60):02d}:{int(t_tot % 60):02d}",
                    (rx + 28, 122), 2, 0.75, (235, 230, 220), 1, cv2.LINE_AA)
        cv2.putText(frame, "TOTAL FLIPS", (rx + 28, 200), 1, 0.7, (110, 110, 115), 1, cv2.LINE_AA)
        cv2.putText(frame, f"{int(counts.sum()):,}", (rx + 28, 236), 2, 0.9, (240, 200, 130), 1, cv2.LINE_AA)
        cv2.putText(frame, "MAX DEPTH", (rx + 28, 310), 1, 0.7, (110, 110, 115), 1, cv2.LINE_AA)
        cv2.putText(frame, f"{int(counts.max())}", (rx + 28, 344), 2, 0.9, (150, 210, 230), 1, cv2.LINE_AA)

        # 右栏中部的垂直进度线
        px, py, ph = rx + 34, 420, 560
        cv2.line(frame, (px, py), (px, py + ph), (60, 60, 66), 2, cv2.LINE_AA)
        cv2.line(frame, (px, py + ph), (px, py + int(ph * (1 - idx / n_frames))), (200, 180, 120), 2, cv2.LINE_AA)

        # 画中画小窗（原片当前帧）
        cap_frame = np.stack([b] * 3, axis=-1).astype(np.uint8) * 255
        pip = cv2.resize(cap_frame, (PIP_W, PIP_H), interpolation=cv2.INTER_NEAREST)
        pipx, pipy = OUT_W - PIP_W - 28, OUT_H - PIP_H - 34
        cv2.rectangle(frame, (pipx - 3, pipy - 3), (pipx + PIP_W + 3, pipy + PIP_H + 3), (235, 235, 235), 3)
        frame[pipy:pipy + PIP_H, pipx:pipx + PIP_W] = pip

        # 底部进度条
        bw = int(OUT_W * idx / n_frames)
        frame[OUT_H - 8:, :bw] = (185, 170, 120)
        frame[OUT_H - 8:, bw:] = (45, 45, 50)

        ff.stdin.write(frame.tobytes())
        if (idx + 1) % 300 == 0:
            print(f"  已渲染 {idx + 1}/{n_frames} 帧")
        if idx + 1 >= n_frames:
            break

    ff.stdin.close()
    ff.wait()
    print(f"完成: {args.out}")
