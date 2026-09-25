"""Ollama probing must not crash when Windows returns stdout=None.

On Windows ``subprocess.run(capture_output=True)`` can yield ``stdout=None``
even with ``returncode == 0``. Unguarded, that surfaced as the opaque agent
error ``'NoneType' object has no attribute 'splitlines'``. These tests pin the
guard on every site that parses Ollama output.
"""

from __future__ import annotations

import subprocess

import pytest


class _FakeCompleted:
    """A CompletedProcess with stdout=None — the Windows failure mode."""

    def __init__(self, returncode: int = 0) -> None:
        self.returncode = returncode
        self.stdout = None
        self.stderr = None
        self.args: list[str] = []


def _patch_run(monkeypatch: pytest.MonkeyPatch, module) -> None:
    """Replace ``module.subprocess.run`` with a None-stdout stub."""

    class _Sub:
        TimeoutExpired = subprocess.TimeoutExpired
        CalledProcessError = subprocess.CalledProcessError

        @staticmethod
        def run(*args, **kwargs):  # noqa: ANN002, ANN003, ANN205
            return _FakeCompleted()

    monkeypatch.setattr(module, "subprocess", _Sub, raising=True)


# ── the reported crash ───────────────────────────────────────────────────────


def test_ollama_runtime_info_survives_none_stdout(monkeypatch):
    """The exact site that raised 'NoneType' ... 'splitlines'."""
    from novacode_cli.context import _dynamic

    _patch_run(monkeypatch, _dynamic)
    assert _dynamic.get_ollama_runtime_info("some-model") is None


def test_ollama_context_length_survives_none_stdout(monkeypatch):
    from novacode_cli.context import _dynamic

    _patch_run(monkeypatch, _dynamic)
    assert _dynamic.get_ollama_context_length("some-model") is None


def test_ollama_offloading_check_survives_none_stdout(monkeypatch):
    from novacode_cli.context import _dynamic

    _patch_run(monkeypatch, _dynamic)
    assert _dynamic.check_ollama_offloading("some-model") is None


# ── the sibling parsers ──────────────────────────────────────────────────────


def test_model_info_models_survives_none_stdout(monkeypatch):
    from novacode_cli.utils import model_info

    _patch_run(monkeypatch, model_info)
    assert model_info.get_ollama_models() == []


def test_model_manager_models_survives_none_stdout(monkeypatch):
    """model_manager already swallows everything and falls back to presets.

    Pinned as a regression guard (not a fix): its broad ``except Exception``
    means a None stdout degrades rather than raising.
    """
    from novacode_cli.config import model_manager

    _patch_run(monkeypatch, model_manager)
    assert isinstance(model_manager.get_ollama_models(), list)


# ── the guard must not mask real output ──────────────────────────────────────


def test_runtime_info_still_parses_real_output(monkeypatch):
    """Sensitivity: the guard must not break the working path."""
    from novacode_cli.context import _dynamic

    stdout = (
        "NAME          ID     SIZE     PROCESSOR    CONTEXT\n"
        "glm-5:cloud   abc    6.6 GB   100% GPU     202752\n"
    )

    class _Sub:
        TimeoutExpired = subprocess.TimeoutExpired

        @staticmethod
        def run(*args, **kwargs):  # noqa: ANN002, ANN003, ANN205
            cp = _FakeCompleted()
            cp.stdout = stdout
            return cp

    monkeypatch.setattr(_dynamic, "subprocess", _Sub, raising=True)
    info = _dynamic.get_ollama_runtime_info("glm-5:cloud")
    assert info is not None
    assert info["context"] == 202752
    assert "GPU" in info["processor"]
