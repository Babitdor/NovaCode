"""Encode the generated light/dark hero PNGs without altering their artwork.

Run: python tools/make_hero_assets.py (requires Pillow).
The depth renderer uses the intact images, never a painted background/cutout.
"""

from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "assets"


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    for source, stem in (("dark.png", "hero"), ("light.png", "hero-light")):
        art = Image.open(OUT / "hero-source" / source).convert("RGB")
        w, h = art.size
        art.save(OUT / f"{stem}.webp", "WEBP", quality=88, method=6)
        art.resize((960, round(h * 960 / w)), Image.LANCZOS).save(
            OUT / f"{stem}-960.webp", "WEBP", quality=84, method=6)
        art.crop((300, 120, 790, 610)).resize((128, 128), Image.LANCZOS).save(
            OUT / f"avatar-{source.removesuffix('.png')}.webp", "WEBP", quality=88, method=6)
    art = Image.open(OUT / "hero-source" / "dark.png").convert("RGB")
    w, h = art.size

    band = round(w * 630 / 1200)  # height of a 1200:630 band at full width
    top = (h - band) // 2
    og = art.crop((0, top, w, top + band)).resize((1200, 630), Image.LANCZOS)
    og.save(OUT / "og.jpg", "JPEG", quality=88, optimize=True, progressive=True)

    for name in ("hero.webp", "hero-960.webp", "hero-light.webp", "hero-light-960.webp", "og.jpg"):
        print(f"{name:14s} {(OUT / name).stat().st_size / 1024:7.0f} KB")


if __name__ == "__main__":
    main()
