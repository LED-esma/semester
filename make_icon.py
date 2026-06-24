#!/usr/bin/env python3
"""Generate the Semester app icon: a white 'S' on a black rounded square.
Produces icon.icns (macOS), icon.ico (Windows), and icon.png (Linux/README)."""
import os
import subprocess
from PIL import Image, ImageDraw, ImageFont

HERE = os.path.dirname(os.path.abspath(__file__))
FONTS = ["/System/Library/Fonts/Helvetica.ttc", "/System/Library/Fonts/SFNS.ttf",
         "/System/Library/Fonts/Supplemental/Arial Bold.ttf"]


def render(size):
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    r = int(size * 0.22)
    d.rounded_rectangle([0, 0, size - 1, size - 1], radius=r, fill=(10, 10, 12, 255))
    font = None
    for path in FONTS:
        try:
            font = ImageFont.truetype(path, int(size * 0.74)); break
        except Exception:
            continue
    if font is None:
        font = ImageFont.load_default()
    box = d.textbbox((0, 0), "S", font=font)
    w, h = box[2] - box[0], box[3] - box[1]
    d.text(((size - w) / 2 - box[0], (size - h) / 2 - box[1]), "S", font=font, fill=(255, 255, 255, 255))
    return img


def main():
    iconset = os.path.join(HERE, "icon.iconset")
    os.makedirs(iconset, exist_ok=True)
    specs = [(16, ""), (16, "@2x"), (32, ""), (32, "@2x"), (128, ""), (128, "@2x"),
             (256, ""), (256, "@2x"), (512, ""), (512, "@2x")]
    for base, suf in specs:
        px = base * (2 if suf else 1)
        render(px).save(os.path.join(iconset, f"icon_{base}x{base}{suf}.png"))
    render(512).save(os.path.join(HERE, "icon.png"))
    render(256).save(os.path.join(HERE, "icon.ico"), sizes=[(16, 16), (32, 32), (48, 48), (128, 128), (256, 256)])
    try:
        subprocess.run(["iconutil", "-c", "icns", iconset, "-o", os.path.join(HERE, "icon.icns")], check=True)
        print("wrote icon.icns")
    except Exception as e:
        print("icns skipped (non-macOS or iconutil missing):", e)
    print("wrote icon.png, icon.ico")


if __name__ == "__main__":
    main()
