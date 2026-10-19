"""High-definition source sheets keep the compact avatar's frame contract."""

from __future__ import annotations

import pytest
from PIL import Image, ImageDraw

from scripts.build_avatar import BANDS, REFERENCE_SIZE, _cut


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
