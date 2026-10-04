"""Regressions found in ~/.nova/logs/nova.log.

* Hands-free voice never got a VAD model: silero_vad imports torch, and the
  start-up import guard (``_lazy_heavy``) blocked it. 24 warm-up failures in
  four days, all the same ImportError.
* nova.log had grown to 26 MB since April, and most of its "errors" were the
  test suite's deliberate failures, which buried the real ones.
"""

from __future__ import annotations

import logging
import sys
import types


def test_loading_the_vad_model_lifts_the_torch_guard(monkeypatch):
    from novacode_cli import _lazy_heavy
    from novacode_cli.audio import vad

    seen: dict = {}

    def load_silero_vad():  # noqa: ANN202
        seen["guard_active"] = _lazy_heavy.is_active()
        return object()

    monkeypatch.setitem(
        sys.modules, "silero_vad", types.SimpleNamespace(load_silero_vad=load_silero_vad)
    )
    monkeypatch.setattr(_lazy_heavy, "_guard", _lazy_heavy._HeavyImportGuard())
    monkeypatch.setattr(sys, "meta_path", [_lazy_heavy._guard, *sys.meta_path])
    assert _lazy_heavy.is_active()

    endpointer = next(
        cls for cls in vars(vad).values() if isinstance(cls, type) and hasattr(cls, "_ensure_model")
    )()
    endpointer._ensure_model()
    assert seen == {"guard_active": False}, "torch must be importable by the time silero loads"


def test_a_test_run_does_not_write_to_the_users_log():
    import novacode_cli.main as main

    assert isinstance(main.file_handler, logging.NullHandler)
