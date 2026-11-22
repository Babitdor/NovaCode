"""Selectable status-line fields.

Kept as the Textual UI's import path; the definitions moved to
:mod:`novacode_cli.ui.statusline_fields` so the socket server can read them
without importing Textual. Both names stay importable from here.
"""

from __future__ import annotations

from novacode_cli.ui.statusline_fields import DEFAULT_FIELDS, FIELDS, selected_fields

__all__ = ["DEFAULT_FIELDS", "FIELDS", "selected_fields"]
