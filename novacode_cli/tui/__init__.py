"""Textual TUI front-end for NovaCode — the only interactive UI.

The chat screen consumes the UI-agnostic
:func:`novacode_cli.agent_stream.run_agent_stream` and renders its events.
There is no console REPL: the non-interactive paths are headless mode (``-p``)
and the parallel-session worker.
"""

from novacode_cli.tui.app import run_tui

__all__ = ["run_tui"]
