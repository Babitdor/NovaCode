"""Sixel lifecycle fixes for the Python 3.11-compatible textual-image release."""

from rich.control import Control
from rich.segment import Segment
from rich.style import Style
from textual.strip import Strip
from textual_image.widget import SixelImage
from textual_image.widget.sixel import _ImageSixelImpl


class _AvatarSixelData(_ImageSixelImpl):
    """Clear old animation pixels and leave the cursor on the strip's current row."""

    def render_lines(self, crop):
        lines = super().render_lines(crop)
        if not lines:
            return lines
        _, color = self.background_colors
        clear = Segment(" " * crop.width, Style(bgcolor=color.rich_color))
        # 0.12 omits blank cells, leaving previous frames visible through transparent pixels.
        return [Strip([clear], cell_length=crop.width) for _ in lines[:-1]] + [
            Strip([clear, *lines[-1]], cell_length=crop.width)
        ]

    def _get_sixel_segments(self, sixel_data):
        segments = list(super()._get_sixel_segments(sixel_data))
        visible = self.screen.find_widget(self).visible_region
        # 0.12 restores one row too low, shifting any following widgets on that line.
        segments[-1] = Segment(Control.move_to(visible.right, visible.bottom - 1).segment.text)
        return segments


class SixelAvatarImage(SixelImage, Renderable=SixelImage._Renderable):
    """Use the upstream encoder, crop and cache with corrected repaint behavior."""

    def compose(self):
        yield _AvatarSixelData(self.image, self._sixel_options)
