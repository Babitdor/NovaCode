"""Sixel lifecycle fixes for the Python 3.11-compatible textual-image release."""

from rich.control import Control
from rich.segment import Segment
from rich.style import Style
from textual.dom import NoScreen
from textual.geometry import Region
from textual.strip import Strip
from textual_image._pixeldata import PixelData
from textual_image._terminal import get_cell_size
from textual_image.widget import SixelImage
from textual_image.widget.sixel import _ImageSixelImpl


class _SixelStrip(Strip):
    """Keep graphics attached when Textual divides or crops a terminal row."""

    def __init__(self, strip, sixel, origin, start):
        super().__init__(strip, strip.cell_length)
        self._sixel = sixel
        self._origin = origin
        self._start = start

    def render(self, console):
        if not self.cell_length:
            return ""
        # Trailing zero-width Segments are discarded by Rich's Segment.divide.
        return (
            super().render(console)
            + Control.move_to(*self._origin).segment.text
            + self._sixel
            + Control.move_to(self._start + self.cell_length, self._origin[1]).segment.text
        )

    def divide(self, cuts):
        parts = []
        start = self._start
        for strip in super().divide(cuts):
            parts.append(_SixelStrip(strip, self._sixel, self._origin, start))
            start += strip.cell_length
        return parts

    def crop(self, start, end=None):
        strip = super().crop(start, end)
        return _SixelStrip(strip, self._sixel, self._origin, self._start + max(0, start))


class _AvatarSixelData(_ImageSixelImpl):
    """Render independently repaintable pixel bands rather than one trailing image."""

    def render_lines(self, crop):
        try:
            if self.image is None or not self.screen.is_active:
                return []
        except NoScreen:
            return []
        sizes = get_cell_size()
        background = self._get_background_rgba()
        key = (
            id(self.image),
            crop,
            self.region,
            self.content_size,
            sizes,
            background,
            self._sixel_options,
        )
        if key == getattr(self, "_avatar_cache_key", None):
            return self._avatar_strips
        pixels = self._scale_image(PixelData(self.image), sizes)
        _, color = self.background_colors
        clear = Strip([Segment(" " * crop.width, Style(bgcolor=color.rich_color))], crop.width)
        strips = []
        for row in range(crop.y, crop.bottom):
            band = self._crop_image(pixels, Region(crop.x, row, crop.width, 1), sizes)
            sixel = self._image_to_sixels(band.pil_image, self._sixel_options, background)
            origin = (self.region.x + crop.x, self.region.y + row)
            strips.append(_SixelStrip(clear, sixel, origin, origin[0]))
        self._avatar_cache_key, self._avatar_strips = key, strips
        return strips


class SixelAvatarImage(SixelImage, Renderable=SixelImage._Renderable):
    """Use the upstream encoder, crop and cache with corrected repaint behavior."""

    def compose(self):
        yield _AvatarSixelData(self.image, self._sixel_options)
