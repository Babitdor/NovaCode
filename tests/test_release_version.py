"""Public version displays must follow the current package release."""

import subprocess
import sys
import tomllib
from pathlib import Path

from novacode_cli._version import __version__
from novacode_cli.config.config import Settings, get_responsive_ascii, settings


def test_release_metadata_and_banner_use_shared_version():
    root = Path(__file__).resolve().parents[1]
    project = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    assert project["project"]["version"] == __version__
    assert Settings.__dataclass_fields__["version"].default == __version__
    assert settings.version == __version__
    for width in (50, 80, 120, 200):
        assert f"v{__version__}" in get_responsive_ascii(width=width)


def test_version_command_runs_without_importing_agent_runtime(monkeypatch, capsys):
    from rich.console import Console

    from novacode_cli.config import config
    from novacode_cli.entrypoint import cli_main

    monkeypatch.setattr(config, "console", Console(file=sys.stdout, width=100))
    monkeypatch.setattr(sys, "argv", ["nova", "--version"])
    # A version-only command must work even if optional agent dependencies are
    # absent or incompatible. Forbid the runtime import at its actual boundary.
    monkeypatch.setitem(sys.modules, "novacode_cli.main", None)
    cli_main()
    assert f"v{__version__}" in capsys.readouterr().out


def test_version_command_via_real_module_entrypoint():
    result = subprocess.run(
        [sys.executable, "-m", "novacode_cli", "--version"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
        timeout=20,
    )
    assert f"v{__version__}" in result.stdout
