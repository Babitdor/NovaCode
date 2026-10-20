"""The HTTP agents API (create_server) reads both agent forms and creates flat files."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from novacode_cli.commands import create_server


@pytest.fixture
def api(monkeypatch, tmp_path):
    from novacode_cli.config.config import settings

    home = tmp_path / "agents"
    (home / "old-style").mkdir(parents=True)
    (home / "old-style" / "agent.md").write_text("---\ndescription: folder\n---\nOld", encoding="utf-8")
    (home / "flat.md").write_text("---\ndescription: file\n---\nFlat", encoding="utf-8")
    monkeypatch.setattr(settings, "get_agents_root_dir", lambda: home)
    monkeypatch.setattr(settings, "get_project_agents_dir", lambda: None)

    sent = []
    handler = SimpleNamespace(settings=settings)
    handler._send_json = lambda data, status=200: sent.append((status, data))
    handler._send_error = lambda message, status=400: sent.append((status, message))
    cls = create_server.CreateRequestHandler if hasattr(create_server, "CreateRequestHandler") else None
    owner = cls or next(
        v for v in vars(create_server).values() if isinstance(v, type) and hasattr(v, "_create_agent")
    )
    for method in ("_list_agents", "_get_agent", "_create_agent", "_update_agent",
                   "_delete_agent", "_write_atomically"):
        setattr(handler, method, getattr(owner, method).__get__(handler))
    return handler, home, sent


def test_lists_and_reads_both_forms(api):
    handler, _home, _sent = api
    listed = {a["name"]: a["description"] for a in handler._list_agents()}
    assert listed == {"old-style": "folder", "flat": "file"}
    assert handler._get_agent("flat")["content"].endswith("Flat")
    assert handler._get_agent("old-style")["content"].endswith("Old")


def test_create_update_delete_use_the_flat_file(api):
    handler, home, sent = api
    handler._create_agent({"name": "fresh", "description": "New", "content": "Do things"})
    assert sent[-1][0] == 201
    assert (home / "fresh.md").is_file() and not (home / "fresh").exists()

    handler._create_agent({"name": "old-style", "content": "x"})
    assert sent[-1][0] == 409, "a folder agent blocks the name too"

    handler._update_agent("fresh", {"content": "---\ndescription: New\n---\nChanged"})
    assert (home / "fresh.md").read_text(encoding="utf-8").endswith("Changed")

    handler._delete_agent("fresh")
    handler._delete_agent("old-style")
    assert not (home / "fresh.md").exists() and not (home / "old-style").exists()


def test_a_project_agent_overrides_and_is_created_flat(api, monkeypatch, tmp_path):
    from novacode_cli.config.config import settings

    handler, home, sent = api
    project = tmp_path / "project-agents"
    project.mkdir()
    (project / "flat.md").write_text("---\ndescription: mine\n---\nP", encoding="utf-8")
    monkeypatch.setattr(settings, "get_project_agents_dir", lambda: project)

    listed = {a["name"]: (a["source"], a["description"]) for a in handler._list_agents()}
    assert listed["flat"] == ("project", "mine")
    handler._create_agent({"name": "local", "content": "x", "scope": "project"})
    assert sent[-1][0] == 201
    assert (project / "local.md").is_file() and not (home / "local.md").exists()
