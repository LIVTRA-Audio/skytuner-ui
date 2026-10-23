"""Generate the included original navy background; needs Pillow only."""
from pathlib import Path
import math

from PIL import Image, ImageDraw


def generate(path=None):
    image = Image.new("RGB", (128, 160))
    pixels = image.load()
    for y in range(160):
        for x in range(128):
            glow = max(0, 1 - math.hypot((x - 64) / 100, (y - 78) / 125))
            pixels[x, y] = (int(7 + 5 * glow), int(16 + 10 * glow), int(28 + 16 * glow))
    draw = ImageDraw.Draw(image)
    for x in range(0, 128, 16):
        draw.line((x, 16, x, 143), fill=(14, 29, 44))
    for y in range(16, 144, 16):
        draw.line((0, y, 127, y), fill=(14, 29, 44))
    for phase in range(4):
        points = [(x, int(128 + 7 * math.sin(x / 29 + phase / 3) + phase * 3))
                  for x in range(128)]
        draw.line(points, fill=(19 + phase, 40 + phase * 2, 59 + phase * 2), width=1)
    destination = Path(path) if path else Path(__file__).with_name("background.png")
    image.save(destination)
    return destination


if __name__ == "__main__":
    print(generate())
