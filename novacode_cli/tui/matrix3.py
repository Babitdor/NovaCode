"""Textual-safe adaptation of the supplied matrix3.py code-rain animation.

Original animation Copyright (C) 2022 Nik Stromberg. This port keeps its 90%
density, weighted speeds, 80 ms steps, character mutation and quadratic fade,
without terminal writes, input polling, sleeps or terminal-mode changes.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from rich.style import Style

_STEP_SECONDS = 0.08
_DENSITY = 0.9
_MUTATION_RATE = 0.002
# "Love of my life": each character occupies two terminal cells.
_CHARACTERS = "生涯最愛の人"


@dataclass
class _Column:
    x: int
    head: int
    speed: int
    chain: list[str]


class Matrix3:
    """Keep bounded live-chain buffers independently of the terminal driver."""

    def __init__(self, width: int, height: int) -> None:
        """Seed visible chains immediately rather than waiting for an empty intro."""
        self.width, self.height = width, height
        self._random = random.Random()  # noqa: S311 -- decorative animation.
        self._elapsed = 0.0
        self._glyphs = [[" "] * width for _ in range(height)]
        self._levels = [[-1] * width for _ in range(height)]
        self._slots = tuple(range(0, max(0, width - 1), 2))
        self._columns = [
            self._column(x, visible=True)
            for x in self._random.sample(self._slots, int(len(self._slots) * _DENSITY))
        ]
        self._paint_chains()

    def _column(self, x: int, *, visible: bool = False) -> _Column:
        length = self._random.randint(min(4, self.height), self.height)
        return _Column(
            x,
            self._random.randint(-self.height, self.height if visible else 0),
            self._random.choices((1, 2, 3), cum_weights=(1, 1.4, 1.42))[0],
            self._random.choices(_CHARACTERS, k=length),
        )

    def _paint_chains(self) -> None:
        # Erase the previous frame so departed chains leave no shadow or ghost text.
        for row in range(self.height):
            self._glyphs[row][:] = [" "] * self.width
            self._levels[row][:] = [-1] * self.width
        for column in self._columns:
            for distance, glyph in enumerate(column.chain):
                row = column.head - distance
                if 0 <= row < self.height:
                    self._glyphs[row][column.x] = glyph
                    self._glyphs[row][column.x + 1] = ""
                    # Level zero is the bright, unsaturated head; other levels
                    # use the source's 1 - (distance / length) ** 2 fade.
                    self._levels[row][column.x] = (
                        0 if distance == 0 else 1 + round(15 * (distance / len(column.chain)) ** 2)
                    )
                    self._levels[row][column.x + 1] = self._levels[row][column.x]

    def _step(self) -> None:
        occupied = {column.x for column in self._columns}
        for index, column in enumerate(self._columns):
            for _ in range(column.speed):
                column.chain.insert(0, self._random.choice(_CHARACTERS))
                column.chain.pop()
            for position in range(len(column.chain)):
                if self._random.random() < _MUTATION_RATE:
                    column.chain[position] = self._random.choice(_CHARACTERS)
            column.head += column.speed
            if column.head - len(column.chain) >= self.height:
                occupied.remove(column.x)
                available = sorted(set(self._slots) - occupied)
                replacement = self._column(self._random.choice(available))
                occupied.add(replacement.x)
                self._columns[index] = replacement
        self._paint_chains()

    def draw(
        self,
        lines: list[list[str]],
        styles: list[list[Style | None]],
        palette: tuple[Style, ...],
        fps: int,
    ) -> None:
        """Advance at the source rate and map live chains to the active theme."""
        self._elapsed += 1 / fps
        while self._elapsed + 1e-9 >= _STEP_SECONDS:
            self._elapsed -= _STEP_SECONDS
            self._step()
        for row in range(self.height):
            lines[row][:] = self._glyphs[row]
            styles[row][:] = [palette[level] if level >= 0 else None for level in self._levels[row]]
