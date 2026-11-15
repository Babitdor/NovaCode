"""High-definition source sheets keep the compact avatar's frame contract."""

from __future__ import annotations

import pytest
from PIL import Image, ImageDraw

from scripts.build_avatar import BANDS, REFERENCE_SIZE, SIMPLE_STATES, _cut, _cut_grid


def test_hd_sheet_keeps_frame_counts_and_shared_bounds():
    sheet = Image.new("RGBA", REFERENCE_SIZE)
    draw = ImageDraw.Draw(sheet)
    for y0, y1, x0, x1, count in BANDS.values():
        step = (x1 - x0) / count
        for index in range(count):
            left = round(x0 + step * (index + 0.3))
            right = round(x0 + step * (index + 0.7))
            draw.rectangle((left, y0 + 10, right, y1 - 10), fill=(80, 40, 20, 255))
    original = _cut(sheet, 24 / 160)
    hd = _cut(sheet.resize((3072, 2048), Image.Resampling.NEAREST), 24 / 160)
    assert set(original) == set(hd)
    assert {key: len(value) for key, value in original.items()} == {
        key: len(value) for key, value in hd.items()
    }
    assert sum(map(len, hd.values())) == 91
    bounds = {frame.size for frames in hd.values() for frame in frames}
    assert len(bounds) == 1
    assert bounds == {frame.size for frames in original.values() for frame in frames}
    assert all(frame.getbbox() for frames in hd.values() for frame in frames)


def test_sheet_rejects_changed_layout_aspect_ratio():
    with pytest.raises(ValueError, match="aspect ratio"):
        _cut(Image.new("RGBA", (1024, 1024)), 24 / 160)


def test_simple_grid_keeps_pairs_in_state_order_and_transparent_bounds():
    sheet = Image.new("RGBA", (128, 192))
    draw = ImageDraw.Draw(sheet)
    for index in range(24):
        x, y = index % 4 * 32, index // 4 * 32
        draw.rectangle((x + 8, y + 4, x + 24, y + 28), fill=(index * 10, 80, 40, 255))
    frames = _cut_grid(sheet, 40)
    assert tuple(frames) == SIMPLE_STATES
    for index, pair in enumerate(frames.values()):
        assert len(pair) == 2
        for step, frame in enumerate(pair):
            assert frame.size == (40, 40)
            assert frame.getpixel((20, 20)) == ((index * 2 + step) * 10, 80, 40, 255)
            assert frame.getpixel((0, 0))[3] == 0
    sheet.paste((0, 0, 0, 0), (0, 0, 32, 32))
    with pytest.raises(ValueError, match="Empty sprite: idle frame 0"):
        _cut_grid(sheet, 40)


def test_simple_grid_rejects_non_square_cells():
    with pytest.raises(ValueError, match="square cells"):
        _cut_grid(Image.new("RGBA", (1024, 1024)), 40)


def test_simple_grid_downsampling_does_not_alias_fine_details():
    sheet = Image.new("RGBA", (128, 192), (0, 0, 0, 255))
    draw = ImageDraw.Draw(sheet)
    for column in range(0, sheet.width, 2):
        draw.line((column, 0, column, sheet.height - 1), fill=(255, 255, 255, 255))
    frame = _cut_grid(sheet, 8)["idle"][0]
    # Fine light/dark detail should converge to its tone, not disappear into one color.
    assert all(120 <= frame.getpixel((x, 4))[0] <= 135 for x in range(1, 7))
