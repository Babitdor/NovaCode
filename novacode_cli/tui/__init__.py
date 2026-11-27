"""Textual TUI front-end for NovaCode — the only interactive UI.

The chat screen consumes the UI-agnostic
:func:`novacode_cli.agent_stream.run_agent_stream` and renders its events.
There is no console REPL: the non-interactive paths are headless mode (``-p``)
and the parallel-session worker.

``run_tui`` resolves lazily (PEP 562): importing any ``novacode_cli.tui.*``
module must not pay for the whole app import chain.
"""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from novacode_cli.tui.app import run_tui

__all__ = ["run_tui"]


def __getattr__(name: str):  # noqa: ANN202 - PEP 562 accessor.
    if name == "run_tui":
        from novacode_cli.tui.app import run_tui

        return run_tui
    msg = f"module {__name__!r} has no attribute {name!r}"
    raise AttributeError(msg)
