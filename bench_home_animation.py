"""rust-anything / fast-anything workload: the home-screen animations.

Drives the two animators that run while Nova's home screen is visible:

- ``Matrix3`` — the "signal" style code rain: one 80 ms simulation step
  (column advance, glyph mutation, full-buffer repaint) plus the per-frame
  ``draw`` copy into the widget's line/style buffers, on a 194x30 grid.
- ``scene_frame`` — the terminal/studio/blueprint scenes at 6 fps.

Seeded: Matrix3's RNG is reseeded deterministically, and the scene frames run
over a fixed base art, so runs are comparable.
"""

import random
import time

from rich.text import Text

from novacode_cli.tui.home_scene import scene_frame
from novacode_cli.tui.matrix3 import Matrix3

WIDTH, HEIGHT = 194, 30
FRAMES = 900  # ~15 fps x 60 s of animation work

BASE_ART = Text("\n".join(f"Nova line {n}: " + "prototype " * 4 for n in range(14)))


class _Palette:
    accent = "#7aa2f7"
    text = "#c0caf5"


def main() -> None:
    engine = Matrix3(WIDTH, HEIGHT)
    engine._random.seed(2026)  # noqa: SLF001 -- determinism for the workload
    rng = random.Random(7)
    palette = tuple(object() for _ in range(16))  # opaque stand-ins: identity-mapped
    lines = [[" "] * WIDTH for _ in range(HEIGHT)]
    styles = [[None] * WIDTH for _ in range(HEIGHT)]

    start = time.perf_counter()
    steps = 0
    for frame in range(FRAMES):
        engine.draw(lines, styles, palette, 15)
        steps += 1
        if frame % 100 == 0:
            engine._random.seed(2026 + frame)  # noqa: SLF001
    elapsed_matrix = time.perf_counter() - start

    start = time.perf_counter()
    for frame in range(600):  # 6 fps x 100 s
        scene_frame("terminal", BASE_ART, _Palette(), frame)  # type: ignore[arg-type]
        scene_frame("studio", BASE_ART, _Palette(), frame)  # type: ignore[arg-type]
        scene_frame("blueprint", BASE_ART, _Palette(), frame)  # type: ignore[arg-type]
        rng.random()
    elapsed_scene = time.perf_counter() - start

    print(
        f"home animation workload: {steps} matrix steps, matrix {elapsed_matrix:.3f}s, scenes {elapsed_scene:.3f}s"
    )


main()
