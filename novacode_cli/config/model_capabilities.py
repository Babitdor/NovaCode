"""Static model-capability registry — which models can accept image input.

Nova's vision path (:mod:`novacode_cli.bootstrap.vision_router`) historically
assumed the main model was **text-only** and force-captioned every image through
a separate ``vision_model``. That is wrong for a multimodal main model: the
capable model is bypassed in favour of an auxiliary one that may not exist, so
``read_file`` on an image degrades to ``[image: vision model unavailable]`` even
though the main model could have read it directly.

This module answers one question — *can the main model see images?* — so the
middleware can pass images straight through when it can, and only caption when
it genuinely cannot.

Detection is a **static pattern list** (mirroring ``MODEL_CONTEXT_WINDOWS`` in
``context/_analysis.py``) plus an explicit user override stored in
``~/.nova/Nova.config.json`` (``main_model_multimodal``). The override wins, so
an unlisted multimodal model can be declared without a code change.

The answer is also written back onto the bound model's ``ModelProfile`` by
:func:`declare_multimodal_profile`, because deepagents gates every multimodal
block a ``read_file`` produces on that profile — Nova deciding a model is
multimodal is not enough on its own.

Pure and dependency-free: safe to import on the middleware hot path.
"""

from __future__ import annotations


def _is_chat_model(model: object) -> bool:
    """Whether *model* is a LangChain chat model (duck-typed, import-free).

    ``BaseChatModel`` is imported lazily rather than at module scope: this
    module is on the middleware hot path and is documented as dependency-free.
    A model that is not a chat model has no ``profile`` to seed, so the caller
    is a no-op either way.
    """
    try:
        from langchain_core.language_models import BaseChatModel

        return isinstance(model, BaseChatModel)
    except Exception:  # noqa: BLE001 — an unimportable langchain means "not a model"
        return False


#: Substrings that identify a known vision-capable model. Matched
#: case-insensitively against the model name. Deliberately specific — a broad
#: pattern (``qwen3``) would mark a text-only model multimodal and make it error
#: on image input, so vision variants are named explicitly (``qwen3-vl``).
MULTIMODAL_MODEL_PATTERNS: tuple[str, ...] = (
    # OpenAI
    "gpt-4o",
    "gpt-4-turbo",
    "gpt-4.1",
    "gpt-5",
    "o3",
    "o4-mini",
    # Anthropic (Claude 3+ are all multimodal)
    "claude-3",
    "claude-4",
    "claude-opus",
    "claude-sonnet",
    "claude-haiku",
    # Google
    "gemini-",
    # Ollama / open-weight vision models
    "qwen3-vl",
    "qwen2.5-vl",
    "qwen2-vl",
    "llava",
    "minicpm-v",
    "gemma3",
    "gemma4",
    "pixtral",
    "internvl",
    "moondream",
    "phi-3.5-vision",
    "phi-4-multimodal",
    "llama-3.2-11b-vision",
    "llama-3.2-90b-vision",
    "mistral-small-3.1",
    "mistral-small-3.2",
    "deepseek-v4.1-flash",
    "deepseek-vl",
    "glm-4v",
    "glm-4.5v",
    "cogvlm",
    "idefics",
    "smolvlm",
    "granite-vision",
)


#: What the *bound* model's own ``ModelProfile`` says about image input, as
#: recorded by :func:`note_bound_model` when the agent is built. ``None`` means
#: "no profile said anything", not "no".
#:
#: Cached rather than re-derived because the two callers see different things:
#: agent construction has the model object, prompt preparation (which must give
#: the SAME answer) has only the config. A stale value cannot outlive a model
#: switch — that rebuilds the agent, which re-notes it.
_bound_profile_support: bool | None = None


#: ``ModelProfile`` fields deepagents consults before it will attach a
#: multimodal block from a ``read_file`` result. Each maps to the block type
#: ``read_file`` emits for that modality (see deepagents'
#: ``_PROFILE_FIELD_BY_BLOCK_TYPE``). A field that is absent defaults to
#: "supported"; only an explicit ``False`` makes deepagents replace the block
#: with ``[read_file: X was not attached because this model does not support
#: <type> content]``.
#:
#: ``file`` covers PDFs (``application/pdf``) and, for the providers deepagents
#: hard-codes as tolerant (OpenAI, Google), ``.ppt``/``.pptx`` as well.
MULTIMODAL_PROFILE_FIELDS: tuple[str, ...] = (
    "image_inputs",
    "audio_inputs",
    "video_inputs",
    "pdf_inputs",
)

#: The same fields again, but scoped to a ``ToolMessage`` — deepagents checks
#: these *in addition* to the fields above when the block arrives in a tool
#: result rather than a user message. ``read_file`` always produces a
#: ``ToolMessage``, so a profile that sets only the plain field still has the
#: block stripped if the tool-scoped field is ``False``.
MULTIMODAL_TOOL_PROFILE_FIELDS: tuple[str, ...] = (
    "image_tool_message",
    "pdf_tool_message",
)


def note_bound_model(model: object) -> None:
    """Record what *model*'s own profile declares about image input.

    LangChain populates ``model.profile`` from its provider's profile data, which
    knows about models this module's pattern list never will. Trusting it is the
    difference between a capable model reading a screenshot and being handed
    ``[read_file: UI.png was not attached ...]`` — at which point the model does
    the only thing left and writes a script to inspect the pixels.
    """
    global _bound_profile_support
    profile = getattr(model, "profile", None)
    if isinstance(profile, dict) and "image_inputs" in profile:
        _bound_profile_support = bool(profile["image_inputs"])
    else:
        _bound_profile_support = None


def _matches_pattern(model_name: str) -> bool:
    """True when *model_name* matches a known multimodal pattern."""
    low = (model_name or "").lower()
    return any(pattern in low for pattern in MULTIMODAL_MODEL_PATTERNS)


def model_supports_images(
    provider: str,
    model_name: str,
    *,
    override: bool | None = None,
) -> bool:
    """Whether the main model can accept image input.

    Args:
        provider: Provider id (``ollama``, ``openai``, ``anthropic``, …). Used
            only to normalise the model name (Ollama tags carry a ``:tag``
            suffix that never affects capability).
        model_name: The model identifier.
        override: Explicit user setting. ``True``/``False`` forces the answer;
            ``None`` (default) falls back to pattern detection.

    Returns:
        ``True`` when images may be sent to the main model directly.
    """
    if override is not None:
        return bool(override)
    name = model_name or ""
    if provider == "ollama":
        # Ollama model ids are ``name:tag`` (e.g. ``qwen3-vl:235b-cloud``); the
        # tag never changes capability, so match on the name part only.
        name = name.split(":", 1)[0]
    return _matches_pattern(name)


def resolve_main_model_multimodal(model: object) -> bool:
    """Whether the *configured* main model can accept image input.

    The single source of truth for this question. It reads the provider and
    model from ``Nova.config.json`` and applies the user override, so the
    answer matches the model Nova is actually running.

    Used both when building the agent (to configure the vision middleware) and
    when preparing a prompt (to decide whether a pasted image can be handed to
    the main model directly instead of being captioned). Deriving it twice would
    let those two decisions disagree — the shape of the original bug, where a
    multimodal model still had its images captioned.

    Args:
        model: The bound model or its name. Only used to tell whether the main
            model is multimodal; the provider comes from the saved config.

    Precedence: the user's explicit override, then the bound model's own
    profile (see :func:`note_bound_model`), then the static pattern list.

    Returns:
        ``True`` when images may be sent straight to the main model.
    """
    try:
        from novacode_cli.config.nova_config import NovaConfig

        override = NovaConfig().get_main_model_multimodal()
        if override is not None:
            return bool(override)
        if _bound_profile_support is not None:
            return _bound_profile_support
        cfg = NovaConfig().get_model_config() or {}
        provider = cfg.get("provider", "") if isinstance(cfg, dict) else ""
        name = cfg.get("model") if isinstance(cfg, dict) else None
        if not name:
            name = (
                model
                if isinstance(model, str)
                else getattr(model, "model_name", getattr(model, "model", ""))
            )
        return model_supports_images(str(provider), str(name))
    except Exception:  # noqa: BLE001 — capability detection must never break a turn
        return False


def declare_multimodal_profile(model: object, *, images: bool) -> None:
    """Tell deepagents which multimodal blocks *model* will accept.

    deepagents gates every multimodal block a ``read_file`` produces on the
    bound model's ``ModelProfile``. A field that is absent defaults to
    "supported", but a provider that ships a profile with the field set to
    ``False`` (or a model whose profile simply omits the modality) has the block
    silently swapped for ``[read_file: X was not attached because this model does
    not support <type> content]`` before the request leaves Nova. The model is
    then told only that something was withheld, and does the only thing left:
    writes a script to decode the file itself.

    Nova already decides the image question (see
    :func:`resolve_main_model_multimodal`); this records that decision on the
    profile so deepagents agrees with it, and clears the tool-scoped fields that
    would otherwise veto the same block inside a ``ToolMessage``.

    Only the fields Nova has an opinion about are touched. ``images`` is the one
    answer Nova actually derives; the remaining modalities are left at whatever
    the provider's own profile said, so a model that genuinely cannot take audio
    still gets the placeholder rather than a 400 from the provider.

    Never raises — a profile hint must not block an agent build.

    Args:
        model: The bound chat model.
        images: ``True`` when Nova has concluded the model accepts image input.
    """
    try:
        if not _is_chat_model(model):
            return
        existing = getattr(model, "profile", None)
        profile = dict(existing) if isinstance(existing, dict) else {}
        changed = False
        if images and profile.get("image_inputs") is not True:
            profile["image_inputs"] = True
            changed = True
        # A tool-scoped ``False`` vetoes the block even when the plain field
        # says yes, and ``read_file`` results are always ToolMessages.
        for field in MULTIMODAL_TOOL_PROFILE_FIELDS:
            if profile.get(field) is False:
                profile[field] = True
                changed = True
        if changed:
            model.profile = profile  # type: ignore[assignment]
    except Exception:  # noqa: BLE001 — never block an agent build on a profile hint
        __import__("logging").getLogger(__name__).debug(
            "Could not declare multimodal support on the model profile", exc_info=True
        )


__all__ = [
    "MULTIMODAL_MODEL_PATTERNS",
    "MULTIMODAL_PROFILE_FIELDS",
    "MULTIMODAL_TOOL_PROFILE_FIELDS",
    "declare_multimodal_profile",
    "model_supports_images",
    "note_bound_model",
    "resolve_main_model_multimodal",
]
