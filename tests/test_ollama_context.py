"""Tests for Ollama context-window tracking and offload detection.

Covers the num_ctx single-source-of-truth, `ollama ps` runtime parsing
(allocated CONTEXT + PROCESSOR split), and the effective-window logic in
get_context_window_size.
"""

from types import SimpleNamespace

import pytest

from novacode_cli.context import _dynamic
from novacode_cli.context._analysis import get_context_window_size

PS_HEADER = "NAME             ID              SIZE      PROCESSOR    CONTEXT    UNTIL"
PS_GPU = (
    "gemma3:latest    a2af6cc3eb7f    6.6 GB    100% GPU     65536      2 minutes from now"
)
PS_CPU = (
    "qwen3:8b         deadbeef0000    9.0 GB    48%/52% CPU/GPU  40960   4 minutes from now"
)


def _fake_ps(output: str, returncode: int = 0):
    def _run(cmd, *a, **k):
        return SimpleNamespace(returncode=returncode, stdout=output, stderr="")

    return _run


# ── num_ctx single source of truth ──────────────────────────────────────────


def test_num_ctx_default(monkeypatch):
    monkeypatch.delenv("OLLAMA_NUM_CTX", raising=False)
    assert _dynamic.get_ollama_num_ctx() == _dynamic.DEFAULT_OLLAMA_NUM_CTX


def test_num_ctx_env_override(monkeypatch):
    monkeypatch.setenv("OLLAMA_NUM_CTX", "32768")
    assert _dynamic.get_ollama_num_ctx() == 32768


def test_num_ctx_invalid_falls_back(monkeypatch):
    monkeypatch.setenv("OLLAMA_NUM_CTX", "not-a-number")
    assert _dynamic.get_ollama_num_ctx() == _dynamic.DEFAULT_OLLAMA_NUM_CTX
    monkeypatch.setenv("OLLAMA_NUM_CTX", "-5")
    assert _dynamic.get_ollama_num_ctx() == _dynamic.DEFAULT_OLLAMA_NUM_CTX


# ── ollama ps parsing ───────────────────────────────────────────────────────


def test_runtime_info_parses_context_and_processor(monkeypatch):
    monkeypatch.setattr(_dynamic.subprocess, "run", _fake_ps(PS_HEADER + "\n" + PS_GPU))
    info = _dynamic.get_ollama_runtime_info("gemma3")
    assert info["context"] == 65536
    assert info["processor"] == "100% GPU"
    assert _dynamic.get_ollama_running_context("gemma3:latest") == 65536


def test_runtime_info_none_when_not_loaded(monkeypatch):
    monkeypatch.setattr(_dynamic.subprocess, "run", _fake_ps(PS_HEADER))  # header only
    assert _dynamic.get_ollama_runtime_info("gemma3") is None
    assert _dynamic.get_ollama_running_context("gemma3") is None


def test_offload_warns_on_cpu_split(monkeypatch):
    monkeypatch.setattr(_dynamic.subprocess, "run", _fake_ps(PS_HEADER + "\n" + PS_CPU))
    warning = _dynamic.check_ollama_offloading("qwen3:8b")
    assert warning and "CPU" in warning


def test_offload_silent_when_full_gpu(monkeypatch):
    monkeypatch.setattr(_dynamic.subprocess, "run", _fake_ps(PS_HEADER + "\n" + PS_GPU))
    assert _dynamic.check_ollama_offloading("gemma3") is None


def test_runtime_info_handles_missing_context_column(monkeypatch):
    # Older Ollama without a CONTEXT column must not crash.
    old_header = "NAME             ID              SIZE      PROCESSOR    UNTIL"
    old_row = "gemma3:latest    a2af6cc3eb7f    6.6 GB    100% GPU     2 minutes from now"
    monkeypatch.setattr(_dynamic.subprocess, "run", _fake_ps(old_header + "\n" + old_row))
    info = _dynamic.get_ollama_runtime_info("gemma3")
    assert info is not None
    assert "context" not in info  # no CONTEXT column → no context key
    assert info["processor"] == "100% GPU"


# ── effective window sizing ─────────────────────────────────────────────────


def test_running_context_is_authoritative(monkeypatch):
    monkeypatch.setattr(_dynamic.subprocess, "run", _fake_ps(PS_HEADER + "\n" + PS_GPU))
    # gemma3 arch is large + num_ctx is 200k, but ps says 65536 is allocated.
    assert get_context_window_size("gemma3", use_dynamic=True) == 65536


def test_local_window_capped_by_num_ctx_when_not_loaded(monkeypatch):
    monkeypatch.setenv("OLLAMA_NUM_CTX", "100000")
    # use_dynamic=False → skip subprocess; local llama3.1 arch (131072) capped to 100000.
    assert get_context_window_size("llama3.1", use_dynamic=False) == 100000


def test_local_window_uses_arch_when_smaller_than_num_ctx(monkeypatch):
    monkeypatch.setenv("OLLAMA_NUM_CTX", "200000")
    # local qwen3 arch is 40960 < 200000 → effective is the arch ceiling.
    assert get_context_window_size("qwen3", use_dynamic=False) == 40960


def test_cloud_api_model_not_capped(monkeypatch):
    monkeypatch.setenv("OLLAMA_NUM_CTX", "8000")
    # Claude is a cloud API model — num_ctx must NOT apply.
    assert get_context_window_size("claude-opus-4-8", use_dynamic=False) == 200_000


def test_ollama_cloud_model_uses_max_uncapped(monkeypatch):
    monkeypatch.setenv("OLLAMA_NUM_CTX", "8000")
    # `:cloud` models run on Ollama's servers — the window is the model MAX
    # (from the table / ollama show), never capped by the local num_ctx.
    assert get_context_window_size("qwen3-coder:480b-cloud", use_dynamic=False) == 262_144


def test_ollama_cloud_model_skips_ollama_ps(monkeypatch):
    # A cloud model isn't loaded locally, so `ollama ps` must never be invoked
    # for it (only `ollama show` is valid). Record the commands to prove it.
    calls: list[list[str]] = []

    def _track(cmd, *a, **k):
        calls.append(cmd)
        return SimpleNamespace(returncode=1, stdout="", stderr="")

    monkeypatch.setattr(_dynamic.subprocess, "run", _track)
    monkeypatch.setenv("OLLAMA_NUM_CTX", "8000")
    win = get_context_window_size("qwen3-coder:480b-cloud", use_dynamic=True)
    assert win == 262_144  # table max, uncapped
    assert not any("ps" in c for c in calls), f"ollama ps was called: {calls}"


def test_is_ollama_cloud_model():
    assert _dynamic.is_ollama_cloud_model("qwen3-coder:480b-cloud")
    assert _dynamic.is_ollama_cloud_model("deepseek-v4-pro:cloud")
    assert not _dynamic.is_ollama_cloud_model("qwen3")
    assert not _dynamic.is_ollama_cloud_model("claude-opus-4-8")


# ── a local model's context is the SERVER's to decide ──────────────────────
# Nova reaches Ollama through its OpenAI-compatible endpoint, which cannot carry
# num_ctx. Measured against a live server: `tev1:0.8b` (Modelfile `num_ctx 2050`)
# was allocated 2,050 tokens while Nova, still assuming the 200,000 it used to
# request, sized its whole session for a window 100x too large.


def _show_parameters(text: str, returncode: int = 0):
    def _run(cmd, *a, **k):
        if "--parameters" in cmd:
            return SimpleNamespace(returncode=returncode, stdout=text, stderr="")
        return SimpleNamespace(returncode=1, stdout="", stderr="")

    return _run


def test_an_unloaded_local_model_uses_its_modelfile_num_ctx(monkeypatch):
    _dynamic.get_ollama_modelfile_num_ctx.cache_clear()
    monkeypatch.setattr(_dynamic.subprocess, "run", _show_parameters("num_ctx    2050\ntemperature    0.7\n"))
    monkeypatch.delenv("OLLAMA_CONTEXT_LENGTH", raising=False)
    assert get_context_window_size("qwen3", use_dynamic=True) == 2050
    _dynamic.get_ollama_modelfile_num_ctx.cache_clear()


def test_without_a_modelfile_value_the_servers_context_length_is_used(monkeypatch):
    _dynamic.get_ollama_modelfile_num_ctx.cache_clear()
    monkeypatch.setattr(_dynamic.subprocess, "run", _show_parameters("temperature    0.7\n"))
    monkeypatch.setenv("OLLAMA_CONTEXT_LENGTH", "32768")
    assert get_context_window_size("qwen3", use_dynamic=True) == 32768
    _dynamic.get_ollama_modelfile_num_ctx.cache_clear()


def test_the_architecture_is_still_the_ceiling(monkeypatch):
    _dynamic.get_ollama_modelfile_num_ctx.cache_clear()
    monkeypatch.setattr(_dynamic.subprocess, "run", _show_parameters("num_ctx    999999\n"))
    assert get_context_window_size("qwen3", use_dynamic=True) == 40960
    _dynamic.get_ollama_modelfile_num_ctx.cache_clear()


def test_a_cloud_model_is_untouched_by_local_settings(monkeypatch):
    monkeypatch.setenv("OLLAMA_CONTEXT_LENGTH", "4096")
    assert get_context_window_size("qwen3-coder:480b-cloud", use_dynamic=False) == 262_144
