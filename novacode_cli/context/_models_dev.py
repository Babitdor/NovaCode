"""models.dev — dynamic model-metadata catalog (OpenCode's model database).

OpenCode / OpenRouter gateway models (glm, kimi, deepseek, MiMo, …) aren't in
Nova's hardcoded context table, and those gateways' OpenAI ``/models`` endpoints
return no context field — so their context window fell back to the 128K default
and ctx% ran past 100%. models.dev (maintained by the OpenCode team; the source
the OpenCode CLI itself uses) publishes every model's ``limit.context``. This
fetches that catalog once (network → weekly disk cache → in-memory) and looks a
model id up across all providers, since the ids are shared. Best-effort: offline
or unknown → ``None`` and the caller falls back.
"""

from __future__ import annotations

import json
import logging
import time
import urllib.request
from pathlib import Path

logger = logging.getLogger("nova.models_dev")

_URL = "https://models.dev/api.json"
_DISK = Path.home() / ".nova" / "models_dev.json"
_TTL = 7 * 86_400  # refresh weekly
# One shared in-process cache: data + whether we already tried the network this run.
_state: dict = {"data": None, "net_tried": False}


def _load_catalog() -> dict:
    """Return the models.dev catalog: in-memory → fresh disk cache → network."""
    if _state["data"] is not None:
        return _state["data"]

    # Fresh disk cache (avoids a network hit on every restart).
    try:
        if _DISK.exists() and (time.time() - _DISK.stat().st_mtime) < _TTL:
            _state["data"] = json.loads(_DISK.read_text(encoding="utf-8"))
            return _state["data"]
    except Exception:  # noqa: BLE001
        pass

    # Network, at most once per process.
    if not _state["net_tried"]:
        _state["net_tried"] = True
        try:
            # models.dev returns 403 to the default urllib UA — send a browser one.
            req = urllib.request.Request(_URL, headers={"User-Agent": "Mozilla/5.0 (Nova-Code)"})
            with urllib.request.urlopen(req, timeout=10) as r:  # noqa: S310 — fixed https URL
                data = json.load(r)
            if isinstance(data, dict) and data:
                _state["data"] = data
                try:
                    _DISK.parent.mkdir(parents=True, exist_ok=True)
                    _DISK.write_text(json.dumps(data), encoding="utf-8")
                except Exception:  # noqa: BLE001
                    pass
                return data
        except Exception as e:  # noqa: BLE001
            logger.debug("models.dev fetch failed: %s", e)

    # Network failed — fall back to a stale disk copy if one exists.
    try:
        if _DISK.exists():
            _state["data"] = json.loads(_DISK.read_text(encoding="utf-8"))
            return _state["data"]
    except Exception:  # noqa: BLE001
        pass
    _state["data"] = _state["data"] or {}
    return _state["data"]


def get_models_dev_context(model_name: str) -> int | None:
    """Max context length for *model_name* from models.dev (searched across every
    provider, since OpenCode/OpenRouter share model ids). ``None`` if unknown."""
    if not model_name:
        return None
    catalog = _load_catalog()
    if not isinstance(catalog, dict) or not catalog:
        return None
    lower = model_name.strip().lower()
    # Command Code prefixes the org ("zai-org/GLM-5.3"): match on the last segment.
    tail = lower.rsplit("/", 1)[-1]
    best: int | None = None
    for provider in catalog.values():
        models = provider.get("models") if isinstance(provider, dict) else None
        if not isinstance(models, dict):
            continue
        for mid, entry in models.items():
            ml = mid.lower()
            # OpenCode/OpenRouter use bare ids ("glm-5.3"); models.dev may prefix
            # them ("deepseek/deepseek-v4-flash"), so match the last path segment.
            if ml != lower and ml.rsplit("/", 1)[-1] != tail:
                continue
            if isinstance(entry, dict):
                ctx = (entry.get("limit") or {}).get("context")
                if isinstance(ctx, int) and ctx > 0:
                    best = ctx if best is None else max(best, ctx)
    return best
