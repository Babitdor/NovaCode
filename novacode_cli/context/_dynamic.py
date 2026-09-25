"""Dynamic context-window detection for Ollama models.

Private to the ``context`` package. Probes a locally-installed Ollama for a
model's real context length via ``ollama show``, cached for the process
lifetime. Used by :mod:`novacode_cli.context._analysis` as the first source for
``get_context_window_size`` (Ollama models only).
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
from functools import lru_cache
from typing import Optional

logger = logging.getLogger(__name__)

# The context window Nova requests when loading an Ollama model. This is the
# REAL hard limit: requests are truncated at ``num_ctx`` tokens regardless of the
# model's trained ("architecture") context length. The same value must be used
# both when creating the ChatOllama client AND when sizing the context window for
# usage tracking, or the two disagree (the bug this fixes). Overridable via the
# ``OLLAMA_NUM_CTX`` env var for machines that can't allocate a 200K KV cache, or
# power users who want more.
DEFAULT_OLLAMA_NUM_CTX = 200_000


def is_ollama_cloud_model(model_name: str) -> bool:
    """Whether a model name refers to an Ollama *cloud* model (``:cloud`` suffix).

    Cloud models (e.g. ``qwen3-coder:480b-cloud``, ``deepseek-v4-pro:cloud``) run
    entirely on Ollama's servers — they are never loaded into local VRAM, so they
    don't appear in ``ollama ps`` and the local ``num_ctx`` allocation doesn't
    apply. Their context length comes from ``ollama show`` (which works for cloud
    models) and is the model's maximum.
    """
    name = model_name.lower()
    return name.endswith("-cloud") or name.endswith(":cloud")


def get_ollama_num_ctx() -> int:
    """The ``num_ctx`` Nova loads Ollama models with (env ``OLLAMA_NUM_CTX``).

    Returns ``DEFAULT_OLLAMA_NUM_CTX`` when unset or malformed.
    """
    raw = os.environ.get("OLLAMA_NUM_CTX", "").strip()
    if raw:
        try:
            value = int(raw)
            if value > 0:
                return value
        except ValueError:
            logger.warning("Invalid OLLAMA_NUM_CTX=%r; using default", raw)
    return DEFAULT_OLLAMA_NUM_CTX


@lru_cache(maxsize=128)
def get_ollama_context_length(model_name: str) -> Optional[int]:
    """Get context length for an Ollama model dynamically.

    Uses caching to avoid repeated Ollama queries. Results are cached
    for the lifetime of the process.

    Args:
        model_name: Name of the Ollama model (e.g., "glm-5:cloud")

    Returns:
        Context length in tokens, or None if not found.
    """
    try:
        result = subprocess.run(
            ["ollama", "show", model_name],
            capture_output=True,
            text=True,
            # Answers in ~0.1s when healthy; a busy daemon can hang. A failure is
            # cached for the session anyway, so waiting longer buys nothing.
            timeout=3,
        )

        if result.returncode != 0:
            logger.warning(f"Failed to get context for {model_name}: {result.stderr}")
            return None

        output = result.stdout

        # Look for a "context length" line, e.g. "context length      202752"
        for line in output.split("\n"):
            if "context" in line.lower() and "length" in line.lower():
                match = re.search(r"(\d+)", line)
                if match:
                    context_length = int(match.group(1))
                    logger.info(
                        f"Detected context length for {model_name}: "
                        f"{context_length:,} tokens"
                    )
                    return context_length

        logger.warning(f"No context length found for {model_name}")
        return None

    except subprocess.TimeoutExpired:
        logger.error(f"Timeout getting context for {model_name}")
        return None
    except FileNotFoundError:
        logger.error("Ollama not found. Make sure Ollama is installed and in PATH.")
        return None
    except Exception as e:  # noqa: BLE001
        logger.error(f"Error getting context for {model_name}: {e}")
        return None


def _names_match(ps_name: str, model_name: str) -> bool:
    """Whether an ``ollama ps`` NAME refers to the requested model.

    Handles tag differences: ``gemma3`` vs ``gemma3:latest``,
    ``qwen3-coder:480b-cloud`` vs ``qwen3-coder``.
    """
    ps_name = ps_name.strip()
    model_name = model_name.strip()
    if ps_name == model_name:
        return True
    return ps_name.split(":", 1)[0] == model_name.split(":", 1)[0]


def get_ollama_runtime_info(model_name: str) -> Optional[dict]:
    """Parse ``ollama ps`` for the running model's actual allocated state.

    ``ollama ps`` reports the *real* runtime values for a loaded model — the
    ``CONTEXT`` actually allocated (which Ollama may clamp below the requested
    ``num_ctx`` to fit VRAM) and the ``PROCESSOR`` split (e.g. ``100% GPU`` vs a
    CPU-offloaded ``48%/52% CPU/GPU``). This is the most accurate source for both
    context-usage tracking and performance diagnostics.

    Not cached: a model loads after the first request and its allocation can
    change between runs, so each call reflects the current state.

    Returns:
        A dict with any of ``{"name", "context", "processor"}`` for the matching
        model, or ``None`` if Ollama isn't running, has no such model loaded, or
        the installed Ollama predates the ``CONTEXT`` column.
    """
    try:
        result = subprocess.run(
            ["ollama", "ps"], capture_output=True, text=True, timeout=3
        )
        if result.returncode != 0:
            return None
        output = result.stdout
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return None

    # On Windows `capture_output=True` can yield stdout=None even with a 0
    # returncode, so guard before splitting rather than crashing on None.
    if not output:
        return None

    lines = [ln for ln in output.splitlines() if ln.strip()]
    if len(lines) < 2:  # header only (nothing loaded) or empty
        return None

    # Columns are separated by runs of 2+ spaces; single spaces inside a field
    # ("6.6 GB", "100% GPU", "2 minutes from now") are preserved.
    headers = re.split(r"\s{2,}", lines[0].strip())
    col = {h.upper(): i for i, h in enumerate(headers)}
    name_i = col.get("NAME", 0)
    ctx_i = col.get("CONTEXT")
    proc_i = col.get("PROCESSOR")

    for line in lines[1:]:
        fields = re.split(r"\s{2,}", line.strip())
        if name_i >= len(fields):
            continue
        if not _names_match(fields[name_i], model_name):
            continue
        info: dict = {"name": fields[name_i]}
        if ctx_i is not None and ctx_i < len(fields):
            m = re.search(r"\d+", fields[ctx_i])
            if m:
                info["context"] = int(m.group())
        if proc_i is not None and proc_i < len(fields):
            info["processor"] = fields[proc_i].strip()
        return info

    return None


def get_ollama_running_context(model_name: str) -> Optional[int]:
    """The actual allocated context length for a loaded Ollama model, or None.

    Sourced from ``ollama ps`` (see :func:`get_ollama_runtime_info`); this is the
    authoritative window — what the model was really loaded with — superseding
    both the architecture max and the requested ``num_ctx``.
    """
    info = get_ollama_runtime_info(model_name)
    return info.get("context") if info else None


def check_ollama_offloading(model_name: str) -> Optional[str]:
    """Return a warning if a loaded Ollama model isn't fully on the GPU.

    CPU offloading (PROCESSOR not ``100% GPU``) drastically slows generation.
    Returns ``None`` when the model is fully GPU-resident, not loaded, or the
    PROCESSOR split is unavailable.
    """
    info = get_ollama_runtime_info(model_name)
    if not info:
        return None
    processor = info.get("processor")
    if not processor or "CPU" not in processor.upper():
        return None  # fully on GPU (or no split reported)
    return (
        f"Ollama model '{info.get('name', model_name)}' is partially offloaded to "
        f"CPU (PROCESSOR: {processor}) — generation will be slow. Free VRAM, lower "
        f"OLLAMA_NUM_CTX, or use a smaller model to keep it 100% on GPU."
    )
