"""Encode aligned image planes and their neutral composite for the hero.

Run: python tools/make_hero_assets.py (requires Pillow).
Use --ink to build the new character planes and matching composites without
overwriting the original artwork or its social assets.
The browser translates complete planes; no depth map or pixel displacement.
"""

import argparse
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "assets"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ink", action="store_true", help="build the inked character variant")
    ink = parser.parse_args().ink
    OUT.mkdir(parents=True, exist_ok=True)
    layers_out = OUT / "hero-layers"
    layers_out.mkdir(exist_ok=True)
    scenes = {}
    for theme, stem in (("dark", "hero"), ("light", "hero-light")):
        if ink:
            stem += "-ink"
        scene = Image.new("RGBA", (1672, 941))
        for part in ("sky", "room", "subject", "plants"):
            layer_name = f"{theme}-{part}" + ("-ink" if ink and part == "subject" else "")
            path = OUT / "hero-source" / "layers" / f"{layer_name}.png"
            layer = Image.open(path)
            # Generated planes may be one or two pixels narrower; pad without stretching.
            if layer.size in ((1670, 941), (1671, 941)):
                layer = layer.crop((0, 0, 1672, 941))
            if layer.size != scene.size:
                raise ValueError(f"{path}: expected full aligned canvas {scene.size}, got {layer.size}")
            if part != "sky" and (layer.mode != "RGBA" or layer.getchannel("A").getextrema()[0] != 0
                                  or layer.getchannel("A").getextrema()[1] < 128):
                raise ValueError(f"{path}: a real transparent cutout is required")
            scene = Image.alpha_composite(scene, layer.convert("RGBA"))
            if not ink or part == "subject":
                layer.save(layers_out / f"{layer_name}.webp", "WEBP", quality=86, method=6)
                layer.resize((960, 540), Image.LANCZOS).save(
                    layers_out / f"{layer_name}-960.webp", "WEBP", quality=82, method=6)
        art = scene.convert("RGB")
        scenes[theme] = art
        w, h = art.size
        art.save(OUT / f"{stem}.webp", "WEBP", quality=88, method=6)
        art.resize((960, round(h * 960 / w)), Image.LANCZOS).save(
            OUT / f"{stem}-960.webp", "WEBP", quality=84, method=6)
        if not ink:
            art.crop((300, 120, 790, 610)).resize((128, 128), Image.LANCZOS).save(
                OUT / f"avatar-{theme}.webp", "WEBP", quality=88, method=6)
    if ink:
        for name in ("hero-ink.webp", "hero-ink-960.webp", "hero-light-ink.webp", "hero-light-ink-960.webp"):
            print(f"{name:24s} {(OUT / name).stat().st_size / 1024:7.0f} KB")
        return
    art = scenes["dark"]
    w, h = art.size

    band = round(w * 630 / 1200)  # height of a 1200:630 band at full width
    top = (h - band) // 2
    og = art.crop((0, top, w, top + band)).resize((1200, 630), Image.LANCZOS)
    og.save(OUT / "og.jpg", "JPEG", quality=88, optimize=True, progressive=True)

    for name in ("hero.webp", "hero-960.webp", "hero-light.webp", "hero-light-960.webp", "og.jpg"):
        print(f"{name:14s} {(OUT / name).stat().st_size / 1024:7.0f} KB")


if __name__ == "__main__":
    main()
