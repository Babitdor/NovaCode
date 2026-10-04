"""Crop the real assets/Preview.png into the hero's product screenshot.

The full 1920x1032 capture is far too dense to read at hero scale, so we take
the bottom-left region: the condensed tool-call group, the todo checklist, and
the prompt input. The right edge is excluded because the status tooltip there
is garbled and truncated in the source capture.

Run with the repo venv's Python (needs Pillow):
    .venv\\Scripts\\python.exe landing\\tools\\make_session_crops.py
"""

from pathlib import Path

from PIL import Image

REPO = Path(__file__).resolve().parents[2]
SRC = REPO / "assets" / "Preview.png"
OUT = Path(__file__).resolve().parent.parent / "assets"

# (left, upper, right, lower)
HERO_BOX = (0, 596, 1160, 1000)


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    hero = Image.open(SRC).convert("RGB").crop(HERO_BOX)
    target = OUT / "session-hero.png"
    hero.save(target, optimize=True)
    print(f"session-hero.png  {hero.size}  {target.stat().st_size:,} bytes")


if __name__ == "__main__":
    main()