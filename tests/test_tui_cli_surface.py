"""The CLI surface the ratatui frontend hangs off.

``--mode`` already means `{tui,pipe}`, so the UI choice is a separate flag with a
separate default, and ``--serve`` is an internal endpoint rather than a user mode.
These tests pin that split, because a regression here silently changes which
frontend a user gets.
"""

from __future__ import annotations

import sys
from typing import Any

import pytest

from novacode_cli.cli_args import parse_args


def _parse(monkeypatch: pytest.MonkeyPatch, argv: list[str]) -> Any:  # noqa: ANN401
    """Parse *argv* as if it were the process command line."""
    monkeypatch.setattr(sys, "argv", ["nova", *argv])
    return parse_args()


def test_textual_is_the_default_frontend(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NOVA_UI", raising=False)
    args = _parse(monkeypatch, [])
    assert args.ui == "textual"


def test_nova_ui_sets_the_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NOVA_UI", "ratatui")
    assert _parse(monkeypatch, []).ui == "ratatui"


def test_an_unknown_nova_ui_falls_back_to_textual(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NOVA_UI", "kitty")
    assert _parse(monkeypatch, []).ui == "textual"


def test_the_flag_beats_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NOVA_UI", "ratatui")
    assert _parse(monkeypatch, ["--ui", "textual"]).ui == "textual"


def test_serve_defaults_are_taken(monkeypatch: pytest.MonkeyPatch) -> None:
    args = _parse(monkeypatch, ["--serve"])
    assert args.serve is True
    assert args.serve_port == 0
    assert args.serve_token is None
    assert _parse(monkeypatch, ["--serve", "--serve-port", "8123"]).serve_port == 8123


def test_serve_is_hidden_from_help(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(SystemExit) as exit_info:
        _parse(monkeypatch, ["--help"])
    assert exit_info.value.code == 0


@pytest.mark.parametrize(
    "argv",
    [
        ["--serve", "--print", "hi"],
        ["--serve", "--mode", "pipe"],
        ["--serve", "--session-worker"],
    ],
)
def test_serve_refuses_incompatible_modes(
    monkeypatch: pytest.MonkeyPatch, argv: list[str]
) -> None:
    with pytest.raises(SystemExit) as exit_info:
        _parse(monkeypatch, argv)
    assert exit_info.value.code == 2
