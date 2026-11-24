"""Validated image attachments shared by remote sessions."""

from __future__ import annotations

import base64
import io
import re
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import unquote, urlsplit

from PIL import Image

from novacode_cli.image_utils import MAX_IMAGE_DIMENSION, MAX_IMAGE_SIZE_BYTES, ImageData

if TYPE_CHECKING:
    from novacode_cli.input_utils import ImageTracker


def resolve_workspace_image(source: str, workspace_root: str | None) -> Path | None:
    """Map an agent-side image path to a real file inside the workspace.

    Accepts the paths an agent reads — virtual roots such as ``/.shots/x.png``
    map to the workspace root, relative paths join it, and absolute local paths
    are kept only when they stay inside the workspace. URLs and anything
    outside the workspace (or not an image file) return ``None``.
    """
    if not source or not workspace_root:
        return None
    root = Path(workspace_root).expanduser().resolve()
    windows_absolute = bool(re.match(r"^[A-Za-z]:[\\/]", source))
    parsed = urlsplit(source)
    if (parsed.scheme and not windows_absolute) or parsed.netloc:
        return None
    raw_path = unquote(source if windows_absolute else parsed.path).strip()
    if not raw_path:
        return None
    candidate = Path(raw_path)
    if raw_path.startswith(("/", "\\")) and not candidate.is_relative_to(root):
        candidate = root / raw_path.lstrip("/\\")
    elif not candidate.is_absolute():
        candidate = root / candidate
    try:
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(root)
    except (OSError, ValueError):
        return None
    from novacode_cli.image_utils import is_image_file

    if not resolved.is_file() or not is_image_file(resolved):
        return None
    return resolved


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


def prepare_telegram_photo(path: Path) -> tuple[bytes, str, str]:
    """Convert an image to a Telegram-compatible JPEG photo upload."""
    if path.stat().st_size > MAX_IMAGE_SIZE_BYTES:
        raise ValueError("Images larger than 20 MB cannot be previewed in Telegram.")
    try:
        with Image.open(path) as source:
            if source.width < 1 or source.height < 1:
                raise ValueError("The image has invalid dimensions.")
            if source.width * source.height > MAX_IMAGE_DIMENSION**2:
                raise ValueError("The image dimensions exceed the supported limit.")
            source.load()
            # Telegram's photo endpoint limits width + height to 10,000 px.
            scale = min(
                1.0,
                MAX_IMAGE_DIMENSION / max(source.size),
                9999 / sum(source.size),
            )
            if scale < 1:
                source.thumbnail(
                    (max(1, int(source.width * scale)), max(1, int(source.height * scale))),
                    Image.Resampling.LANCZOS,
                )
            rgba = source.convert("RGBA")
            image = Image.new("RGB", rgba.size, "white")
            image.paste(rgba, mask=rgba.getchannel("A"))

            # Leave headroom below Telegram's 10 MB photo limit.
            for quality in (90, 82, 74, 66, 58):
                output = io.BytesIO()
                image.save(output, format="JPEG", quality=quality, optimize=True)
                if output.tell() <= 9_500_000:
                    return output.getvalue(), f"{path.stem}.jpg", "image/jpeg"
            image.thumbnail((3000, 3000), Image.Resampling.LANCZOS)
            output = io.BytesIO()
            image.save(output, format="JPEG", quality=58, optimize=True)
            if output.tell() <= 9_500_000:
                return output.getvalue(), f"{path.stem}.jpg", "image/jpeg"
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError("The image could not be prepared for Telegram.") from exc
    raise ValueError("The image is too large to preview in Telegram.")
