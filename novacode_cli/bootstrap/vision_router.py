"""On-demand image inspection for a text-only main model.

When the main model **cannot** see images, raw image content stays in graph state
and is stripped from its model requests. The main model receives a note that it
can invoke ``VISION`` when it needs visual details. That tool makes one bounded
side-call to the separately configured vision model and returns a text caption.

When the main model **can** see images (a multimodal model — see
:mod:`novacode_cli.config.model_capabilities`), captioning is skipped entirely:
the image is passed straight through to the main model, which reads it directly.
This is the fix for the long-standing "read_file on an image returns
``[image: vision model unavailable]`` even though my model is multimodal" bug —
the capable model was being bypassed in favour of an auxiliary one.

Two image sources are available to ``VISION`` (text-only main model only):

- **disk reads** — image-bearing ``read_file`` results remain in graph state.
- **clipboard pastes** — image blocks remain in the user message.

:meth:`VisionCaptionMiddleware.awrap_model_call` strips image blocks from
text-only requests and appends a tool-use hint. Multimodal main models receive
the image blocks directly. The session model never changes.
"""

from __future__ import annotations

import asyncio
import base64
import functools
import io
import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from langchain.tools.tool_node import ToolCallRequest
    from langchain_core.language_models import BaseChatModel
    from langgraph.types import Command

from langchain.agents.middleware.types import (
    AgentMiddleware,
    ModelRequest,
    ModelResponse,
)
from langchain.tools import ToolRuntime, tool
from langchain_core.messages import AIMessage, AnyMessage, HumanMessage, ToolMessage

logger = logging.getLogger("nova.vision_router")

# MIME type fallback by extension suffix
_MIME_BY_SUFFIX: dict[str, str] = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
    ".tiff": "image/tiff",
    ".tif": "image/tiff",
}

#: Appended to every failure placeholder. Without it the model is handed a
#: bracketed note about an image it cannot see and improvises — in practice it
#: writes a script to decode the file and read raw pixel values, which answers
#: nothing and costs a tool round-trip. Saying so explicitly stops that.
_NO_PIXEL_ARCHAEOLOGY = (
    " Do not try to inspect the image yourself (no pixel decoding, no PIL/ffmpeg "
    "scripts) — it cannot be read that way. Ask the user what it shows, or "
    "suggest `/vision on` if their model can see images."
)

# Placeholder captions returned (instead of leaking an image) when vision fails.
_VISION_UNAVAILABLE = (
    "[image: vision model unavailable — configure it in `/model` → `Vision`, "
    "or run `/vision <provider> <model>`]"
)
_VISION_UNAVAILABLE += _NO_PIXEL_ARCHAEOLOGY
_VISION_FAILED = (
    "[image: vision captioning failed — the configured vision_model rejected the "
    "image or is unavailable]" + _NO_PIXEL_ARCHAEOLOGY
)
_VISION_EMPTY = "[image: vision model returned no description]" + _NO_PIXEL_ARCHAEOLOGY

#: How many consecutive images to skip captioning for after a hard failure.
#: A single transient failure (a cold Ollama model, a momentary network blip)
#: must not disable image reading for the rest of the session — the old
#: permanent latch did exactly that. After this many skips the vision model is
#: retried, so a recovered provider starts working again without a restart.
_VISION_FAILURE_COOLDOWN = 3
VISION_TOOL_TIMEOUT_SECONDS = 30
_VISION_TOOL_NOTE = (
    "[An image is attached but was not sent to this text-only model. "
    "Call VISION if you need to inspect its visual contents.]"
)


def _suffix_to_mime_type(suffix: str | None) -> str:
    """Best-effort MIME type from a file suffix (defaults to ``image/png``)."""
    if suffix:
        return _MIME_BY_SUFFIX.get(suffix.lower(), "image/png")
    return "image/png"


# ── Vision model (process-wide cache, shared by both entry points) ──────────

_vision_model_instance: BaseChatModel | None = None
_vision_model_errored: bool = False
_vision_failure_reason: str | None = None
#: Remaining images to skip before retrying the vision model after a failure.
#: Decremented on each caption attempt while > 0; when it reaches 0 the model is
#: retried. Replaces the old permanent latch (one failure killed vision for the
#: whole session).
_vision_cooldown_remaining: int = 0


def get_vision_model() -> BaseChatModel | None:
    """Lazily create the configured vision model (cached process-wide).

    Returns ``None`` when creation fails or the provider is unavailable. A
    failure starts a short cooldown (see :data:`_VISION_FAILURE_COOLDOWN`) rather
    than disabling vision permanently, so a transient problem self-heals.
    """
    global _vision_model_instance, _vision_model_errored  # noqa: PLW0603
    if _vision_model_errored:
        return None
    if _vision_model_instance is not None:
        return _vision_model_instance
    try:
        from novacode_cli.config.model_create import create_model_from_config
        from novacode_cli.config.nova_config import NovaConfig

        cfg = NovaConfig().get_vision_model_config()
        model = create_model_from_config(cfg["provider"], cfg["model"])
    except Exception:
        logger.exception("Failed to create vision model")
        _vision_model_errored = True
        return None
    if model is None:
        logger.warning("Vision model unavailable (missing provider/API key?)")
        _vision_model_errored = True
        return None
    _vision_model_instance = model
    logger.info("Vision captioning model ready")
    return model


def _mark_vision_errored() -> None:
    """Start a short cooldown after a hard failure (not a permanent disable).

    The vision model is dropped and skipped for the next
    :data:`_VISION_FAILURE_COOLDOWN` images, then retried — so a transient
    failure (cold model, network blip) recovers without a Nova restart.
    """
    global _vision_model_instance, _vision_cooldown_remaining  # noqa: PLW0603
    _vision_model_instance = None
    _vision_cooldown_remaining = _VISION_FAILURE_COOLDOWN


def reset_vision_model_cache() -> None:
    """Forget a cached model/error after its provider or model is reconfigured."""
    global _vision_model_instance, _vision_model_errored, _vision_cooldown_remaining, _vision_failure_reason  # noqa: PLW0603
    _vision_model_instance = None
    _vision_model_errored = False
    _vision_cooldown_remaining = 0
    _vision_failure_reason = None


def _vision_in_cooldown() -> bool:
    """True while the vision model is being skipped after a recent failure."""
    global _vision_cooldown_remaining  # noqa: PLW0603
    if _vision_cooldown_remaining > 0:
        _vision_cooldown_remaining -= 1
        return True
    return False


async def caption_images(image_urls: list[str], task_hint: str = "") -> str:
    """Caption one or more images to text via the vision model (out-of-band).

    Never raises and never returns image data — on any failure it returns a
    short placeholder string so the conversation continues as text-only.

    Args:
        image_urls: ``data:<mime>;base64,<…>`` URLs (or http(s) URLs).
        task_hint: The user's current request, so the caption targets the task.
    """
    global _vision_failure_reason  # noqa: PLW0603
    if not image_urls:
        return ""
    if _vision_in_cooldown():
        reason = _vision_failure_reason or "temporarily paused after a previous failure"
        return f"{_VISION_FAILED} ({reason})"
    model = get_vision_model()
    if model is None:
        try:
            from novacode_cli.config.nova_config import NovaConfig

            cfg = NovaConfig().get_vision_model_config()
            selected = f"{cfg['provider']}/{cfg['model']}"
        except Exception:  # noqa: BLE001 — this is already a fail-open path
            selected = "the configured vision model"
        return f"{_VISION_UNAVAILABLE} (could not initialize {selected})"
    try:
        from novacode_cli.prompts import render_template

        prompt = render_template("vision_caption.jinja", task_hint=(task_hint or "").strip())
        human_content: list[dict[str, Any]] = [
            {"type": "text", "text": prompt},
            *[{"type": "image_url", "image_url": {"url": u}} for u in image_urls],
        ]
        async with asyncio.timeout(VISION_TOOL_TIMEOUT_SECONDS):
            resp = await model.ainvoke(
                [HumanMessage(content=human_content)],
                config={
                    "run_name": "nova_vision_caption",
                    "tags": ["nova", "vision", "caption"],
                    "metadata": {"nova_oob": True},
                },
            )
        raw = getattr(resp, "content", "")
        text = (raw if isinstance(raw, str) else str(raw)).strip()
    except TimeoutError:
        _vision_failure_reason = f"request timed out after {VISION_TOOL_TIMEOUT_SECONDS} seconds"
        _mark_vision_errored()
        return f"{_VISION_FAILED} ({_vision_failure_reason})"
    except Exception as e:  # noqa: BLE001 — never crash a turn over a caption
        # A KNOWN provider problem (region opt-in, auth, quota) means the vision
        # model is simply unavailable — log one clean line, not a stack trace, so
        # a region-restricted auxiliary model doesn't spam tracebacks every image.
        from novacode_cli.errors import friendly_model_error

        notice = friendly_model_error(e)
        if notice:
            reason = notice.splitlines()[0]
            logger.warning("Vision captioning unavailable: %s", reason)
        else:
            reason = str(e).splitlines()[0] if str(e) else "unknown model error"
            logger.warning("Vision captioning failed", exc_info=True)
        _vision_failure_reason = reason
        _mark_vision_errored()
        return f"{_VISION_FAILED} ({reason})"
    _vision_failure_reason = None
    return text or _VISION_EMPTY


# ── Image extraction / stripping helpers ────────────────────────────────────


def _extract_image_urls(msg: AnyMessage) -> list[str]:
    """Extract ``data:...;base64,...`` URLs from a message's ``content_blocks``.

    deepagents' ``read_file`` stores image data as ``content_blocks`` of
    ``type == "image"`` with a ``base64`` field; the MIME type falls back to the
    file suffix recorded in ``additional_kwargs['read_file_path']``.
    """
    urls: list[str] = []
    content_blocks = getattr(msg, "content_blocks", None) or []
    for block in content_blocks:
        if isinstance(block, dict) and block.get("type") == "image":
            b64 = block.get("base64", "")
            mime = block.get("mime_type", None)
            if not mime:
                rfp = getattr(msg, "additional_kwargs", {}).get("read_file_path", "")
                suffix = None
                if rfp:
                    from pathlib import Path

                    suffix = Path(rfp).suffix
                mime = _suffix_to_mime_type(suffix)
            if b64:
                urls.append(f"data:{mime};base64,{b64}")
    return urls


def _collect_image_urls(msg: AnyMessage) -> list[str]:
    """Collect image URLs from BOTH ``content_blocks`` and a list ``content``.

    Covers read_file (image blocks in ``content_blocks``) and pasted multimodal
    messages (``{"type": "image_url", "image_url": {"url": …}}`` in ``content``).
    Order-preserving and de-duplicated.
    """
    urls: list[str] = list(_extract_image_urls(msg))
    content = getattr(msg, "content", None)
    if isinstance(content, list):
        for block in content:
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if btype == "image_url":
                image_url = block.get("image_url")
                url = image_url.get("url") if isinstance(image_url, dict) else image_url
                if url:
                    urls.append(url)
            elif btype == "image" and block.get("base64"):
                mime = block.get("mime_type") or "image/png"
                urls.append(f"data:{mime};base64,{block['base64']}")
    seen: set[str] = set()
    out: list[str] = []
    for url in urls:
        if url not in seen:
            seen.add(url)
            out.append(url)
    return out


def _strip_image_content(msg: AnyMessage) -> AnyMessage:
    """Return *msg* stripped of any image content blocks.

    Fast path: returns the same object when no image data is present (zero
    allocation for the common text-only case). Used as a defensive safety net so
    no residual image reaches the main model.
    """
    content_blocks = getattr(msg, "content_blocks", None) or []
    has_image_in_blocks = any(
        isinstance(b, dict) and b.get("type") == "image" for b in content_blocks
    )
    raw_content = msg.content
    has_image_in_content = isinstance(raw_content, list) and any(
        isinstance(b, dict) and b.get("type") in ("image", "image_url") for b in raw_content
    )
    if not has_image_in_blocks and not has_image_in_content:
        return msg  # fast path — same object

    if isinstance(raw_content, list):
        clean_blocks = [
            b
            for b in raw_content
            if not (isinstance(b, dict) and b.get("type") in ("image", "image_url"))
        ]
        if isinstance(msg, ToolMessage):
            text_parts = [
                b.get("text", "")
                for b in clean_blocks
                if isinstance(b, dict) and b.get("type") == "text"
            ]
            new_content: str | list = " ".join(text_parts) if text_parts else ""
        else:
            new_content = clean_blocks if clean_blocks else ""
    else:
        new_content = raw_content

    # Keep a useful breadcrumb for the text-only model, but never send the
    # encoded image itself. The original message remains in graph state for the
    # VISION tool to read only if the model chooses to call it.
    if isinstance(msg, HumanMessage):
        text_content = (
            " ".join(
                b.get("text", "")
                for b in new_content
                if isinstance(b, dict) and b.get("type") == "text"
            )
            if isinstance(new_content, list)
            else str(new_content or "")
        )
        if _VISION_TOOL_NOTE not in text_content:
            if isinstance(new_content, list):
                new_content.append({"type": "text", "text": _VISION_TOOL_NOTE})
            else:
                new_content = f"{text_content}\n\n{_VISION_TOOL_NOTE}".strip()
    elif isinstance(msg, ToolMessage):
        text_content = (
            " ".join(
                b.get("text", "")
                for b in new_content
                if isinstance(b, dict) and b.get("type") == "text"
            )
            if isinstance(new_content, list)
            else str(new_content or "")
        )
        notice = "[Image content omitted; call VISION to inspect it if needed.]"
        new_content = f"{text_content}\n{notice}".strip()

    kwargs: dict[str, Any] = {
        "content": new_content,
        "additional_kwargs": getattr(msg, "additional_kwargs", {}),
    }
    if isinstance(msg, HumanMessage):
        return HumanMessage(**kwargs)
    if isinstance(msg, AIMessage):
        return AIMessage(**kwargs)
    if isinstance(msg, ToolMessage):
        return ToolMessage(
            **kwargs,
            name=getattr(msg, "name", None),
            tool_call_id=getattr(msg, "tool_call_id", ""),
            artifact=getattr(msg, "artifact", None),
        )
    return msg


def _strip_all_images(messages: list[AnyMessage]) -> list[AnyMessage]:
    """Return *messages* with all image blocks stripped (copy-on-write).

    Returns the original list (by identity) when nothing needs stripping.
    """
    needs_strip = False
    for i, msg in enumerate(messages):
        stripped = _strip_image_content(msg)
        if stripped is not msg:
            if not needs_strip:
                messages = list(messages)
                needs_strip = True
            messages[i] = stripped
    return messages


#: Formats every vision API accepts. Ollama rejects the rest ("invalid image
#: input"), and one such block in history fails every later call.
_PORTABLE_IMAGE_TYPES = frozenset({"image/png", "image/jpeg"})


@functools.lru_cache(maxsize=32)
def _as_jpeg(data: str) -> str:
    from PIL import Image

    from novacode_cli.image_utils import ImageProcessor

    image = Image.open(io.BytesIO(base64.b64decode(data)))
    return base64.b64encode(ImageProcessor.optimize_image(image, output_format="jpeg")).decode()


def _portable_images(messages: list[AnyMessage]) -> list[AnyMessage]:
    """Re-encode WebP/GIF/BMP/TIFF image blocks as JPEG (copy-on-write)."""
    out = messages
    for i, msg in enumerate(messages):
        if not isinstance(msg.content, list):
            continue
        blocks = list(msg.content)
        for j, block in enumerate(blocks):
            if not (
                isinstance(block, dict)
                and block.get("type") == "image"
                and isinstance(block.get("base64"), str)
                and block.get("mime_type") not in _PORTABLE_IMAGE_TYPES
            ):
                continue
            try:
                jpeg = _as_jpeg(block["base64"])
                blocks[j] = {**block, "base64": jpeg, "mime_type": "image/jpeg"}
            except Exception:  # noqa: BLE001 — unreadable: send as is, the provider decides
                logger.warning("could not convert %s", block.get("mime_type"), exc_info=True)
        if blocks != msg.content:
            if out is messages:
                out = list(messages)
            out[i] = msg.model_copy(update={"content": blocks})
    return out


def _with_portable_images(request: ModelRequest) -> ModelRequest:
    messages = _portable_images(request.messages)
    return request if messages is request.messages else request.override(messages=messages)


def _latest_user_text(messages: list[AnyMessage]) -> str:
    """Return the newest HumanMessage's text (capped), for the caption task hint."""
    for msg in reversed(messages or []):
        if not isinstance(msg, HumanMessage):
            continue
        content = msg.content
        if isinstance(content, str):
            return content[:500]
        if isinstance(content, list):
            texts = [
                b.get("text", "")
                for b in content
                if isinstance(b, dict) and b.get("type") == "text"
            ]
            return " ".join(t for t in texts if t)[:500]
    return ""


class VisionCaptionMiddleware(AgentMiddleware):
    """Route images safely for text-only and multimodal main models.

    When ``main_model_supports_images`` is ``True`` (a multimodal main model),
    this middleware is a **pass-through**: images flow straight to the main
    model, which reads them directly. Captioning and stripping are skipped.

    When it is ``False``, image data stays in graph state for the ``VISION`` tool
    and is stripped from model requests with a note inviting the model to invoke
    that tool. The main model never receives image blocks and never changes.
    """

    def __init__(self, main_model_supports_images: bool = False) -> None:  # noqa: FBT001, FBT002
        """Create the middleware.

        Args:
            main_model_supports_images: ``True`` when the main model can accept
                image input, in which case images are passed through untouched.
                ``False`` (default) preserves the on-demand VISION tool path for
                text-only models.
        """
        super().__init__()
        self.main_model_supports_images = main_model_supports_images

    @staticmethod
    def get_vision_model() -> BaseChatModel | None:
        """Expose the shared vision model accessor (back-compat / tests)."""
        return get_vision_model()

    # -- tool path: retain image results in graph state ----------------------

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command]],
    ) -> ToolMessage | Command:
        """Leave image results in graph state; defer inspection to VISION."""
        return await handler(request)

    def wrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], ToolMessage | Command],
    ) -> ToolMessage | Command:
        """Synchronous pass-through; image inspection is handled by VISION."""
        return handler(request)

    # -- model path: strip-only safety net ----------------------------------

    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Awaitable[ModelResponse]],
    ) -> ModelResponse:
        """Strip residual image blocks before a text-only main model.

        Pass-through when the main model is multimodal — images are left intact
        for the model to read.
        """
        if self.main_model_supports_images:
            return await handler(_with_portable_images(request))
        return await handler(
            request.override(messages=_strip_all_images(request.messages))
        )

    def wrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], ModelResponse],
    ) -> ModelResponse:
        """Synchronous variant of the strip safety net."""
        if self.main_model_supports_images:
            return handler(_with_portable_images(request))
        return handler(request.override(messages=_strip_all_images(request.messages)))


def build_vision_tool():
    """Create the on-demand VISION tool for a text-only main model.

    Image bytes remain in graph state but are omitted from the main model's
    request. This tool selects the most recent image-bearing message and sends
    it to the independently configured vision model only after the main model
    explicitly calls it.
    """
    @tool(
        "VISION",
        description=(
            "Inspect the most recently attached image using the separately configured "
            "vision model. Call only when visual details are needed; returns a textual "
            "description and never changes the main model."
        ),
    )
    async def vision(runtime: ToolRuntime) -> str:
        messages = runtime.state.get("messages", [])
        # Do not let a later turn accidentally inspect an image attached in an
        # earlier one. read_file images from this turn follow the latest user
        # message, so they remain eligible alongside pasted attachments.
        turn_start = next(
            (i for i in range(len(messages) - 1, -1, -1) if isinstance(messages[i], HumanMessage)),
            0,
        )
        current_turn = messages[turn_start:]
        for message in reversed(current_turn):
            image_urls = _collect_image_urls(message)
            if not image_urls:
                continue
            task_hint = _latest_user_text(current_turn)
            return await caption_images(image_urls, task_hint)
        return "VISION could not find an attached or recently read image in this turn."

    return vision


def is_vision_failure(caption: str) -> bool:
    """True when *caption* is a failure placeholder rather than a description.

    The placeholders are ordinary (truthy) strings, so a caller that only tests
    ``if captions`` will treat a failed caption as a successful one. Use this to
    tell them apart.
    """
    return caption.startswith((_VISION_FAILED, _VISION_UNAVAILABLE, _VISION_EMPTY))


__all__ = [
    "VisionCaptionMiddleware",
    "VISION_TOOL_TIMEOUT_SECONDS",
    "build_vision_tool",
    "caption_images",
    "get_vision_model",
    "is_vision_failure",
    "reset_vision_model_cache",
]
