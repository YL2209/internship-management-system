#!/usr/bin/env python3
"""
为 PWA 生成全平台图标（基于 logo.svg 的设计语言重新绘制）。

logo 设计要素：
  - 蓝色圆角矩形背景 (#0084FF)
  - 白色文档横线（3条）
  - 右下角黄色云形装饰 (#FFC000)

输出:
  icon-*.png         — 标准 PWA 图标（16/32/57/60/72/76/96/114/120/144/150/152/180/192/256/310/512）
  icon-*-maskable.png — maskable（带安全区内边距）
  apple-touch-icon.png — Apple (无透明背景，180x180)
  favicon.ico        — 多尺寸 ICO
"""

import math
import os
from PIL import Image, ImageDraw

# ---- 颜色 ----
BLUE = (0, 132, 255)        # #0084FF
WHITE = (255, 255, 255)
YELLOW = (255, 192, 0)      # #FFC000
SHADOW = (0, 100, 200)      # 深蓝阴影

OUTPUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static", "pwa")


def rounded_rect(draw, xy, radius, **kw):
    """画圆角矩形（兼容 Pillow 各版本）。"""
    draw.rounded_rectangle(xy, radius=radius, **kw)


def draw_logo(size, maskable=False):
    """
    在 size×size 画布上绘制 logo。
    maskable=True 时，logo 缩小到安全区（80%）并铺满背景色。
    """
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    # ---- 背景圆角矩形 ----
    if maskable:
        # maskable: 整个画布铺满背景色（无圆角，边缘到边）
        margin = int(size * 0.0)  # 全出血
        bg_radius = int(size * 0.18)
        rounded_rect(
            draw,
            [margin, margin, size - margin, size - margin],
            radius=bg_radius,
            fill=BLUE,
        )
        scale = 0.62  # 安全区缩放
    else:
        margin = int(size * 0.06)
        bg_radius = int(size * 0.18)
        # 主背景
        rounded_rect(
            draw,
            [margin, margin, size - margin, size - margin],
            radius=bg_radius,
            fill=BLUE,
        )
        scale = 0.72

    # ---- 文档横线（白色） ----
    # 基于 logo.svg 的比例：3 条横线，左侧对齐
    line_area_w = size * scale * 0.52
    line_h = max(2, int(size * 0.045))
    line_gap = size * scale * 0.13
    start_x = size * (0.5 - scale * 0.26)
    start_y = size * (0.5 - scale * 0.16)

    line_widths = [line_area_w * 0.55, line_area_w, line_area_w]  # 第一条短

    for i, lw in enumerate(line_widths):
        y = start_y + i * (line_h + line_gap)
        rounded_rect(
            draw,
            [int(start_x), int(y), int(start_x + lw), int(y + line_h)],
            radius=max(1, line_h // 2),
            fill=WHITE,
        )

    # ---- 右下角黄色云形（简化为圆角叠加） ----
    cloud_cx = size * (0.5 + scale * 0.28)
    cloud_cy = size * (0.5 + scale * 0.22)
    cloud_r = size * scale * 0.16

    # 用多个圆叠加形成云形
    for dx, dy, r_factor in [
        (-0.35, 0.15, 0.85),
        (0.15, -0.2, 1.0),
        (0.45, 0.1, 0.75),
        (0.0, 0.25, 0.7),
    ]:
        r = cloud_r * r_factor
        cx = cloud_cx + dx * cloud_r
        cy = cloud_cy + dy * cloud_r
        draw.ellipse(
            [int(cx - r), int(cy - r), int(cx + r), int(cy + r)],
            fill=YELLOW,
        )

    return img


def add_white_bg(img):
    """给透明背景的图标加白色不透明背景（用于 Apple Touch Icon）。"""
    bg = Image.new("RGBA", img.size, WHITE + (255,))
    return Image.alpha_composite(bg, img)


def main():
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    print(f"输出目录: {OUTPUT_DIR}")

    sizes = {
        "icon-16.png": 16,
        "icon-32.png": 32,
        "icon-57.png": 57,
        "icon-60.png": 60,
        "icon-72.png": 72,
        "icon-76.png": 76,
        "icon-96.png": 96,
        "icon-114.png": 114,
        "icon-120.png": 120,
        "icon-144.png": 144,
        "icon-150.png": 150,
        "icon-152.png": 152,
        "icon-180.png": 180,
        "icon-192.png": 192,
        "icon-256.png": 256,
        "icon-512.png": 512,
    }

    for name, sz in sizes.items():
        img = draw_logo(sz)
        img.save(os.path.join(OUTPUT_DIR, name))
        print(f"  ✓ {name} ({sz}x{sz})")

    # ---- Maskable 图标 ----
    for name, sz in [("icon-192-maskable.png", 192), ("icon-512-maskable.png", 512)]:
        img = draw_logo(sz, maskable=True)
        img.save(os.path.join(OUTPUT_DIR, name))
        print(f"  ✓ {name} ({sz}x{sz}, maskable)")

    # ---- Apple Touch Icon (白色背景) ----
    apple = add_white_bg(draw_logo(180))
    apple.save(os.path.join(OUTPUT_DIR, "apple-touch-icon.png"))
    print(f"  ✓ apple-touch-icon.png (180x180, white bg)")

    # ---- Windows mstile 尺寸 ----
    # 310x310 方形、310x150 宽幅（带主题色背景）
    for name, sz in [
        ("icon-310x310.png", 310),
    ]:
        img = add_white_bg(draw_logo(sz))
        img.save(os.path.join(OUTPUT_DIR, name))
        print(f"  ✓ {name} ({sz}x{sz}, white bg)")

    # 310x150 宽幅（带主题色背景）
    wide = Image.new("RGBA", (310, 150), BLUE + (255,))
    logo_wide = draw_logo(150)
    wide.paste(logo_wide, (0, 0), logo_wide)
    wide.save(os.path.join(OUTPUT_DIR, "icon-310x150.png"))
    print(f"  ✓ icon-310x150.png (310x150, mstile wide)")

    # ---- favicon.ico (多尺寸) ----
    ico_sizes = [16, 32, 48, 64]
    ico_images = [draw_logo(s).convert("RGBA") for s in ico_sizes]
    ico_path = os.path.join(OUTPUT_DIR, "favicon.ico")
    ico_images[0].save(
        ico_path,
        format="ICO",
        sizes=[(s, s) for s in ico_sizes],
        append_images=ico_images[1:],
    )
    print(f"  ✓ favicon.ico (multi-size)")

    print("\n✅ 全部图标生成完成！")


if __name__ == "__main__":
    main()
