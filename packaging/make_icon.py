"""Generate the application icon (no external assets): packaging/alphanode.png + .ico.

A simple "node graph": a rounded gradient square, the letter α and node dots. Only Pillow is
needed (already in the environment). Run: python packaging/make_icon.py
"""
import os

from PIL import Image, ImageDraw, ImageFont

HERE = os.path.dirname(os.path.abspath(__file__))
SIZE = 512
BG_TOP = (79, 70, 229)        # indigo (ACC_HI)
BG_BOT = (99, 102, 241)       # lighter indigo (ACC)
FG = (255, 255, 255)


def _rounded_mask(size, radius):
    m = Image.new('L', (size, size), 0)
    d = ImageDraw.Draw(m)
    d.rounded_rectangle([0, 0, size - 1, size - 1], radius=radius, fill=255)
    return m


def _gradient(size, top, bot):
    base = Image.new('RGB', (size, size), top)
    top_img = Image.new('RGB', (size, size), bot)
    mask = Image.new('L', (size, size))
    mask.putdata([int(255 * (y / size)) for y in range(size) for _ in range(size)])
    base.paste(top_img, (0, 0), mask)
    return base


def _font(px):
    for name in ('DejaVuSans-Bold.ttf', 'DejaVuSans.ttf', 'Arial Bold.ttf', 'arial.ttf'):
        try:
            return ImageFont.truetype(name, px)
        except OSError:
            continue
    return ImageFont.load_default()


def main():
    img = _gradient(SIZE, BG_TOP, BG_BOT)
    img.putalpha(_rounded_mask(SIZE, radius=int(SIZE * 0.22)))
    d = ImageDraw.Draw(img)

    # node dots + connections (a light "search graph")
    nodes = [(140, 150), (370, 130), (300, 300), (160, 360), (390, 370)]
    for a in nodes:
        for b in nodes:
            if a < b:
                d.line([a, b], fill=(255, 255, 255, 70), width=5)
    for (x, y) in nodes:
        d.ellipse([x - 15, y - 15, x + 15, y + 15], fill=(255, 255, 255, 235))

    # large α in the center
    f = _font(int(SIZE * 0.5))
    text = 'α'
    tb = d.textbbox((0, 0), text, font=f)
    tw, th = tb[2] - tb[0], tb[3] - tb[1]
    d.text(((SIZE - tw) / 2 - tb[0], (SIZE - th) / 2 - tb[1]), text, font=f, fill=FG)

    png = os.path.join(HERE, 'alphanode.png')
    img.save(png)
    # set of sizes for .ico (Windows)
    ico = os.path.join(HERE, 'alphanode.ico')
    img.save(ico, sizes=[(16, 16), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    # downscaled 256 png for .desktop
    img.resize((256, 256)).save(os.path.join(HERE, 'alphanode-256.png'))
    print('done:', png, ico)


if __name__ == '__main__':
    main()
