"""Reviewed, offline provider metadata; importing it never contacts a provider."""

from __future__ import annotations

import json
import os
from pathlib import Path
from string import Template
from typing import Any

_snapshot = json.loads(
    Path(__file__).with_name("provider_catalog.json").read_text(encoding="utf-8")
)
CATALOG_PROVIDERS: dict[str, dict[str, Any]] = _snapshot["providers"]

# These are also excluded from subprocess environments, including credentials
# with unusual names such as ABLIT_KEY and CLARIFAI_PAT.
CATALOG_CREDENTIAL_VARS: frozenset[str] = frozenset(
    var
    for preset in CATALOG_PROVIDERS.values()
    for var in (preset.get("api_key_var"), *preset.get("key_aliases", ()))
    if var
)


def missing_provider_settings(provider: str, model_name: str | None = None) -> list[str]:
    """Return missing endpoint/project settings without probing credentials or the network."""
    preset = CATALOG_PROVIDERS.get(provider, {})
    required = set(preset.get("required_env", ()))
    required.difference_update(Template(preset.get("base_url", "")).get_identifiers())
    override = preset.get("model_overrides", {}).get(model_name or preset.get("default_model"), {})
    template = (
        os.environ.get(preset.get("base_url_env", ""), "").strip()
        or override.get("base_url")
        or preset.get("base_url", "")
    )
    required.update(Template(template).get_identifiers())
    return sorted(var for var in required if not os.environ.get(var, "").strip())
