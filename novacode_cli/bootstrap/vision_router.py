"""Vision captioning — convert images to text for a text-only main model.

When the main model **cannot** see images, a raw image must NOT enter its
conversation — that raises "this model does not support image input". Instead, a
vision-capable model (configured under ``"vision_model"`` in
``~/.nova/Nova.config.json``) **captions** the image once, and only that text
flows on.

When the main model **can** see images (a multimodal model — see
:mod:`novacode_cli.config.model_capabilities`), captioning is skipped entirely:
the image is passed straight through to the main model, which reads it directly.
This is the fix for the long-standing "read_file on an image returns
``[image: vision model unavailable]`` even though my model is multimodal" bug —
the capable model was being bypassed in favour of an auxiliary one.

Two entry points are captioned (text-only main model only):

- **disk reads** — :class:`VisionCaptionMiddleware.awrap_tool_call` rewrites a
  ``read_file`` ``ToolMessage`` that carries image blocks into a text description.
- **clipboard pastes** — captioned at *ingestion* in
  ``ui/input_preparation.prepare_input_content`` (once, persisted as text), via
  the module-level :func:`caption_images`.

:meth:`VisionCaptionMiddleware.awrap_model_call` is a pure **safety net**: for a
text-only main model it strips any residual image blocks from history (e.g. a
restored session) before forwarding. There is no model swapping — the source of
the old "image input" crash class.
"""

from __future__ import annotations

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
    "[image: vision model unavailable — set a multimodal `vision_model` in "
    "~/.nova/Nova.config.json, or run `/vision` to configure it. If your main "
    "model is multimodal, run `/vision on` to send images to it directly]"
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


def _suffix_to_mime_type(suffix: str | None) -> str:
    """Best-effort MIME type from a file suffix (defaults to ``image/png``)."""
    if suffix:
        return _MIME_BY_SUFFIX.get(suffix.lower(), "image/png")
    return "image/png"


# ── Vision model (process-wide cache, shared by both entry points) ──────────

_vision_model_instance: BaseChatModel | None = None
_vision_model_errored: bool = False
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
    if not image_urls:
        return ""
    if _vision_in_cooldown():
        return _VISION_FAILED
    model = get_vision_model()
    if model is None:
        return _VISION_UNAVAILABLE
    try:
        from novacode_cli.prompts import render_template

        prompt = render_template("vision_caption.jinja", task_hint=(task_hint or "").strip())
        human_content: list[dict[str, Any]] = [
            {"type": "text", "text": prompt},
            *[{"type": "image_url", "image_url": {"url": u}} for u in image_urls],
        ]
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
    except Exception as e:  # noqa: BLE001 — never crash a turn over a caption
        # A KNOWN provider problem (region opt-in, auth, quota) means the vision
        # model is simply unavailable — log one clean line, not a stack trace, so
        # a region-restricted auxiliary model doesn't spam tracebacks every image.
        from novacode_cli.errors import friendly_model_error

        notice = friendly_model_error(e)
        if notice:
            logger.warning("Vision captioning unavailable: %s", notice.splitlines()[0])
        else:
            logger.warning("Vision captioning failed", exc_info=True)
        _mark_vision_errored()
        return _VISION_FAILED
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

    new_content_blocks = [
        b for b in content_blocks if not (isinstance(b, dict) and b.get("type") == "image")
    ]

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

    kwargs: dict[str, Any] = {
        "content": new_content,
        "additional_kwargs": getattr(msg, "additional_kwargs", {}),
    }
    if new_content_blocks:
        kwargs["content_blocks"] = new_content_blocks

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
    """Convert images to text so a text-only main model never sees an image.

    When ``main_model_supports_images`` is ``True`` (a multimodal main model),
    this middleware is a **pass-through**: images flow straight to the main
    model, which reads them directly. Captioning and stripping are skipped.

    When it is ``False`` (the default — a text-only main model):

    - ``awrap_tool_call``: a ``read_file`` result carrying image blocks is
      captioned by the vision model and returned as a text ``ToolMessage``.
    - ``awrap_model_call``: a pure safety net — strips any residual image blocks
      from history before forwarding to the (unchanged) main model. No swapping.

    Pasted clipboard images are captioned upstream at ingestion (see
    ``ui/input_preparation.prepare_input_content``), so they never reach here as
    images either.
    """

    def __init__(self, main_model_supports_images: bool = False) -> None:  # noqa: FBT001, FBT002
        """Create the middleware.

        Args:
            main_model_supports_images: ``True`` when the main model can accept
                image input, in which case images are passed through untouched.
                ``False`` (default) preserves the caption-and-strip behaviour for
                text-only models.
        """
        super().__init__()
        self.main_model_supports_images = main_model_supports_images

    @staticmethod
    def get_vision_model() -> BaseChatModel | None:
        """Expose the shared vision model accessor (back-compat / tests)."""
        return get_vision_model()

    # -- tool path: caption read_file images --------------------------------

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command]],
    ) -> ToolMessage | Command:
        """Caption images returned by ``read_file`` into a text ToolMessage.

        Pass-through when the main model is multimodal — the image is returned
        unchanged so the main model reads it directly.
        """
        result = await handler(request)
        if self.main_model_supports_images:
            return result
        try:
            tool_call = getattr(request, "tool_call", None) or {}
            if tool_call.get("name") != "read_file" or not isinstance(result, ToolMessage):
                return result
            image_urls = _collect_image_urls(result)
            if not image_urls:
                return result

            # An image was found — from here we MUST return text, never the
            # original image-bearing result (that would leak it to the main model).
            task_hint = ""
            state = getattr(request, "state", None)
            if isinstance(state, dict):
                task_hint = _latest_user_text(state.get("messages") or [])
            args = tool_call.get("args") or {}
            path = args.get("file_path") or args.get("path") or "image"
            caption = await caption_images(image_urls, task_hint)
            return ToolMessage(
                content=f"[Image: {path}]\n{caption}",
                tool_call_id=result.tool_call_id,
                name=getattr(result, "name", None),
            )
        except Exception:  # noqa: BLE001 — on any failure, strip (never leak the image)
            logger.warning(
                "read_file image captioning failed; stripping image to be safe",
                exc_info=True,
            )
            return _strip_image_content(result) if isinstance(result, ToolMessage) else result

    def wrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], ToolMessage | Command],
    ) -> ToolMessage | Command:
        """Synchronous pass-through — captioning needs the async path."""
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
            return await handler(request)
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
            return handler(request)
        return handler(request.override(messages=_strip_all_images(request.messages)))


def is_vision_failure(caption: str) -> bool:
    """True when *caption* is a failure placeholder rather than a description.

    The placeholders are ordinary (truthy) strings, so a caller that only tests
    ``if captions`` will treat a failed caption as a successful one. Use this to
    tell them apart.
    """
    return caption in (_VISION_FAILED, _VISION_UNAVAILABLE, _VISION_EMPTY)


__all__ = [
    "VisionCaptionMiddleware",
    "caption_images",
    "get_vision_model",
    "is_vision_failure",
]
