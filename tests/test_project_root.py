"""Nova resolves ONE project directory, wherever inside the repo it is launched.

Launched from a subfolder, Nova used to split into two roots: NOVA.md and the
system prompt described the whole repository (the git root) while the file
tools, shell, approval policy and session lookup all used the launch folder. So
the model was told ``/`` was the project root but ``write_file("/tests/x.py")``
landed in ``<subfolder>/tests/``, and — the security-relevant half — a project's
own ``.nova/approval-policy.json`` silently stopped applying: its deny rules
became "ask". Measured: a policy denying ``terraform destroy`` returned DENY
from the repo root and ASK from ``src/`` and ``src/pkg/``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import novacode_cli.config.config as cfg


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A git repo with a nested package, a project policy and a project MCP config."""
    root = tmp_path / "proj"
    (root / ".git").mkdir(parents=True)
    (root / "src" / "pkg").mkdir(parents=True)
    (root / ".nova").mkdir()
    (root / ".nova" / "approval-policy.json").write_text(
        json.dumps({"shell": {"deny": [r"\bterraform\s+destroy\b"]}}), encoding="utf-8"
    )
    (root / ".nova" / "mcp.json").write_text(json.dumps({"mcpServers": {}}), encoding="utf-8")
    return root


@pytest.fixture
def launch(monkeypatch):
    """Simulate starting Nova in *directory*: cwd plus a freshly detected Settings."""

    def _launch(directory: Path):
        monkeypatch.chdir(directory)
        fresh = cfg.Settings.from_environment()
        monkeypatch.setattr(cfg, "settings", fresh)
        return fresh

    return _launch


LAUNCH_POINTS = ["", "src", "src/pkg"]


# ── One root ────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("where", LAUNCH_POINTS)
def test_the_workspace_is_the_repo_root_from_anywhere_inside_it(repo, launch, where):
    settings = launch(repo / where)
    assert settings.get_workspace_root() == repo.resolve()


@pytest.mark.parametrize("where", LAUNCH_POINTS)
def test_the_file_tools_and_nova_md_agree(repo, launch, where):
    """The split this fixes: prompt said one root, file tools used another."""
    settings = launch(repo / where)
    assert settings.get_workspace_root() == Path(settings.project_root)


def test_outside_any_repo_the_launch_directory_is_the_project(tmp_path, launch):
    loose = tmp_path / "no_repo_here"
    loose.mkdir()
    settings = launch(loose)
    assert settings.project_root is None
    assert settings.get_workspace_root() == loose.resolve()


def test_the_workspace_root_is_always_resolved(tmp_path, launch):
    """Compared against other resolved paths; an unresolved one broke on
    subst drives and junctions."""
    loose = tmp_path / "x"
    loose.mkdir()
    root = launch(loose).get_workspace_root()
    assert root == root.resolve()


# ── The approval policy (the security-relevant half) ────────────────────────


@pytest.mark.parametrize("where", LAUNCH_POINTS)
def test_the_projects_approval_policy_applies_from_any_subfolder(repo, launch, where):
    from novacode_cli.security.policy import get_policy

    launch(repo / where)
    decision = get_policy(refresh=True).evaluate("shell", {"command": "terraform destroy"})
    assert decision.tier == "deny", (
        f"launched from {where or 'the repo root'!r}, the project's deny rule "
        f"was dropped: got {decision.tier!r}"
    )


def test_an_explicit_policy_root_still_wins(repo, launch, tmp_path):
    """Callers that pass a root (the policy writer) must keep control."""
    from novacode_cli.security.policy import load_policy

    other = tmp_path / "elsewhere"
    other.mkdir()
    launch(repo / "src")
    policy = load_policy(other)
    assert policy.evaluate("shell", {"command": "terraform destroy"}).tier != "deny"


# ── MCP config discovery ────────────────────────────────────────────────────


def _project_mcp_configs():
    from novacode_cli.mcp.client import discover_mcp_configs

    home = Path(cfg.HOME_DIR).resolve()
    return [p for p in discover_mcp_configs() if home not in p.resolve().parents]


@pytest.mark.parametrize("where", LAUNCH_POINTS)
def test_the_repos_own_mcp_config_is_found(repo, launch, where):
    """From the repo root this used to be skipped: the walk read cwd.parents,
    which excludes cwd itself."""
    launch(repo / where)
    found = [p.resolve() for p in _project_mcp_configs()]
    assert (repo / ".nova" / "mcp.json").resolve() in found


def test_a_marker_in_a_parent_folder_does_not_steal_the_project(tmp_path, launch):
    """The old walk accepted pyproject.toml, so a parent holding one claimed
    the project and the repo's own config was ignored."""
    (tmp_path / "pyproject.toml").write_text("[project]\nname='outer'\n", encoding="utf-8")
    inner = tmp_path / "inner"
    (inner / ".git").mkdir(parents=True)
    (inner / ".nova").mkdir()
    (inner / ".nova" / "mcp.json").write_text("{}", encoding="utf-8")
    launch(inner)
    found = [p.resolve() for p in _project_mcp_configs()]
    assert (inner / ".nova" / "mcp.json").resolve() in found
    assert not any(p.parent.parent == tmp_path.resolve() for p in found)


# ── Sessions ────────────────────────────────────────────────────────────────


def test_session_save_and_restore_use_the_project_root():
    """Saved from the repo root, a session was not found when resuming from a
    subfolder: save and restore keyed on different directories."""
    import novacode_cli

    pkg = Path(novacode_cli.__file__).parent
    for rel in ("main.py", "commands/session_commands.py"):
        text = (pkg / rel).read_text(encoding="utf-8")
        assert "project_root=Path.cwd()" not in text, f"{rel} saves under the launch dir"
        assert "project_root = Path.cwd()" not in text, f"{rel} keys a session on cwd"
