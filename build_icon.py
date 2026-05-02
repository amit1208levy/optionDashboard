"""
Generate the OptionsDashboard.icns app icon from scratch.

Design: Apple-style rounded purple square (uses the app's PURPLE2 from theme.py)
with three white candlesticks of staggered heights — the middle one a soft
green to suggest a bullish position. Result: AppIcon.icns at the project root.
"""
import os
import shutil
import subprocess
from PIL import Image, ImageDraw

# Dark background — deep indigo fading into near-black for a finance-terminal
# vibe. Candles are kept saturated and bright so they pop against it.
BG_TOP        = ( 49,  46, 129)   # #312e81  (indigo-900)
BG_BOTTOM    = (  9,   9,  20)   # #090914  (near-black with violet undertone)

# Vibrant candle colors
WHITE         = (255, 255, 255)
NEON_GREEN    = ( 16, 245, 129)   # #10f581  (bullish, slightly brighter than emerald)
NEON_RED      = (255,  60,  76)   # #ff3c4c  (bearish)

SIZE   = 1024
MARGIN = 64                       # transparent padding around the rounded square
RADIUS = 224                      # ~22% of SIZE — Apple's macOS icon corner radius


def vertical_gradient(size: int, top: tuple, bottom: tuple) -> Image.Image:
    """Build a top-to-bottom RGB gradient. Cheap row-fill, no numpy required."""
    img = Image.new("RGB", (size, size), top)
    draw = ImageDraw.Draw(img)
    for y in range(size):
        t = y / (size - 1)
        c = tuple(int(top[i] + (bottom[i] - top[i]) * t) for i in range(3))
        draw.line([(0, y), (size, y)], fill=c)
    return img


def rounded_mask(size: int, margin: int, radius: int) -> Image.Image:
    """White rounded-square mask used to clip the gradient to icon shape."""
    mask = Image.new("L", (size, size), 0)
    d = ImageDraw.Draw(mask)
    d.rounded_rectangle((margin, margin, size - margin, size - margin),
                        radius=radius, fill=255)
    return mask


def draw_candlesticks(img: Image.Image) -> None:
    """Three vibrant candlesticks: red bearish, green bullish, neutral white."""
    d = ImageDraw.Draw(img)

    candle_w = 110     # body width
    wick_w   = 22      # wick width
    body_r   = 14      # body corner radius

    # (center_x, wick_top, wick_bot, body_top, body_bot, body_color, wick_color)
    candles = [
        (340, 400, 700, 470, 640,  NEON_RED,    NEON_RED),     # bearish
        (512, 280, 770, 350, 690,  NEON_GREEN,  NEON_GREEN),   # bullish (tallest)
        (684, 380, 680, 460, 620,  WHITE,       WHITE),        # neutral
    ]

    for cx, wt, wb, bt, bb, body_color, wick_color in candles:
        # Wick
        d.rectangle((cx - wick_w // 2, wt, cx + wick_w // 2, wb),
                    fill=wick_color)
        # Body
        d.rounded_rectangle(
            (cx - candle_w // 2, bt, cx + candle_w // 2, bb),
            radius=body_r,
            fill=body_color,
        )


def build_master() -> Image.Image:
    """1024x1024 RGBA master image."""
    gradient = vertical_gradient(SIZE, BG_TOP, BG_BOTTOM)
    mask     = rounded_mask(SIZE, MARGIN, RADIUS)

    canvas = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    canvas.paste(gradient, (0, 0), mask)

    draw_candlesticks(canvas)
    return canvas


def build_icns(master: Image.Image, out_icns: str) -> None:
    """Render every macOS icon size and assemble via iconutil."""
    iconset_dir = os.path.abspath("AppIcon.iconset")
    if os.path.isdir(iconset_dir):
        shutil.rmtree(iconset_dir)
    os.makedirs(iconset_dir)

    # Apple's required sizes for a complete .icns
    pairs = [
        (16,   "icon_16x16.png"),
        (32,   "icon_16x16@2x.png"),
        (32,   "icon_32x32.png"),
        (64,   "icon_32x32@2x.png"),
        (128,  "icon_128x128.png"),
        (256,  "icon_128x128@2x.png"),
        (256,  "icon_256x256.png"),
        (512,  "icon_256x256@2x.png"),
        (512,  "icon_512x512.png"),
        (1024, "icon_512x512@2x.png"),
    ]
    for size, name in pairs:
        master.resize((size, size), Image.LANCZOS).save(
            os.path.join(iconset_dir, name)
        )

    subprocess.run(
        ["iconutil", "-c", "icns", iconset_dir, "-o", out_icns],
        check=True,
    )
    shutil.rmtree(iconset_dir)


if __name__ == "__main__":
    here = os.path.dirname(os.path.abspath(__file__))
    master = build_master()
    master.save(os.path.join(here, "AppIcon.png"))   # PNG for previews
    build_icns(master, os.path.join(here, "AppIcon.icns"))
    print(f"✓ Wrote AppIcon.png and AppIcon.icns to {here}")
