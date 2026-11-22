"""Bounded display-only output tails.

Kept as the Textual UI's import path; the implementation lives in
:mod:`novacode_cli.ui.output_buffer` so the server can read it without importing
Textual. Both names stay importable from here.
"""

from __future__ import annotations

from novacode_cli.ui.output_buffer import MAX_PENDING_CALLS, MAX_PENDING_CHARS, OutputTail

__all__ = ["MAX_PENDING_CALLS", "MAX_PENDING_CHARS", "OutputTail"]
