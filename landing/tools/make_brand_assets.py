"""Derive the landing page's brand assets from the repo's assets/Nova.png.

Produces (into ../assets):
  favicon.png     - 64px emblem
  emblem-256.png  - 256px emblem for the nav lockup
  og.png          - 1200x630 social card

Crop boxes were measured by scanning the source for non-black pixels rather
than guessed: the emblem circle occupies x 62..751, and the wordmark
x 809..1907, y 270..537 in the 1954x805 original.

Run with the repo venv's Python (needs Pillow):
    .venv\\Scripts\\python.exe landing\\tools\\make_brand_assets.py
"""

from pathlib import Path

from PIL import Image

REPO = Path(__file__).resolve().parents[2]
SRC = REPO / "assets" / "Nova.png"
OUT = Path(__file__).resolve().parent.parent / "assets"

EMBLEM_BOX = (62, 58, 752, 740)
WORD_BOX = (809, 270, 1908, 538)
INK = (10, 13, 28)


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    src = Image.open(SRC).convert("RGB")
    emblem = src.crop(EMBLEM_BOX)

    emblem.resize((64, 64), Image.LANCZOS).save(OUT / "favicon.png")
    emblem.resize((256, 256), Image.LANCZOS).save(OUT / "emblem-256.png")

    # Social card: emblem left, wordmark right, on the brand night-sky ink.
    card = Image.new("RGB", (1200, 630), INK)
    card.paste(emblem.resize((400, 400), Image.LANCZOS), (90, 115))
    card.paste(src.crop(WORD_BOX).resize((740, 184), Image.LANCZOS), (460, 223))
    card.save(OUT / "og.png", optimize=True)

    for name in ("favicon.png", "emblem-256.png", "og.png"):
        path = OUT / name
        print(f"{name:18} {str(Image.open(path).size):14} {path.stat().st_size:>8,} bytes")


if __name__ == "__main__":
    main()