"""MCP executable options must not replace the top-level command dispatch."""

from __future__ import annotations

from pathlib import Path

import pytest

from novacode_cli.cli_args import parse_args
from novacode_cli.mcp import commands
from novacode_cli.mcp.config import MCPConfig


@pytest.mark.parametrize(
    ("executable", "arguments"),
    [
        (r"C:\Tools\Obscura\obscura.exe", ["mcp"]),
        (r"C:\Program Files\Obscura\obscura.exe", ["mcp"]),
        ("obscura", []),
    ],
)
def test_stdio_add_keeps_dispatch_and_persists_executable(
    executable: str, arguments: list[str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Exercise config persistence without requiring Obscura on the test machine.
    original_is_file = Path.is_file
    monkeypatch.setattr(
        Path, "is_file", lambda path: str(path) == executable or original_is_file(path)
    )
    monkeypatch.setattr("novacode_cli.mcp.config.shutil.which", lambda _name: executable)
    argv = ["nova", "mcp", "add", "obscura", "--transport", "stdio", "--command", executable]
    if arguments:
        argv.extend(["--args", *arguments])
    monkeypatch.setattr("sys.argv", argv)
    path = tmp_path / "mcp.json"
    monkeypatch.setattr(commands, "MCPConfig", lambda: MCPConfig(path))

    parsed = parse_args()
    assert parsed.command == "mcp"
    assert parsed.mcp_command == "add"
    commands.execute_mcp_command(parsed)

    saved = MCPConfig(path).get_server("obscura")
    assert saved is not None
    assert saved.transport == "stdio"
    assert saved.command == executable
    assert saved.args == arguments


@pytest.mark.parametrize(
    "url_args", [["https://example.test/mcp"], ["--url", "https://example.test/mcp"]]
)
def test_http_add_keeps_dispatch_without_an_executable(
    url_args: list[str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "sys.argv", ["nova", "mcp", "add", "--transport", "http", "example", *url_args]
    )
    path = tmp_path / "mcp.json"
    monkeypatch.setattr(commands, "MCPConfig", lambda: MCPConfig(path))

    parsed = parse_args()
    assert parsed.command == "mcp"
    commands.execute_mcp_command(parsed)

    saved = MCPConfig(path).get_server("example")
    assert saved is not None
    assert saved.transport == "http"
    assert saved.url == "https://example.test/mcp"
    assert saved.command is None


def test_missing_stdio_executable_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executable = str(tmp_path / "missing-obscura.exe")
    monkeypatch.setattr(
        "sys.argv",
        ["nova", "mcp", "add", "obscura", "--transport", "stdio", "--command", executable],
    )
    path = tmp_path / "mcp.json"
    monkeypatch.setattr(commands, "MCPConfig", lambda: MCPConfig(path))

    with pytest.raises(SystemExit) as error:
        commands.execute_mcp_command(parse_args())
    assert error.value.code == 1
    assert not path.exists()
