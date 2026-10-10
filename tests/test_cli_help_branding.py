"""Public CLI help and version identify the NovaCode package."""

from __future__ import annotations

import sys

import pytest

from novacode_cli._version import __version__


def test_cli_help_uses_novacode_name_and_version(monkeypatch, capsys):
    from novacode_cli.cli_args import parse_args

    monkeypatch.setattr(sys, "argv", ["nova", "--help"])
    with pytest.raises(SystemExit) as exit_info:
        parse_args()

    output = capsys.readouterr().out
    assert exit_info.value.code == 0
    assert f"NovaCode v{__version__} - AI Coding Assistant" in output
    assert "DeepAgents - AI Coding Assistant" not in output
