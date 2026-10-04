"""Derive the landing page's hero art from the repo's assets/Landing.png.

Produces (into ../assets):
  hero.webp      - full 1672px hero, ~10x smaller than the 2 MB source PNG
  hero-960.webp  - the same art for phones
  og.jpg         - 1200x630 social card (replaces the emblem-only og.png)

The social crop keeps the full width and trims height evenly from top and
bottom: the wordmark sits at ~45% of the height, so a centred band keeps both
it and the face.

Run with the repo venv's Python (needs Pillow):
    .venv\\Scripts\\python.exe landing\\tools\\make_hero_assets.py
"""

from pathlib import Path

from PIL import Image

REPO = Path(__file__).resolve().parents[2]
SRC = REPO / "assets" / "Landing.png"
OUT = Path(__file__).resolve().parent.parent / "assets"


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    art = Image.open(SRC).convert("RGB")
    w, h = art.size

    art.save(OUT / "hero.webp", "WEBP", quality=84, method=6)
    small = art.resize((960, round(h * 960 / w)), Image.LANCZOS)
    small.save(OUT / "hero-960.webp", "WEBP", quality=80, method=6)

    band = round(w * 630 / 1200)  # height of a 1200:630 band at full width
    top = (h - band) // 2
    og = art.crop((0, top, w, top + band)).resize((1200, 630), Image.LANCZOS)
    og.save(OUT / "og.jpg", "JPEG", quality=88, optimize=True, progressive=True)

    for name in ("hero.webp", "hero-960.webp", "og.jpg"):
        print(f"{name:14s} {(OUT / name).stat().st_size / 1024:7.0f} KB")


if __name__ == "__main__":
    main()
