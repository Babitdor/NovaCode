"""Safety patches for third-party LLM backends that don't handle all content block types.

When the read_file tool returns a ToolMessage with content_blocks of type "file"
(e.g. PDFs), some backends like Ollama crash with:
    "Blocks of type file not supported."

This module monkey-patches the conversion functions to handle unsupported block
types gracefully, converting them to text or skipping them instead of raising.

The primary defense is in FileTrackerMiddleware (converts file blocks → text at
the tool-result layer).  This module is a safety net for any file blocks that
escape that layer (e.g. from restored sessions or third-party tools).
"""

import functools
import logging
from typing import Any

logger = logging.getLogger(__name__)

_patched = False
_fs_host_path_patched = False
_write_file_content_patched = False
_reasoning_content_patched = False


def apply_write_file_dict_content_patch() -> None:
    """Tolerate a dict/list ``content`` passed to the ``write_file`` tool.

    Models (especially weaker ones, and any time the content *is* JSON) often
    emit the ``content`` argument as a structured object rather than a string —
    e.g. /init's semantic-extraction subagents write a graph fragment and pass
    ``content={"nodes": [...], "edges": [...]}``. deepagents' ``WriteFileSchema``
    types ``content: str``, so langchain's ``_parse_input`` rejects the call at
    pydantic validation *before* the tool body runs:

        1 validation error for WriteFileSchema / content
        Input should be a valid string ... input_type=dict

    The file is never written and the chunk is lost (LangSmith shows no tool
    error because it fails at arg validation). This wraps the schema's
    ``model_validate`` so a dict/list ``content`` is JSON-serialized to a string
    first — the deliverable file then contains exactly the intended JSON.
    Idempotent and best-effort.
    """
    global _write_file_content_patched
    if _write_file_content_patched:
        return

    try:
        import deepagents.middleware.filesystem as _fsmod
    except ImportError:
        return

    schema = getattr(_fsmod, "WriteFileSchema", None)
    if schema is None:
        return

    import json

    _orig_model_validate = schema.model_validate.__func__  # unwrap classmethod

    def _patched_model_validate(cls, obj, *args, **kwargs):
        if isinstance(obj, dict):
            content = obj.get("content")
            if isinstance(content, (dict, list)):
                obj = {**obj, "content": json.dumps(content, ensure_ascii=False)}
        return _orig_model_validate(cls, obj, *args, **kwargs)

    schema.model_validate = classmethod(_patched_model_validate)
    _write_file_content_patched = True
    logger.debug("Applied write_file dict-content coercion patch")


def apply_filesystem_host_path_patch() -> None:
    """Make deepagents' ``validate_path`` tolerate host paths inside the project.

    The model frequently passes a real host absolute path (e.g.
    ``B:/…/novacode_cli/prompts/plan_agent.jinja``) to a file tool because it
    sees such paths everywhere (IDE context, ``@mentions``, traces).
    ``FilesystemMiddleware`` validates the path *before* the backend via
    ``validate_path``, which rejects any drive-letter path outright:

        "Windows absolute paths are not supported: B:/… Please use virtual
         paths starting with / (e.g., /workspace/file.txt)"

    This wraps ``validate_path`` so any host path at/under the current project
    root is first rewritten to its ``/``-rooted virtual form (see
    :func:`novacode_cli.integrations.host_path.host_path_to_virtual`). Paths that
    are already virtual, relative, or outside the project are passed through
    unchanged, so genuinely-invalid paths still raise the original helpful error.

    Idempotent and best-effort: a missing/renamed symbol just leaves the stock
    behavior in place.
    """
    global _fs_host_path_patched
    if _fs_host_path_patched:
        return

    try:
        import deepagents.middleware.filesystem as _fsmod
    except ImportError:
        return

    _original = getattr(_fsmod, "validate_path", None)
    if _original is None:
        return

    from novacode_cli.integrations.host_path import host_path_to_virtual

    def _current_workspace_root() -> str | None:
        try:
            from novacode_cli.config.config import settings

            return str(settings.get_workspace_root())
        except Exception:  # noqa: BLE001
            return None

    @functools.cache
    def _mount_roots() -> tuple[tuple[str, str], ...]:
        """Extra ``(host_root, virtual_prefix)`` mounts served outside the project.

        Mirrors the skill routes wired in ``core_agent.py`` so the agent can pass
        a skill's host path (``~/.nova/skills/x/SKILL.md``) instead of its virtual
        path (``/skills/x/SKILL.md``) and still have it resolve. Cached — the dirs
        are fixed for the session.
        """
        from novacode_cli.config.config import settings

        pairs: list[tuple[str, str]] = []
        for getter, prefix in (
            (settings.ensure_user_skills_dir, "/skills/"),
            (settings.get_global_claude_skills_dir, "/claude-skills/"),
        ):
            try:
                d = getter()
                if d:
                    pairs.append((str(d), prefix))
            except Exception:  # noqa: BLE001
                pass
        return tuple(pairs)

    def _is_real_host_path(path: str) -> bool:
        """True if *path* names a real location on the host filesystem.

        The path itself need only exist *or* have an existing parent directory —
        ``write_file`` creates new files, so requiring the file to exist would
        allow reads of outside paths while still refusing to create anything
        next to them.

        A virtual path, a relative path, or a typo in a nonexistent directory
        still falls through to the stock validator.
        """
        from pathlib import Path

        try:
            p = Path(path)
            if not p.is_absolute():
                return False
            return p.exists() or p.parent.exists()
        except OSError:
            return False

    def _patched_validate_path(path: Any, *, allowed_prefixes: Any = None) -> str:
        if isinstance(path, str):
            try:
                root = _current_workspace_root()
                rewritten = host_path_to_virtual(path, root or "", list(_mount_roots()))
            except Exception:  # noqa: BLE001
                rewritten = path  # never let normalization break validation

            if rewritten != path:
                # Inside the workspace (or a mounted skills dir): use the virtual
                # form, which keeps every existing route working.
                path = rewritten
            elif _is_real_host_path(path):
                # Outside the workspace, but a real file/dir on disk. deepagents
                # refuses drive-letter paths outright, which made reading a
                # sibling project impossible ("Windows absolute paths are not
                # supported: B:/…/CV.pdf") for reads AND writes. Pass it through:
                # drive roots are mounted as routes (see core_agent), and the
                # approval policy gates access — write_file/edit_file default to
                # "ask" and system/secret globs are denied. Same model as Claude
                # Code: real paths, gated by permissions, not by path rewriting.
                #
                # allowed_prefixes is deliberately not applied — it scopes
                # VIRTUAL routes, and a host path belongs to none of them.
                return str(path).replace("\\", "/")

        return _original(path, allowed_prefixes=allowed_prefixes)

    _fsmod.validate_path = _patched_validate_path
    _fs_host_path_patched = True
    logger.debug("Applied filesystem host-path normalization patch")


def apply_ollama_content_block_patch() -> None:
    """Monkey-patch langchain_ollama to handle 'file' type content blocks.

    The stock _get_image_from_data_content_block only supports type="image"
    and raises ValueError for any other type. We patch the message-conversion
    loop to:
    - Skip file/audio/video content blocks with a log message instead of crashing
    - Filter out empty image entries that result from skipped blocks
    """
    global _patched
    if _patched:
        return

    try:
        import langchain_ollama.chat_models as _ollama_mod
    except ImportError:
        return

    # Patch 1: Make _get_image_from_data_content_block tolerate non-image blocks
    _original_fn = getattr(_ollama_mod, "_get_image_from_data_content_block", None)
    if _original_fn is None:
        return

    _SKIP_SENTINEL = object()  # Returned for blocks that should be skipped entirely

    def _patched_get_image_from_data_content_block(block: dict) -> Any:
        """Handle image and file content blocks for Ollama message conversion.

        Returns base64 image data for image blocks.
        For file blocks (PDF, etc.) and other unsupported types, returns the
        _SKIP_SENTINEL object so the caller can filter it from the images list.
        """
        block_type = block.get("type", "unknown")

        if block_type == "image":
            return _original_fn(block)

        if block_type == "file":
            mime_type = block.get("mime_type", "unknown")
            logger.info(
                f"Skipping unsupported 'file' content block (mime_type={mime_type}) "
                f"in Ollama message conversion. PDF text extraction should have "
                f"been handled upstream by FileTrackerMiddleware."
            )
            return _SKIP_SENTINEL

        logger.warning(
            f"Skipping unsupported content block type '{block_type}' "
            f"in Ollama message conversion"
        )
        return _SKIP_SENTINEL

    _ollama_mod._get_image_from_data_content_block = _patched_get_image_from_data_content_block

    # Patch 2: Wrap _convert_messages_to_ollama_messages to filter sentinel values
    # from the images list. Without this, sentinel objects would end up in the
    # API payload sent to Ollama.
    _original_convert = getattr(
        _ollama_mod.ChatOllama, "_convert_messages_to_ollama_messages", None
    )
    if _original_convert is None:
        _patched = True
        return

    def _patched_convert(self: Any, messages: Any) -> list[dict[str, Any]]:
        result = _original_convert(self, messages)
        # Filter out sentinel objects from images lists in each message
        for msg_dict in result:
            if "images" in msg_dict:
                filtered = [
                    img for img in msg_dict["images"]
                    if img is not _SKIP_SENTINEL
                ]
                # Remove images key entirely if no images remain
                if filtered:
                    msg_dict["images"] = filtered
                else:
                    msg_dict.pop("images", None)
        return result

    _ollama_mod.ChatOllama._convert_messages_to_ollama_messages = _patched_convert  # type: ignore

    # Also patch the async variant if it exists (some versions split sync/async paths)
    _original_aconvert = getattr(
        _ollama_mod.ChatOllama, "_aconvert_messages_to_ollama_messages", None
    )
    if _original_aconvert is not None:
        async def _patched_aconvert(self: Any, messages: Any) -> list[dict[str, Any]]:
            result = await _original_aconvert(self, messages)
            for msg_dict in result:
                if "images" in msg_dict:
                    filtered = [
                        img for img in msg_dict["images"]
                        if img is not _SKIP_SENTINEL
                    ]
                    if filtered:
                        msg_dict["images"] = filtered
                    else:
                        msg_dict.pop("images", None)
            return result

        _ollama_mod.ChatOllama._aconvert_messages_to_ollama_messages = _patched_aconvert  # type: ignore

    _patched = True
    logger.debug("Applied Ollama content block + message-conversion patch for file type support")


#: Fields a thinking model may return that the API then requires echoed back.
#: Only ever re-attached when the model actually produced them, so a provider
#: that has never heard of them is never sent one.
_REASONING_FIELDS = ("reasoning_content", "reasoning")


def apply_openai_reasoning_content_patch() -> None:
    """Echo ``reasoning_content`` back to OpenAI-compatible thinking models.

    DeepSeek- and GLM-style reasoning models return their chain of thought in a
    ``reasoning_content`` field beside ``content``, and then REQUIRE it to be
    sent back with that assistant message on the following turn:

        400 invalid_request_error — "The `reasoning_content` in the thinking
        mode must be passed back to the API."

    ``langchain_openai`` drops the field at BOTH ends, and deliberately — its
    own module docstring says non-standard provider fields "are **not**
    extracted or preserved", with a pointer to ``ChatDeepSeek`` instead. So
    three converters need patching, not one:

    * ``_convert_delta_to_message_chunk`` — streaming responses. It builds
      ``additional_kwargs`` from ``function_call`` and ``tool_calls`` only, so
      the reasoning never reaches the message at all. This is the one that
      matters: Nova streams.
    * ``_convert_dict_to_message`` — the non-streaming equivalent.
    * ``_convert_message_to_dict`` — the outbound payload, which emits only
      role/content/tool_calls.

    Patching only the outbound side (as this did originally) fixes nothing:
    there is nothing in ``additional_kwargs`` left to echo.

    Every arm re-attaches the field only when the model itself produced it.
    That guard is what makes this safe to apply to every OpenAI-compatible
    provider: a model that never returns reasoning_content never gets sent one,
    so strict endpoints (OpenAI's own) see exactly the payload they see today.

    Streaming arrives in pieces, one fragment of reasoning per delta. Each lands
    in that chunk's ``additional_kwargs``; LangChain's chunk merge concatenates
    same-key strings, so the reassembled message carries the whole thought.
    """
    global _reasoning_content_patched
    if _reasoning_content_patched:
        return

    try:
        import langchain_openai.chat_models.base as _oai_base
    except ImportError:
        return

    original = getattr(_oai_base, "_convert_message_to_dict", None)
    if original is None:  # pragma: no cover - upstream renamed it
        logger.debug("No _convert_message_to_dict to patch; skipping")
        return

    @functools.wraps(original)
    def _convert_with_reasoning(message: Any) -> dict:
        payload = original(message)
        extra = getattr(message, "additional_kwargs", None)
        if isinstance(extra, dict) and isinstance(payload, dict):
            for field in _REASONING_FIELDS:
                value = extra.get(field)
                # Empty string / None means "the model returned nothing here";
                # sending an empty field is not the same as sending the thought.
                if value and field not in payload:
                    payload[field] = value
        return payload

    _oai_base._convert_message_to_dict = _convert_with_reasoning

    def _keep_reasoning(original_fn):  # noqa: ANN001, ANN202
        """Wrap an inbound converter so it preserves the reasoning fields."""

        @functools.wraps(original_fn)
        def _inbound(_dict: Any, *args: Any, **kwargs: Any) -> Any:
            message = original_fn(_dict, *args, **kwargs)
            extra = getattr(message, "additional_kwargs", None)
            if isinstance(_dict, dict) and isinstance(extra, dict):
                for field in _REASONING_FIELDS:
                    value = _dict.get(field)
                    if value:
                        extra[field] = value
            return message

        return _inbound

    for name in ("_convert_delta_to_message_chunk", "_convert_dict_to_message"):
        inbound = getattr(_oai_base, name, None)
        if inbound is not None:
            setattr(_oai_base, name, _keep_reasoning(inbound))
        else:  # pragma: no cover - upstream renamed it
            logger.debug("No %s to patch; reasoning may not round-trip", name)

    _reasoning_content_patched = True
    logger.debug("Applied OpenAI reasoning_content round-trip patch")
