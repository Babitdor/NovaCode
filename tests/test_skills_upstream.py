"""Upstream CLI forwarding and shared skill discovery regression tests."""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path
from unittest.mock import Mock

import pytest

from novacode_cli.skills import upstream


@pytest.mark.parametrize("prefix", [["skills", "add"], ["add", "skill"]])
def test_add_alias_preserves_all_arguments(prefix: list[str]):
    arguments = ["-g", "owner/repo", "-s", "first", "second", "--skill", "third", "--copy", "-y"]
    assert upstream.add_arguments([*prefix, *arguments]) == arguments
    assert upstream.add_arguments(["skills", "list"]) is None


def test_parser_forwards_options_before_source():
    from novacode_cli.skills.skill_creation import setup_skills_parser

    parser = argparse.ArgumentParser()
    setup_skills_parser(parser.add_subparsers(dest="command"))
    arguments = ["--list", "owner/repo", "--skill", "one", "two", "--agent", "*", "--future-option"]
    args = parser.parse_args(["skills", "add", *arguments])
    assert args.upstream_args == arguments


@pytest.mark.parametrize("code", [0, 1, 7, -2])
def test_terminal_arguments_and_exit_codes(monkeypatch: pytest.MonkeyPatch, code: int):
    monkeypatch.setattr(
        upstream, "skills_command", lambda: ["node", "npx-cli.js", "--yes", "skills@latest"]
    )
    run = Mock(return_value=subprocess.CompletedProcess([], code))
    monkeypatch.setattr(upstream.subprocess, "run", run)
    arguments = ["https://example.org/skill?one=1&two=2", "--skill", "Name With Spaces", "--list"]
    assert upstream.run_skills_cli("add", arguments) == (code if code >= 0 else 128 - code)
    run.assert_called_once_with(
        ["node", "npx-cli.js", "--yes", "skills@latest", "add", *arguments],
        check=False,
    )


def test_windows_uses_node_instead_of_shell(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    launcher = tmp_path / "npx.cmd"
    entry = tmp_path / "node_modules/npm/bin/npx-cli.js"
    entry.parent.mkdir(parents=True)
    entry.write_text("", encoding="utf-8")
    monkeypatch.setattr(
        upstream.shutil, "which", lambda name: str(launcher) if name == "npx" else "node.exe"
    )
    assert upstream.skills_command() == ["node.exe", str(entry), "--yes", "skills@latest"]


def test_missing_npx_reports_failure(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
):
    monkeypatch.setattr(upstream.shutil, "which", lambda _: None)
    assert upstream.run_skills_cli("add", ["owner/repo"]) == 127
    assert "Node.js and npx are required" in capsys.readouterr().err


def test_cancel_returns_130(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(upstream, "skills_command", lambda: ["npx"])
    monkeypatch.setattr(upstream.subprocess, "run", Mock(side_effect=KeyboardInterrupt))
    assert upstream.run_skills_cli("add", ["owner/repo"]) == 130


def test_entrypoint_installs_without_importing_agent(monkeypatch: pytest.MonkeyPatch):
    from novacode_cli import entrypoint

    monkeypatch.setattr(sys, "argv", ["nova", "skills", "add", "./local", "--list"])
    run = Mock(return_value=7)
    monkeypatch.setattr(entrypoint, "run_skills_cli", run)
    # If the entry point imports the agent entry point, it cannot run.
    monkeypatch.setitem(sys.modules, "novacode_cli.main", None)
    with pytest.raises(SystemExit) as exc:
        entrypoint.cli_main()
    assert exc.value.code == 7
    run.assert_called_once_with("add", ["./local", "--list"])


def write_skill(directory: Path, name: str, body: str) -> Path:
    folder = directory / name
    folder.mkdir(parents=True)
    (folder / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: test skill\n---\n\n{body}\n",
        encoding="utf-8",
    )
    return folder


def test_shared_skills_load_with_real_paths_and_project_precedence(tmp_path: Path):
    from novacode_cli.skills.load import list_skills

    shared = tmp_path / "home/.agents/skills"
    project = tmp_path / "project/.agents/skills"
    write_skill(shared, "example", "global instructions")
    project_folder = write_skill(project, "example", "project instructions")
    write_skill(shared, "global-only", "global instructions")
    result = {
        s["name"]: s for s in list_skills(shared_skills_dir=shared, project_skills_dirs=[project])
    }
    assert result["example"]["source"] == "project"
    assert Path(result["example"]["path"]) == project_folder / "SKILL.md"
    assert result["global-only"]["source"] == "user"
    assert Path(result["global-only"]["path"]).is_file()


def test_no_git_project_discovers_upstream_skills(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from novacode_cli.config.config import Settings

    write_skill(tmp_path / ".agents/skills", "example", "instructions")
    monkeypatch.chdir(tmp_path)
    settings = Settings.from_environment()
    assert settings.project_root is None
    assert tmp_path / ".agents/skills" in settings.get_project_skills_dirs()


def test_nested_install_is_discovered(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from novacode_cli.config.config import Settings

    (tmp_path / ".git").mkdir()
    nested = tmp_path / "package"
    write_skill(nested / ".agents/skills", "example", "instructions")
    monkeypatch.chdir(nested)
    settings = Settings.from_environment()
    assert nested / ".agents/skills" in settings.get_project_skills_dirs()
