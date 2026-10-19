"""Derive the landing page's brand assets from the repo's assets/Nova.png.

Produces (into ../assets):
  favicon.png      - 64px emblem
  emblem-256.png   - 256px emblem
  emblem-128.webp  - 128px emblem, the file the nav lockup actually loads

All three keep the night-sky ink keyed to transparency. The source paints the
emblem on INK, so the ink is the darkest thing in the crop: alpha is the
brightest channel with the ink floor removed, which preserves the glow's soft
edge but makes a background pixel fully transparent. The mark can then sit on
the nav's art, on a light ground or in a browser tab without a black tile.
Crop boxes were measured by scanning the source for non-black pixels rather
than guessed: the emblem circle occupies x 62..751 of the 1954x805 original.

Run with a Python that has Pillow, e.g.:
    python landing/tools/make_brand_assets.py --source <Nova.png>
When the source is not found the script keys the existing emblem-256.png
instead, provided it is still opaque.

The social card is versioned separately: the site's og.jpg comes from
make_hero_assets.py.
"""

import argparse
from pathlib import Path

from PIL import Image, ImageChops

REPO = Path(__file__).resolve().parents[2]
DEFAULT_SRC = REPO / "assets" / "Nova.png"
OUT = Path(__file__).resolve().parent.parent / "assets"

EMBLEM_BOX = (62, 58, 752, 740)
INK = (10, 13, 28)


def key_ink(im: "Image.Image") -> "Image.Image":
    """Return an RGBA copy of `im` with the night-sky ink keyed to alpha 0."""
    r, g, b = im.convert("RGB").split()
    alpha = ImageChops.lighter(ImageChops.lighter(r, g), b)
    floor = max(INK)
    alpha = alpha.point(lambda v: 0 if v <= floor else round((v - floor) * 255 / (255 - floor)))
    out = im.convert("RGBA")
    out.putalpha(alpha)
    return out


def report(name: str, path: Path) -> None:
    """Print size, mode and alpha evidence: corners empty, middle lit."""
    im = Image.open(path)
    a = im.convert("RGBA").getchannel("A")
    w, h = im.size
    corners = [a.getpixel(p) for p in ((0, 0), (w - 1, 0), (0, h - 1), (w - 1, h - 1))]
    print(f"{name:16} {str(im.size):12} {im.mode:5} {path.stat().st_size:>8,} bytes"
          f"  corners {corners}  centre {a.getpixel((w // 2, h // 2))}  peak {a.getextrema()[1]}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--source", default=str(DEFAULT_SRC), help="original Nova.png")
    args = ap.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)

    src_path = Path(args.source)
    if src_path.exists():
        emblem = key_ink(Image.open(src_path).convert("RGB").crop(EMBLEM_BOX))
    else:
        existing = Image.open(OUT / "emblem-256.png")
        if existing.mode == "RGBA":
            raise SystemExit(f"{src_path} is missing and emblem-256.png is already keyed; pass --source")
        emblem = key_ink(existing)
        print(f"note: {src_path} not found; keying the existing emblem-256.png")

    emblem.resize((64, 64), Image.LANCZOS).save(OUT / "favicon.png")
    emblem.resize((256, 256), Image.LANCZOS).save(OUT / "emblem-256.png")
    emblem.resize((128, 128), Image.LANCZOS).save(OUT / "emblem-128.webp", "WEBP", quality=88, method=6)

    for name in ("favicon.png", "emblem-128.webp", "emblem-256.png"):
        report(name, OUT / name)


if __name__ == "__main__":
    main()