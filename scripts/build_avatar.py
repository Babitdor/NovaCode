"""Cut the Nova avatar frames out of a sprite sheet into the TUI's sprite sheet.

    python scripts/build_avatar.py assets/nova/NovaGirl-HD.png [--height 64]

The sheet must have a transparent background. Each state is a band of the sheet
(pixel box below); frames inside a band are found by their transparent gaps and
then downscaled so a standing frame is ``--height`` pixels tall. The frames go
to ``novacode_cli/assets/nova-avatar.png`` (full colour, one row per state) and
their layout to ``avatar_frames.py``. The TUI draws 2x4 pixels per terminal
cell (octants), or 2x2 per half cell where octants are missing (half-blocks).
"""

from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np
from PIL import Image

# Reference coordinates; uniformly scaled for higher-definition source sheets.
REFERENCE_SIZE = (1536, 1024)
# state -> (y0, y1, x0, x1, expected frame count) on the reference sheet.
BANDS = {
    "idle": (45, 215, 0, 650, 7),
    "walking": (45, 164, 650, 1536, 13),
    "walking-back": (167, 278, 650, 1536, 13),
    "running": (285, 435, 0, 422, 5),
    "sitting": (285, 440, 422, 790, 4),
    "sleeping": (310, 440, 890, 1536, 5),
    "cursor": (494, 647, 0, 950, 9),
    "thinking": (680, 845, 515, 945, 4),
    "talking": (690, 845, 950, 1536, 6),
    "happy": (495, 655, 955, 1536, 5),
    "confused": (690, 845, 0, 515, 5),
    "window": (849, 1024, 15, 730, 7),
    "playful": (880, 1024, 735, 1536, 8),
}
# Section labels that overlap a band (x0, y0, x1, y1); blanked before cutting.
LABELS = [
    (515, 650, 680, 690),
    (955, 650, 1115, 690),
    (15, 838, 285, 875),
    (752, 843, 985, 880),
]
STANDING_PX = 160  # source height of a standing frame, scaled to --height
# These bands were drawn at a smaller scale on the reference sheet.
SOURCE_STATURE = {"walking": 112, "walking-back": 112, "running": 140}
PACKAGE = Path(__file__).resolve().parents[1] / "novacode_cli"
OUT = PACKAGE / "tui" / "avatar_frames.py"
ATLAS = PACKAGE / "assets" / "nova-avatar.png"
SOLID = 100  # alpha above which a sheet pixel belongs to a sprite
OPAQUE = 128  # alpha at which a downscaled pixel is drawn
MIN_COLUMN = 2  # opaque pixels a column needs to count as part of a frame
CELL_W, CELL_H = 2, 4  # pixels per terminal cell (octants)


def _frames(
    alpha: np.ndarray, y0: int, y1: int, x0: int, x1: int, count: int
) -> list[tuple[int, int]]:
    """Split a band into ``count`` column ranges at its emptiest columns."""
    cols = alpha[y0:y1, x0:x1].sum(axis=0).astype(float)
    filled = np.flatnonzero(cols > MIN_COLUMN)
    lo, hi = int(filled[0]), int(filled[-1]) + 1
    cuts = [lo]
    for k in range(1, count):
        guess = lo + (hi - lo) * k // count
        win = max(8, (hi - lo) // count // 3)
        a, b = guess - win, guess + win
        cuts.append(a + int(np.argmin(cols[a:b])))
    cuts.append(hi)
    return [(x0 + cuts[i], x0 + cuts[i + 1]) for i in range(count)]


def _cut(sheet: Image.Image, scale: float) -> dict[str, list[Image.Image]]:
    """Extract reference-layout frames at any resolution onto one shared canvas."""
    source_scale = sheet.width / REFERENCE_SIZE[0]
    if sheet.width * REFERENCE_SIZE[1] != sheet.height * REFERENCE_SIZE[0]:
        msg = "Sprite sheet must preserve the reference's 3:2 aspect ratio"
        raise ValueError(msg)
    alpha = np.asarray(sheet)[..., 3] > SOLID
    out: dict[str, list[Image.Image]] = {}
    max_height = round(STANDING_PX * scale)
    max_height += -max_height % CELL_H
    for state, band in BANDS.items():
        y0, y1, x0, x1 = (round(v * source_scale) for v in band[:4])
        count = band[4]
        rows = np.flatnonzero(alpha[y0:y1, x0:x1].any(axis=1))
        top, bottom = y0 + int(rows[0]), y0 + int(rows[-1]) + 1
        frames = []
        frame_scale = scale * STANDING_PX / SOURCE_STATURE.get(state, STANDING_PX) / source_scale
        for fx0, fx1 in _frames(alpha, y0, y1, x0, x1, count):
            crop = sheet.crop((fx0, top, fx1, bottom))
            size = (
                max(1, round(crop.width * frame_scale)),
                max(1, round(crop.height * frame_scale)),
            )
            if size[1] > max_height:
                size = (max(1, round(size[0] * max_height / size[1])), max_height)
            frames.append(crop.resize(size, Image.Resampling.NEAREST))
        # Feet on the bottom row, frames centred, whole terminal cells.
        w = max(f.width for f in frames)
        h = max(f.height for f in frames)
        w += -w % CELL_W
        h += -h % CELL_H
        canvases = []
        for f in frames:
            c = Image.new("RGBA", (w, h))
            c.alpha_composite(f, ((w - f.width) // 2, h - f.height))
            canvases.append(c)
        out[state] = canvases
    out["walking"].extend(out.pop("walking-back"))
    # One canvas for the whole sheet: changing poses never changes the bounds.
    width = max(frame.width for frames in out.values() for frame in frames)
    height = max(frame.height for frames in out.values() for frame in frames)
    for state, frames in out.items():
        aligned = []
        for frame in frames:
            canvas = Image.new("RGBA", (width, height))
            canvas.alpha_composite(frame, ((width - frame.width) // 2, height - frame.height))
            aligned.append(canvas)
        out[state] = aligned
    return out


def main() -> None:
    """Build the avatar sprite sheet and its layout module from a sprite sheet."""
    ap = argparse.ArgumentParser()
    ap.add_argument("sheet")
    ap.add_argument("--height", type=int, default=64)
    args = ap.parse_args()

    sheet = Image.open(args.sheet).convert("RGBA")
    source_scale = sheet.width / REFERENCE_SIZE[0]
    for box in LABELS:
        sheet.paste((0, 0, 0, 0), tuple(round(v * source_scale) for v in box))
    frames = _cut(sheet, args.height / STANDING_PX)

    # One row of frames per state, in the order of COUNTS.
    width, height = next(iter(frames.values()))[0].size
    atlas = Image.new("RGBA", (width * max(map(len, frames.values())), height * len(frames)))
    for row, fs in enumerate(frames.values()):
        for col, frame in enumerate(fs):
            # Snap alpha so a half-covered edge pixel is either drawn or not.
            frame.putalpha(frame.getchannel("A").point(lambda a: 255 if a >= OPAQUE else 0))
            atlas.paste(frame, (col * width, row * height))
    # 256 colours with alpha: a fifth of the size, colour error invisible at this scale.
    atlas.quantize(256, method=Image.Quantize.FASTOCTREE).save(ATLAS, optimize=True)

    counts = {state: len(fs) for state, fs in frames.items()}
    OUT.write_text(
        '"""Nova avatar frame layout. Generated by scripts/build_avatar.py; do not edit.\n\n'
        "The pixels are in ``novacode_cli/assets/nova-avatar.png``: one row of\n"
        'FRAME_SIZE frames per state, in the order of COUNTS.\n"""\n\n'
        f"FRAME_SIZE = {(width, height)!r}\n"
        "COUNTS = {\n" + "".join(f'    "{k}": {v},\n' for k, v in counts.items()) + "}\n",
        encoding="utf-8",
    )
    print(f"wrote {ATLAS} and {OUT} ({sum(counts.values())} frames)")  # noqa: T201


if __name__ == "__main__":
    main()
