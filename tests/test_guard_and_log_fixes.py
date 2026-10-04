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


def test_headless_setup_does_not_close_stdout(tmp_path, monkeypatch):
    """Every headless run on Windows ended in "I/O operation on closed file".

    There the console writes through its own TextIOWrapper around stdout's
    buffer. Repointing the console dropped that wrapper, and a collected
    TextIOWrapper closes the buffer it wraps, which was stdout's.
    """
    import gc
    import io
    import os

    import novacode_cli.main as main

    raw = (tmp_path / "out.txt").open("wb", buffering=0)
    buffer = io.BufferedWriter(raw)
    stdout = io.TextIOWrapper(buffer, encoding="utf-8")
    monkeypatch.setattr(sys, "stdout", stdout)
    monkeypatch.setattr(main.console, "file", io.TextIOWrapper(buffer, encoding="utf-8"))

    fd = main._setup_headless_io()
    gc.collect()  # the console's old wrapper is gone now

    assert not buffer.closed, "dropping the console's wrapper must not close stdout"
    assert fd is not None, "the result descriptor must have been duplicated"
    os.write(fd, b"pong")
    os.close(fd)
    stdout.detach()
    buffer.close()
    assert (tmp_path / "out.txt").read_bytes() == b"pong"


def test_settings_never_print_a_credential():
    """A crash screen prints locals; `settings` in scope put every key on screen."""
    from dataclasses import fields, replace

    from novacode_cli.config.config import settings

    secret = "nvapi-" + "x" * 40
    keyed = {f.name: secret for f in fields(settings) if f.name.endswith("_api_key")}
    assert keyed, "expected credential fields"
    shown = repr(replace(settings, **keyed)) + str(replace(settings, **keyed))
    assert secret not in shown
    assert shown.count("'<set>'") >= len(keyed)
    assert "project_root=" in shown, "non-secret fields are still shown"


def test_the_agents_screen_survives_a_reload_after_it_was_closed():
    """Creating an agent finishes in a worker; the screen may be gone by then."""
    from textual.css.query import NoMatches

    from novacode_cli.tui.screens import AgentsScreen

    screen = AgentsScreen.__new__(AgentsScreen)

    def gone(*a, **k):  # noqa: ANN002, ANN003, ANN202
        raise NoMatches("No nodes match '#agents-list' on AgentsScreen()")

    screen.query_one = gone  # type: ignore[method-assign]
    screen._reload()  # must return quietly, not take the app down
