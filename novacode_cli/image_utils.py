"""Utilities for handling image paste from clipboard and file loading."""

import base64
import io
import os
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

from PIL import Image

# Constants for image validation
MAX_IMAGE_SIZE_BYTES = 20 * 1024 * 1024  # 20MB
MAX_IMAGE_DIMENSION = 7680  # Max width/height
SUPPORTED_FORMATS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tiff", ".tif"}


@dataclass
class ImageData:
    """Represents a pasted image with its base64 encoding."""

    base64_data: str
    format: str  # "png", "jpeg", etc.
    placeholder: str  # Display text like "[image 1]"

    def to_data_url(self) -> str:
        """The image as a ``data:<mime>;base64,<…>`` URL.

        This is the form multimodal chat APIs accept for an ``image_url``
        content part, and the form :func:`~novacode_cli.bootstrap.vision_router.
        caption_images` expects — it takes URL strings, not :class:`ImageData`
        objects.
        """
        return f"data:image/{self.format};base64,{self.base64_data}"

    def to_message_content(self) -> dict:
        """Convert to LangChain message content format.

        Returns:
            Dict with type and image_url for multimodal messages
        """
        return {
            "type": "image_url",
            "image_url": {"url": self.to_data_url()},
        }

    @property
    def size_kb(self) -> float:
        """Get approximate size in KB."""
        return len(self.base64_data) * 3 / 4 / 1024  # Base64 is ~4/3 of original


def get_clipboard_image() -> ImageData | None:
    """Attempt to read an image from the system clipboard.

    Supports macOS, Windows, and Linux (Wayland/X11).

    Returns:
        ImageData if an image is found, None otherwise
    """
    if sys.platform == "darwin":
        return _get_macos_clipboard_image()
    if sys.platform == "win32":
        return _get_windows_clipboard_image()
    if sys.platform.startswith("linux"):
        return _get_linux_clipboard_image()
    return None


def _get_windows_clipboard_image() -> ImageData | None:
    """Get clipboard image on Windows using PIL.ImageGrab.

    Returns:
        ImageData if an image is found, None otherwise
    """
    try:
        from PIL import ImageGrab

        img = ImageGrab.grabclipboard()
        if img is None:
            return None

        # ImageGrab can return a list of file paths or an Image
        if isinstance(img, list):
            # It's a list of file paths - try to load the first image
            for file_path in img:
                if Path(file_path).suffix.lower() in SUPPORTED_FORMATS:
                    try:
                        return load_image_from_path(Path(file_path))
                    except (FileNotFoundError, ValueError):
                        continue
            return None

        if isinstance(img, Image.Image):
            # Convert to PNG and encode
            buffer = io.BytesIO()
            # Convert to RGB if necessary (handle RGBA, palette modes)
            if img.mode in ("RGBA", "LA", "P"):
                # Create white background for transparency
                background = Image.new("RGB", img.size, (255, 255, 255))
                if img.mode == "P":
                    img = img.convert("RGBA")
                if img.mode in ("RGBA", "LA"):
                    background.paste(img, mask=img.split()[-1])
                    img = background
            elif img.mode != "RGB":
                img = img.convert("RGB")

            img.save(buffer, format="PNG")
            base64_data = base64.b64encode(buffer.getvalue()).decode("utf-8")
            return ImageData(
                base64_data=base64_data,
                format="png",
                placeholder="[image]",
            )
    except ImportError:
        # PIL.ImageGrab not available
        pass
    except Exception:
        pass
    return None


def _get_linux_clipboard_image() -> ImageData | None:
    """Get clipboard image on Linux using wl-paste (Wayland) or xclip (X11).

    Returns:
        ImageData if an image is found, None otherwise
    """
    # Try Wayland first (wl-paste)
    try:
        result = subprocess.run(
            ["wl-paste", "--type", "image/png"],
            capture_output=True,
            check=False,
            timeout=2,
        )
        if result.returncode == 0 and result.stdout:
            try:
                # Validate it's a real image
                Image.open(io.BytesIO(result.stdout))
                base64_data = base64.b64encode(result.stdout).decode("utf-8")
                return ImageData(
                    base64_data=base64_data,
                    format="png",
                    placeholder="[image]",
                )
            except Exception:
                pass
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass

    # Try X11 (xclip)
    try:
        result = subprocess.run(
            ["xclip", "-selection", "clipboard", "-t", "image/png", "-o"],
            capture_output=True,
            check=False,
            timeout=2,
        )
        if result.returncode == 0 and result.stdout:
            try:
                Image.open(io.BytesIO(result.stdout))
                base64_data = base64.b64encode(result.stdout).decode("utf-8")
                return ImageData(
                    base64_data=base64_data,
                    format="png",
                    placeholder="[image]",
                )
            except Exception:
                pass
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass

    return None


def _get_macos_clipboard_image() -> ImageData | None:
    """Get clipboard image on macOS using pngpaste or osascript.

    First tries pngpaste (faster if installed), then falls back to osascript.

    Returns:
        ImageData if an image is found, None otherwise
    """
    # Try pngpaste first (fast if installed)
    try:
        result = subprocess.run(
            ["pngpaste", "-"],
            capture_output=True,
            check=False,
            timeout=2,
        )
        if result.returncode == 0 and result.stdout:
            # Successfully got PNG data
            try:
                Image.open(io.BytesIO(result.stdout))  # Validate it's a real image
                base64_data = base64.b64encode(result.stdout).decode("utf-8")
                return ImageData(
                    base64_data=base64_data,
                    format="png",  # 'pngpaste -' always outputs PNG
                    placeholder="[image]",
                )
            except Exception:
                pass  # Invalid image data
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass  # pngpaste not installed or timed out

    # Fallback to osascript with temp file (built-in but slower)
    return _get_clipboard_via_osascript()


def _get_clipboard_via_osascript() -> ImageData | None:
    """Get clipboard image via osascript using a temp file.

    osascript outputs data in a special format that can't be captured as raw binary,
    so we write to a temp file instead.

    Returns:
        ImageData if an image is found, None otherwise
    """
    # Create a temp file for the image
    fd, temp_path = tempfile.mkstemp(suffix=".png")
    os.close(fd)

    try:
        # First check if clipboard has PNG data
        check_result = subprocess.run(
            ["osascript", "-e", "clipboard info"],
            capture_output=True,
            check=False,
            timeout=2,
            text=True,
        )

        if check_result.returncode != 0:
            return None

        # Check for PNG or TIFF in clipboard info
        clipboard_info = check_result.stdout.lower()
        if "pngf" not in clipboard_info and "tiff" not in clipboard_info:
            return None

        # Try to get PNG first, fall back to TIFF
        if "pngf" in clipboard_info:
            get_script = f"""
            set pngData to the clipboard as «class PNGf»
            set theFile to open for access POSIX file "{temp_path}" with write permission
            write pngData to theFile
            close access theFile
            return "success"
            """
        else:
            get_script = f"""
            set tiffData to the clipboard as TIFF picture
            set theFile to open for access POSIX file "{temp_path}" with write permission
            write tiffData to theFile
            close access theFile
            return "success"
            """

        result = subprocess.run(
            ["osascript", "-e", get_script],
            capture_output=True,
            check=False,
            timeout=3,
            text=True,
        )

        if result.returncode != 0 or "success" not in result.stdout:
            return None

        # Check if file was created and has content
        if not os.path.exists(temp_path) or os.path.getsize(temp_path) == 0:
            return None

        # Read and validate the image
        with open(temp_path, "rb") as f:
            image_data = f.read()

        try:
            image = Image.open(io.BytesIO(image_data))
            # Convert to PNG if it's not already (e.g., if we got TIFF)
            buffer = io.BytesIO()
            image.save(buffer, format="PNG")
            buffer.seek(0)
            base64_data = base64.b64encode(buffer.getvalue()).decode("utf-8")

            return ImageData(
                base64_data=base64_data,
                format="png",
                placeholder="[image]",
            )
        except Exception:
            return None

    except (subprocess.TimeoutExpired, OSError):
        return None
    finally:
        # Clean up temp file
        try:
            os.unlink(temp_path)
        except OSError:
            pass


def encode_image_to_base64(image_bytes: bytes) -> str:
    """Encode image bytes to base64 string.

    Args:
        image_bytes: Raw image bytes

    Returns:
        Base64-encoded string
    """
    return base64.b64encode(image_bytes).decode("utf-8")


def create_multimodal_content(text: str, images: list[ImageData]) -> list[dict]:
    """Create multimodal message content with text and images.

    Args:
        text: Text content of the message
        images: List of ImageData objects

    Returns:
        List of content blocks in LangChain format
    """
    content_blocks = []

    # Add text block
    if text.strip():
        content_blocks.append({"type": "text", "text": text})

    # Add image blocks
    for image in images:
        content_blocks.append(image.to_message_content())

    return content_blocks


def load_image_from_path(image_path: Path) -> ImageData:
    """Load an image from a file path.

    Args:
        image_path: Path to the image file

    Returns:
        ImageData with base64 encoding

    Raises:
        FileNotFoundError: If file doesn't exist
        ValueError: If file is too large, invalid format, or invalid image
    """
    if not image_path.exists():
        raise FileNotFoundError(f"Image file not found: {image_path}")

    # Check file extension
    suffix = image_path.suffix.lower()
    if suffix not in SUPPORTED_FORMATS:
        raise ValueError(
            f"Unsupported image format: {suffix}. Supported: {', '.join(sorted(SUPPORTED_FORMATS))}"
        )

    # Check file size
    file_size = image_path.stat().st_size
    if file_size > MAX_IMAGE_SIZE_BYTES:
        raise ValueError(
            f"Image too large ({file_size / 1024 / 1024:.1f}MB). "
            f"Maximum size is {MAX_IMAGE_SIZE_BYTES / 1024 / 1024:.0f}MB"
        )

    # Load and validate image
    try:
        image = Image.open(image_path)
    except Exception as e:
        raise ValueError(f"Invalid image file: {e}") from e

    # Check dimensions
    width, height = image.size
    if width > MAX_IMAGE_DIMENSION or height > MAX_IMAGE_DIMENSION:
        raise ValueError(
            f"Image dimensions too large: {width}x{height}. "
            f"Maximum dimension is {MAX_IMAGE_DIMENSION}px"
        )

    # Convert to RGB if necessary and save as PNG
    if image.mode in ("RGBA", "LA", "P"):
        background = Image.new("RGB", image.size, (255, 255, 255))
        if image.mode == "P":
            image = image.convert("RGBA")
        if image.mode in ("RGBA", "LA"):
            background.paste(image, mask=image.split()[-1])
            image = background
    elif image.mode != "RGB":
        image = image.convert("RGB")

    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    base64_data = base64.b64encode(buffer.getvalue()).decode("utf-8")

    return ImageData(
        base64_data=base64_data,
        format="png",
        placeholder=f"[image {image_path.name}]",
    )


def is_image_file(path: Path) -> bool:
    """Check if a path points to a supported image file.

    Args:
        path: Path to check

    Returns:
        True if path is a supported image file
    """
    return path.is_file() and path.suffix.lower() in SUPPORTED_FORMATS


class ImageProcessor:
    """Handle image preprocessing for optimal API usage."""

    MAX_DIMENSION = 4096  # Most vision APIs support up to 4096x4096
    TARGET_QUALITY = 85  # JPEG quality
    MAX_FILE_SIZE_BYTES = 5 * 1024 * 1024  # 5MB target for optimized images

    @staticmethod
    def optimize_image(
        image: Image.Image,
        output_format: str = "jpeg",
    ) -> bytes:
        """Optimize an image for API transmission.

        Args:
            image: PIL Image object
            output_format: Output format ("jpeg" or "png")

        Returns:
            Optimized image bytes
        """
        # Resize if too large
        width, height = image.size
        if width > ImageProcessor.MAX_DIMENSION or height > ImageProcessor.MAX_DIMENSION:
            image.thumbnail(
                (ImageProcessor.MAX_DIMENSION, ImageProcessor.MAX_DIMENSION),
                Image.Resampling.LANCZOS,
            )

        # Convert to RGB if saving as JPEG
        if output_format == "jpeg" and image.mode in ("RGBA", "LA", "P"):
            background = Image.new("RGB", image.size, (255, 255, 255))
            if image.mode == "P":
                image = image.convert("RGBA")
            if image.mode in ("RGBA", "LA"):
                background.paste(image, mask=image.split()[-1])
                image = background
        elif image.mode not in ("RGB", "L"):
            image = image.convert("RGB")

        # Save with optimization
        buffer = io.BytesIO()
        save_kwargs: dict = {"optimize": True}

        if output_format == "jpeg":
            save_kwargs.update(
                {
                    "quality": ImageProcessor.TARGET_QUALITY,
                    "progressive": True,
                }
            )
            image.save(buffer, format="JPEG", **save_kwargs)
        else:
            save_kwargs.update({"compress_level": 6})
            image.save(buffer, format="PNG", **save_kwargs)

        return buffer.getvalue()

    @staticmethod
    def process_image_data(image_data: ImageData, optimize: bool = True) -> ImageData:
        """Process and optionally optimize ImageData.

        Args:
            image_data: Raw ImageData
            optimize: Whether to apply optimization

        Returns:
            Processed ImageData (possibly optimized)
        """
        if not optimize:
            return image_data

        # Decode base64
        image_bytes = base64.b64decode(image_data.base64_data)
        image = Image.open(io.BytesIO(image_bytes))

        # Check if optimization is needed
        width, height = image.size
        current_size = len(image_bytes)

        needs_resize = width > ImageProcessor.MAX_DIMENSION or height > ImageProcessor.MAX_DIMENSION
        needs_compress = current_size > ImageProcessor.MAX_FILE_SIZE_BYTES

        if not needs_resize and not needs_compress:
            return image_data

        # Optimize
        optimized_bytes = ImageProcessor.optimize_image(image, output_format="jpeg")
        optimized_base64 = base64.b64encode(optimized_bytes).decode("utf-8")

        return ImageData(
            base64_data=optimized_base64,
            format="jpeg",
            placeholder=image_data.placeholder,
        )
