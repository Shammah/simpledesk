"""Draws the home-screen icons (PNG) to match static/favicon.svg.

Phones and desktops need PNG pictures for installed apps, so this redraws the
same mark: an indigo-to-teal rounded square, a white open "C", a teal dot.

Run again only if you change the design:
    .venv\\Scripts\\python.exe tools\\make_icons.py
Needs Pillow (pip install pillow); the app itself does not.
"""
from pathlib import Path

from PIL import Image, ImageDraw

OUT = Path(__file__).resolve().parent.parent / "static" / "icons"
STOPS = [(0.0, (0x4F, 0x46, 0xE5)), (0.45, (0x3B, 0x6F, 0xD8)), (1.0, (0x0D, 0x94, 0x88))]
WHITE = (255, 255, 255, 255)
TEAL = (0x5E, 0xEA, 0xD4, 255)
SS = 4  # draw 4x larger, then shrink, for smooth edges


def blend_at(t):
    for (t0, c0), (t1, c1) in zip(STOPS, STOPS[1:]):
        if t <= t1:
            f = (t - t0) / (t1 - t0)
            return tuple(round(a + (b - a) * f) for a, b in zip(c0, c1))
    return STOPS[-1][1]


def gradient(size):
    """Diagonal blend, top-left to bottom-right."""
    ramp = Image.linear_gradient("L").rotate(45, expand=True)
    w, h = ramp.size
    ramp = ramp.crop((w // 4, h // 4, w * 3 // 4, h * 3 // 4)).resize((size, size))
    colours = [blend_at(i / 255) for i in range(256)]
    channels = [ramp.point([c[i] for c in colours]) for i in range(3)]
    return Image.merge("RGB", channels).convert("RGBA")


def draw_glyph(draw, s, ox=0.0, oy=0.0):
    """The white "C" and teal dot, in the favicon's 64-unit grid scaled by s."""
    def p(x, y):
        return (ox + x * s, oy + y * s)
    w = 5.5 * s
    r = 5 * s
    draw.line([p(26, 21.5), p(42, 21.5)], fill=WHITE, width=round(w))
    draw.line([p(26, 42.5), p(42, 42.5)], fill=WHITE, width=round(w))
    draw.line([p(21, 26.5), p(21, 37.5)], fill=WHITE, width=round(w))
    for cx, cy, start, end in ((26, 26.5, 180, 270), (26, 37.5, 90, 180)):
        x, y = p(cx, cy)
        box = [x - r - w / 2, y - r - w / 2, x + r + w / 2, y + r + w / 2]
        draw.arc(box, start, end, fill=WHITE, width=round(w))
    for x, y in (p(42, 21.5), p(42, 42.5), p(21, 26.5), p(21, 37.5)):
        draw.ellipse([x - w / 2, y - w / 2, x + w / 2, y + w / 2], fill=WHITE)
    x, y = p(45, 32)
    draw.ellipse([x - 3.6 * s, y - 3.6 * s, x + 3.6 * s, y + 3.6 * s], fill=TEAL)


def icon(size, kind):
    big = size * SS
    canvas = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    if kind == "any":
        # Rounded square with see-through corners, like the favicon.
        s = big / 64
        mask = Image.new("L", (big, big), 0)
        ImageDraw.Draw(mask).rounded_rectangle([2 * s, 2 * s, 62 * s, 62 * s], radius=16 * s, fill=255)
        canvas.paste(gradient(big), (0, 0), mask)
        glyph_scale, offset = s, 0.0
    else:
        # Full square: the phone cuts its own shape ("maskable"), and Apple
        # rounds the corners itself. Keep the mark inside the middle 60%.
        canvas.paste(gradient(big), (0, 0))
        glyph_scale = big * 0.5 / 30          # the mark is about 30 units wide
        offset = big / 2 - 33.4 * glyph_scale  # centre on the mark, not the grid
    # Soft light from the top, fading out by the middle.
    fade = Image.linear_gradient("L").resize((big, big)).point(lambda v: max(0, 34 - v * 34 // 128))
    shine = Image.new("RGBA", (big, big), (255, 255, 255, 0))
    shine.putalpha(Image.composite(fade, Image.new("L", (big, big), 0), canvas.split()[3]))
    canvas = Image.alpha_composite(canvas, shine)
    draw_glyph(ImageDraw.Draw(canvas), glyph_scale, offset, offset + (0 if kind == "any" else (33.4 - 32) * glyph_scale))
    out = canvas.resize((size, size), Image.Resampling.LANCZOS)
    return out.convert("RGB") if kind == "apple" else out


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    files = {
        "icon-192.png": (192, "any"),
        "icon-512.png": (512, "any"),
        "maskable-192.png": (192, "maskable"),
        "maskable-512.png": (512, "maskable"),
        "apple-touch-icon.png": (180, "apple"),
    }
    for name, (size, kind) in files.items():
        icon(size, kind).save(OUT / name, optimize=True)
        print("wrote", OUT / name)


if __name__ == "__main__":
    main()
