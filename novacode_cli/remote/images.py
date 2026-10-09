"""Validated image attachments shared by remote sessions."""

from __future__ import annotations

import base64
import io
from typing import TYPE_CHECKING

from PIL import Image

from novacode_cli.image_utils import MAX_IMAGE_DIMENSION, MAX_IMAGE_SIZE_BYTES, ImageData

if TYPE_CHECKING:
    from novacode_cli.input_utils import ImageTracker


def decode_image(data: bytes) -> ImageData:
    """Validate downloaded bytes and normalize the first frame to PNG."""
    if not data or len(data) > MAX_IMAGE_SIZE_BYTES:
        raise ValueError("Images must be nonempty and no larger than 20 MB.")
    try:
        with Image.open(io.BytesIO(data)) as source:
            if max(source.size) > MAX_IMAGE_DIMENSION:
                raise ValueError(f"Image dimensions must not exceed {MAX_IMAGE_DIMENSION} pixels.")
            source.load()
            rgba = source.convert("RGBA")
            image = Image.new("RGB", source.size, "white")
            image.paste(rgba, mask=rgba.getchannel("A"))
            output = io.BytesIO()
            image.save(output, format="PNG")
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError("This file could not be decoded as an image.") from exc
    normalized = output.getvalue()
    if len(normalized) > MAX_IMAGE_SIZE_BYTES:
        raise ValueError("The decoded image exceeds 20 MB. Please send a smaller image.")
    return ImageData(base64.b64encode(normalized).decode("ascii"), "png", "[image]")


def attach_images(
    tracker: ImageTracker | None, images: list[ImageData] | None
) -> ImageTracker | None:
    """Return a conversation tracker containing the supplied attachments."""
    from novacode_cli.input_utils import ImageTracker

    if images:
        if tracker is None:
            tracker = ImageTracker()
        for image in images:
            tracker.add_image(image)
    return tracker
