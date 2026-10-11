"""Regression tests for gateway-model context-window sizing (the 216% bug).

OpenCode / OpenRouter gateway models (glm, kimi, deepseek, …) aren't in Nova's
hardcoded table and their ``/models`` endpoints report no context field. They
were misclassified as *local* Ollama and capped at ``min(128K default, num_ctx)``
— so a 1M-context model read as ~128K and ctx% ran past 100% (the reported
216%), which in turn drove the auto-compaction loop.

The fix: for a gateway model, consult models.dev (OpenCode's own catalog) and
return the real published window *uncapped* by the local ``num_ctx``. These tests
pin that path with an injected catalog (no network) plus the ollama probes
neutralised, and assert the mis-cap can never come back.

Runnable directly (``python tests/test_models_dev_context.py``) or via pytest.
"""

from __future__ import annotations

import pytest

import novacode_cli.context._models_dev as md
from novacode_cli.context._analysis import (
    build_context_breakdown,
    model_window_size,
)
from novacode_cli.context._models_dev import get_models_dev_context

# models.dev catalog shape: {provider: {"models": {id: {"limit": {"context": N}}}}}.
# glm-5.3 appears under two providers with different windows → the max must win.
# deepseek's id is path-prefixed → must match on the last segment.
_FAKE_CATALOG = {
    "opencode": {
        "models": {
            "glm-5.3": {"limit": {"context": 1_048_576}},
            "kimi-k2": {"limit": {"context": 262_144}},
            "no-limit-model": {"name": "broken"},  # missing limit → skipped
        }
    },
    "deepseek": {
        "models": {
            "deepseek/deepseek-v4-flash": {"limit": {"context": 131_072}},
        }
    },
    "other": {
        "models": {
            "glm-5.3": {"limit": {"context": 200_000}},  # smaller dup
        }
    },
}


def _inject_catalog(monkeypatch, catalog: dict) -> None:
    """Force ``_load_catalog`` to return *catalog* without any network/disk I/O."""
    monkeypatch.setattr(md, "_state", {"data": catalog, "net_tried": True})


def _neutralise_ollama(monkeypatch, num_ctx: int = 4096) -> None:
    """Make every Ollama probe a fast no-op so the gateway path is deterministic.

    ``num_ctx`` is set small on purpose: if the models.dev window ever gets
    capped by it again, the uncapped assertions below fail loudly.
    """
    dyn = "novacode_cli.context._dynamic."
    monkeypatch.setattr(dyn + "is_ollama_cloud_model", lambda _m: False)
    monkeypatch.setattr(dyn + "get_ollama_running_context", lambda _m: None)
    monkeypatch.setattr(dyn + "get_ollama_context_length", lambda _m: None)
    monkeypatch.setattr(dyn + "get_ollama_num_ctx", lambda: num_ctx)


# ── catalog lookup ───────────────────────────────────────────────────────────


def test_exact_match_returns_max_across_providers(monkeypatch):
    _inject_catalog(monkeypatch, _FAKE_CATALOG)
    # glm-5.3 is in two providers (1M and 200K) — the larger window wins.
    assert get_models_dev_context("glm-5.3") == 1_048_576


def test_suffix_match_for_path_prefixed_ids(monkeypatch):
    _inject_catalog(monkeypatch, _FAKE_CATALOG)
    # Gateway sends the bare id; models.dev stores it prefixed.
    assert get_models_dev_context("deepseek-v4-flash") == 131_072


def test_case_insensitive(monkeypatch):
    _inject_catalog(monkeypatch, _FAKE_CATALOG)
    assert get_models_dev_context("GLM-5.3") == 1_048_576


@pytest.mark.parametrize(
    "name",
    [
        pytest.param("model-that-does-not-exist", id="unknown-model"),
        pytest.param("", id="empty-name"),
        pytest.param("no-limit-model", id="entry-missing-limit"),
    ],
)
def test_lookup_returns_none(monkeypatch, name):
    # A present catalog that simply has no usable window for this id → None,
    # so the caller falls back rather than trusting a bogus number.
    _inject_catalog(monkeypatch, _FAKE_CATALOG)
    assert get_models_dev_context(name) is None


def test_empty_catalog_returns_none(monkeypatch):
    # Offline with no disk cache → empty dict → None (caller falls back).
    _inject_catalog(monkeypatch, {})
    assert get_models_dev_context("glm-5.3") is None


# ── window resolution (the actual bug) ───────────────────────────────────────


def test_gateway_model_uses_models_dev_uncapped(monkeypatch):
    _inject_catalog(monkeypatch, _FAKE_CATALOG)
    _neutralise_ollama(monkeypatch, num_ctx=4096)
    win = model_window_size("glm-5.3", use_dynamic=True)
    # The real 1M window — NOT the 128K default and NOT capped to num_ctx (4096).
    assert win == 1_048_576


def test_gateway_216_percent_cannot_recur(monkeypatch):
    """With the real window, a heavy conversation stays well under 100%."""
    _inject_catalog(monkeypatch, _FAKE_CATALOG)
    _neutralise_ollama(monkeypatch, num_ctx=4096)
    # With no working budget, so this measures the model's own window.
    from novacode_cli.config.nova_config import NovaConfig

    monkeypatch.setattr(
        NovaConfig, "get", lambda _s, key, default=None: 0 if key == "context_budget_tokens" else default
    )
    from langchain_core.messages import HumanMessage

    # ~250K tokens of conversation. Against the OLD wrong 128K window this reads
    # ~200% (the reported 216%); against the real 1M window it's a healthy ~25%.
    msgs = [HumanMessage(content="X" * 1_000_000)]
    bd = build_context_breakdown(msgs, "glm-5.3", use_dynamic=True)
    assert bd.context_window_size == 1_048_576
    assert bd.usage_percentage < 100
    assert not bd.is_critical


def test_cloud_api_model_bypasses_models_dev(monkeypatch):
    # A poisoned catalog claim for claude must be ignored — cloud-API models
    # never take the gateway path; they use their known published window.
    _inject_catalog(
        monkeypatch,
        {"x": {"models": {"claude-opus-4-8": {"limit": {"context": 999}}}}},
    )
    _neutralise_ollama(monkeypatch)
    assert model_window_size("claude-opus-4-8", use_dynamic=True) == 200_000


def test_unknown_gateway_model_falls_back_to_default(monkeypatch):
    # Not in models.dev and not ollama-known → hardcoded 128K default, no crash.
    # (num_ctx set generous so the default surfaces uncapped; an unknown name is
    # treated as local Ollama, where min(window, num_ctx) legitimately applies.)
    _inject_catalog(monkeypatch, _FAKE_CATALOG)
    _neutralise_ollama(monkeypatch, num_ctx=1_000_000)
    assert model_window_size("brand-new-gateway-model", use_dynamic=True) == 128_000


if __name__ == "__main__":
    import sys

    import pytest

    sys.exit(pytest.main([__file__, "-v", "--assert=plain"]))
