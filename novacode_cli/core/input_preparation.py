"""Input preparation and message content building.

This module handles:
- Building agent config for task execution
- Getting agent display names
- Preparing messages for the agent

Shared between the TUI, headless mode, and server mode.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from novacode_cli.image_utils import is_image_file, load_image_from_path
from novacode_cli.input_utils import (
    MAX_MENTION_DIR_ENTRIES,
    MAX_MENTION_FILE_CHARS,
    MAX_MENTION_TOTAL_CHARS,
    ImageTracker,
    parse_file_mentions,
)

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)


def _is_binary(sample: bytes) -> bool:
    """Heuristic: a NUL byte in the first block means binary, not text."""
    return b"\x00" in sample


def _render_file_mention(path: Path, content: str, *, truncated: bool) -> str:
    """Render one mentioned file as a fenced Markdown block."""
    note = "\n... (file truncated)" if truncated else ""
    return f"\n### {path.name}\nPath: `{path}`\n```\n{content}{note}\n```"


def _render_dir_mention(path: Path) -> str:
    """Render a mentioned directory as a capped, non-recursive listing.

    Deliberately a listing rather than a recursive dump: silently inlining a
    whole tree would be the fastest way to exhaust the context window.
    """
    try:
        entries = sorted(path.iterdir(), key=lambda p: (p.is_file(), p.name.lower()))
    except OSError as exc:
        return f"\n### {path.name}/\nPath: `{path}`\n[could not read directory: {exc}]"

    shown = entries[:MAX_MENTION_DIR_ENTRIES]
    lines = [f"  {e.name}{'/' if e.is_dir() else ''}" for e in shown]
    if len(entries) > len(shown):
        lines.append(f"  ... and {len(entries) - len(shown)} more")
    listing = "\n".join(lines) if lines else "  (empty directory)"
    return f"\n### {path.name}/\nPath: `{path}`\n```\n{listing}\n```"


def _is_dir(path: Path) -> bool:
    """Blocking directory check, split out so callers can offload it."""
    return path.is_dir()


async def _read_mention(path: Path) -> str:
    """Read one mention (file or directory) as a rendered Markdown block.

    Every filesystem call is blocking, so each one runs on a worker thread — an
    async function must not do path I/O inline, or it stalls the event loop.
    """
    if await asyncio.to_thread(_is_dir, path):
        return await asyncio.to_thread(_render_dir_mention, path)

    try:
        raw = await asyncio.to_thread(path.read_bytes)
    except OSError as exc:
        return f"\n### {path.name}\nPath: `{path}`\n[could not read file: {exc}]"

    if _is_binary(raw[:8192]):
        return f"\n### {path.name}\nPath: `{path}`\n[binary file, {len(raw)} bytes — not inlined]"

    text = raw.decode("utf-8", errors="replace")
    if len(text) > MAX_MENTION_FILE_CHARS:
        return _render_file_mention(path, text[:MAX_MENTION_FILE_CHARS], truncated=True)
    return _render_file_mention(path, text, truncated=False)


async def _inline_mentions(prompt_text: str, mentioned: list[Path]) -> str:
    """Append a ``## Referenced Files`` section for the mentioned paths.

    Bounded two ways: a per-file cap (truncated with a visible notice, never
    silently) and a total budget across the message. Mentions past the budget
    are listed by path only, so the model still knows what was referenced.
    """
    if not mentioned:
        return prompt_text

    parts: list[str] = [prompt_text, "\n\n## Referenced Files\n"]
    used = 0
    skipped: list[Path] = []

    for path in mentioned:
        block = await _read_mention(path)
        if used and used + len(block) > MAX_MENTION_TOTAL_CHARS:
            skipped.append(path)
            continue
        used += len(block)
        parts.append(block)

    if skipped:
        names = ", ".join(f"`{p}`" for p in skipped)
        parts.append(
            f"\n### Omitted (context budget reached)\n"
            f"These were referenced but not inlined: {names}\n"
            "Read them explicitly if you need their contents."
        )

    return "\n".join(parts)


async def _split_image_mentions(paths: list[Path]) -> tuple[list[Path], list]:
    """Partition mentions into (paths to inline as text, loaded images).

    An ``@screenshot.png`` mention used to reach the model as
    ``[binary file, N bytes — not inlined]``: the file was detected as binary
    and skipped, so a multimodal model never received the picture it was told
    about and had to be asked again. Image mentions are loaded here into the
    same :class:`~novacode_cli.image_utils.ImageData` form the clipboard path
    produces, so both ingest routes hand the model the actual image.

    A mention that fails to load (too large, corrupt, unreadable) is left in
    the text list so the existing ``[binary file …]`` / ``[could not read …]``
    notice still explains what happened instead of failing silently.
    """
    text_paths: list[Path] = []
    loaded: list = []
    for path in paths:
        if not is_image_file(path):
            text_paths.append(path)
            continue
        try:
            # Blocking decode + re-encode; never run it on the event loop.
            loaded.append(await asyncio.to_thread(load_image_from_path, path))
        except Exception:  # noqa: BLE001 — fall back to the binary notice
            logger.warning("Could not load image mention %s", path, exc_info=True)
            text_paths.append(path)
    return text_paths, loaded


async def prepare_input_content(
    user_input: str,
    image_tracker: ImageTracker | None = None,
    *,
    skip_file_mentions: bool = False,
) -> str | list:
    """Prepare input content with file mentions and images.

    Args:
        user_input: The raw user input string
        image_tracker: Optional image tracker for multimodal content
        skip_file_mentions: If True, skip @file mention parsing. Use this
            for programmatic prompts (e.g., skill invocations) that contain
            @ symbols which should not be interpreted as file references.

    Returns:
        Prepared content as string or list of content blocks
    """
    # Images reach the main model from two ingest routes: a clipboard paste
    # (already tracked) and an ``@path/to/image.png`` mention (loaded here).
    # Both must arrive as image content blocks — inlining an image as text is
    # impossible, and the old binary-file notice told the model nothing.
    images: list = []

    if skip_file_mentions:
        cleaned_input = user_input
    else:
        # Parse @file mentions, then inline their contents into the prompt so
        # the model actually sees the referenced files (paths alone are inert).
        cleaned_input, mentioned_files = parse_file_mentions(user_input)
        text_mentions, image_mentions = await _split_image_mentions(mentioned_files)
        cleaned_input = await _inline_mentions(cleaned_input, text_mentions)
        images.extend(image_mentions)

    if image_tracker:
        try:
            tracked = image_tracker.get_images()
        except Exception:  # noqa: BLE001
            tracked = []
        images.extend(tracked)

    if images:
        # A multimodal main model reads the image itself, so it is handed the
        # image content blocks directly. Captioning it through the auxiliary
        # vision model would both lose detail AND fail the whole paste when
        # no vision model is configured — the reported
        # "[image: vision captioning failed]" on a model that can see
        # perfectly well.
        if _main_model_can_see_images():
            return _image_content_blocks(cleaned_input, images)
        try:
            from novacode_cli.bootstrap.vision_router import caption_images

            # caption_images takes data: URL *strings* — passing the
            # ImageData objects instead made the vision model raise
            # "Only string image_url ... supported", which surfaced to the
            # user as "[image: vision captioning failed]".
            image_urls = [img.to_data_url() for img in images]
            captions = await caption_images(image_urls)
            # Only a real description counts. The failure placeholders ARE
            # truthy strings, so `if captions` accepted them and returned a
            # "[image: ...]" notice as if it were a caption, making the
            # image-blocks fallback below unreachable.
            if captions and not _is_vision_failure(captions):
                return f"{cleaned_input}\n\n[Attached image: {captions}]"
        except Exception:  # noqa: BLE001
            logger.warning("Clipboard image captioning failed", exc_info=True)
        # Captioning failed, or returned no description: pass the images
        # through so a multimodal model still sees them, instead of sending a
        # failure notice in their place.
        return _image_content_blocks(cleaned_input, images)

    return cleaned_input


def _image_content_blocks(cleaned_input: str, images: list) -> list:
    """Build ``[text, *image]`` content blocks for the given images."""
    content: list = [{"type": "text", "text": cleaned_input}]
    for img in images:
        if hasattr(img, "to_content_block"):
            content.append(img.to_content_block())
        elif hasattr(img, "to_message_content"):
            content.append(img.to_message_content())
    return content


def _is_vision_failure(caption: str) -> bool:
    """True when *caption* is one of the vision path's failure placeholders."""
    from novacode_cli.bootstrap.vision_router import is_vision_failure

    return is_vision_failure(caption)


def _main_model_can_see_images() -> bool:
    """Whether the configured main model accepts image input directly.

    Resolved through the shared capability helper so this decision matches the
    one the vision middleware makes when the agent is built.
    """
    try:
        from novacode_cli.config.model_capabilities import resolve_main_model_multimodal

        return resolve_main_model_multimodal(None)
    except Exception:  # noqa: BLE001 — treat an unknown capability as text-only
        return False


def build_agent_config(
    thread_id: str,
    model: str | None = None,
    **kwargs: object,
) -> dict:
    """Build the standard agent configuration dict.

    Args:
        thread_id: The conversation thread ID
        model: Optional model name override
        **kwargs: Additional config keys

    Returns:
        Configuration dict for the agent
    """
    config: dict = {"configurable": {"thread_id": thread_id}}
    if model:
        config["configurable"]["model"] = model
    config.update(kwargs)
    return config


def get_agent_display_name(agent_name: str | None) -> str:
    """Get a human-readable display name for an agent.

    Args:
        agent_name: The agent's identifier name

    Returns:
        Display name string
    """
    if not agent_name:
        return "Nova"
    return agent_name.replace("-", " ").title()
