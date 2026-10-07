"""Small compatibility fixes for measured startup costs."""
# ruff: noqa: INP001 -- utils is an existing namespace package

from __future__ import annotations

from functools import partial


def apply_deepagents_version_scan_patch() -> None:
    """Restrict Deep Agents' version scan to its own distributions.

    Deep Agents 0.7.10 enumerates every installed distribution and reads each
    METADATA file to identify its editable install. On Windows this measured
    3.7 seconds at the first graph build. importlib.metadata's name filter
    selects the same matching records without reading unrelated metadata.
    Unlike a single distribution lookup, it still finds duplicate records
    and preserves the upstream source-root check for editable installations.
    """
    try:
        import deepagents._version as version
    except ImportError:
        return

    original = getattr(version, "distributions", None)
    if not callable(original) or getattr(original, "_nova_name_filtered", False):
        return
    filtered = partial(original, name="deepagents")
    filtered._nova_name_filtered = True
    version.distributions = filtered
